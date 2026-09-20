#!/usr/bin/env python3
"""Med Search CLI — v3 (single canonical CLI).

Twin-track parallel PubMed / EuropePMC search, cache, and researcher workflow
tooling for AI agents and clinicians.

v3 changes vs the legacy v2 script
----------------------------------
* fixes empty-text / wrong-source bug when a full-text section index exists
  but holds no text (abstract fallback now mandatory)
* fixes cited-by overlay (falsy 0 from EuropePMC was being dropped)
* ``--min-citations`` and ``--sort citations`` actually work; the citation
  floor is pushed down to EuropePMC (``CITED:[N TO *]``)
* ``--study-type`` is pushed down to PubMed (``[pt]``) and EuropePMC
  (``PUB_TYPE:"..."``) instead of filtering a tiny post-hoc page
* whitespace / empty / operator-only queries are rejected instead of
  silently returning unrelated records
* structured abstract labels preserved (AIMS / METHODS / RESULTS / CONCLUSION)
* retraction + erratum + expression-of-concern flags on every record
* new researcher commands: mesh, related, citedby, refs, export, trials, watch
"""

import click
import json
import os
import re
import sqlite3
import sys
import time
import random
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from collections import deque
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import pymed

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DB_FILE = os.path.expanduser(os.getenv("MED_SEARCH_DB", "pubmed_agent_cache.db"))
AGENT_EMAIL = os.getenv("MED_SEARCH_EMAIL", "your_official_email@example.com")
DEFAULT_TTL_DAYS = int(os.getenv("MED_SEARCH_TTL", "30"))
MAX_BACKOFF_ATTEMPTS = 4

# Per-endpoint rolling latency trackers (seconds)
_latency = {
    "ncbi": deque(maxlen=10),
    "europepmc": deque(maxlen=10),
    "unpaywall": deque(maxlen=10),
    # OpenAlex backs the reference-list fallback (EuropePMC /references 503s).
    "openalex": deque(maxlen=10),
}

# Background executor for stale-cache refresh (daemon threads, torn down at exit)
_bg_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="bgcache")

# ---------------------------------------------------------------------------
# Query utilities
# ---------------------------------------------------------------------------
_PUBMED_FIELD_QUALIFIER_RE = re.compile(
    r"\[(?i:pmid|uid|mesh(?:\s+terms)?|mh|majr|minr|tiab|ti|ab|tw|ot|au|ad|"
    r"dp|edat|pdat|cdat|la|pt|ta|jt|so|vi|ip|pg|is|rn|nm|sh|sb|ed|cn|gr|rd|"
    r"all|filt|subset|sb|journal|book|conf)\s*\]"
)
_PUBMED_BOOLEAN_RE = re.compile(r"\b(AND|OR|NOT)\b")

# PubMed qualifier -> EuropePMC field, for the twin-track leg. Anything not in
# this map gets stripped: EPMC cannot parse PubMed-only qualifiers and returns
# unrelated records for them (e.g. '9500320[pmid]' matched random papers).
_EPMC_FIELD_MAP = {
    "pmid": "EXT_ID", "uid": "EXT_ID",
    "tiab": "TITLE_ABS", "ti": "TITLE", "ab": "ABSTRACT",
    "mh": "MESH", "mesh": "MESH", "mesh terms": "MESH",
    "au": "AUTH", "ta": "JOURNAL", "journal": "JOURNAL",
    "pt": "PUB_TYPE", "dp": "FIRST_PDATE", "pdat": "FIRST_PDATE",
    "la": "LANG", "rn": "CHEM",
}


def is_pubmed_syntax_query(query: str) -> bool:
    """Return True if query contains PubMed field qualifiers or explicit
    uppercase boolean operators (AND/OR/NOT).  When True, the query should
    be passed verbatim to E-utilities rather than TIAB-wrapped."""
    if _PUBMED_FIELD_QUALIFIER_RE.search(query):
        return True
    if _PUBMED_BOOLEAN_RE.search(query):
        return True
    return False


def europepmc_query(query: str) -> str:
    """Translate a PubMed-syntax query into EuropePMC syntax.

    EPMC silently mis-parses PubMed-only qualifiers ([pmid], [tiab], [mh]...)
    and answers with unrelated records, which then pollute the merged result
    set. Map the supported ones, drop the rest, keep quoted phrases.
    """
    if not _PUBMED_FIELD_QUALIFIER_RE.search(query):
        return query

    def _rewrite(m: "re.Match[str]") -> str:
        term = m.group(1).strip()
        field = m.group(2).strip().lower()
        epmc_field = _EPMC_FIELD_MAP.get(field)
        if not epmc_field:
            return term  # unsupported qualifier — keep the term, drop the tag
        if epmc_field == "EXT_ID":
            return f'EXT_ID:{term.strip(chr(34))}'
        return f"{epmc_field}:{term}"

    translated = re.sub(
        r'(\"[^\"]+\"|\([^()]+\)|[^\s\[\]()]+)\s*\[([^\]]+)\]',
        _rewrite, query)
    return f"({translated})" if " " in translated.strip() else translated


# ---------------------------------------------------------------------------
# Author / MeSH extraction helpers
# ---------------------------------------------------------------------------
def _extract_authors_pubmed(authors_data, max_authors: int = 3) -> list[str]:
    """Normalize pymed author dicts to a flat list of 'Lastname FN' strings."""
    if not authors_data:
        return []
    out = []
    for a in authors_data[:max_authors]:
        if isinstance(a, dict):
            last = a.get("lastname", "")
            first = a.get("firstname", "") or a.get("initials", "")
            name = f"{last} {first}".strip()
            if name:
                out.append(name)
        elif isinstance(a, str):
            out.append(a)
    return out


def _extract_authors_europepmc(result: dict, max_authors: int = 3) -> list[str]:
    """Extract first N author names from EuropePMC result dict."""
    author_string = result.get("authorString")
    if author_string:
        return [a.strip() for a in author_string.split(",")][:max_authors]
    author_list = result.get("authorList", {})
    if isinstance(author_list, dict):
        authors = author_list.get("author", [])
        if isinstance(authors, list):
            out = []
            for a in authors[:max_authors]:
                if isinstance(a, dict):
                    name = f"{a.get('lastName', '')} {a.get('initials', '')}".strip()
                    if name:
                        out.append(name)
            return out
    return []


def _extract_mesh_pubmed(xml_obj) -> list[str]:
    """Parse MeSH descriptor names from a PubMed XML payload (Element or string)."""
    root = _xml_root(xml_obj)
    if root is None:
        return []
    try:
        descriptors = []
        for mh in root.findall(".//MeshHeading/DescriptorName"):
            txt = mh.text or ""
            if txt.strip():
                descriptors.append(txt.strip())
        return descriptors
    except Exception:
        return []


def _extract_mesh_europepmc(mesh_heading_list) -> list[str]:
    """Parse descriptorName values from EuropePMC meshHeadingList."""
    if not mesh_heading_list or not isinstance(mesh_heading_list, dict):
        return []
    headings = mesh_heading_list.get("meshHeading", [])
    if not isinstance(headings, list):
        return []
    descriptors = []
    for h in headings:
        if isinstance(h, dict):
            dn = h.get("descriptorName")
            if isinstance(dn, dict):
                txt = dn.get("#text") or dn.get("value") or ""
                if txt.strip():
                    descriptors.append(txt.strip())
            elif isinstance(dn, str) and dn.strip():
                descriptors.append(dn.strip())
    return descriptors


# ---------------------------------------------------------------------------
# Query validation
# ---------------------------------------------------------------------------
# PubMed field qualifiers / boolean operators alone are not a search.
_QUERY_STOPWORDS = {"and", "or", "not"}


def validate_query(query: str) -> tuple[str | None, str | None]:
    """Return (clean_query, error). Rejects empty, whitespace-only, and
    operator-only queries — these silently return unrelated records at both
    NCBI and EuropePMC."""
    if query is None:
        return None, "query is required"
    clean = query.strip()
    if not clean:
        return None, "query is empty (whitespace only)"
    stripped = re.sub(r"[()\"\[\]:*]+", " ", clean)
    words = [w for w in stripped.split() if w.strip() and w.lower() not in _QUERY_STOPWORDS]
    if not words:
        return None, f"query '{clean}' contains only operators/qualifiers, no searchable terms"
    if not any(len(w) > 1 for w in words):
        return None, f"query '{clean}' has no term longer than 1 character"
    return clean, None


# ---------------------------------------------------------------------------
# Retraction / correction flags
# ---------------------------------------------------------------------------
_RETRACT_PT = {"retracted publication", "retraction of publication"}
_CONCERN_PT = {"expression of concern"}
_ERRATUM_PT = {"published erratum", "corrected and republished article"}


def _retraction_flags(pub_types: list[str] | None, comments=None) -> dict:
    """Detect retraction / erratum / expression-of-concern status.

    ``comments`` accepts the EuropePMC commentCorrectionList types.
    """
    pts = {str(p).strip().lower() for p in (pub_types or [])}
    types = {str(c).strip().lower() for c in (comments or [])}
    flags: dict = {}
    if pts & _RETRACT_PT or "retraction" in types or "retraction of publication" in types:
        flags["retracted"] = True
    if pts & _CONCERN_PT or "expression of concern" in types:
        flags["expression_of_concern"] = True
    if pts & _ERRATUM_PT or "erratum" in types or "correction" in types:
        flags["corrected"] = True
    return flags


def _epmc_comments(art: dict) -> list[str]:
    """Pull commentCorrection types out of a EuropePMC core record."""
    out: list[str] = []
    lst = (art.get("commentCorrectionList") or {}).get("commentCorrection") or []
    for c in lst if isinstance(lst, list) else []:
        if isinstance(c, dict) and c.get("type"):
            out.append(str(c["type"]))
    return out


# ---------------------------------------------------------------------------
# Structured abstracts (preserve AIMS / METHODS / RESULTS labels)
# ---------------------------------------------------------------------------
def _abstract_with_labels(abstract_el) -> str:
    """Rebuild a PubMed structured abstract with its section labels intact.

    pymed flattens <AbstractText Label="..."> nodes, losing the labels a
    clinician needs to read RESULTS vs CONCLUSION at a glance.
    """
    if abstract_el is None:
        return ""
    parts: list[str] = []
    for node in abstract_el.iter():
        tag = node.tag.split("}")[-1]
        if tag == "AbstractText":
            text = "".join(node.itertext()).strip()
            if not text:
                continue
            label = node.get("Label") or node.get("NlmCategory") or ""
            if label:
                parts.append(f"{label.strip().upper()}: {text}")
            else:
                parts.append(text)
    if parts:
        return " ".join(parts)
    # Not structured — single blob
    return " ".join("".join(abstract_el.itertext()).split())


