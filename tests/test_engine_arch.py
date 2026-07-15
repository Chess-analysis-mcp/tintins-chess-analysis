"""Apple-Silicon / Rosetta Stockfish detection + one-click arch-fix wiring.

Covers the dependency-free Mach-O arch parser, the "running the Intel build under Rosetta 2"
report, the download-script's arch selection (which must trust the *hardware*, since a translated
process reports x86_64 from platform.machine()), and the fix endpoint's no-op guard. No network,
no Stockfish, no subprocess — pure header bytes + monkeypatched hardware detection.
"""
from __future__ import annotations

import importlib.util
import os
import struct

from server import config

# --- Mach-O header fixtures -------------------------------------------------
_MH_MAGIC_64_LE = b"\xcf\xfa\xed\xfe"          # thin 64-bit, little-endian (the common macOS case)
_FAT_MAGIC = b"\xca\xfe\xba\xbe"               # fat / universal
_CPU_ARM64 = 0x0100000C
_CPU_X86_64 = 0x01000007


def _thin_macho(cputype: int) -> bytes:
    return _MH_MAGIC_64_LE + struct.pack("<I", cputype) + b"\x00" * 24


def test_macho_arch_thin_binaries(tmp_path):
    arm = tmp_path / "arm"
    arm.write_bytes(_thin_macho(_CPU_ARM64))
    intel = tmp_path / "intel"
    intel.write_bytes(_thin_macho(_CPU_X86_64))
    assert config.macho_arch(str(arm)) == "arm64"
    assert config.macho_arch(str(intel)) == "x86_64"


def test_macho_arch_universal_and_unknown(tmp_path):
    fat = tmp_path / "fat"
    fat.write_bytes(_FAT_MAGIC + b"\x00" * 20)
    short = tmp_path / "short"
    short.write_bytes(b"\xcf\xfa")            # truncated header
    junk = tmp_path / "junk"
    junk.write_bytes(b"not a macho binary at all")
    assert config.macho_arch(str(fat)) == "universal"
    assert config.macho_arch(str(short)) == "unknown"
    assert config.macho_arch(str(junk)) == "unknown"
    assert config.macho_arch(str(tmp_path / "missing")) == "unknown"


# --- stockfish_arch_report --------------------------------------------------
def test_report_flags_intel_on_apple_silicon(tmp_path, monkeypatch):
    intel = tmp_path / "stockfish"
    intel.write_bytes(_thin_macho(_CPU_X86_64))
    monkeypatch.setattr(config, "is_apple_silicon", lambda: True)
    rep = config.stockfish_arch_report(str(intel))
    assert rep["suboptimal"] is True and rep["can_fix"] is True
    assert rep["binary"] == "x86_64" and rep["hardware"] == "arm64"


def test_report_clears_for_native_or_universal(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "is_apple_silicon", lambda: True)
    native = tmp_path / "arm"
    native.write_bytes(_thin_macho(_CPU_ARM64))
    fat = tmp_path / "fat"
    fat.write_bytes(_FAT_MAGIC + b"\x00" * 20)
    assert config.stockfish_arch_report(str(native))["suboptimal"] is False
    assert config.stockfish_arch_report(str(fat))["suboptimal"] is False


def test_report_never_fires_off_apple_silicon(tmp_path, monkeypatch):
    intel = tmp_path / "stockfish"
    intel.write_bytes(_thin_macho(_CPU_X86_64))
    monkeypatch.setattr(config, "is_apple_silicon", lambda: False)
    rep = config.stockfish_arch_report(str(intel))
    assert rep["suboptimal"] is False and rep["can_fix"] is False


# --- download-script arch selection (must trust hardware, not platform.machine) ---
def _load_download_module():
    path = os.path.join(config.PROJECT_ROOT, "scripts", "download_stockfish.py")
    spec = importlib.util.spec_from_file_location("dl_stockfish", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_download_picks_arm_on_apple_silicon(monkeypatch):
    dl = _load_download_module()
    # Force the darwin branch of the candidate table regardless of the host running the test.
    monkeypatch.setattr(dl.sys, "platform", "darwin")
    monkeypatch.setattr(dl.config, "is_apple_silicon", lambda: True)
    monkeypatch.delenv("CHESS_FORCE_STOCKFISH_ARCH", raising=False)
    assert dl._candidates() == ["stockfish-macos-m1-apple-silicon"]


def test_download_force_arch_overrides(monkeypatch):
    dl = _load_download_module()
    monkeypatch.setattr(dl.sys, "platform", "darwin")
    monkeypatch.setattr(dl.config, "is_apple_silicon", lambda: False)
    assert dl._candidates("arm64") == ["stockfish-macos-m1-apple-silicon"]
    monkeypatch.setenv("CHESS_FORCE_STOCKFISH_ARCH", "arm64")
    assert dl._candidates() == ["stockfish-macos-m1-apple-silicon"]


# --- fix endpoint guard -----------------------------------------------------
def test_fix_endpoint_no_op_when_nothing_to_fix(monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("CHESS_WEB_AUTOSTART", "0")
    from server.web.app import create_app

    monkeypatch.setattr(
        config, "stockfish_arch_report", lambda *a, **k: {"suboptimal": False, "can_fix": False}
    )
    client = TestClient(create_app())
    resp = client.post("/api/fix-stockfish-arch")
    assert resp.status_code == 409
    assert resp.json()["ok"] is False
