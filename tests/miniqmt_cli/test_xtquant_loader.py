"""Verify the sys.path injection behavior of xtquant_loader.

We build a fake qmt_path layout that contains a stub xtquant package,
then call load_xtquant. The package should become importable and the
directory should be at the front of sys.path.
"""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from miniqmt_cli.server import xtquant_loader


def _build_fake_install(base: Path) -> None:
    site = base / "bin.x64" / "Lib" / "site-packages" / "xtquant"
    site.mkdir(parents=True)
    (site / "__init__.py").write_text("")
    (site / "xtdata.py").write_text(
        "def get_sector_list():\n    return ['sentinel']\n"
    )
    (site / "xttrader.py").write_text("class XtQuantTrader: pass\n")


@pytest.fixture
def clean_loader():
    # Remember and clear any prior xtquant imports
    saved = {k: v for k, v in sys.modules.items() if k.startswith("xtquant")}
    for k in list(saved):
        sys.modules.pop(k, None)
    saved_path = list(sys.path)
    xtquant_loader.reset_for_tests()
    yield
    for k in [k for k in sys.modules if k.startswith("xtquant")]:
        sys.modules.pop(k, None)
    sys.path[:] = saved_path
    xtquant_loader.reset_for_tests()


def test_load_injects_sys_path(tmp_path, clean_loader):
    _build_fake_install(tmp_path)
    xtquant_loader.load_xtquant(str(tmp_path))
    injected = str(tmp_path / "bin.x64" / "Lib" / "site-packages")
    assert injected in sys.path
    import xtquant.xtdata as xd
    assert xd.get_sector_list() == ["sentinel"]


def test_load_missing_path_raises(tmp_path, clean_loader):
    with pytest.raises(RuntimeError, match="xtquant not found"):
        xtquant_loader.load_xtquant(str(tmp_path / "nope"))


def test_load_empty_path_raises(clean_loader):
    with pytest.raises(RuntimeError, match="qmt_path is empty"):
        xtquant_loader.load_xtquant("")


def test_load_idempotent(tmp_path, clean_loader):
    _build_fake_install(tmp_path)
    xtquant_loader.load_xtquant(str(tmp_path))
    # Second call should not raise or re-inject a duplicate path
    xtquant_loader.load_xtquant(str(tmp_path))
    injected = str(tmp_path / "bin.x64" / "Lib" / "site-packages")
    assert sys.path.count(injected) == 1