def _xml_root(xml_obj):
    """Normalise pymed's XML payloads to an ElementTree Element.

    pymed exposes ``article.xml`` as an **Element**, not a string — code that
    guards with ``isinstance(xml, str)`` silently drops every publication type,
    MeSH heading and structured-abstract label (which is why retraction flags
    and study types never appeared).
    """
    if xml_obj is None:
        return None
    if hasattr(xml_obj, "findall"):  # already an Element
        return xml_obj
    if isinstance(xml_obj, (bytes, bytearray)):
        xml_obj = xml_obj.decode("utf-8", "replace")
    if isinstance(xml_obj, str):
        if not xml_obj.strip():
            return None
        try:
            return ET.fromstring(xml_obj)
        except Exception:
            return None
    return None


def _pubmed_abstract_from_xml(xml_obj) -> str:
    """Extract a label-preserving abstract from a PubMed XML payload."""
    root = _xml_root(xml_obj)
    if root is None:
        return ""
    el = root.find(".//Article/Abstract") or root.find(".//Abstract")
    return _abstract_with_labels(el)


def _pubmed_publication_types(xml_obj) -> list[str]:
    """PublicationType values (RCT, Retracted Publication, ...) from PubMed XML."""
    root = _xml_root(xml_obj)
    if root is None:
        return []
    out: list[str] = []
    for pt in root.findall(".//PublicationType"):
        txt = (pt.text or "").strip()
        if txt:
            out.append(txt)
    return out


# ---------------------------------------------------------------------------
# Study-type pushdown (search-time filtering at the API, not post-hoc)
# ---------------------------------------------------------------------------
# tag -> (PubMed publication-type clause value, EuropePMC PUB_TYPE value)
_STUDY_TYPE_PUSHDOWN = {
    "RCT": ("randomized controlled trial", "randomized controlled trial"),
    "Meta-Analysis": ("meta-analysis", "meta-analysis"),
    "Systematic Review": ("systematic review", "systematic review"),
    "Review": ("review", "review"),
    "Observational Study": ("observational study", "observational study"),
    "Case Reports": ("case reports", "case report"),
    "Clinical Trial": ("clinical trial", "clinical trial"),
    "Practice Guideline": ("practice guideline", "practice guideline"),
}


def pushdown_clauses(tags: set[str]) -> tuple[str, str]:
    """Build (pubmed_clause, europepmc_clause) from study-type tags."""
    pm_terms: list[str] = []
    ep_terms: list[str] = []
    for t in sorted(tags):
        pair = _STUDY_TYPE_PUSHDOWN.get(t)
        if pair:
            pm_terms.append(f'"{pair[0]}"[pt]')
            ep_terms.append(f'PUB_TYPE:"{pair[1]}"')
    pm = " AND (" + " OR ".join(pm_terms) + ")" if pm_terms else ""
    ep = " AND (" + " OR ".join(ep_terms) + ")" if ep_terms else ""
    return pm, ep


# ---------------------------------------------------------------------------
# Study-type detection
# ---------------------------------------------------------------------------
_STUDY_TYPE_PATTERNS = [
    (r"\b(randomi[sz]ed\s+(controlled\s+)?(clinical\s+)?trial|RCT)\b", "RCT"),
    (r"\b(meta[- ]?analysis)\b", "Meta-Analysis"),
    (r"\b(systematic\s+review)\b", "Systematic Review"),
    (r"\b(observational\s+study|cohort|case[ -]control|cross[ -]sectional)\b", "Observational Study"),
    (r"\b(case\s+report|case\s+series)\b", "Case Reports"),
    (r"\b(clinical\s+trial|non[ -]randomi[sz]ed)\b", "Clinical Trial"),
    (r"\b(guideline|consensus\s+statement)\b", "Practice Guideline"),
]
_STUDY_TYPE_RE = [(re.compile(p, re.IGNORECASE), tag) for p, tag in _STUDY_TYPE_PATTERNS]

_MESH_TYPE_MAP = {
    "randomized controlled trial": "RCT",
    "meta-analysis": "Meta-Analysis",
    "systematic review": "Systematic Review",
    "review": "Review",
    "observational study": "Observational Study",
    "cohort studies": "Observational Study",
    "case reports": "Case Reports",
    "clinical trial": "Clinical Trial",
    "practice guideline": "Practice Guideline",
}

# All known study-type tags for CLI choices
_STUDY_TYPE_TAGS = sorted(set(
    list(_MESH_TYPE_MAP.values()) + [tag for _, tag in _STUDY_TYPE_PATTERNS]
))


def detect_study_type(title: str, abstract: str, mesh_types: list[str] | None = None) -> str:
    """Return a study-type tag, preferring MeSH when available."""
    if mesh_types:
        for mt in mesh_types:
            tag = _MESH_TYPE_MAP.get(mt.lower())
            if tag:
                return tag
    text = f"{title or ''} {abstract or ''}"
    for pattern, tag in _STUDY_TYPE_RE:
        if pattern.search(text):
            return tag
    return "Unknown"


# ---------------------------------------------------------------------------
# Boilerplate stripping
# ---------------------------------------------------------------------------
_BOILERPLATE_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"^(conflict[s]?\s*of\s*interest|competing\s*interest|disclosure|funding|acknowledg?ments?):?\s",
        r"^(this\s+(work|study|research)\s+was\s+(supported|funded))",
        r"\b(all\s+rights?\s+reserved)\b",
        r"^(copyright|©)\s",
        r"^(supplementary|additional)\s+(material|data|information)",
        r"^(published\s+by|correspondence\s+to)",
        r"^(to\s+whom\s+correspondence)",
        r"^(received:|accepted:|published\s+online)",
        r"^(clinical\s+trial\s+registration)",
    ]
]


def _is_boilerplate(sentence: str) -> bool:
    for pat in _BOILERPLATE_PATTERNS:
        if pat.search(sentence):
            return True
    return False


# ---------------------------------------------------------------------------
# Smart truncation — keyword-density scoring with position bonus
# ---------------------------------------------------------------------------
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


def _tokenize(text: str) -> set[str]:
    return set(re.findall(r"[a-z]{3,}", text.lower()))


def smart_truncate(text: str, limit: int, keywords: set[str] | None = None,
                   section_bonus: int = 0) -> tuple[str, bool, int]:
    """Keep highest-scoring sentences that fit within *limit* characters.

    Scoring = count of keyword hits per sentence + *section_bonus*.
    Boilerplate sentences get a −10 penalty.
    """
    if len(text) <= limit:
        return text, False, len(text)

    if not keywords:
        keywords = _tokenize(text)

    sentences = _SENTENCE_RE.split(text)
    if not sentences:
        return text[:limit], True, limit

    scored: list[tuple[float, str]] = []
    for s in sentences:
        s = s.strip()
        if not s:
            continue
        score = sum(1 for kw in keywords if kw in s.lower()) + section_bonus
        if _is_boilerplate(s):
            score -= 10
        scored.append((score, s))

    scored.sort(key=lambda x: x[0], reverse=True)

    kept: list[str] = []
    total = 0
    truncated = False
    for _, s in scored:
        if total + len(s) + 1 <= limit:
            kept.append(s)
            total += len(s) + 1
        else:
            truncated = True
            remaining = limit - total - 4
            if remaining > 40:
                cut = s[:remaining]
                # Word-boundary cut: back off to the last space.
                sp = cut.rfind(" ")
                if sp > remaining * 0.5:
                    cut = cut[:sp]
                kept.append(cut + "...")
            break

    result = " ".join(kept)
    return result, truncated, len(result)


# ---------------------------------------------------------------------------
# Cache layer (SQLite + FTS5)
# ---------------------------------------------------------------------------
def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db() -> None:
    conn = _connect()
    conn.execute(
        """CREATE TABLE IF NOT EXISTS lit_cache (
            pmid TEXT PRIMARY KEY,
            source TEXT,
            payload TEXT,
            updated TEXT,
            ttl_days INTEGER DEFAULT 30
        )"""
    )
    conn.execute(
        """CREATE VIRTUAL TABLE IF NOT EXISTS lit_fts USING fts5(
            pmid, title, abstract, fulltext,
            tokenize='porter'
        )"""
    )
    conn.commit()
    conn.close()


