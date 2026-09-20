"""Pure-function regression tests (no network).

Covers the review fixes: query validation, EPMC translation,
smart_truncate word boundary, FTS sanitiser, study-type pushdown.
Run: python3 -m pytest tests/ -q
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from med_search_cli import (
    validate_query,
    europepmc_query,
    smart_truncate,
    search_cache,
    pushdown_clauses,
)


def test_validate_query_rejects_empty():
    _, e1 = validate_query("")
    _, e2 = validate_query("   ")
    _, e3 = validate_query("AND OR NOT")
    assert e1 and e2 and e3


def test_validate_query_accepts_real():
    q, e = validate_query("gastric antral vascular ectasia")
    assert e is None and q


def test_validate_query_rejects_single_char():
    _, e = validate_query("a")
    assert e


def test_europepmc_pmid_translation():
    out = europepmc_query("9500320[pmid]")
    assert "EXT_ID:9500320" in out, out


def test_europepmc_tiab_translation():
    out = europepmc_query('"lupus nephritis"[tiab]')
    assert "TITLE_ABS" in out, out


def test_smart_truncate_word_boundary():
    text = " ".join(f"word{i:03d}" for i in range(200))
    out, trunc, _ = smart_truncate(text, 200)
    assert trunc
    assert not out.endswith("...") or out[-4] != " "  # ends with ... after word
    body = out[:-3] if out.endswith("...") else out
    assert not body.endswith(" "), repr(body[-20:])


def test_search_cache_operators_safe():
    # Must not raise sqlite OperationalError on operator input
    assert search_cache("AND", 5) == []
    assert search_cache("x OR y", 5) == [] or isinstance(search_cache("x OR y", 5), list)


def test_pushdown_clauses():
    pm, ep = pushdown_clauses({"RCT", "Meta-Analysis"})
    assert "[pt]" in pm and "PUB_TYPE" in ep, (pm, ep)
    pm0, ep0 = pushdown_clauses(set())
    assert pm0 == "" and ep0 == ""
