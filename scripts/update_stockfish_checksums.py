#!/usr/bin/env python3
"""Regenerate scripts/stockfish_sha256.txt for a Stockfish release tag.

Run after bumping ``TAG`` in download_stockfish.py (and ``tag=`` in build_app.sh):

    python scripts/update_stockfish_checksums.py sf_18

It downloads every asset the installer can pick, hashes it, and rewrites the checksum file. The
hashes are trust-on-first-use: review the diff, and where possible compare against hashes
published by the Stockfish project before committing.
"""
from __future__ import annotations

import hashlib
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "stockfish_sha256.txt")

ASSETS = [
    "stockfish-macos-m1-apple-silicon.tar",
    "stockfish-macos-x86-64-avx2.tar",
    "stockfish-macos-x86-64-sse41-popcnt.tar",
    "stockfish-macos-x86-64.tar",
    "stockfish-ubuntu-x86-64-avx2.tar",
    "stockfish-ubuntu-x86-64-sse41-popcnt.tar",
    "stockfish-ubuntu-x86-64.tar",
    "stockfish-windows-armv8.zip",
    "stockfish-windows-x86-64-avx2.zip",
    "stockfish-windows-x86-64-sse41-popcnt.zip",
    "stockfish-windows-x86-64.zip",
]


def _sha256_url(url: str) -> str:
    h = hashlib.sha256()
    req = urllib.request.Request(url, headers={"User-Agent": "tintins-chess-analysis"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        for chunk in iter(lambda: resp.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        sys.stderr.write("usage: update_stockfish_checksums.py <tag, e.g. sf_18>\n")
        return 2
    tag = argv[1]
    base = f"https://github.com/official-stockfish/Stockfish/releases/download/{tag}"
    lines = []
    for name in ASSETS:
        sys.stderr.write(f"hashing {name}...\n")
        lines.append(f"{_sha256_url(f'{base}/{name}')}  {name}")
    header = (
        f"# SHA-256 of the pinned Stockfish release assets (tag: {tag}), sha256sum format.\n"
        "# Trust-on-first-use pins: computed from the official release when this file was generated, so\n"
        "# they detect any LATER tampering/substitution of the download, not a compromise that predates\n"
        "# the pin. Read by scripts/download_stockfish.py and the launcher in scripts/build_app.sh.\n"
        f"# Regenerate after bumping the tag:  python scripts/update_stockfish_checksums.py {tag}\n"
    )
    with open(OUT, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(header + "\n".join(lines) + "\n")
    sys.stderr.write(f"wrote {OUT}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
