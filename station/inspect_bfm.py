"""
inspect_bfm.py — READ-ONLY inspection tool for Bambu Farm Manager.

Goal
----
We need to know how to programmatically get a student-submitted .gcode.3mf file
into Bambu Farm Manager's "Files" tab (so a human in the lab can later click
Create → Direct to Print). BFM is an Electron client + a local server that uses a
RocksDB store, and it has no documented upload API. This script gathers clues
without modifying anything:

  1. Scans the server's log files for upload-related lines (URLs, paths, filenames).
  2. Lists the server_data RocksDB files with sizes and extracts printable strings
     from them, filtering for endpoints / filenames / cloud hosts.
  3. Static-analyzes the Electron Client's bundled app.asar for API endpoint strings
     and auth header names.
  4. Optional monitor mode: watches server_data sizes + tails debug.log while you
     click Upload once in the Client, so we see exactly what changes in flight.

It only opens files for reading and lists directories. It writes nothing, sends
nothing anywhere, and touches no BFM database. Run it on the Windows laptop where
BFM is installed:

    python station/inspect_bfm.py
    python station/inspect_bfm.py --monitor 60

Then paste the printed report back into the chat.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path
from typing import Iterable

# ---------------------------------------------------------------------------
# Configuration (override via CLI)
# ---------------------------------------------------------------------------

DEFAULT_SERVER_DIR = Path(r"C:\Program Files\Bambu Farm Manager Server")
DEFAULT_CLIENT_DIR = Path(r"C:\Program Files\Bambu Farm Manager Client")

# Keywords we care about. Case-insensitive. Matched as substrings.
KEYWORDS = [
    "3mf", "gcode", "upload", "download", "/api", "http", "https",
    "bambulab", "aliyuncs", "oss", "cloud", "file", "filename",
    "authorization", "bearer", "token", "access", "sst", "rocksdb",
]

# Patterns that look like endpoints or URLs (printed with higher prominence).
URL_RE = re.compile(r"https?://[^\s\"'<>\\]{4,}|/[a-z0-9_\-]{2,}(?:/[a-z0-9_\-]{2,})+", re.I)

# Printable-ASCII run of >= 6 chars, for pulling strings out of binary files.
PRINTABLE_RE = re.compile(rb"[\x20-\x7e]{6,}")

MAX_MATCHES_PER_SECTION = 60
MAX_LINE_LEN = 240


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def banner(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def trunc(s: str, n: int = MAX_LINE_LEN) -> str:
    s = s.strip()
    return s if len(s) <= n else s[:n] + " …(+%d)" % (len(s) - n)


def matches_keywords(text: str, keywords: Iterable[str]) -> list[str]:
    low = text.lower()
    return [kw for kw in keywords if kw in low]


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def binary_strings(path: Path, max_bytes: int = 80 * 1024 * 1024) -> list[str]:
    """Extract printable-ASCII runs (>=6 chars) from a binary file, fast."""
    out: list[str] = []
    size = path.stat().st_size
    if size == 0:
        return out
    # Read in chunks with overlap so strings spanning a boundary survive.
    chunk = 4 * 1024 * 1024
    overlap = 256
    with path.open("rb") as f:
        prev_tail = b""
        while True:
            data = f.read(chunk)
            if not data:
                break
            blob = prev_tail + data
            for m in PRINTABLE_RE.findall(blob):
                out.append(m.decode("ascii", "replace"))
            prev_tail = data[-overlap:] if len(data) >= overlap else data
            if f.tell() >= max_bytes:
                out.append(f"…(truncated at {human_size(max_bytes)})")
                break
    return out


# ---------------------------------------------------------------------------
# Section 1: server logs
# ---------------------------------------------------------------------------

def scan_logs(server_dir: Path) -> None:
    banner("1) SERVER LOGS")
    logs_dir = server_dir / "logs"
    if not logs_dir.is_dir():
        print(f"  [not found] {logs_dir}")
        return
    for log in sorted(logs_dir.glob("*.log")):
        size = log.stat().st_size
        print(f"\n  -- {log.name} ({human_size(size)}) --")
        if size > 200 * 1024 * 1024:
            print("  [skipped: file too large to read fully]")
            continue
        shown = 0
        total = 0
        with log.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if matches_keywords(line, KEYWORDS):
                    total += 1
                    if shown < MAX_MATCHES_PER_SECTION:
                        print("    " + trunc(line.rstrip("\n")))
                        shown += 1
        print(f"  ({total} matching line(s); showed first {shown})")


# ---------------------------------------------------------------------------
# Section 2: server_data RocksDB store
# ---------------------------------------------------------------------------

def scan_server_data(server_dir: Path) -> None:
    banner("2) SERVER_DATA (RocksDB store)")
    sd = server_dir / "server_data"
    if not sd.is_dir():
        print(f"  [not found] {sd}")
        return
    files = sorted(sd.iterdir(), key=lambda p: p.name)
    print("  Contents:")
    for p in files:
        if p.is_file():
            print(f"    {p.name:30s}  {human_size(p.stat().st_size):>10s}  {time.ctime(p.stat().st_mtime)}")

    print("\n  Printable strings matching keywords (from .sst/.vlog):")
    shown = 0
    seen = set()
    for p in files:
        if not p.is_file() or p.suffix.lower() not in (".sst", ".vlog", ".log", ".db"):
            continue
        for s in binary_strings(p):
            if matches_keywords(s, KEYWORDS) and s not in seen:
                seen.add(s)
                if shown < MAX_MATCHES_PER_SECTION:
                    print("    " + trunc(s))
                    shown += 1
    print(f"  ({len(seen)} unique matching string(s); showed first {shown})")


# ---------------------------------------------------------------------------
# Section 3: Client app.asar static analysis
# ---------------------------------------------------------------------------

def scan_client(client_dir: Path) -> None:
    banner("3) CLIENT app.asar (static endpoint analysis)")
    resources = client_dir / "resources"
    if not resources.is_dir():
        print(f"  [not found] {resources}")
        return
    asar = resources / "app.asar"
    if not asar.is_file():
        print(f"  [not found] {asar}")
        print("  resources/ contents: " + ", ".join(p.name for p in resources.iterdir()))
        return

    print(f"  Scanning {asar} ({human_size(asar.stat().st_size)}) …")
    strings = binary_strings(asar)
    # Focus on likely endpoint / auth strings.
    endpoint_hits: list[str] = []
    auth_hits: list[str] = []
    other_hits: list[str] = []
    seen = set()
    for s in strings:
        if s in seen:
            continue
        seen.add(s)
        low = s.lower()
        if "authorization" in low or "bearer" in low or "token" in low or "x-" in low:
            auth_hits.append(s)
        elif URL_RE.search(s) or "/api" in low or "bambulab" in low or "aliyuncs" in low or "upload" in low:
            endpoint_hits.append(s)
        elif matches_keywords(s, ("3mf", "gcode", "file", "cloud")):
            other_hits.append(s)

    def dump(label: str, items: list[str]) -> None:
        print(f"\n  {label} ({len(items)}):")
        for s in items[:MAX_MATCHES_PER_SECTION]:
            print("    " + trunc(s))

    dump("Endpoint / URL strings", endpoint_hits)
    dump("Auth / header strings", auth_hits)
    dump("Other file-related strings", other_hits)


# ---------------------------------------------------------------------------
# Section 4: monitor mode (watch during one manual upload)
# ---------------------------------------------------------------------------

def monitor(server_dir: Path, seconds: int) -> None:
    banner(f"4) MONITOR ({seconds}s) — click Upload in the Client NOW")
    sd = server_dir / "server_data"
    debug_log = server_dir / "logs" / "debug.log"

    def snapshot() -> dict[str, int]:
        if not sd.is_dir():
            return {}
        return {p.name: p.stat().st_size for p in sd.iterdir() if p.is_file()}

    prev = snapshot()
    log_pos = debug_log.stat().st_size if debug_log.is_file() else 0
    print(f"  Watching: {sd}")
    print(f"  Tailing : {debug_log}")
    print("  " + "-" * 74)

    deadline = time.time() + seconds
    while time.time() < deadline:
        time.sleep(1.0)
        cur = snapshot()
        for name, size in cur.items():
            old = prev.get(name)
            if old is None:
                print(f"  [+] new file      {name} ({human_size(size)})")
            elif size != old:
                print(f"  [~] size changed  {name}: {human_size(old)} -> {human_size(size)}")
        for name in prev.keys() - cur.keys():
            print(f"  [-] file removed  {name}")
        prev = cur

        if debug_log.is_file():
            with debug_log.open("r", encoding="utf-8", errors="replace") as f:
                f.seek(log_pos)
                for line in f:
                    if matches_keywords(line, KEYWORDS):
                        print("  [log] " + trunc(line.rstrip("\n")))
                log_pos = f.tell()
    print("  [monitor finished]")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description="Read-only Bambu Farm Manager inspector.")
    p.add_argument("--server", default=str(DEFAULT_SERVER_DIR), help="BFM Server install dir")
    p.add_argument("--client", default=str(DEFAULT_CLIENT_DIR), help="BFM Client install dir")
    p.add_argument("--monitor", type=int, metavar="SECONDS", default=0,
                   help="Watch server_data + debug.log for SECONDS while you click Upload")
    args = p.parse_args()

    server_dir = Path(args.server)
    client_dir = Path(args.client)

    print("Bambu Farm Manager — READ-ONLY inspection")
    print(f"Server dir: {server_dir}  [{'exists' if server_dir.is_dir() else 'NOT FOUND'}]")
    print(f"Client dir: {client_dir}  [{'exists' if client_dir.is_dir() else 'NOT FOUND'}]")

    if not server_dir.is_dir() and not client_dir.is_dir():
        print("\nNeither install dir was found. Pass --server and --client with the correct paths.")
        sys.exit(1)

    scan_logs(server_dir)
    scan_server_data(server_dir)
    scan_client(client_dir)

    if args.monitor > 0:
        monitor(server_dir, args.monitor)

    banner("DONE")
    print("Copy everything above and paste it back into the chat.")


if __name__ == "__main__":
    main()
