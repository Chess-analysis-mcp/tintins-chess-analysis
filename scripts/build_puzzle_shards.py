#!/usr/bin/env python3
"""Developer-side: slice the CC0 Lichess puzzle DB into rating-banded, theme-stratified shards.

Produces, for a release of the SEPARATE puzzle-data repo (config.PUZZLE_SHARD_REPO):

  out/band_<lo>_<hi>.jsonl.gz   dense per-band shard (~10k puzzles, gzip JSONL)
  out/manifest.json             {shards: [{band, count, themes, bytes, sha256}], ...}

and the committed offline fallback, vendored in-repo (>=100 puzzles per band):

  server/data/puzzles/baseline.jsonl.gz

Runtime ships only stdlib gzip; this script reads the Lichess `.csv.zst` source via the `zstd`
CLI (preferred, no Python dep) or the `zstandard` package as a fallback. Per-band sampling uses a
uniform reservoir so memory stays bounded even though the middle bands hold millions of rows.

Usage:
  python scripts/build_puzzle_shards.py                 # download + build everything
  python scripts/build_puzzle_shards.py --source FILE   # use a local lichess_db_puzzle.csv.zst
  python scripts/build_puzzle_shards.py --limit 200000  # quick test on the first N rows
Then publish (separate data repo):
  gh repo create Chess-analysis-mcp/tintins-chess-puzzles --public
  gh release create puzzles-v1 out/*.jsonl.gz out/manifest.json -t "Puzzle shards v1"
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DB_URL = "https://database.lichess.org/lichess_db_puzzle.csv.zst"
REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = REPO_ROOT / "server" / "data" / "puzzles" / "baseline.jsonl.gz"

BAND_WIDTH = 100
BAND_LO = 600
BAND_HI = 2900  # exclusive upper edge of the last band's range start (last band 2800-2900)


def band_of(rating: int) -> tuple[int, int]:
    lo = max(BAND_LO, min(BAND_HI - BAND_WIDTH, (rating // BAND_WIDTH) * BAND_WIDTH))
    return lo, lo + BAND_WIDTH


def _open_rows(source: str | None):
    """Yield decoded CSV text lines from the `.csv.zst` source (download if no local file)."""
    if source:
        path = source
        cleanup = False
    else:
        fd, path = tempfile.mkstemp(suffix=".csv.zst")
        os.close(fd)
        cleanup = True
        print(f"Downloading {DB_URL} -> {path} ...", file=sys.stderr)
        import httpx

        with httpx.stream("GET", DB_URL, follow_redirects=True, timeout=None) as resp:
            resp.raise_for_status()
            with open(path, "wb") as fh:
                for chunk in resp.iter_bytes(1 << 20):
                    fh.write(chunk)

    zstd = shutil.which("zstd") or shutil.which("unzstd")
    try:
        if zstd:
            proc = subprocess.Popen([zstd, "-dc", path], stdout=subprocess.PIPE)
            stream = io.TextIOWrapper(proc.stdout, encoding="utf-8", newline="")
            yield from stream
            proc.wait()
        else:
            import zstandard  # type: ignore

            with open(path, "rb") as fh:
                reader = zstandard.ZstdDecompressor().stream_reader(fh)
                stream = io.TextIOWrapper(reader, encoding="utf-8", newline="")
                yield from stream
    finally:
        if cleanup:
            try:
                os.remove(path)
            except OSError:
                pass


def _normalize(row: dict) -> dict | None:
    try:
        moves = (row["Moves"] or "").split()
        if len(moves) < 2:
            return None
        return {
            "id": row["PuzzleId"],
            "fen": row["FEN"],
            "moves": moves,
            "rating": int(row["Rating"]),
            "rd": int(row["RatingDeviation"]),
            "themes": (row["Themes"] or "").split(),
            "popularity": int(row.get("Popularity", 0) or 0),
            "nbplays": int(row.get("NbPlays", 0) or 0),
            "game_url": row.get("GameUrl", ""),
        }
    except (KeyError, ValueError):
        return None


def _theme_spread(puzzles: list[dict], n: int, rng: random.Random) -> list[dict]:
    """Pick up to n puzzles, round-robin across themes, so rare motifs aren't crowded out."""
    if len(puzzles) <= n:
        return puzzles[:]
    buckets: dict[str, list[dict]] = {}
    for p in puzzles:
        key = p["themes"][0] if p["themes"] else "_none"
        buckets.setdefault(key, []).append(p)
    for b in buckets.values():
        rng.shuffle(b)
    chosen: list[dict] = []
    seen_ids: set[str] = set()
    keys = list(buckets)
    rng.shuffle(keys)
    while len(chosen) < n:
        progressed = False
        for k in keys:
            if buckets[k]:
                p = buckets[k].pop()
                if p["id"] not in seen_ids:
                    chosen.append(p)
                    seen_ids.add(p["id"])
                    progressed = True
                    if len(chosen) >= n:
                        break
        if not progressed:
            break
    return chosen


