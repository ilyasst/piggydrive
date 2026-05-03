#!/usr/bin/env python3
"""piggydrive-sidecar — runs on the bridge (Mac with OneDrive).

Exposes a small HTTP+JSON API so a piggydrive client on another machine can
list, stat, pull, push, and inspect files inside the local OneDrive folder.
The OneDrive client (Microsoft's) handles all auth and Files-On-Demand cloud
fetching; this daemon just orchestrates filesystem access on top of it.

Run directly:
    python3 sidecar.py [--config PATH]

Or via launchd — see com.piggydrive.sidecar.plist for the service template.

Single-file by design: drop on a Mac, point a config at it, run. Stdlib only.
"""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

LOG = logging.getLogger("piggydrive-sidecar")

DEFAULT_CONFIG_PATH = Path("~/.config/piggydrive-sidecar/config.toml").expanduser()
DEFAULT_TOKEN_PATH = Path("~/.config/piggydrive-sidecar/token").expanduser()


# ── Configuration ────────────────────────────────────────────────────


class Config:
    """Loaded sidecar configuration. See docs/architecture.md for fields."""

    def __init__(self, raw: dict[str, Any]) -> None:
        srv = raw.get("server", {})
        self.host: str = srv.get("host", "0.0.0.0")
        self.port: int = int(srv.get("port", 9090))

        od = raw.get("onedrive", {})
        root = od.get("root")
        if not root:
            raise ValueError("onedrive.root is required in config")
        self.root: Path = Path(os.path.expanduser(root)).resolve()
        if not self.root.is_dir():
            raise ValueError(f"onedrive.root does not exist or is not a directory: {self.root}")

        auth = raw.get("auth", {})
        token_file = Path(os.path.expanduser(
            auth.get("token_file", str(DEFAULT_TOKEN_PATH))
        ))
        self.token: str = self._load_token(token_file)

        mat = raw.get("materialize", {})
        self.poll_interval_ms: int = int(mat.get("poll_interval_ms", 250))
        self.default_timeout_seconds: int = int(mat.get("default_timeout_seconds", 120))

    @staticmethod
    def _load_token(path: Path) -> str:
        if not path.is_file():
            raise ValueError(
                f"Auth token file not found: {path}\n"
                f"Generate one with: openssl rand -hex 32 > {path} && chmod 600 {path}"
            )
        token = path.read_text().strip()
        if not token:
            raise ValueError(f"Auth token file is empty: {path}")
        return token

    @classmethod
    def load(cls, path: Path) -> "Config":
        if not path.is_file():
            raise ValueError(f"Config file not found: {path}")
        with path.open("rb") as f:
            raw = tomllib.load(f)
        return cls(raw)


# ── OneDrive filesystem helpers ──────────────────────────────────────


def is_stub(path: Path) -> bool:
    """Detect a Files-On-Demand stub: reported size > 0 but blocks == 0.

    macOS APFS / Windows NTFS reparse-point semantics both put cloud-only
    files on disk with their full reported size but zero allocated blocks.
    """
    try:
        st = path.stat()
    except (FileNotFoundError, PermissionError):
        return False
    return st.st_size > 0 and st.st_blocks == 0


def stat_entry(path: Path, root: Path) -> dict[str, Any]:
    """Build a JSON entry describing one filesystem path."""
    try:
        st = path.lstat()
    except FileNotFoundError:
        return {"path": str(path.relative_to(root)), "exists": False}

    is_dir = path.is_dir() and not path.is_symlink()
    materialized = is_dir or st.st_blocks > 0 or st.st_size == 0

    return {
        "path": "/" + str(path.relative_to(root)),
        "exists": True,
        "is_dir": is_dir,
        "size_bytes": st.st_size,
        "materialized": materialized,
        "syncing": False,  # TODO: detect from OneDrive app state
        "modified_utc": datetime.fromtimestamp(
            st.st_mtime, tz=timezone.utc
        ).isoformat().replace("+00:00", "Z"),
    }


