"""Checksum pinning for the Stockfish downloader (scripts/download_stockfish.py)."""
from __future__ import annotations

import hashlib

import pytest

import scripts.download_stockfish as dl


def _archive(tmp_path, data=b"engine-bytes"):
    p = tmp_path / "sf.tar"
    p.write_bytes(data)
    return str(p), hashlib.sha256(data).hexdigest()


def test_matching_checksum_passes(tmp_path):
    path, digest = _archive(tmp_path)
    dl._verify_checksum(path, "asset.tar", {"asset.tar": digest})  # no exception


def test_mismatch_fails_closed(tmp_path):
    path, _ = _archive(tmp_path)
    with pytest.raises(ValueError, match="checksum mismatch"):
        dl._verify_checksum(path, "asset.tar", {"asset.tar": "0" * 64})


def test_unpinned_asset_fails_closed(tmp_path):
    path, digest = _archive(tmp_path)
    with pytest.raises(ValueError, match="no pinned checksum"):
        dl._verify_checksum(path, "other.tar", {"asset.tar": digest})


def test_every_candidate_asset_is_pinned(monkeypatch):
    """Each build the installer can pick, on every platform, must have a pin."""
    pins = dl._load_checksums()
    assert all(len(d) == 64 for d in pins.values())
    for plat, name, machines in [
        ("darwin", "posix", ["arm64", "x86_64"]),
        ("linux", "posix", ["x86_64"]),
        ("win32", "nt", ["AMD64", "ARM64"]),
    ]:
        monkeypatch.setattr(dl.sys, "platform", plat)
        monkeypatch.setattr(dl.os, "name", name)
        ext = ".zip" if name == "nt" else ".tar"
        for m in machines:
            for asset in dl._candidates(force_arch=m):
                assert asset + ext in pins, asset + ext


def test_load_checksums_parses_sha256sum_format(tmp_path):
    f = tmp_path / "sums.txt"
    f.write_text("# comment\n\n" + "a" * 64 + "  one.tar\n" + "B" * 64 + "  *two.zip\n")
    assert dl._load_checksums(str(f)) == {"one.tar": "a" * 64, "two.zip": "b" * 64}
