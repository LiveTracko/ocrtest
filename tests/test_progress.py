"""Percentage progress bar — no network, no API key, no PDF needed."""
from pathlib import Path

from utils import calc_progress_pct, format_progress


def test_pct_zero():
    assert calc_progress_pct(0, 10) == 0


def test_pct_half():
    assert calc_progress_pct(5, 10) == 50


def test_pct_full():
    assert calc_progress_pct(10, 10) == 100


def test_pct_rounding():
    # 1/3 -> 33%, 2/3 -> 67%
    assert calc_progress_pct(1, 3) == 33
    assert calc_progress_pct(2, 3) == 67


def test_pct_clamped():
    assert calc_progress_pct(15, 10) == 100
    assert calc_progress_pct(-2, 10) == 0


def test_pct_zero_total():
    assert calc_progress_pct(0, 0) == 0
    assert calc_progress_pct(5, 0) == 100


def test_format_progress():
    assert format_progress(3, 10) == "3/10 pages (30%)"
    assert format_progress(0, 5) == "0/5 pages (0%)"
    assert format_progress(5, 5) == "5/5 pages (100%)"


def test_tkinter_app_has_determinate_pct():
    src = (Path(__file__).resolve().parent.parent / "app.py").read_text(encoding="utf-8")
    assert 'mode="determinate"' in src, "Progressbar must be determinate"
    assert "indeterminate" not in src or "determinate" in src
    assert "pct_var" in src, "Tk UI must have pct_var label"
    assert "_progress_set" in src, "Tk UI must update % via _progress_set"
    assert '"0%"' in src or "'0%'" in src or "pct}%" in src or "%" in src
    # no spinning indeterminate start() should remain
    assert ".progress.start(" not in src, "indeterminate start() must be removed"


def test_main_emits_progress_pct():
    src = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")
    assert "[PROGRESS]" in src
    assert "calc_progress_pct" in src or "format_progress" in src


def test_web_progress_shows_pct():
    js = (Path(__file__).resolve().parent.parent / "docs" / "app.js").read_text(encoding="utf-8")
    html = (Path(__file__).resolve().parent.parent / "docs" / "index.html").read_text(encoding="utf-8")
    assert "barPct" in js, "web progress() must update #barPct"
    assert "(${pct}%)" in js or "pct + '%'" in js
    assert "barPct" in html, "index.html must contain #barPct element"
