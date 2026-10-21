"""Monetization layer: batch convert + the licensing gate. The default tier
is Pro (no cap); the opt-in community tier caps a batch at a fixed limit and
LOCKS the excess. A silent
break here is a revenue leak, so it's gated by tests: the cap is enforced,
the assessment HTML and batch zip generate, and the locked items are named.
"""
from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from converter import licensing  # noqa: E402
from converter.batch import (  # noqa: E402
    batch_convert, build_assessment_html, build_batch_zip,
)

_FIX = (ROOT / "tests" / "fixtures" / "source_of_truth" / "master_detail"
        / "source.xml")


def _items(n):
    blob = _FIX.read_bytes()
    return [(f"report_{i}.xml", blob) for i in range(n)]


def test_default_tier_is_pro_and_uncapped(monkeypatch):
    monkeypatch.delenv("O2S_LICENSE", raising=False)
    monkeypatch.delenv("O2S_BATCH_LIMIT", raising=False)
    assert licensing.current_tier() == "pro"
    assert licensing.batch_limit() is None
    assert licensing.tier_label() == "Pro"


def test_community_tier_caps_batch_and_locks_excess(monkeypatch):
    if not _FIX.exists():
        pytest.skip("fixture missing")
    monkeypatch.setenv("O2S_LICENSE", "community")
    monkeypatch.delenv("O2S_BATCH_LIMIT", raising=False)
    limit = licensing.batch_limit()
    assert limit == 10
    out = batch_convert(_items(limit + 5))
    results = out.get("results") or []
    locked = out.get("locked") or []
    # exactly `limit` processed, the remaining 5 locked (named for the upsell)
    assert len(results) == limit, (len(results), limit)
    assert len(locked) == 5, locked
    # tier is surfaced (batch stores the human label, not the key)
    assert out.get("tier") in (licensing.current_tier(), licensing.tier_label())


def test_batch_assessment_and_zip_generate():
    if not _FIX.exists():
        pytest.skip("fixture missing")
    out = batch_convert(_items(3))
    html = build_assessment_html(out)
    assert "<table" in html.lower() and len(html) > 500
    z = build_batch_zip(out)
    zf = zipfile.ZipFile(io.BytesIO(z))
    rdls = [n for n in zf.namelist() if n.endswith(".rdl")]
    assert len(rdls) >= 1, zf.namelist()
