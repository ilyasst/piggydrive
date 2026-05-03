#!/usr/bin/env python3
"""piggydrive — Linux CLI for piggydrive-sidecar.

Talks to a piggydrive-sidecar daemon running on a trusted bridge (e.g. a Mac
with OneDrive). Designed for both human and agent (Hermes) use:

- Default JSON output for stat / sync-status / ls --json (machine-readable)
- Distinct exit codes per failure mode (see EXIT_* constants below)
- Idempotent operations where possible
- Predictable blocking semantics: pull blocks until file is fully fetched

Single-file, stdlib only.

Usage:
    piggydrive ls /Path/To/Folder
    piggydrive stat /Path/To/file.pdf
    piggydrive pull /Remote/file.pdf ./local/file.pdf
    piggydrive push ./local/file.pdf /Remote/file.pdf
    piggydrive sync-status
    piggydrive wait-online --timeout 30
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

# ── Exit codes (see docs/architecture.md) ────────────────────────────

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_BRIDGE_UNREACHABLE = 10
EXIT_ONEDRIVE_NOT_RUNNING = 11
EXIT_NOT_FOUND = 12
EXIT_MATERIALIZE_TIMEOUT = 13
EXIT_SYNC_FAILED = 14
EXIT_PERMISSION = 15
EXIT_AUTH = 16

DEFAULT_CONFIG_PATH = Path("~/.config/piggydrive/config.toml").expanduser()


# ── Config ───────────────────────────────────────────────────────────


class Config:
    def __init__(self, raw: dict[str, Any]) -> None:
        bridge = raw.get("bridge", {})
        url = bridge.get("url")
        token = bridge.get("token")
        if not url or not token:
            raise ValueError("bridge.url and bridge.token are required in config")
        self.url: str = url.rstrip("/")
        self.token: str = token

        defaults = raw.get("defaults", {})
        self.pull_timeout_seconds: int = int(defaults.get("pull_timeout_seconds", 120))
        self.verbose: bool = bool(defaults.get("verbose", False))

    @classmethod
    def load(cls, path: Path) -> "Config":
        if not path.is_file():
            raise FileNotFoundError(
                f"Config not found: {path}\n"
                f"Create it with bridge.url and bridge.token. "
                f"See docs/architecture.md for the format."
            )
        with path.open("rb") as f:
            return cls(tomllib.load(f))


# ── HTTP client to sidecar ───────────────────────────────────────────


class BridgeError(Exception):
    """Raised by Bridge methods. exit_code maps to a CLI exit code."""
    def __init__(self, message: str, exit_code: int = EXIT_BRIDGE_UNREACHABLE) -> None:
        super().__init__(message)
        self.exit_code = exit_code


class Bridge:
    def __init__(self, config: Config) -> None:
        self.config = config

    def _request(
        self, method: str, endpoint: str, *,
        query: dict[str, Any] | None = None,
        body: bytes | None = None,
        body_content_type: str = "application/octet-stream",
        timeout: int | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        url = self.config.url + endpoint
        if query:
            url += "?" + urllib.parse.urlencode(
                {k: str(v) for k, v in query.items() if v is not None}
            )
        req = urllib.request.Request(url, method=method, data=body)
        req.add_header("Authorization", f"Bearer {self.config.token}")
        if body is not None:
            req.add_header("Content-Type", body_content_type)
            req.add_header("Content-Length", str(len(body)))
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers or {}), exc.read()
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise BridgeError(
                f"bridge unreachable at {self.config.url}: {exc}",
                EXIT_BRIDGE_UNREACHABLE,
            ) from exc

    def _request_json(
        self, method: str, endpoint: str, *,
        query: dict[str, Any] | None = None,
        body: bytes | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        status, _, raw = self._request(
            method, endpoint, query=query, body=body, timeout=timeout,
        )
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise BridgeError(
                f"bridge returned non-JSON ({status}): {raw[:200]!r}",
                EXIT_BRIDGE_UNREACHABLE,
            )
        if status >= 400:
            err = payload.get("error", "unknown error")
            exit_code = self._map_status(status, err)
            raise BridgeError(f"{status}: {err}", exit_code)
        return payload

    @staticmethod
    def _map_status(status: int, error: str) -> int:
        if status == 401:
            return EXIT_AUTH
        if status == 404:
            return EXIT_NOT_FOUND
        if status == 504 or "timed out" in error.lower():
            return EXIT_MATERIALIZE_TIMEOUT
        if status == 403 or "permission" in error.lower():
            return EXIT_PERMISSION
        return EXIT_SYNC_FAILED

    # ── public API ──────────────────────────────────────────────────

    def health(self, timeout: int = 5) -> bool:
        try:
            status, _, _ = self._request("GET", "/healthz", timeout=timeout)
            return status == 200
        except BridgeError:
            return False

    def sync_status(self) -> dict[str, Any]:
        return self._request_json("GET", "/sync-status", timeout=10)

    def ls(self, path: str, depth: int = 1) -> dict[str, Any]:
        return self._request_json("GET", "/ls", query={"path": path, "depth": depth}, timeout=30)

    def find(self, name: str, in_path: str = "/", max_results: int = 200) -> dict[str, Any]:
        return self._request_json(
            "GET", "/find",
            query={"name": name, "path": in_path, "max_results": max_results},
            timeout=30,
        )

    def stat(self, path: str) -> dict[str, Any]:
        return self._request_json("GET", "/stat", query={"path": path}, timeout=10)

    def pull(self, remote_path: str, timeout: int | None = None) -> bytes:
        timeout = timeout or self.config.pull_timeout_seconds
        status, _, raw = self._request(
            "GET", "/pull",
            query={"path": remote_path, "timeout_seconds": timeout},
            timeout=timeout + 10,  # HTTP timeout slightly larger than materialize timeout
        )
        if status >= 400:
            try:
                err = json.loads(raw).get("error", "unknown")
            except (UnicodeDecodeError, json.JSONDecodeError):
                err = raw[:200].decode("utf-8", errors="replace")
            raise BridgeError(f"{status}: {err}", self._map_status(status, err))
        return raw

    def push(self, remote_path: str, data: bytes, timeout: int = 120) -> dict[str, Any]:
        return self._request_json(
            "POST", "/push", query={"path": remote_path}, body=data, timeout=timeout,
        )

    def mkdir(self, path: str) -> dict[str, Any]:
        return self._request_json("POST", "/mkdir", query={"path": path}, timeout=10)

    def rm(self, path: str, recursive: bool = False) -> dict[str, Any]:
        return self._request_json(
            "DELETE", "/rm",
            query={"path": path, "recursive": str(recursive).lower()},
            timeout=30,
        )

    def mv(self, src: str, dst: str) -> dict[str, Any]:
        return self._request_json("POST", "/mv", query={"src": src, "dst": dst}, timeout=10)


# ── CLI commands ─────────────────────────────────────────────────────


def cmd_ls(bridge: Bridge, args: argparse.Namespace) -> int:
    result = bridge.ls(args.path)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return EXIT_OK
    for entry in result["entries"]:
        flag = "d" if entry["is_dir"] else ("s" if not entry["materialized"] else "-")
        size = entry["size_bytes"]
        size_str = f"{size:>12,}".replace(",", "_")
        print(f"{flag} {size_str}  {entry['path']}")
    return EXIT_OK


def cmd_stat(bridge: Bridge, args: argparse.Namespace) -> int:
    result = bridge.stat(args.path)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return EXIT_OK


def cmd_find(bridge: Bridge, args: argparse.Namespace) -> int:
    result = bridge.find(args.query, in_path=args.in_path, max_results=args.max)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return EXIT_OK
    if not result["results"]:
        print(f"(no matches for {args.query!r} under {args.in_path})")
        return EXIT_OK
    for entry in result["results"]:
        flag = "d" if entry["is_dir"] else ("s" if not entry["materialized"] else "-")
        size = entry["size_bytes"]
        size_str = f"{size:>12,}".replace(",", "_")
        print(f"{flag} {size_str}  {entry['path']}")
    if result.get("truncated"):
        print(f"\n(truncated to {len(result['results'])} results — use --max to widen)")
    return EXIT_OK


def cmd_pull(bridge: Bridge, args: argparse.Namespace) -> int:
    data = bridge.pull(args.remote, timeout=args.timeout)
    out = Path(args.local)
    if out.is_dir():
        out = out / Path(args.remote).name
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    print(f"pulled {len(data):,} bytes -> {out}")
    return EXIT_OK


def cmd_push(bridge: Bridge, args: argparse.Namespace) -> int:
    src = Path(args.local)
    if not src.is_file():
        print(f"error: not a file: {src}", file=sys.stderr)
        return EXIT_NOT_FOUND
    data = src.read_bytes()
    result = bridge.push(args.remote, data)
    print(json.dumps(result, ensure_ascii=False))
    return EXIT_OK


def cmd_cat(bridge: Bridge, args: argparse.Namespace) -> int:
    data = bridge.pull(args.remote, timeout=args.timeout)
    sys.stdout.buffer.write(data)
    return EXIT_OK


def cmd_mkdir(bridge: Bridge, args: argparse.Namespace) -> int:
    print(json.dumps(bridge.mkdir(args.path), ensure_ascii=False))
    return EXIT_OK


def cmd_rm(bridge: Bridge, args: argparse.Namespace) -> int:
    print(json.dumps(bridge.rm(args.path, recursive=args.recursive), ensure_ascii=False))
    return EXIT_OK


def cmd_mv(bridge: Bridge, args: argparse.Namespace) -> int:
    print(json.dumps(bridge.mv(args.src, args.dst), ensure_ascii=False))
    return EXIT_OK


def cmd_sync_status(bridge: Bridge, args: argparse.Namespace) -> int:
    result = bridge.sync_status()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("onedrive_running", False):
        return EXIT_ONEDRIVE_NOT_RUNNING
    return EXIT_OK


def cmd_wait_online(bridge: Bridge, args: argparse.Namespace) -> int:
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        if bridge.health():
            print("online")
            return EXIT_OK
        time.sleep(2)
    print("timeout: bridge did not come online", file=sys.stderr)
    return EXIT_BRIDGE_UNREACHABLE


def cmd_config_check(bridge: Bridge, args: argparse.Namespace) -> int:
    """Smoke test: does the bridge respond, is OneDrive running, can we list root?"""
    result: dict[str, Any] = {"checks": []}
    overall = EXIT_OK

    def add(name: str, ok: bool, detail: str = "") -> None:
        nonlocal overall
        result["checks"].append({"name": name, "ok": ok, "detail": detail})
        if not ok:
            overall = max(overall, EXIT_BRIDGE_UNREACHABLE)

    try:
        ok = bridge.health()
        add("bridge_healthz", ok, "" if ok else "did not respond to /healthz")
    except Exception as exc:
        add("bridge_healthz", False, str(exc))

    try:
        ss = bridge.sync_status()
        add("onedrive_running", ss.get("onedrive_running", False),
            f"root={ss.get('onedrive_root')}")
    except Exception as exc:
        add("onedrive_running", False, str(exc))

    try:
        ls = bridge.ls("/")
        add("ls_root", True, f"{len(ls.get('entries', []))} entries")
    except Exception as exc:
        add("ls_root", False, str(exc))

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return overall


# ── Entry point ──────────────────────────────────────────────────────


def make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="piggydrive",
        description="Linux CLI for piggydrive — delegate cloud sync to a trusted bridge.",
    )
    p.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG_PATH,
        help=f"Config file (default: {DEFAULT_CONFIG_PATH})",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("ls", help="List a remote directory")
    s.add_argument("path", nargs="?", default="/")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_ls)

    s = sub.add_parser("stat", help="Stat a remote path (always JSON)")
    s.add_argument("path")
    s.set_defaults(func=cmd_stat)

    s = sub.add_parser(
        "find",
        help="Search for filenames containing a substring (Spotlight on macOS)",
    )
    s.add_argument("query", help="Substring of filename to match (case-insensitive)")
    s.add_argument(
        "--in", dest="in_path", default="/",
        help="Limit search to this subtree (default: /)",
    )
    s.add_argument(
        "--max", type=int, default=100,
        help="Max results to return (default: 100, sidecar caps at 5000)",
    )
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_find)

    s = sub.add_parser("pull", help="Download a remote file")
    s.add_argument("remote")
    s.add_argument("local")
    s.add_argument("--timeout", type=int, default=None,
                   help="Materialize timeout (seconds)")
    s.set_defaults(func=cmd_pull)

    s = sub.add_parser("push", help="Upload a local file")
    s.add_argument("local")
    s.add_argument("remote")
    s.set_defaults(func=cmd_push)

    s = sub.add_parser("cat", help="Print remote file to stdout")
    s.add_argument("remote")
    s.add_argument("--timeout", type=int, default=None)
    s.set_defaults(func=cmd_cat)

    s = sub.add_parser("mkdir", help="Create a remote directory")
    s.add_argument("path")
    s.set_defaults(func=cmd_mkdir)

    s = sub.add_parser("rm", help="Remove a remote path")
    s.add_argument("path")
    s.add_argument("--recursive", action="store_true")
    s.set_defaults(func=cmd_rm)

    s = sub.add_parser("mv", help="Move / rename within remote")
    s.add_argument("src")
    s.add_argument("dst")
    s.set_defaults(func=cmd_mv)

    s = sub.add_parser("sync-status", help="Bridge + OneDrive sync state")
    s.set_defaults(func=cmd_sync_status)

    s = sub.add_parser("wait-online", help="Block until bridge is reachable")
    s.add_argument("--timeout", type=int, default=60)
    s.set_defaults(func=cmd_wait_online)

    s = sub.add_parser("config", help="Inspect / smoke-test config")
    config_sub = s.add_subparsers(dest="config_cmd", required=True)
    cs = config_sub.add_parser("check", help="Run a smoke test against the bridge")
    cs.set_defaults(func=cmd_config_check)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = make_parser()
    args = parser.parse_args(argv)

    try:
        config = Config.load(args.config)
    except (ValueError, FileNotFoundError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    bridge = Bridge(config)

    try:
        return args.func(bridge, args)
    except BridgeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    sys.exit(main())