def get_cached_data(pmid: str) -> dict | None:
    conn = _connect()
    cur = conn.execute(
        "SELECT source, payload, updated, ttl_days FROM lit_cache WHERE pmid=?",
        (pmid,),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    try:
        payload = json.loads(row[1])
    except (json.JSONDecodeError, TypeError):
        return None
    try:
        updated = datetime.fromisoformat(row[2])
    except (ValueError, TypeError):
        return None
    ttl_days = row[3] if row[3] is not None else DEFAULT_TTL_DAYS
    age = (datetime.now(timezone.utc).replace(tzinfo=None) - updated.replace(tzinfo=None)).days
    return {
        "source": row[0],
        "data": payload,
        "stale": age > ttl_days,
    }


def save_to_cache(pmid: str, source: str, data: dict, ttl_days: int = DEFAULT_TTL_DAYS) -> None:
    conn = _connect()
    now = datetime.now(timezone.utc).isoformat()
    payload = json.dumps(data, ensure_ascii=False)
    conn.execute(
        "INSERT OR REPLACE INTO lit_cache (pmid, source, payload, updated, ttl_days) VALUES (?,?,?,?,?)",
        (pmid, source, payload, now, ttl_days),
    )
    title = data.get("title", "")
    abstract = data.get("abstract", "")
    fulltext = ""
    secs = data.get("sections")
    if isinstance(secs, dict):
        fulltext = " ".join(v for v in secs.values() if isinstance(v, str))

    conn.execute("DELETE FROM lit_fts WHERE pmid=?", (pmid,))
    conn.execute(
        "INSERT INTO lit_fts (pmid, title, abstract, fulltext) VALUES (?,?,?,?)",
        (pmid, title, abstract, fulltext),
    )
    conn.commit()
    conn.close()


def search_cache(query: str, limit: int = 20) -> list[dict]:
    """FTS across cached records.

    The raw query is sanitised into quoted terms first: passing user text such
    as 'AND' or 'x OR y' straight into MATCH raises a sqlite3 OperationalError
    (syntax error) and aborts the command.
    """
    terms = [t for t in re.findall(r"[A-Za-z0-9]{2,}", query or "")
             if t.lower() not in _QUERY_STOPWORDS]
    if not terms:
        return []
    match_expr = " AND ".join(f'"{t}"' for t in terms)
    conn = _connect()
    try:
        rows = conn.execute(
            """SELECT pmid, snippet(lit_fts, 1, '<b>', '</b>', '…', 40) AS title_snip,
                      snippet(lit_fts, 2, '<b>', '</b>', '…', 40) AS abs_snip
               FROM lit_fts WHERE lit_fts MATCH ? LIMIT ?""",
            (match_expr, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        conn.close()
        return []
    conn.close()
    return [
        {"pmid": r[0], "title_snippet": r[1], "abstract_snippet": r[2]} for r in rows
    ]


def cache_stats() -> dict:
    conn = _connect()
    total = conn.execute("SELECT COUNT(*) FROM lit_cache").fetchone()[0]
    stale = conn.execute(
        "SELECT COUNT(*) FROM lit_cache WHERE datetime(updated, '+' || ttl_days || ' days') < datetime('now')"
    ).fetchone()[0]
    fts_count = conn.execute("SELECT COUNT(*) FROM lit_fts").fetchone()[0]
    conn.close()
    return {"total_records": total, "stale_records": stale, "fts_indexed": fts_count}


# ---------------------------------------------------------------------------
# Resilience — dynamic timeouts + exponential backoff
# ---------------------------------------------------------------------------
def _dynamic_timeout(endpoint: str, floor: float = 4.0, ceil: float = 30.0) -> float:
    samples = _latency.get(endpoint, deque(maxlen=10))
    if not samples:
        return floor
    avg = sum(samples) / len(samples)
    return max(floor, min(avg * 2.5, ceil))


def _record_latency(endpoint: str, elapsed: float) -> None:
    if endpoint in _latency:
        _latency[endpoint].append(elapsed)


def http_request(
    url: str,
    endpoint: str,
    headers: dict | None = None,
    timeout: float | None = None,
    attempts: int = MAX_BACKOFF_ATTEMPTS,
) -> bytes | None:
    if headers is None:
        headers = {"User-Agent": "Mozilla/5.0"}
    if timeout is None:
        timeout = _dynamic_timeout(endpoint)

    last_status: int | None = None
    for attempt in range(attempts):
        try:
            t0 = time.monotonic()
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                _record_latency(endpoint, time.monotonic() - t0)
                return body
        except urllib.error.HTTPError as e:
            last_status = e.code
            if e.code == 429:
                retry_after = e.headers.get("Retry-After")
                wait = int(retry_after) if retry_after and retry_after.isdigit() else 2**attempt
                time.sleep(wait + random.uniform(0, wait * 0.5))
            elif e.code >= 500:
                if attempt < attempts - 1:
                    time.sleep(2**attempt + random.uniform(0, 2**attempt))
            else:
                return None
        except Exception:
            if attempt < attempts - 1:
                time.sleep(2**attempt + random.uniform(0, 2**attempt))
    return None


# ---------------------------------------------------------------------------
# PubMed helpers
# ---------------------------------------------------------------------------
def _pubmed_client() -> pymed.PubMed:
    pubmed = pymed.PubMed(tool="AgentCLI", email=AGENT_EMAIL)
    api_key = os.getenv("NCBI_API_KEY")
    if api_key:
        pubmed.parameters["api_key"] = api_key
    return pubmed


def search_pubmed(query: str, max_results: int,
                  from_date: str | None = None,
                  to_date: str | None = None,
                  extra_clause: str = "") -> list[dict]:
    """Search PubMed via pymed.  When *query* contains PubMed field qualifiers
    or boolean operators it is passed verbatim; otherwise each keyword is
    wrapped with ``[TIAB]``."""
    pubmed = _pubmed_client()

    # Date filtering via E-utilities mindate/maxdate
    if from_date:
        pubmed.parameters["mindate"] = from_date.replace("-", "/")
    if to_date:
        pubmed.parameters["maxdate"] = to_date.replace("-", "/")
    if from_date or to_date:
        pubmed.parameters["datetype"] = "pdat"

    # Query construction: passthrough if PubMed syntax detected
    if is_pubmed_syntax_query(query):
        optimized = query
    else:
        optimized = " AND ".join(f'"{kw}"[TIAB]' for kw in query.split())
    if extra_clause:
        optimized = f"({optimized}){extra_clause}"

    try:
        results = pubmed.query(optimized, max_results=max_results)
    except Exception:
        return []

    articles: list[dict] = []
    for art in results:
        pmid = art.pubmed_id.split("\n")[0] if art.pubmed_id else None
        if not pmid:
            continue

        title = getattr(art, "title", "Untitled")
        abstract = getattr(art, "abstract", "") or ""

        # Publication types + MeSH from the article XML (pymed returns an
        # Element here, not a string).
        art_xml = getattr(art, "xml", None)
        mesh_types = _pubmed_publication_types(art_xml)

        # Prefer the label-preserving structured abstract over pymed's
        # flattened string (AIMS/METHODS/RESULTS/CONCLUSION labels).
        labeled = _pubmed_abstract_from_xml(art_xml)
        if labeled:
            abstract = labeled

        record = {
            "pmid": pmid,
            "title": title,
            "date": str(art.publication_date) if art.publication_date else None,
            "source": "pubmed",
            "abstract": abstract,
            "journal": getattr(art, "journal", None),
            "authors": _extract_authors_pubmed(getattr(art, "authors", None)),
            "mesh_keywords": _extract_mesh_pubmed(art_xml),
            "cited_by": None,  # PubMed doesn't provide citation counts
            "in_pmc": False,
            "in_epmc": False,
            "is_open_access": False,
            "study_type": detect_study_type(title, abstract, mesh_types),
            "pub_types": mesh_types,
            "doi": getattr(art, "doi", None),
        }
        record.update(_retraction_flags(mesh_types))
        articles.append(record)

    # Clean up date params so they don't leak to subsequent calls
    pubmed.parameters.pop("mindate", None)
    pubmed.parameters.pop("maxdate", None)
    pubmed.parameters.pop("datetype", None)

    return articles


def _pubdate_fallback(pmid: str) -> str | None:
    """Resolve a publication date when pymed returns none (ahead-of-print,
    electronic-only records). Falls back to ESummary pubdate/epubdate."""
    url = (
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
        f"?db=pubmed&id={urllib.parse.quote(pmid)}&retmode=json"
    )
    body = http_request(url, "ncbi")
    if not body:
        return None
    try:
        rec = json.loads(body.decode("utf-8"))["result"].get(pmid) or {}
    except Exception:
        return None
    for key in ("pubdate", "epubdate", "sortpubdate"):
        val = (rec.get(key) or "").strip()
        if not val:
            continue
        m = re.match(r"(\d{4})(?:\s+([A-Za-z]{3}))?(?:\s+(\d{1,2}))?", val)
        if not m:
            continue
        year, mon, day = m.group(1), m.group(2), m.group(3)
        if not mon:
            return f"{year}-01-01"
        months = {m2: i for i, m2 in enumerate(
            ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
             "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}
        mm = months.get(mon.capitalize())
        if not mm:
            return f"{year}-01-01"
        return f"{year}-{mm:02d}-{int(day) if day else 1:02d}"
    return None


def pmid_metadata(pmid: str) -> dict | None:
    """Resolve title, abstract, DOI, MeSH publication types, and enrich with
    journal / authors / MeSH keywords."""
    pubmed = _pubmed_client()
    try:
        results = list(pubmed.query(f"{pmid}[pmid]", max_results=1))
    except Exception:
        return None
    if not results:
        return None
    art = results[0]
    doi = getattr(art, "doi", None)
    title = getattr(art, "title", "Untitled")
    abstract = getattr(art, "abstract", "No abstract available")
    pub_date = getattr(art, "publication_date", None)
    date_str = str(pub_date) if pub_date else None

    mesh_types: list[str] = []
    art_xml = getattr(art, "xml", None)
    mesh_types = _pubmed_publication_types(art_xml)

    # Label-preserving structured abstract beats pymed's flattened string.
    labeled = _pubmed_abstract_from_xml(art_xml)
    if labeled:
        abstract = labeled

    # A missing date silently propagates into batch results (date: null).
    if not date_str:
        date_str = _pubdate_fallback(pmid)

    study_type = detect_study_type(title, abstract, mesh_types)
    is_commentary = bool(set(mesh_types or []) & {"Editorial", "Comment", "Letter", "Published Erratum"})
    meta = {
        "doi": doi,
        "title": title,
        "abstract": abstract,
        "date": date_str,
        "study_type": study_type,
        "commentary": is_commentary,
        "journal": getattr(art, "journal", None),
        "authors": _extract_authors_pubmed(getattr(art, "authors", None)),
        "mesh_keywords": _extract_mesh_pubmed(art_xml),
        "pub_types": mesh_types,
        "open_access": getattr(art, "is_open_access", None),
    }
    meta.update(_retraction_flags(mesh_types))
    return meta


def parse_pmc_sections(xml_bytes: bytes) -> dict[str, str] | None:
    """Extract intro / methods / results / discussion sections from PMC OpenAccess XML."""
    sections: dict[str, list[str]] = {
        "intro": [],
        "methods": [],
        "results": [],
        "discussion": [],
        "other": [],
    }
    try:
        root = ET.fromstring(xml_bytes)
        for sec in root.findall(".//body//sec"):
            title_node = sec.find("title")
            title_text = "".join(title_node.itertext()).lower() if title_node is not None else ""
            sec_text = "".join(sec.itertext()).strip()
            if title_node is not None:
                sec_text = sec_text.replace("".join(title_node.itertext()), "", 1).strip()

            if any(k in title_text for k in ["intro", "background"]):
                sections["intro"].append(sec_text)
            elif any(k in title_text for k in ["method", "patient", "material"]):
                sections["methods"].append(sec_text)
            elif any(k in title_text for k in ["result", "find"]):
                sections["results"].append(sec_text)
            elif any(k in title_text for k in ["discuss", "conclus", "limit"]):
                sections["discussion"].append(sec_text)
            else:
                if sec_text:
                    sections["other"].append(sec_text)
        return {k: "\n\n".join(v).strip() for k, v in sections.items() if v}
    except Exception:
        return None


def fetch_pubmed_fulltext(pmid: str) -> dict | None:
    """Attempt to fetch structured full-text from PubMed Central Open Access."""
    conv_url = (
        f"https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/"
        f"?tool=AgentCLI&email={AGENT_EMAIL}&ids={pmid}&format=json"
    )
    body = http_request(conv_url, "ncbi")
    if not body:
        return None
    try:
        res = json.loads(body.decode("utf-8"))
        records = res.get("records", [])
        if not records or "pmcid" not in records[0]:
            return None
        pmcid = records[0]["pmcid"]
    except (json.JSONDecodeError, UnicodeDecodeError, KeyError):
        return None

    fetch_url = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pmc&id={pmcid}&retmode=xml"
    xml_body = http_request(fetch_url, "ncbi")
    if not xml_body:
        return None
    secs = parse_pmc_sections(xml_body)
    return {"sections": secs} if secs else None


# ---------------------------------------------------------------------------
# EuropePMC helpers
# ---------------------------------------------------------------------------
def _parse_europepmc_fulltext_xml(xml_bytes: bytes) -> dict[str, str] | None:
    """Extract intro / methods / results / discussion sections from EuropePMC
    JATS-format full-text XML.  Mirrors ``parse_pmc_sections`` logic."""
    sections: dict[str, list[str]] = {
        "intro": [],
        "methods": [],
        "results": [],
        "discussion": [],
        "other": [],
    }
    try:
        root = ET.fromstring(xml_bytes)
        # Handle namespaced JATS: try with and without ns prefix
        ns = ""
        tag = root.tag
        if "}" in tag:
            ns = tag.split("}")[0] + "}"
        body = root.find(f".//{ns}body")
        if body is None:
            # fallback: try unprefixed
            body = root.find(".//body")
        if body is None:
            return None

        for sec in body.findall(f".//{ns}sec"):
            title_node = sec.find(f"{ns}title")
            title_text = "".join(title_node.itertext()).lower() if title_node is not None else ""
            sec_text = "".join(sec.itertext()).strip()
            if title_node is not None:
                sec_text = sec_text.replace("".join(title_node.itertext()), "", 1).strip()

            if any(k in title_text for k in ["intro", "background"]):
                sections["intro"].append(sec_text)
            elif any(k in title_text for k in ["method", "patient", "material"]):
                sections["methods"].append(sec_text)
            elif any(k in title_text for k in ["result", "find"]):
                sections["results"].append(sec_text)
            elif any(k in title_text for k in ["discuss", "conclus", "limit"]):
                sections["discussion"].append(sec_text)
            else:
                if sec_text:
                    sections["other"].append(sec_text)
        return {k: "\n\n".join(v).strip() for k, v in sections.items() if v}
    except Exception:
        return None


def search_europepmc(query: str, max_results: int,
                     from_date: str | None = None,
                     to_date: str | None = None,
                     sort: str = "relevance",
                     extra_clause: str = "",
                     min_citations: int = 0) -> list[dict]:
    """Search EuropePMC REST API with optional date filtering, sort control,
    citation floor, study-type clause, and ``resultType=core`` for richer
    metadata."""

    # PubMed-syntax qualifiers must be translated before they reach EPMC.
    query = europepmc_query(query)

    # Append date range filter to query
    if from_date and to_date:
        query += f" AND FIRST_PDATE:[{from_date} TO {to_date}]"
    elif from_date:
        query += f" AND FIRST_PDATE:[{from_date} TO 3000-01-01]"
    elif to_date:
        query += f" AND FIRST_PDATE:[0000-01-01 TO {to_date}]"

    if extra_clause:
        query += extra_clause

    # Citation floor pushed down to the API — CITED:N is an exact match,
    # the range form is what we want for "at least N citations".
    if min_citations > 0:
        query += f" AND CITED:[{min_citations} TO 3000000]"

    encoded = urllib.parse.quote(query)

    # Sort parameter
    sort_param = ""
    if sort == "date":
        sort_param = "&sort=FIRST_PDATE%20desc"
    elif sort == "citations":
        sort_param = "&sort=CITED%20desc"

    url = (
        f"https://www.ebi.ac.uk/europepmc/webservices/rest/search"
        f"?query={encoded}&format=json&resultType=core&pageSize={max_results}{sort_param}"
    )
    body = http_request(url, "europepmc")
    if not body:
        return []
    try:
        data = json.loads(body.decode("utf-8"))
        results = data.get("resultList", {}).get("result", [])
    except (json.JSONDecodeError, UnicodeDecodeError):
        return []

    articles: list[dict] = []
    for art in results:
        pmid = art.get("pmid")
        if not pmid:
            continue

        title = art.get("title", "Untitled")
        abstract = art.get("abstractText", "") or ""
        pub_types = [
            pt for pt in (art.get("pubTypeList", {}) or {}).get("pubType", []) if pt
        ]
        journal_info = (art.get("journalInfo", {}) or {})
        journal = None
        if isinstance(journal_info, dict):
            j = journal_info.get("journal", {})
            if isinstance(j, dict):
                journal = j.get("title")

        articles.append({
            "pmid": str(pmid),
            "title": title,
            "date": art.get("firstPublicationDate"),
            "source": "europepmc",
            "abstract": abstract,
            "journal": journal,
            "cited_by": art.get("citedByCount", 0),
            "authors": _extract_authors_europepmc(art),
            "mesh_keywords": _extract_mesh_europepmc(art.get("meshHeadingList")),
            "pub_types": pub_types,
            "in_epmc": art.get("inEPMC") == "Y",
            "in_pmc": art.get("inPMC") == "Y",
            "is_open_access": art.get("isOpenAccess") == "Y",
            "study_type": detect_study_type(title, abstract, pub_types),
            "doi": art.get("doi"),
            **_retraction_flags(pub_types, _epmc_comments(art)),
        })
    return articles


def fetch_europepmc_fulltext(pmid: str) -> dict | None:
    """Fetch full-text from EuropePMC.  First tries JATS XML via the
    /fullTextXML endpoint using PMCID resolution, then falls back to
    structured abstract parsing."""
    # Step 1: resolve PMCID
    url = (
        f"https://www.ebi.ac.uk/europepmc/webservices/rest/search"
        f"?query=ext_id:{pmid}%20src:med&format=json&resultType=core"
    )
    body = http_request(url, "europepmc")
    if not body:
        return None
    try:
        data = json.loads(body.decode("utf-8"))
        results = data.get("resultList", {}).get("result", [])
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not results:
        return None

    paper = results[0]
    pmcid = paper.get("pmcid")

    # Step 2: try to fetch JATS full-text XML via PMCID
    if pmcid:
        ft_url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"
        ft_body = http_request(ft_url, "europepmc")
        if ft_body:
            secs = _parse_europepmc_fulltext_xml(ft_body)
            if secs:
                body_len = sum(len(v) for v in secs.values() if isinstance(v, str))
                if body_len >= 200:
                    return {"sections": secs}
    # No JATS full text — return None so the caller falls through to the
    # abstract with text_from=abstract (never claim a *_ft source).
    return None


def fetch_unpaywall(doi: str) -> dict | None:
    encoded = urllib.parse.quote(doi)
    url = f"https://api.unpaywall.org/v2/{encoded}?email={AGENT_EMAIL}"
    body = http_request(url, "unpaywall")
    if not body:
        return None
    try:
        data = json.loads(body.decode("utf-8"))
        if data.get("is_oa"):
            best = data.get("best_oa_location", {})
            pdf_url = best.get("url_for_pdf")
            if pdf_url:
                return {"pdf": pdf_url}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return None


# ---------------------------------------------------------------------------
# Search result enrichment
# ---------------------------------------------------------------------------
def _enrich_search_result(record: dict) -> dict:
    """Ensure all expected fields exist with sensible defaults."""
    defaults = {
        "abstract": "",
        "journal": None,
        "cited_by": None,
        "authors": [],
        "mesh_keywords": [],
        "study_type": "Unknown",
        "pub_types": [],
        "in_epmc": False,
        "in_pmc": False,
        "is_open_access": False,
        "doi": None,
        "retracted": False,
        "expression_of_concern": False,
        "corrected": False,
        "cited_by_source": None,
    }
    for k, v in defaults.items():
        if k not in record:
            record[k] = v
    return record


# ---------------------------------------------------------------------------
# Background cache refresh
# ---------------------------------------------------------------------------
def _refresh_cache(pmid: str, ttl: int) -> None:
    """Fetch fresh metadata + fulltext and overwrite cache entry."""
    meta = pmid_metadata(pmid)
    if not meta:
        return
    doi = meta["doi"]
    out = {
        "pmid": pmid,
        "doi": doi,
        "title": meta["title"],
        "abstract": meta["abstract"],
        "study_type": meta.get("study_type", "Unknown"),
        "sections": None,
        "open_access": None,
        "proxy_url": None,
        "journal": meta.get("journal"),
        "authors": meta.get("authors", []),
        "mesh_keywords": meta.get("mesh_keywords", []),
    }
    source = "fallback"

    tasks = {
        "pubmed": lambda: fetch_pubmed_fulltext(pmid),
        "europepmc": lambda: fetch_europepmc_fulltext(pmid),
    }
    with ThreadPoolExecutor(max_workers=2) as ex:
        futures = {ex.submit(func): name for name, func in tasks.items()}
        for f in as_completed(futures):
            name = futures[f]
            try:
                res = f.result()
                if res and res.get("sections"):
                    out["sections"] = res["sections"]
                    source = f"{name}_ft"
                    break
            except Exception:
                continue

    if source == "fallback" and doi:
        oa = fetch_unpaywall(doi)
        if oa:
            out["open_access"] = oa
            source = "unpaywall"

    proxy_prefix = os.getenv("INSTITUTIONAL_PROXY_PREFIX")
    if doi and proxy_prefix:
        out["proxy_url"] = f"{proxy_prefix}{urllib.parse.quote(f'https://doi.org/{doi}')}"

    save_to_cache(pmid, source, out, ttl)


# ---------------------------------------------------------------------------
# Core fetch logic (extracted for batch reuse)
# ---------------------------------------------------------------------------
def _fetch_one_pmid(pmid: str, section: str, limit: int, ttl: int) -> dict:
    """Fetch a single PMID, returning a JSON-serializable dict."""
    cached = get_cached_data(pmid) if ttl > 0 else None

    if cached and not cached["stale"]:
        out = cached["data"]
        source = cached["source"]
        out["cached"] = True
        # Legacy cache entries (pre-date-field fix) lack date/commentary —
        # patch them once from metadata and re-save.
        if out.get("date") is None:
            meta = pmid_metadata(pmid)
            if meta:
                out["date"] = meta.get("date")
                out.setdefault("commentary", meta.get("commentary", False))
                save_to_cache(pmid, source, out, ttl)
    elif cached and cached["stale"]:
        out = cached["data"]
        source = cached["source"]
        out["cached_stale"] = True
        _bg_executor.submit(_refresh_cache, pmid, ttl)
    else:
        meta = pmid_metadata(pmid)
        if not meta:
            return {"error": "Metadata resolution failed.", "pmid": pmid}

        doi = meta["doi"]
        out = {
            "pmid": pmid,
            "doi": doi,
            "title": meta["title"],
            "abstract": meta["abstract"],
            "date": meta.get("date"),
            "study_type": meta.get("study_type", "Unknown"),
            "commentary": meta.get("commentary", False),
            "sections": None,
            "open_access": None,
            "proxy_url": None,
            "journal": meta.get("journal"),
            "authors": meta.get("authors", []),
            "mesh_keywords": meta.get("mesh_keywords", []),
            "pub_types": meta.get("pub_types", []),
        }
        for flag in ("retracted", "expression_of_concern", "corrected"):
            if meta.get(flag):
                out[flag] = True
        source = "fallback"

        tasks = {
            "pubmed": lambda: fetch_pubmed_fulltext(pmid),
            "europepmc": lambda: fetch_europepmc_fulltext(pmid),
        }
        with ThreadPoolExecutor(max_workers=2) as ex:
            futures = {ex.submit(func): name for name, func in tasks.items()}
            for f in as_completed(futures):
                name = futures[f]
                try:
                    res = f.result()
                    # A section index that exists but holds no usable text must
                    # not be reported as a full-text hit (that produced
                    # source=europepmc_ft with an empty body).
                    secs = (res or {}).get("sections") or {}
                    body_len = sum(len(v) for v in secs.values() if isinstance(v, str))
                    if body_len >= 200:
                        out["sections"] = secs
                        source = f"{name}_ft"
                        break
                except Exception:
                    continue

        if source == "fallback" and doi:
            oa = fetch_unpaywall(doi)
            if oa:
                out["open_access"] = oa
                source = "unpaywall"

        proxy_prefix = os.getenv("INSTITUTIONAL_PROXY_PREFIX")
        if doi and proxy_prefix:
            out["proxy_url"] = f"{proxy_prefix}{urllib.parse.quote(f'https://doi.org/{doi}')}"

        save_to_cache(pmid, source, out, ttl)

    # Build response
    abstract_text = out.get("abstract") or ""
    resp: dict = {
        "pmid": pmid,
        "title": out["title"],
        "date": out.get("date"),
        "source": source,
        "section": section,
        "study_type": out.get("study_type", "Unknown"),
    }
    if out.get("retracted"):
        resp["warning"] = "RETRACTED"
    elif out.get("expression_of_concern"):
        resp["warning"] = "EXPRESSION_OF_CONCERN"
    elif out.get("corrected"):
        resp["warning"] = "CORRECTED"
    if out.get("commentary"):
        resp["note"] = "Editorial/letter/commentary — no abstract available"
    for optional in ("doi", "journal", "authors", "mesh_keywords", "proxy_url",
                     "pub_types", "cached", "cached_stale"):
        if out.get(optional):
            resp[optional] = out[optional]

    # Select and truncate text
    raw: str = ""
    section_bonus = 0
    used_abstract = False
    if section == "abstract":
        raw = abstract_text
    elif section == "all":
        secs = out.get("sections")
        if secs:
            # Per-section truncation with position bonus
            alloc = {"discussion": 0.35, "results": 0.30, "methods": 0.20,
                     "intro": 0.10, "other": 0.05}
            bonus = {"discussion": 2, "results": 1, "methods": 0, "intro": 0, "other": 0}
            parts: list[str] = []
            keywords = _tokenize(f"{out.get('title', '')} {abstract_text}")
            # Process in order: discussion first (gets any overflow)
            for sec_name in ("discussion", "results", "methods", "intro", "other"):
                sec_text = secs.get(sec_name, "")
                if not sec_text or not str(sec_text).strip():
                    continue
                alloc_limit = max(200, int(limit * alloc.get(sec_name, 0.1)))
                trimmed, _, _ = smart_truncate(sec_text, alloc_limit, keywords,
                                               section_bonus=bonus.get(sec_name, 0))
                if trimmed and trimmed.strip():
                    parts.append(trimmed)
            raw = "\n\n".join(parts)
        # Never return an empty body when an abstract exists — an empty
        # response reads as "no data" to an agent and silently kills reviews.
        if not raw.strip():
            raw = abstract_text
            used_abstract = True
            if not secs and out.get("open_access"):
                resp["pdf"] = out["open_access"]["pdf"]
    else:
        secs = out.get("sections")
        if secs and secs.get(section) and str(secs.get(section)).strip():
            sec_bonus_map = {"discussion": 2, "results": 1}
            section_bonus = sec_bonus_map.get(section, 0)
            raw = secs[section]
        else:
            # Fall back to the abstract rather than returning nothing.
            raw = abstract_text
            used_abstract = True
            resp["note"] = (
                f"Section '{section}' not available"
                + (" — returning abstract instead" if abstract_text else "")
            )
            if out.get("open_access"):
                resp["pdf"] = out["open_access"]["pdf"]

    # Smart truncation with position bonus
    if raw:
        if len(raw) > limit:
            keywords = _tokenize(f"{out.get('title', '')} {abstract_text}")
            trimmed, was_truncated, kept_bytes = smart_truncate(
                raw, limit, keywords, section_bonus=section_bonus
            )
            resp["text"] = trimmed
            resp["truncated"] = was_truncated
            if was_truncated:
                resp["truncated_bytes"] = kept_bytes
        else:
            resp["text"] = raw
        if used_abstract:
            resp["text_from"] = "abstract"
            resp.setdefault("note", "Abstract only — no open-access full text found")
    elif "note" not in resp:
        resp["note"] = "No text available for this article (no abstract, no OA full text)"

    return resp


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
@click.group()
def cli():
    init_db()


@cli.command("search")
@click.option("--query", "-q", required=True)
@click.option("--max-results", "-m", default=5, type=int)
@click.option("--from-date", "-f", default=None, help="Lower bound YYYY-MM-DD")
@click.option("--to-date", "-t", default=None, help="Upper bound YYYY-MM-DD")
@click.option("--sort", "-S", type=click.Choice(["relevance", "date", "citations"]),
              default="relevance", help="Sort order (EuropePMC)")
@click.option("--min-citations", "-C", type=int, default=0,
              help="Minimum citation count to include a result")
@click.option("--study-type", "-T", type=click.Choice(_STUDY_TYPE_TAGS), default=None,
              help="Filter to a single study type")
@click.option("--study-types", "-U", default=None,
              help="Comma-separated study types (e.g. 'RCT,Meta-Analysis')")
@click.option("--verbose", "-V", is_flag=True, default=False,
              help="Emit pool/filter diagnostics on stderr")
def search_cmd(query: str, max_results: int, from_date: str | None,
               to_date: str | None, sort: str, min_citations: int,
               study_type: str | None, study_types: str | None,
               verbose: bool = False) -> None:
    """Search PubMed + EuropePMC in parallel, deduplicate, enrich, filter."""
    try:
        # --- Input validation (empty/whitespace/operator-only queries silently
        # return unrelated records at both APIs) ---
        clean_query, qerr = validate_query(query)
        if qerr:
            click.echo(json.dumps({"error": qerr, "query": query},
                                  separators=(",", ":")), err=True)
            sys.exit(2)

        # Validate dates (shape + semantic: datetime.strptime rejects month 13/day 99)
        date_re = re.compile(r"^\d{4}-\d{2}-\d{2}$")
        for label, val in [("from_date", from_date), ("to_date", to_date)]:
            if val and not date_re.match(val):
                click.echo(json.dumps(
                    {"error": f"Invalid {label}: '{val}'. Use YYYY-MM-DD."},
                    separators=(",", ":")), err=True)
                sys.exit(2)
            if val:
                try:
                    datetime.strptime(val, "%Y-%m-%d")
                except ValueError:
                    click.echo(json.dumps(
                        {"error": f"Invalid {label}: '{val}' is not a real calendar date. Use YYYY-MM-DD."},
                        separators=(",", ":")), err=True)
                    sys.exit(2)
        if from_date and to_date and from_date > to_date:
            click.echo(json.dumps(
                {"error": f"from_date {from_date} is after to_date {to_date}"},
                separators=(",", ":")), err=True)
            sys.exit(2)

        # Build study-type filter set
        allowed_types: set[str] | None = None
        if study_type or study_types:
            allowed_types = set()
            if study_type:
                allowed_types.add(study_type)
            if study_types:
                allowed_types.update(t.strip() for t in study_types.split(",") if t.strip())

        # Push the study-type filter down into both APIs. Filtering a single
        # small page client-side returned zero results for common queries
        # (e.g. -T RCT) because the page held few RCTs.
        pm_clause, ep_clause = pushdown_clauses(allowed_types or set())

        # Over-fetch: the merge/dedup pass can shrink the set considerably.
        pool = max(max_results * 3, 20)

        with ThreadPoolExecutor(max_workers=2) as ex:
            f_us = ex.submit(search_pubmed, clean_query, pool, from_date, to_date, pm_clause)
            f_eu = ex.submit(search_europepmc, clean_query, pool,
                             from_date, to_date, sort, ep_clause, min_citations)
            res_us = f_us.result() or []
            res_eu = f_eu.result() or []

        # Merge and deduplicate: prefer EuropePMC metadata when richer
        deduped: dict[str, dict] = {}
        for item in res_us + res_eu:
            pid = item.get("pmid")
            if not pid:
                continue
            if pid not in deduped:
                deduped[pid] = _enrich_search_result(item)
            else:
                existing = deduped[pid]
                # Overlay richer fields from the newer item.
                # NOTE: falsy-but-meaningful values (cited_by == 0) must still
                # overlay, otherwise a real citation count of 0 blocks the
                # EuropePMC count from landing on a PubMed-first record.
                for key in ("abstract", "journal", "cited_by", "authors",
                            "mesh_keywords", "pub_types", "in_epmc", "in_pmc",
                            "is_open_access", "doi"):
                    new_val = item.get(key)
                    if new_val is None or new_val == [] or new_val == "":
                        continue
                    if existing.get(key) is None or existing.get(key) == [] or existing.get(key) == "":
                        existing[key] = new_val
                        if key == "cited_by":
                            existing["cited_by_source"] = item.get("source")
                # Upgrade study_type if currently Unknown
                if existing.get("study_type") == "Unknown" and item.get("study_type") != "Unknown":
                    existing["study_type"] = item["study_type"]
                # Merge retraction flags (any source flags it → flag it)
                for flag in ("retracted", "expression_of_concern", "corrected"):
                    if item.get(flag):
                        existing[flag] = True
                # Merge sources
                item_src = item.get("source", "")
                exist_src = existing.get("source", "")
                if item_src and exist_src and item_src != exist_src:
                    existing["source"] = "both"

        # Interleaved merge: "both" first, then zigzag PubMed/EuropePMC
        both = [r for r in deduped.values() if r.get("source") == "both"]
        pm_only = [r for r in deduped.values() if r.get("source") == "pubmed"]
        ep_only = [r for r in deduped.values() if r.get("source") == "europepmc"]
        interleaved: list[dict] = both[:]
        for pm, ep in zip(pm_only, ep_only):
            interleaved.append(pm)
            interleaved.append(ep)
        interleaved += pm_only[len(ep_only):]
        interleaved += ep_only[len(pm_only):]
        final = interleaved

        pre_filter = len(final)
        if allowed_types:
            final = [r for r in final if r.get("study_type", "Unknown") in allowed_types]
        if min_citations > 0:
            # Records with no citation data at all are reported separately
            # rather than being silently passed by the filter.
            kept, unknown = [], 0
            for r in final:
                cb = r.get("cited_by")
                if cb is None:
                    unknown += 1
                    continue
                if cb >= min_citations:
                    kept.append(r)
            final = kept
        else:
            unknown = 0

        final = final[:max_results]

        # Citation sort is otherwise only applied on the EuropePMC leg
        if sort == "citations":
            final.sort(key=lambda r: (r.get("cited_by") is None, -(r.get("cited_by") or 0)))

        # Flag retracted records prominently — a retracted paper must never be
        # quoted as evidence without the reader noticing.
        for record in final:
            flags = [f for f in ("retracted", "expression_of_concern", "corrected")
                     if record.get(f)]
            if flags:
                record["warning"] = "RETRACTED" if record.get("retracted") else flags[0].upper()

        click.echo(json.dumps(final, ensure_ascii=False, separators=(",", ":")))
        if verbose:
            n_retracted = sum(1 for r in final if r.get("retracted"))
            click.echo(json.dumps({
                "pool": {"pubmed": len(res_us), "europepmc": len(res_eu), "deduped": pre_filter},
                "kept": len(final),
                "dropped_no_citation_data": unknown,
                "retracted_in_results": n_retracted,
            }, separators=(",", ":")), err=True)
    except Exception as exc:
        click.echo(json.dumps({"error": str(exc)}, separators=(",", ":")), err=True)
        sys.exit(1)


@cli.command("fetch")
@click.option("--pmid", "-p", required=True)
@click.option(
    "--section", "-s",
    type=click.Choice(["all", "abstract", "intro", "methods", "results", "discussion"]),
    default="all",
)
@click.option("--limit", "-l", default=6000, type=int, help="Character limit for returned text")
@click.option("--ttl", default=DEFAULT_TTL_DAYS, type=int, help="Cache TTL in days")
def fetch_cmd(pmid: str, section: str, limit: int, ttl: int) -> None:
    """Fetch one or more papers by PMID (comma-separated for batch)."""
    try:
        if limit <= 0:
            click.echo(json.dumps(
                {"error": f"Invalid --limit {limit}: must be a positive character budget."},
                separators=(",", ":")), err=True)
            sys.exit(2)
        pmids = [p.strip() for p in pmid.split(",") if p.strip()]
        if not pmids:
            click.echo(json.dumps({"error": "No valid PMIDs provided."},
                                  separators=(",", ":")), err=True)
            sys.exit(2)
        bad = [p for p in pmids if not re.fullmatch(r"\d{1,9}", p)]
        if bad:
            click.echo(json.dumps(
                {"error": f"Invalid PMID(s): {', '.join(bad)} — expected digits only."},
                separators=(",", ":")), err=True)
            sys.exit(2)
        if len(pmids) > 50:
            click.echo(json.dumps(
                {"error": f"{len(pmids)} PMIDs requested — limit is 50 per call."},
                separators=(",", ":")), err=True)
            sys.exit(2)

        if len(pmids) == 1:
            # Single PMID — backward-compatible object response
            result = _fetch_one_pmid(pmids[0], section, limit, ttl)
            click.echo(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        else:
            # Batch — array response
            results: list[dict] = []
            with ThreadPoolExecutor(max_workers=min(4, len(pmids))) as ex:
                futures = {
                    ex.submit(_fetch_one_pmid, pid, section, limit, ttl): pid
                    for pid in pmids
                }
                # Collect in input order
                pid_to_result: dict[str, dict] = {}
                for f in as_completed(futures):
                    try:
                        pid_to_result[futures[f]] = f.result()
                    except Exception as exc:
                        pid_to_result[futures[f]] = {
                            "error": str(exc), "pmid": futures[f]
                        }
                results = [pid_to_result.get(pid, {"error": "Unknown", "pmid": pid})
                          for pid in pmids]
            click.echo(json.dumps(results, ensure_ascii=False, separators=(",", ":")))
    except Exception as exc:
        click.echo(json.dumps({"error": str(exc)}, separators=(",", ":")), err=True)
        sys.exit(1)


@cli.command("cache-stats")
def cache_stats_cmd() -> None:
    """Show cache statistics."""
    stats = cache_stats()
    click.echo(json.dumps(stats, ensure_ascii=False, separators=(",", ":")))


@cli.command("search-cache")
@click.option("--query", "-q", required=True)
@click.option("--limit", "-l", default=20, type=int)
def search_cache_cmd(query: str, limit: int) -> None:
    """Full-text search across locally cached papers (zero network calls)."""
    results = search_cache(query, limit)
    click.echo(json.dumps(results, ensure_ascii=False, separators=(",", ":")))


# ═══════════════════════════════════════════════════════════════════════════
# Researcher workflow commands
# ═══════════════════════════════════════════════════════════════════════════

def _esummary_batch(pmids: list[str]) -> dict[str, dict]:
    """Fetch ESummary records for a list of PMIDs (one HTTP call)."""
    if not pmids:
        return {}
    url = (
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
        f"?db=pubmed&id={urllib.parse.quote(','.join(pmids))}&retmode=json"
    )
    body = http_request(url, "ncbi")
    if not body:
        return {}
    try:
        return json.loads(body.decode("utf-8")).get("result", {}) or {}
    except Exception:
        return {}


def _esummary_to_record(pmid: str, rec: dict) -> dict:
    """Convert an ESummary record into the CLI's compact search shape."""
    doi = None
    for aid in rec.get("articleids", []) or []:
        if isinstance(aid, dict) and aid.get("idtype") == "doi":
            doi = aid.get("value")
    return {
        "pmid": pmid,
        "title": rec.get("title", "Untitled"),
        "journal": rec.get("fulljournalname") or rec.get("source"),
        "date": _iso_from_pubdate(rec.get("pubdate") or rec.get("epubdate") or ""),
        "authors": [a.get("name") for a in (rec.get("authors") or [])[:3] if isinstance(a, dict)],
        "study_type": detect_study_type(rec.get("title", ""), "", rec.get("pubtype") or []),
        "pub_types": rec.get("pubtype") or [],
        "doi": doi,
        **_retraction_flags(rec.get("pubtype") or []),
    }


def _iso_from_pubdate(pubdate: str) -> str | None:
    m = re.match(r"(\d{4})(?:\s+([A-Za-z]{3}))?(?:\s+(\d{1,2}))?", (pubdate or "").strip())
    if not m:
        return None
    months = {mm: i for i, mm in enumerate(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
         "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}
    mm = months.get((m.group(2) or "").capitalize(), 1)
    return f"{m.group(1)}-{mm:02d}-{int(m.group(3) or 1):02d}"


def _require_pmid(pmid: str) -> str:
    """Validate a PMID and exit with a clear error otherwise."""
    clean = (pmid or "").strip()
    if not re.fullmatch(r"\d{1,9}", clean):
        click.echo(json.dumps(
            {"error": f"Invalid PMID '{pmid}' — expected digits only (e.g. 38261728)."},
            separators=(",", ":")), err=True)
        sys.exit(2)
    return clean


@cli.command("mesh")
@click.argument("term")
@click.option("--limit", "-l", default=5, type=int, help="Max descriptors to return")
@click.option("--exact", is_flag=True, default=False, help="Exact label match only")
@click.option("--counts", is_flag=True, default=False,
              help="Also report PubMed hit counts for each descriptor (strategy validation)")
def mesh_cmd(term: str, limit: int, exact: bool, counts: bool) -> None:
    """Look up MeSH descriptors for a term (build/validate a search strategy)."""
    clean, err = validate_query(term)
    if err or not clean:
        click.echo(json.dumps({"error": err}, separators=(",", ":")), err=True)
        sys.exit(2)
    match = "exact" if exact else "contains"
    url = (
        "https://id.nlm.nih.gov/mesh/lookup/descriptor"
        f"?label={urllib.parse.quote(clean)}&match={match}&limit={limit}"
    )
    body = http_request(url, "ncbi")
    if not body:
        click.echo(json.dumps({"error": "MeSH lookup failed (no response)."},
                              separators=(",", ":")), err=True)
        sys.exit(1)
    try:
        hits = json.loads(body.decode("utf-8"))
    except Exception as exc:
        click.echo(json.dumps({"error": f"MeSH lookup parse failed: {exc}"},
                              separators=(",", ":")), err=True)
        sys.exit(1)

    # Canonical term labels (entry terms) for the query string.
    term_body = http_request(
        "https://id.nlm.nih.gov/mesh/lookup/term"
        f"?label={urllib.parse.quote(clean)}&match=contains&limit=10", "ncbi")
    term_labels: list[str] = []
    if term_body:
        try:
            for t in json.loads(term_body.decode("utf-8")) or []:
                if isinstance(t, dict) and t.get("label"):
                    term_labels.append(t["label"])
        except Exception:
            pass

    out = []
    for h in hits if isinstance(hits, list) else []:
        ui = str(h.get("resource", "")).rsplit("/", 1)[-1]
        entry: dict = {"ui": ui, "label": h.get("label")}
        if term_labels:
            entry["terms"] = term_labels[:6]
        # Detail pass: tree numbers place the descriptor in the MeSH hierarchy,
        # which is what decides "explode" vs "no explode" in a strategy.
        detail = http_request(f"https://id.nlm.nih.gov/mesh/{ui}.json", "ncbi")
        if detail:
            try:
                d = json.loads(detail.decode("utf-8"))
                lbl = d.get("label")
                if isinstance(lbl, dict):
                    entry["label"] = lbl.get("@value") or entry["label"]
                # treeNumber is a bare URI *or* a list of URIs, depending on
                # the descriptor — normalise both (iterating a string yields
                # one-char "tree numbers").
                tn = d.get("treeNumber") or []
                if isinstance(tn, (str, dict)):
                    tn = [tn]
                trees = []
                for t in tn:
                    val = t.get("@id") if isinstance(t, dict) else t
                    if isinstance(val, str) and val:
                        trees.append(val.rsplit("/", 1)[-1])
                if trees:
                    entry["tree"] = trees
                sn = d.get("scopeNote")
                if isinstance(sn, dict):
                    sn = sn.get("@value")
                if isinstance(sn, str) and sn.strip():
                    entry["scope"] = sn[:400]
            except Exception:
                pass
        if counts:
            # Hit counts validate the strategy before it is written down.
            q = urllib.parse.quote(f'"{entry.get("label", clean)}"[MeSH]')
            cbody = http_request(
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
                f"?db=pubmed&term={q}&rettype=count&retmode=json", "ncbi")
            if cbody:
                try:
                    entry["pubmed_hits"] = int(
                        json.loads(cbody.decode("utf-8"))["esearchresult"]["count"])
                except Exception:
                    pass
        out.append(entry)
    click.echo(json.dumps({"term": term, "n": len(out), "r": out},
                          ensure_ascii=False, separators=(",", ":")))


@cli.command("related")
@click.option("--pmid", "-p", required=True)
@click.option("--max-results", "-m", default=10, type=int)
def related_cmd(pmid: str, max_results: int) -> None:
    """Similar articles (PubMed neighbor links) — snowball from a seed paper."""
    pid = _require_pmid(pmid)
    url = (
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/elink.fcgi"
        f"?dbfrom=pubmed&db=pubmed&id={pid}&cmd=neighbor&retmode=json"
    )
    body = http_request(url, "ncbi")
    neighbors: list[str] = []
    if body:
        try:
            data = json.loads(body.decode("utf-8"))
            for ls in data.get("linksets", []):
                for ldb in ls.get("linksetdbs", []):
                    if ldb.get("linkname") == "pubmed_pubmed":
                        neighbors = [str(x) for x in ldb.get("links", []) if str(x) != pid]
        except Exception:
            pass
    neighbors = neighbors[:max_results]
    if not neighbors:
        click.echo(json.dumps({"pmid": pid, "n": 0, "r": [],
                               "note": "No related articles returned."},
                              separators=(",", ":")))
        return
    summ = _esummary_batch(neighbors)
    records = [_esummary_to_record(p, summ.get(p, {})) for p in neighbors if p in summ]
    click.echo(json.dumps({"pmid": pid, "n": len(records), "r": records},
                          ensure_ascii=False, separators=(",", ":")))


@cli.command("citedby")
@click.option("--pmid", "-p", required=True)
@click.option("--max-results", "-m", default=10, type=int)
@click.option("--sort", "-S", type=click.Choice(["date", "citations"]), default="date")
def citedby_cmd(pmid: str, max_results: int, sort: str) -> None:
    """Who cites this paper (forward citation chasing / surveillance)."""
    pid = _require_pmid(pmid)
    url = (
        "https://www.ebi.ac.uk/europepmc/webservices/rest/MED/"
        f"{pid}/citations?format=json&pageSize={max_results}"
    )
    body = http_request(url, "europepmc")
    if not body:
        click.echo(json.dumps({"error": "EuropePMC citations request failed.",
                               "pmid": pid}, separators=(",", ":")), err=True)
        sys.exit(1)
    try:
        data = json.loads(body.decode("utf-8"))
    except Exception as exc:
        click.echo(json.dumps({"error": f"parse failed: {exc}", "pmid": pid},
                              separators=(",", ":")), err=True)
        sys.exit(1)
    cites = (data.get("citationList") or {}).get("citation") or []
    out = []
    for c in cites:
        if not isinstance(c, dict):
            continue
        # Only MED-source citations carry PubMed IDs — EPMC/preprint IDs
        # must not land in the pmid field.
        src = (c.get("source") or "").upper()
        cid = c.get("id")
        out.append({
            "pmid": cid if src == "MED" else None,
            "id": cid,
            "source": c.get("source"),
            "title": c.get("title"),
            "journal": (c.get("journalAbbreviation") or c.get("journalTitle")),
            "year": c.get("pubYear"),
            "authors": (c.get("authorString") or "")[:80],
            "type": c.get("citationType"),
        })
    if sort == "date":
        out.sort(key=lambda r: (r.get("year") or ""), reverse=True)
    elif sort == "citations":
        # citation records carry no citedByCount — keep input order (most
        # relevant first) instead of pretending to sort.
        pass
    click.echo(json.dumps({"pmid": pid, "hit_count": data.get("hitCount"),
                           "n": len(out), "r": out},
                          ensure_ascii=False, separators=(",", ":")))


@cli.command("refs")
@click.option("--pmid", "-p", required=True)
@click.option("--max-results", "-m", default=15, type=int)
def refs_cmd(pmid: str, max_results: int) -> None:
    """Reference list of a paper (backward citation chasing)."""
    pid = _require_pmid(pmid)
    url = (
        "https://www.ebi.ac.uk/europepmc/webservices/rest/MED/"
        f"{pid}/references?format=json&pageSize={max_results}"
    )
    body = http_request(url, "europepmc")
    out: list[dict] = []
    hit_count = None
    degraded = None
    if body:
        try:
            data = json.loads(body.decode("utf-8"))
            if "referenceList" in data:
                for r in (data.get("referenceList") or {}).get("reference") or []:
                    if not isinstance(r, dict):
                        continue
                    out.append({
                        "pmid": r.get("id") if r.get("source") == "MED" else None,
                        "doi": r.get("doi"),
                        "title": r.get("title"),
                        "journal": r.get("journalAbbreviation"),
                        "year": r.get("pubYear"),
                        "authors": (r.get("authorString") or "")[:80],
                    })
                hit_count = data.get("hitCount")
            else:
                degraded = "EuropePMC references endpoint returned no reference list."
        except Exception as exc:
            degraded = f"parse failed: {exc}"
    else:
        degraded = "EuropePMC references endpoint unavailable."

    # Fallback tiers: the EuropePMC references endpoint is periodically taken
    # down for maintenance (HTTP 503), so backward chasing needs a second path.
    if not out:
        out, degraded = _refs_openalex(pid, max_results, degraded)
    if not out:
        click.echo(json.dumps({
            "pmid": pid, "n": 0, "r": [],
            "note": (degraded or "No reference list available") +
                    " Use `citedby` (forward) or `related` instead.",
        }, separators=(",", ":")))
        return
    click.echo(json.dumps({"pmid": pid, "hit_count": hit_count, "n": len(out), "r": out,
                           **({"note": degraded} if degraded else {})},
                          ensure_ascii=False, separators=(",", ":")))


def _refs_openalex(pmid: str, max_results: int, degraded: str | None) -> tuple[list, str | None]:
    """Resolve a paper's reference list through OpenAlex (no key required).

    EuropePMC's /references endpoint returns 503 during maintenance windows;
    OpenAlex keeps a referenced_works array that maps back to PubMed IDs.
    """
    body = http_request(
        f"https://api.openalex.org/works/pmid:{pmid}?select=id,referenced_works",
        "openalex")
    if not body:
        return [], degraded
    try:
        refs = json.loads(body.decode("utf-8")).get("referenced_works") or []
    except Exception:
        return [], degraded
    if not refs:
        return [], degraded
    ids = [str(r).rsplit("/", 1)[-1] for r in refs][:max_results]
    # OpenAlex OR-filters take the values after ONE field name:
    # filter=openalex_id:W1|W2  (repeating the prefix yields zero results).
    q = "openalex_id:" + "|".join(ids)
    url = ("https://api.openalex.org/works?filter=" + urllib.parse.quote(q) +
           "&per-page=50&select=id,ids,title,publication_year,doi")
    body2 = http_request(url, "openalex")
    out: list[dict] = []
    if body2:
        try:
            for w in json.loads(body2.decode("utf-8")).get("results", []) or []:
                if not isinstance(w, dict):
                    continue
                pmid_val = None
                for key, val in (w.get("ids") or {}).items():
                    if key == "pmid" and val:
                        pmid_val = str(val).rsplit("/", 1)[-1]
                out.append({
                    "pmid": pmid_val,
                    "doi": (w.get("doi") or "").replace("https://doi.org/", "") or None,
                    "title": w.get("title"),
                    "year": str(w.get("publication_year") or ""),
                })
        except Exception:
            pass
    if out:
        note = (degraded + " " if degraded else "") + "OpenAlex reference list used."
        return out, note
    # Last resort: return the OpenAlex IDs so the caller can still chase them.
    return ([{"openalex": r} for r in refs[:max_results]],
            (degraded + " " if degraded else "") + "OpenAlex IDs only (no metadata resolved).")


@cli.command("export")
@click.option("--pmids", "-p", required=True, help="Comma-separated PMIDs")
@click.option("--format", "-F", "fmt",
              type=click.Choice(["bibtex", "ris", "csv", "json"]), default="bibtex")
@click.option("--out", "-o", default=None, help="Write to file instead of stdout")
@click.option("--screen", is_flag=True, default=False,
              help="Add PRISMA screening columns (csv only)")
def export_cmd(pmids: str, fmt: str, out: str | None, screen: bool) -> None:
    """Export records for a reference manager (BibTeX/RIS/CSV/JSON).

    The screened CSV adds blank include/exclude/reason columns plus the
    fields a PRISMA flow diagram needs, so screening can start immediately.
    """
    ids = [_require_pmid(p) for p in pmids.split(",") if p.strip()]
    if not ids:
        click.echo(json.dumps({"error": "no PMIDs given"}, separators=(",", ":")), err=True)
        sys.exit(2)
    if screen and fmt != "csv":
        click.echo(json.dumps({"error": "--screen applies to csv only; rerun with -F csv"},
                              separators=(",", ":")), err=True)
        sys.exit(2)

    summ = _esummary_batch(ids)
    records: list[dict] = []
    for pid in ids:
        rec = summ.get(pid) or {}
        if not rec:
            records.append({"pmid": pid, "error": "not found"})
            continue
        r = _esummary_to_record(pid, rec)
        # Abstract (with structured labels) comes from the metadata endpoint.
        meta = pmid_metadata(pid) or {}
        r["abstract"] = meta.get("abstract") or ""
        r["journal_full"] = meta.get("journal") or r.get("journal")
        r["authors_full"] = meta.get("authors") or r.get("authors")
        r["mesh"] = meta.get("mesh_keywords") or []
        if meta.get("retracted"):
            r["warning"] = "RETRACTED"
        if screen:
            r["included"] = ""
            r["reason"] = ""
        records.append(r)

    if fmt == "json":
        text = json.dumps(records, ensure_ascii=False, indent=1)
    elif fmt == "csv":
        import csv as _csv
        import io as _io
        buf = _io.StringIO()
        fields = ["pmid", "title", "journal", "date", "study_type", "doi", "warning",
                  "authors"] + (["included", "reason"] if screen else [])
        w = _csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in records:
            row = dict(r)
            row["authors"] = "; ".join(r.get("authors") or [])
            w.writerow({k: row.get(k, "") for k in fields})
        text = buf.getvalue()
    elif fmt == "ris":
        lines = []
        for r in records:
            lines.append("TY  - JOUR")
            for a in (r.get("authors") or []):
                lines.append(f"AU  - {a}")
            lines.append(f"TI  - {r.get('title','')}")
            lines.append(f"JO  - {r.get('journal_full') or r.get('journal') or ''}")
            lines.append(f"PY  - {(r.get('date') or '')[:4]}")
            lines.append(f"DA  - {r.get('date') or ''}")
            if r.get("doi"):
                lines.append(f"DO  - {r['doi']}")
            lines.append(f"AN  - {r['pmid']}")
            lines.append(f"UR  - https://pubmed.ncbi.nlm.nih.gov/{r['pmid']}/")
            lines.append("ER  - ")
            lines.append("")
        text = "\n".join(lines)
    else:  # bibtex
        lines = []
        for r in records:
            key = f"pmid{r['pmid']}"
            auth = (r.get("authors") or ["Unknown"])[0].split()[0].lower()
            year = (r.get("date") or "n.d.")[:4]
            key = re.sub(r"[^a-z0-9]", "", f"{auth}{year}pmid{r['pmid']}")
            lines.append(f"@article{{{key},")
            lines.append(f"  pmid = {{{r['pmid']}}},")
            lines.append(f"  title = {{{r.get('title','')}}},")
            if r.get("authors_full"):
                lines.append("  author = {" + " and ".join(r["authors_full"]) + "},")
            lines.append(f"  journal = {{{r.get('journal_full') or r.get('journal') or ''}}},")
            lines.append(f"  year = {{{year}}},")
            if r.get("doi"):
                lines.append(f"  doi = {{{r['doi']}}},")
            lines.append(f"  url = {{https://pubmed.ncbi.nlm.nih.gov/{r['pmid']}/}}")
            lines.append("}")
            lines.append("")
        text = "\n".join(lines)

    if out:
        with open(os.path.expanduser(out), "w", encoding="utf-8") as fh:
            fh.write(text)
        click.echo(json.dumps({"wrote": os.path.expanduser(out), "format": fmt,
                               "n": len(records), "screen": screen},
                              separators=(",", ":")))
    else:
        click.echo(text)


@cli.command("trials")
@click.option("--query", "-q", required=True, help="Condition / drug / NCT id")
@click.option("--max-results", "-m", default=5, type=int)
@click.option("--status", "-s", default=None,
              help="RECRUITING | ACTIVE_NOT_RECRUITING | COMPLETED | NOT_YET_RECRUITING")
@click.option("--full", is_flag=True, default=False, help="Add conditions + locations")
def trials_cmd(query: str, max_results: int, status: str | None, full: bool) -> None:
    """ClinicalTrials.gov search (trial-registry leg of a systematic review)."""
    clean, err = validate_query(query)
    if err:
        click.echo(json.dumps({"error": err}, separators=(",", ":")), err=True)
        sys.exit(2)
    params = {
        "query.term": clean,
        "pageSize": min(max(max_results, 1), 50),
        "countTotal": "true",
    }
    if status:
        params["filter.overallStatus"] = status.upper()
    url = "https://clinicaltrials.gov/api/v2/studies?" + urllib.parse.urlencode(params)
    body = http_request(url, "ncbi")
    if not body:
        click.echo(json.dumps({"error": "ClinicalTrials.gov request failed."},
                              separators=(",", ":")), err=True)
        sys.exit(1)
    try:
        data = json.loads(body.decode("utf-8"))
    except Exception as exc:
        click.echo(json.dumps({"error": f"parse failed: {exc}"},
                              separators=(",", ":")), err=True)
        sys.exit(1)
    out = []
    for s in data.get("studies", []) or []:
        prot = s.get("protocolSection", {}) or {}
        ident = prot.get("identificationModule", {}) or {}
        design = prot.get("designModule", {}) or {}
        stat = prot.get("statusModule", {}) or {}
        entry = {
            "nct": ident.get("nctId"),
            "title": (ident.get("briefTitle") or "")[:250],
            "status": stat.get("overallStatus"),
            "phase": (design.get("phases") or ["N/A"])[0],
            "enroll": (design.get("enrollmentInfo") or {}).get("count"),
        }
        if full:
            entry["conditions"] = (prot.get("conditionsModule", {}) or {}).get("conditions", [])[:4]
            entry["interventions"] = [
                i.get("name") for i in
                ((prot.get("armsInterventionsModule", {}) or {}).get("interventions") or [])[:4]
                if isinstance(i, dict)
            ]
            entry["locations"] = [
                l.get("country") for l in
                ((prot.get("contactsLocationsModule", {}) or {}).get("locations") or [])[:4]
                if isinstance(l, dict)
            ]
            entry["start"] = (stat.get("startDateStruct") or {}).get("date")
        out.append(entry)
    click.echo(json.dumps({"q": clean, "total": data.get("totalCount"),
                           "n": len(out), "r": out},
                          ensure_ascii=False, separators=(",", ":")))


@cli.command("watch")
@click.option("--name", "-n", required=False, default=None, help="Saved query name")
@click.option("--query", "-q", default=None, help="Set/replace the saved query")
@click.option("--max-results", "-m", default=20, type=int)
@click.option("--list", "-L", "list_only", is_flag=True, default=False,
              help="List saved queries instead of running one")
@click.option("--forget", is_flag=True, default=False, help="Delete a saved query")
def watch_cmd(name: str, query: str | None, max_results: int,
              list_only: bool, forget: bool) -> None:
    """Standing-query surveillance: report only papers not seen before.

    First run stores the current hits as the baseline (n_new = 0 by design);
    later runs return just the newly indexed records.
    """
    init_db()
    conn = _connect()
    conn.execute("""CREATE TABLE IF NOT EXISTS saved_queries (
        name TEXT PRIMARY KEY, query TEXT, created TEXT, last_run TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS seen_items (
        name TEXT, pmid TEXT, first_seen TEXT, PRIMARY KEY (name, pmid))""")
    conn.commit()

    if list_only:
        rows = conn.execute(
            "SELECT name, query, last_run FROM saved_queries ORDER BY name").fetchall()
        conn.close()
        click.echo(json.dumps({"n": len(rows),
                               "r": [{"name": r[0], "query": r[1], "last_run": r[2]}
                                     for r in rows]}, separators=(",", ":")))
        return

    if not name:
        conn.close()
        click.echo(json.dumps({"error": "watch requires --name except with --list"},
                              separators=(",", ":")), err=True)
        sys.exit(2)
    if forget:
        conn.execute("DELETE FROM saved_queries WHERE name=?", (name,))
        conn.execute("DELETE FROM seen_items WHERE name=?", (name,))
        conn.commit()
        conn.close()
        click.echo(json.dumps({"forgot": name}, separators=(",", ":")))
        return

    if query:
        clean, err = validate_query(query)
        if err:
            conn.close()
            click.echo(json.dumps({"error": err}, separators=(",", ":")), err=True)
            sys.exit(2)
        conn.execute(
            "INSERT INTO saved_queries (name, query, created, last_run) VALUES (?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET query=excluded.query",
            (name, clean, datetime.now(timezone.utc).isoformat(), None))
        conn.commit()

    row = conn.execute("SELECT query FROM saved_queries WHERE name=?", (name,)).fetchone()
    if not row:
        conn.close()
        click.echo(json.dumps({"error": f"no saved query '{name}' — pass -q to create it"},
                              separators=(",", ":")), err=True)
        sys.exit(2)
    stored_query = row[0]

    # Run the same twin-track search used by `search`.
    results = _run_search(stored_query, max_results, None, None, "date", 0, None)
    seen = {r[0] for r in conn.execute(
        "SELECT pmid FROM seen_items WHERE name=?", (name,)).fetchall()}
    new_items = [r for r in results if r.get("pmid") not in seen]
    baseline = not seen
    now = datetime.now(timezone.utc).isoformat()
    for r in results:
        conn.execute("INSERT OR IGNORE INTO seen_items (name, pmid, first_seen) VALUES (?,?,?)",
                     (name, r.get("pmid"), now))
    conn.execute("UPDATE saved_queries SET last_run=? WHERE name=?", (now, name))
    conn.commit()
    conn.close()

    out = {
        "name": name,
        "query": stored_query,
        "checked": len(results),
        "n_new": 0 if baseline else len(new_items),
        "baseline_established": baseline,
        "r": new_items if not baseline else results,
    }
    if baseline:
        out["note"] = "baseline stored — new records will appear on the next run"
    click.echo(json.dumps(out, ensure_ascii=False, separators=(",", ":")))


def _run_search(query: str, max_results: int, from_date: str | None,
                to_date: str | None, sort: str, min_citations: int,
                allowed_types: set[str] | None) -> list[dict]:
    """Shared twin-track search used by `search` and `watch`."""
    pm_clause, ep_clause = pushdown_clauses(allowed_types or set())
    pool = max(max_results * 3, 20)
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_us = ex.submit(search_pubmed, query, pool, from_date, to_date, pm_clause)
        f_eu = ex.submit(search_europepmc, query, pool, from_date, to_date, sort,
                         ep_clause, min_citations)
        res_us = f_us.result() or []
        res_eu = f_eu.result() or []
    deduped: dict[str, dict] = {}
    for item in res_us + res_eu:
        pid = item.get("pmid")
        if not pid:
            continue
        if pid not in deduped:
            deduped[pid] = _enrich_search_result(item)
        else:
            existing = deduped[pid]
            for key in ("abstract", "journal", "cited_by", "doi"):
                if item.get(key) not in (None, "", []) and existing.get(key) in (None, "", []):
                    existing[key] = item[key]
            for flag in ("retracted", "expression_of_concern", "corrected"):
                if item.get(flag):
                    existing[flag] = True
    final = list(deduped.values())
    if allowed_types:
        final = [r for r in final if r.get("study_type") in allowed_types]
    if min_citations > 0:
        final = [r for r in final if (r.get("cited_by") or 0) >= min_citations]
    final.sort(key=lambda r: (r.get("date") or ""), reverse=True)
    return final[:max_results]


if __name__ == "__main__":
    cli()
