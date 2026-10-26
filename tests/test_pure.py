"""Pure-function regression tests (no network).

Covers the review fixes: query validation, EPMC translation,
smart_truncate word boundary, FTS sanitiser, study-type pushdown.
Run: python3 -m pytest tests/ -q
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import med_search_cli

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


def _stub_meta(pmid="123"):
    return {
        "doi": "10.1/test", "title": "T", "abstract": "A",
        "date": "2024-01-02", "study_type": "RCT", "commentary": False,
        "journal": "J", "authors": ["A B"], "mesh_keywords": [],
        "pub_types": ["Journal Article"],
    }


def test_fresh_record_prefers_pubmed_and_has_full_shape(monkeypatch=None):
    # No network: stub the metadata + full-text fetchers. Both sources
    # have text, so the deterministic contract must pick PubMed.
    orig = (
        med_search_cli.pmid_metadata,
        med_search_cli.fetch_pubmed_fulltext,
        med_search_cli.fetch_europepmc_fulltext,
        med_search_cli.fetch_unpaywall,
    )
    try:
        med_search_cli.pmid_metadata = lambda pmid: _stub_meta()
        med_search_cli.fetch_pubmed_fulltext = lambda pmid: {
            "sections": {"results": "r" * 300}}
        med_search_cli.fetch_europepmc_fulltext = lambda pmid: {
            "sections": {"results": "e" * 300}}
        med_search_cli.fetch_unpaywall = lambda doi: None
        out, source = med_search_cli._fetch_fresh_record("123")
        assert source == "pubmed_ft", source
        assert out["sections"]["results"] == "r" * 300
        # The old _refresh_cache record lacked these three fields.
        assert out["date"] == "2024-01-02"
        assert out["commentary"] is False
        assert out["pub_types"] == ["Journal Article"]
    finally:
        (med_search_cli.pmid_metadata,
         med_search_cli.fetch_pubmed_fulltext,
         med_search_cli.fetch_europepmc_fulltext,
         med_search_cli.fetch_unpaywall) = orig


def test_stale_fetch_refreshes_synchronously(tmp_path=None):
    # A stale cache entry must be refreshed in the same call (fresh
    # record returned with refreshed=True), and a failed refresh must
    # fall back to the stale record with cached_stale=True.
    import sqlite3
    import tempfile
    db = tempfile.mktemp(suffix=".db")
    orig_db = med_search_cli.DB_FILE
    orig = (
        med_search_cli.pmid_metadata,
        med_search_cli.fetch_pubmed_fulltext,
        med_search_cli.fetch_europepmc_fulltext,
        med_search_cli.fetch_unpaywall,
    )
    try:
        med_search_cli.DB_FILE = db
        med_search_cli.init_db()
        med_search_cli.save_to_cache(
            "123", "fallback",
            {"pmid": "123", "title": "OLD", "abstract": "old",
             "date": "2020-01-01", "study_type": "Unknown",
             "commentary": False, "sections": None, "open_access": None,
             "proxy_url": None, "journal": "J", "authors": [],
             "mesh_keywords": [], "pub_types": []}, 30)
        conn = sqlite3.connect(db)
        conn.execute("UPDATE lit_cache SET updated='2020-01-01T00:00:00+00:00' "
                     "WHERE pmid='123'")
        conn.commit()
        conn.close()
        assert med_search_cli.get_cached_data("123")["stale"]
        med_search_cli.pmid_metadata = lambda pmid: _stub_meta()
        med_search_cli.fetch_pubmed_fulltext = lambda pmid: {
            "sections": {"results": "r" * 300}}
        med_search_cli.fetch_europepmc_fulltext = lambda pmid: None
        med_search_cli.fetch_unpaywall = lambda doi: None
        res = med_search_cli._fetch_one_pmid("123", "all", 6000, 30)
        assert res.get("refreshed") is True, res
        assert res.get("title") == "T", res
        assert not res.get("cached_stale"), res
        # Now break metadata: the same (now fresh) entry is stale again
        # only after backdating; refresh fails -> stale record returned.
        conn = sqlite3.connect(db)
        conn.execute("UPDATE lit_cache SET updated='2020-01-01T00:00:00+00:00' "
                     "WHERE pmid='123'")
        conn.commit()
        conn.close()
        med_search_cli.pmid_metadata = lambda pmid: None
        res2 = med_search_cli._fetch_one_pmid("123", "all", 6000, 30)
        assert res2.get("cached_stale") is True, res2
        assert res2.get("title") == "T", res2
    finally:
        (med_search_cli.pmid_metadata,
         med_search_cli.fetch_pubmed_fulltext,
         med_search_cli.fetch_europepmc_fulltext,
         med_search_cli.fetch_unpaywall) = orig
        med_search_cli.DB_FILE = orig_db
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db + suffix)
            except OSError:
                pass
