#!/usr/bin/env python3
"""Re-vendor the frontend's third-party JS/CSS (chessground + chess.js) into frontend/vendor/.

WHY THIS EXISTS
---------------
The board renders fully offline, so chessground + chess.js are NOT loaded from a CDN at runtime —
they're vendored into `frontend/vendor/` and committed to the repo (force-included into the wheel as
`server/_frontend/vendor/`). Because they're committed source, they already ship to users through the
normal update paths (`git pull`, the in-app update button, the release zip) — there is deliberately
NO runtime re-download. This is a MAINTAINER tool: when you want to adopt a newer library version,
bump the pin below, run this once, eyeball the board, and commit the changed files.

Pinned on purpose: a CDN "latest" could pull a breaking major (e.g. chessground v10) and silently
blank the board for everyone on update day. Versions live here, in one place, and must match the
imports in `frontend/index.html` (the CSS <link>s) and `frontend/main.js` (the JS imports).

USAGE
-----
    uv run python scripts/vendor_frontend_assets.py            # refresh to the pinned versions
    uv run python scripts/vendor_frontend_assets.py --check    # CI: fail if vendored files are stale
    /opt/miniconda3/envs/chess-review/bin/python scripts/vendor_frontend_assets.py   # dev interp

Needs network (it fetches from jsdelivr/esm.sh). stdlib + httpx only (httpx is already a dep).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx

# --- Pinned versions (bump these, then re-run + commit) ------------------------------------------
CHESSGROUND_VERSION = "9.2.1"
CHESS_JS_VERSION = "1.4.0"

_VENDOR_DIR = Path(__file__).resolve().parents[1] / "frontend" / "vendor"

# Each entry: local filename -> source URL. The JS comes from esm.sh's *bundled* ESM build (a single
# self-contained module with no further network imports); the CSS from jsdelivr's raw npm assets
# (self-contained — pieces/board are embedded as data: URIs, so nothing is fetched at runtime).
_ASSETS: dict[str, str] = {
    "chessground.min.js": (
        f"https://esm.sh/chessground@{CHESSGROUND_VERSION}/es2020/chessground.bundle.mjs"
    ),
    "chess.min.js": (
        f"https://esm.sh/chess.js@{CHESS_JS_VERSION}/es2020/chess.bundle.mjs"
    ),
    "chessground.base.css": (
        f"https://cdn.jsdelivr.net/npm/chessground@{CHESSGROUND_VERSION}/assets/chessground.base.css"
    ),
    "chessground.brown.css": (
        f"https://cdn.jsdelivr.net/npm/chessground@{CHESSGROUND_VERSION}/assets/chessground.brown.css"
    ),
    "chessground.cburnett.css": (
        f"https://cdn.jsdelivr.net/npm/chessground@{CHESSGROUND_VERSION}"
        "/assets/chessground.cburnett.css"
    ),
}


def _fetch(url: str) -> bytes:
    resp = httpx.get(url, follow_redirects=True, timeout=30.0)
    resp.raise_for_status()
    body = resp.content
    # A self-contained ESM bundle must not import from the network at runtime, or "offline" is a lie.
    if url.endswith(".mjs") and (b"https://esm.sh" in body or b'from"/' in body or b"from '/" in body):
        raise SystemExit(f"FATAL: {url} still has external imports — not a self-contained bundle.")
    return body


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Don't write; exit non-zero if any vendored file differs from upstream (for CI).",
    )
    args = parser.parse_args()

    _VENDOR_DIR.mkdir(parents=True, exist_ok=True)
    stale: list[str] = []
    for name, url in _ASSETS.items():
        dest = _VENDOR_DIR / name
        try:
            fresh = _fetch(url)
        except httpx.HTTPError as exc:
            print(f"  ! {name}: download failed ({exc})", file=sys.stderr)
            return 2
        current = dest.read_bytes() if dest.exists() else None
        if current == fresh:
            print(f"  = {name} ({len(fresh):,} bytes, unchanged)")
            continue
        stale.append(name)
        if args.check:
            print(f"  ~ {name} is STALE vs upstream", file=sys.stderr)
        else:
            dest.write_bytes(fresh)
            verb = "updated" if current is not None else "created"
            print(f"  + {name} ({len(fresh):,} bytes, {verb})")

    if args.check:
        if stale:
            print(
                f"\n{len(stale)} vendored asset(s) are stale: {', '.join(stale)}.\n"
                "Run `python scripts/vendor_frontend_assets.py` and commit the result.",
                file=sys.stderr,
            )
            return 1
        print("\nAll vendored assets match the pinned upstream versions.")
        return 0

    print(
        f"\nDone. chessground@{CHESSGROUND_VERSION}, chess.js@{CHESS_JS_VERSION}.\n"
        "Reminder: these versions must match the imports in frontend/index.html + frontend/main.js. "
        "Open the board, confirm pieces render + moves work, then commit frontend/vendor/."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