def materialize(path: Path, poll_ms: int, timeout_s: int) -> tuple[bool, str | None]:
    """Trigger Files-On-Demand fetch, then wait until blocks > 0.

    Returns (success, error_message). On timeout, success=False.
    """
    if not is_stub(path):
        return True, None  # already materialized (or not a stub at all)

    # Trigger: read one byte. macOS / Windows file provider intercepts.
    try:
        with path.open("rb") as f:
            f.read(1)
    except (FileNotFoundError, PermissionError) as exc:
        return False, f"trigger read failed: {exc}"

    deadline = time.monotonic() + timeout_s
    last_size = -1
    stable_count = 0

    while time.monotonic() < deadline:
        try:
            st = path.stat()
        except FileNotFoundError:
            return False, "file disappeared during materialization"

        if st.st_blocks > 0:
            # Considered done when size has been stable across two polls
            # (large files trickle in; we want the final state).
            if st.st_size == last_size:
                stable_count += 1
                if stable_count >= 2:
                    return True, None
            else:
                stable_count = 0
                last_size = st.st_size

        time.sleep(poll_ms / 1000.0)

    return False, f"materialization timed out after {timeout_s}s"


def safe_resolve(remote_path: str, root: Path) -> Path | None:
    """Resolve a client-supplied remote path to an absolute path inside root.

    Guards against path traversal (../). Returns None if the path escapes root.
    """
    # Strip leading slashes — clients send paths like "/Foo/bar.pdf"
    cleaned = remote_path.lstrip("/")
    candidate = (root / cleaned).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


# ── OneDrive process / sync state ────────────────────────────────────