def _write_jsonl_gz(path: Path, puzzles: list[dict]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for p in puzzles:
            fh.write(json.dumps(p, separators=(",", ":")) + "\n")
    return path.stat().st_size


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", help="local lichess_db_puzzle.csv.zst (skip download)")
    ap.add_argument("--out-dir", default=str(REPO_ROOT / "out" / "puzzles"))
    ap.add_argument("--cap", type=int, default=10000, help="max puzzles per dense band shard")
    ap.add_argument("--baseline-per-band", type=int, default=150)
    ap.add_argument("--limit", type=int, default=0, help="stop after N source rows (testing)")
    ap.add_argument("--seed", type=int, default=12345)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    out_dir = Path(args.out_dir)

    # Per-band uniform reservoir of size `cap`, so memory stays bounded.
    reservoirs: dict[tuple[int, int], list[dict]] = {}
    counts: dict[tuple[int, int], int] = {}

    lines = _open_rows(args.source)
    header = next(lines).rstrip("\n")
    fieldnames = header.split(",")

    seen_rows = 0
    for raw in lines:
        raw = raw.rstrip("\n")
        if not raw:
            continue
        row = dict(zip(fieldnames, next(csv.reader([raw]))))
        p = _normalize(row)
        if p is None:
            continue
        seen_rows += 1
        band = band_of(p["rating"])
        counts[band] = counts.get(band, 0) + 1
        res = reservoirs.setdefault(band, [])
        if len(res) < args.cap:
            res.append(p)
        else:
            j = rng.randint(0, counts[band] - 1)
            if j < args.cap:
                res[j] = p
        if args.limit and seen_rows >= args.limit:
            break
        if seen_rows % 250000 == 0:
            print(f"  ... {seen_rows} rows", file=sys.stderr)

    print(f"Parsed {seen_rows} puzzles across {len(reservoirs)} bands.", file=sys.stderr)

    # Write dense shards + manifest, and collect the baseline.
    shards = []
    baseline: list[dict] = []
    for band in sorted(reservoirs):
        lo, hi = band
        pool = reservoirs[band]
        shard_path = out_dir / f"band_{lo}_{hi}.jsonl.gz"
        size = _write_jsonl_gz(shard_path, pool)
        theme_counts: dict[str, int] = {}
        for p in pool:
            for t in p["themes"]:
                theme_counts[t] = theme_counts.get(t, 0) + 1
        shards.append({
            "band": [lo, hi],
            "file": shard_path.name,
            "count": len(pool),
            "themes": theme_counts,
            "bytes": size,
            "sha256": _sha256(shard_path),
        })
        baseline.extend(_theme_spread(pool, args.baseline_per_band, rng))
        if len(pool) < 100:
            print(f"  WARNING: band {lo}-{hi} has only {len(pool)} puzzles (<100 floor).",
                  file=sys.stderr)

    manifest = {
        "version": 1,
        "source": DB_URL,
        "band_width": BAND_WIDTH,
        "shards": shards,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    rng.shuffle(baseline)
    base_size = _write_jsonl_gz(BASELINE_PATH, baseline)
    print(f"Wrote {len(shards)} dense shards to {out_dir}", file=sys.stderr)
    print(f"Wrote baseline: {len(baseline)} puzzles, {base_size/1024:.0f} KB -> {BASELINE_PATH}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