def onedrive_running() -> bool:
    """Best-effort: pgrep for the OneDrive process."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "OneDrive"],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def sync_status(config: Config) -> dict[str, Any]:
    """Return a snapshot of bridge + OneDrive state.

    pending_uploads is currently a placeholder — we don't have a reliable way
    to query OneDrive's queue without parsing log files. Future work.
    """
    return {
        "bridge_online": True,
        "onedrive_running": onedrive_running(),
        "onedrive_root": str(config.root),
        "pending_uploads": None,  # not yet implemented
        "last_error": None,
    }


# ── HTTP handler ─────────────────────────────────────────────────────


class Handler(BaseHTTPRequestHandler):
    config: Config  # populated by serve()

    # Quieter logs (default BaseHTTPRequestHandler logs every request to stderr).
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        LOG.info("%s - %s", self.address_string(), format % args)

    # ── auth ────────────────────────────────────────────────────────

    def _check_auth(self) -> bool:
        if self.path.startswith("/healthz"):
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            self._send_json({"error": "missing or malformed Authorization header"}, 401)
            return False
        if not hmac.compare_digest(header[7:], self.config.token):
            self._send_json({"error": "invalid token"}, 401)
            return False
        return True

    # ── response helpers ────────────────────────────────────────────

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, body: bytes, content_type: str = "application/octet-stream") -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _query(self) -> dict[str, str]:
        parsed = urllib.parse.urlparse(self.path)
        return {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}

    def _path_only(self) -> str:
        return urllib.parse.urlparse(self.path).path

    # ── method dispatch ─────────────────────────────────────────────

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler convention)
        if not self._check_auth():
            return
        path = self._path_only()
        if path == "/healthz":
            self._send_json({"status": "ok"})
        elif path == "/sync-status":
            self._send_json(sync_status(self.config))
        elif path == "/ls":
            self._handle_ls()
        elif path == "/stat":
            self._handle_stat()
        elif path == "/pull":
            self._handle_pull()
        elif path == "/find":
            self._handle_find()
        else:
            self._send_json({"error": f"unknown endpoint: {path}"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        if not self._check_auth():
            return
        path = self._path_only()
        if path == "/push":
            self._handle_push()
        elif path == "/mkdir":
            self._handle_mkdir()
        elif path == "/mv":
            self._handle_mv()
        else:
            self._send_json({"error": f"unknown endpoint: {path}"}, 404)

    def do_DELETE(self) -> None:  # noqa: N802
        if not self._check_auth():
            return
        path = self._path_only()
        if path == "/rm":
            self._handle_rm()
        else:
            self._send_json({"error": f"unknown endpoint: {path}"}, 404)

    # ── endpoint implementations ────────────────────────────────────

    def _handle_ls(self) -> None:
        q = self._query()
        target = safe_resolve(q.get("path", "/"), self.config.root)
        if target is None:
            self._send_json({"error": "path escapes onedrive root"}, 400)
            return
        if not target.exists():
            self._send_json({"error": "path not found"}, 404)
            return
        if not target.is_dir():
            self._send_json({"error": "path is not a directory"}, 400)
            return
        entries = []
        for child in sorted(target.iterdir()):
            if child.name.startswith("."):
                continue  # skip dotfiles for now (can revisit)
            entries.append(stat_entry(child, self.config.root))
        self._send_json({"path": "/" + str(target.relative_to(self.config.root)), "entries": entries})

    def _handle_stat(self) -> None:
        q = self._query()
        target = safe_resolve(q.get("path", ""), self.config.root)
        if target is None:
            self._send_json({"error": "path escapes onedrive root"}, 400)
            return
        self._send_json(stat_entry(target, self.config.root))

    def _handle_find(self) -> None:
        """Search for filenames containing a substring, scoped to a subtree.

        On macOS, uses Spotlight (`mdfind`) for sub-second results across
        large trees. Stub files ARE indexed by Spotlight (filenames are
        always recorded regardless of Files-On-Demand state), so the search
        works whether files are materialized or not.

        Falls back to rglob() on platforms without mdfind, or if mdfind
        fails. The fallback is much slower on big trees but always works.
        """
        q = self._query()
        name = q.get("name", "").strip()
        if not name:
            self._send_json({"error": "name parameter required"}, 400)
            return

        subtree_param = q.get("path", "/")
        subtree = safe_resolve(subtree_param, self.config.root)
        if subtree is None:
            self._send_json({"error": "path escapes onedrive root"}, 400)
            return
        if not subtree.exists():
            self._send_json({"error": "path not found"}, 404)
            return

        try:
            max_results = max(1, min(int(q.get("max_results", 200)), 5000))
        except ValueError:
            max_results = 200

        paths: list[str] = []
        used_engine = "rglob"

        # Try mdfind first (macOS Spotlight). Returns absolute paths.
        if shutil.which("mdfind"):
            try:
                result = subprocess.run(
                    ["mdfind", "-onlyin", str(subtree), "-name", name],
                    capture_output=True, text=True, timeout=15,
                )
                if result.returncode == 0:
                    paths = [
                        line for line in result.stdout.splitlines()
                        if line.strip()
                    ]
                    used_engine = "mdfind"
            except (subprocess.TimeoutExpired, OSError):
                pass  # fall through to rglob

        if used_engine != "mdfind":
            # Fallback: rglob with substring match. Slower but portable.
            needle = name.lower()
            try:
                for p in subtree.rglob("*"):
                    if needle in p.name.lower():
                        paths.append(str(p))
                        if len(paths) >= max_results:
                            break
            except (OSError, PermissionError) as exc:
                self._send_json({"error": f"rglob failed: {exc}"}, 500)
                return

        # Build stat entries for the matched paths
        truncated = len(paths) > max_results
        results = []
        for path_str in paths[:max_results]:
            p = Path(path_str)
            try:
                # mdfind can return paths outside subtree if Spotlight gives
                # broader scope than expected — re-check containment.
                p.resolve().relative_to(self.config.root)
                results.append(stat_entry(p, self.config.root))
            except (FileNotFoundError, ValueError):
                continue

        self._send_json({
            "query": name,
            "in_path": subtree_param,
            "engine": used_engine,
            "results": results,
            "truncated": truncated,
            "total_returned": len(results),
        })

    def _handle_pull(self) -> None:
        q = self._query()
        target = safe_resolve(q.get("path", ""), self.config.root)
        if target is None:
            self._send_json({"error": "path escapes onedrive root"}, 400)
            return
        if not target.exists():
            self._send_json({"error": "path not found"}, 404)
            return
        if target.is_dir():
            self._send_json({"error": "pull on a directory not supported (use ls + per-file pull)"}, 400)
            return

        timeout = int(q.get("timeout_seconds", self.config.default_timeout_seconds))
        ok, err = materialize(target, self.config.poll_interval_ms, timeout)
        if not ok:
            self._send_json({"error": f"materialize failed: {err}"}, 504)
            return

        try:
            data = target.read_bytes()
        except (OSError, PermissionError) as exc:
            self._send_json({"error": f"read failed: {exc}"}, 500)
            return
        self._send_bytes(data)

    def _handle_push(self) -> None:
        q = self._query()
        target = safe_resolve(q.get("path", ""), self.config.root)
        if target is None:
            self._send_json({"error": "path escapes onedrive root"}, 400)
            return

        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            self._send_json({"error": "missing Content-Length or empty body"}, 400)
            return

        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with target.open("wb") as f:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
        except (OSError, PermissionError) as exc:
            self._send_json({"error": f"write failed: {exc}"}, 500)
            return

        self._send_json({"ok": True, "path": "/" + str(target.relative_to(self.config.root)), "bytes_written": length - max(0, remaining)})

    def _handle_mkdir(self) -> None:
        q = self._query()
        target = safe_resolve(q.get("path", ""), self.config.root)
        if target is None:
            self._send_json({"error": "path escapes onedrive root"}, 400)
            return
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._send_json({"error": f"mkdir failed: {exc}"}, 500)
            return
        self._send_json({"ok": True, "path": "/" + str(target.relative_to(self.config.root))})

    def _handle_rm(self) -> None:
        q = self._query()
        target = safe_resolve(q.get("path", ""), self.config.root)
        if target is None:
            self._send_json({"error": "path escapes onedrive root"}, 400)
            return
        if not target.exists():
            self._send_json({"error": "path not found"}, 404)
            return
        recursive = q.get("recursive", "false").lower() in ("true", "1", "yes")
        try:
            if target.is_dir():
                if not recursive:
                    self._send_json({"error": "directory not empty; pass recursive=true"}, 400)
                    return
                shutil.rmtree(target)
            else:
                target.unlink()
        except OSError as exc:
            self._send_json({"error": f"rm failed: {exc}"}, 500)
            return
        self._send_json({"ok": True})

    def _handle_mv(self) -> None:
        q = self._query()
        src = safe_resolve(q.get("src", ""), self.config.root)
        dst = safe_resolve(q.get("dst", ""), self.config.root)
        if src is None or dst is None:
            self._send_json({"error": "path escapes onedrive root"}, 400)
            return
        if not src.exists():
            self._send_json({"error": "src not found"}, 404)
            return
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dst)
        except OSError as exc:
            self._send_json({"error": f"mv failed: {exc}"}, 500)
            return
        self._send_json({"ok": True, "src": q.get("src"), "dst": q.get("dst")})


# ── Entry point ──────────────────────────────────────────────────────


def serve(config: Config) -> None:
    Handler.config = config
    server = ThreadingHTTPServer((config.host, config.port), Handler)
    LOG.info(
        "piggydrive-sidecar listening on %s:%d, root=%s",
        config.host, config.port, config.root,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("shutting down")


def main() -> int:
    parser = argparse.ArgumentParser(description="piggydrive sidecar daemon")
    parser.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG_PATH,
        help=f"Path to config.toml (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    try:
        config = Config.load(args.config)
    except ValueError as exc:
        LOG.error("config error: %s", exc)
        return 1

    serve(config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
