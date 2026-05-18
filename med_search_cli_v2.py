#!/usr/bin/env python3
"""Twin-track parallel PubMed / EuropePMC CLI — v2 with date filtering, batch fetch,
query passthrough, sort control, citation/journal metadata, study-type filtering,
EuropePMC XML full-text parsing, cache transparency, and position-aware truncation."""

import click
import json
import os
import re
import sqlite3
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
}

# Background executor for stale-cache refresh (daemon threads, torn down at exit)
_bg_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="bgcache")

# ---------------------------------------------------------------------------
# Query utilities
# ---------------------------------------------------------------------------
_PUBMED_FIELD_QUALIFIER_RE = re.compile(
    r"\[(?i:mesh(?:\s+terms)?|tiab|tw|au|ad|dp|la|pt|ta|ti|ab|"
    r"mh|nm|rn|sh|sb|ed|is|ip|pg|pl|pm|vi|cn|gr|rd|ot|so|jt|"
    r"pl|ta|all|uid|rid|filt|subset)\]"
)
_PUBMED_BOOLEAN_RE = re.compile(r"\b(AND|OR|NOT)\b")


def is_pubmed_syntax_query(query: str) -> bool:
    """Return True if query contains PubMed field qualifiers or explicit
    uppercase boolean operators (AND/OR/NOT).  When True, the query should
    be passed verbatim to E-utilities rather than TIAB-wrapped."""
    if _PUBMED_FIELD_QUALIFIER_RE.search(query):
        return True
    if _PUBMED_BOOLEAN_RE.search(query):
        return True
    return False


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


def _extract_mesh_pubmed(xml_str: str | None) -> list[str]:
    """Parse MeSH descriptor names from PubMedArticle XML."""
    if xml_str is None or not xml_str.strip():
        return []
    try:
        root = ET.fromstring(xml_str)
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
                kept.append(s[:remaining] + "...")
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
    updated = datetime.fromisoformat(row[2])
    ttl_days = row[3] if row[3] is not None else DEFAULT_TTL_DAYS
    age = (datetime.now(timezone.utc).replace(tzinfo=None) - updated.replace(tzinfo=None)).days
    return {
        "source": row[0],
        "data": json.loads(row[1]),
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
    conn = _connect()
    rows = conn.execute(
        """SELECT pmid, snippet(lit_fts, 1, '<b>', '</b>', '…', 40) AS title_snip,
                  snippet(lit_fts, 2, '<b>', '</b>', '…', 40) AS abs_snip
           FROM lit_fts WHERE lit_fts MATCH ? LIMIT ?""",
        (query, limit),
    ).fetchall()
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
                  to_date: str | None = None) -> list[dict]:
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

        # Extract MeSH publication types from XML
        mesh_types: list[str] = []
        xml_str = None
        try:
            xml_str = art.xml if hasattr(art, "xml") else None
            if xml_str is not None and (isinstance(xml_str, str) and xml_str.strip()):
                root = ET.fromstring(xml_str)
                for pt in root.findall(".//PublicationType"):
                    txt = pt.text or ""
                    if txt.strip():
                        mesh_types.append(txt.strip())
        except Exception:
            pass

        record = {
            "pmid": pmid,
            "title": title,
            "date": str(art.publication_date) if art.publication_date else None,
            "source": "pubmed",
            "abstract": abstract,
            "journal": getattr(art, "journal", None),
            "authors": _extract_authors_pubmed(getattr(art, "authors", None)),
            "mesh_keywords": _extract_mesh_pubmed(xml_str if isinstance(xml_str, str) else None),
            "cited_by": None,  # PubMed doesn't provide citation counts
            "in_pmc": False,
            "in_epmc": False,
            "is_open_access": False,
            "study_type": detect_study_type(title, abstract, mesh_types),
        }
        articles.append(record)

    # Clean up date params so they don't leak to subsequent calls
    pubmed.parameters.pop("mindate", None)
    pubmed.parameters.pop("maxdate", None)
    pubmed.parameters.pop("datetype", None)

    return articles


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

    mesh_types: list[str] = []
    xml_str = None
    try:
        xml_str = art.xml if hasattr(art, "xml") else None
        if xml_str is not None and (isinstance(xml_str, str) and xml_str.strip()):
            root = ET.fromstring(xml_str)
            for pt in root.findall(".//PublicationType"):
                txt = pt.text or ""
                if txt.strip():
                    mesh_types.append(txt.strip())
    except Exception:
        pass

    study_type = detect_study_type(title, abstract, mesh_types)
    return {
        "doi": doi,
        "title": title,
        "abstract": abstract,
        "study_type": study_type,
        "journal": getattr(art, "journal", None),
        "authors": _extract_authors_pubmed(getattr(art, "authors", None)),
        "mesh_keywords": _extract_mesh_pubmed(xml_str if isinstance(xml_str, str) else None),
    }


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
                     sort: str = "relevance") -> list[dict]:
    """Search EuropePMC REST API with optional date filtering, sort control,
    and ``resultType=core`` for richer metadata."""

    # Append date range filter to query
    if from_date and to_date:
        query += f" AND FIRST_PDATE:[{from_date} TO {to_date}]"
    elif from_date:
        query += f" AND FIRST_PDATE:[{from_date} TO 3000-01-01]"
    elif to_date:
        query += f" AND FIRST_PDATE:[0000-01-01 TO {to_date}]"

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
                return {"sections": secs}

    # Step 3: fallback — check if abstract has structural labels
    abstract = paper.get("abstractText", "")
    if abstract and ("METHODS" in abstract or "RESULTS" in abstract):
        return {
            "sections": {
                "abstract": abstract,
                "intro": "See abstract.",
                "methods": "Structural text chunk: " + abstract,
                "results": "Structural text chunk: " + abstract,
                "discussion": "Refer to publisher web.",
            }
        }
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
    cached = get_cached_data(pmid)

    if cached and not cached["stale"]:
        out = cached["data"]
        source = cached["source"]
        out["cached"] = True
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

    # Build response
    resp: dict = {
        "pmid": pmid,
        "title": out["title"],
        "source": source,
        "section": section,
        "study_type": out.get("study_type", "Unknown"),
    }
    for optional in ("doi", "journal", "authors", "mesh_keywords", "proxy_url",
                     "cached", "cached_stale"):
        if out.get(optional):
            resp[optional] = out[optional]

    # Select and truncate text
    raw: str = ""
    section_bonus = 0
    if section == "abstract":
        raw = out.get("abstract", "")
    elif section == "all":
        secs = out.get("sections")
        if secs:
            # Per-section truncation with position bonus
            alloc = {"discussion": 0.35, "results": 0.30, "methods": 0.20,
                     "intro": 0.10, "other": 0.05}
            bonus = {"discussion": 2, "results": 1, "methods": 0, "intro": 0, "other": 0}
            parts: list[str] = []
            keywords = _tokenize(f"{out.get('title', '')} {out.get('abstract', '')}")
            total_remaining = limit
            # Process in order: discussion first (gets any overflow)
            for sec_name in ("discussion", "results", "methods", "intro", "other"):
                sec_text = secs.get(sec_name, "")
                if not sec_text:
                    continue
                alloc_limit = max(200, int(limit * alloc.get(sec_name, 0.1)))
                trimmed, _, _ = smart_truncate(sec_text, alloc_limit, keywords,
                                               section_bonus=bonus.get(sec_name, 0))
                parts.append(trimmed)
            raw = "\n\n".join(parts)
        else:
            raw = out.get("abstract", "")
            if not secs and out.get("open_access"):
                resp["pdf"] = out["open_access"]["pdf"]
    else:
        secs = out.get("sections")
        if secs and section in secs:
            sec_bonus_map = {"discussion": 2, "results": 1}
            section_bonus = sec_bonus_map.get(section, 0)
            raw = secs[section]
        else:
            raw = f"Section '{section}' not available."
            if out.get("open_access"):
                resp["pdf"] = out["open_access"]["pdf"]

    # Smart truncation with position bonus
    if raw and len(raw) > limit:
        keywords = _tokenize(f"{out.get('title', '')} {out.get('abstract', '')}")
        trimmed, was_truncated, kept_bytes = smart_truncate(
            raw, limit, keywords, section_bonus=section_bonus
        )
        resp["text"] = trimmed
        resp["truncated"] = was_truncated
        if was_truncated:
            resp["truncated_bytes"] = kept_bytes
    else:
        resp["text"] = raw

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
def search_cmd(query: str, max_results: int, from_date: str | None,
               to_date: str | None, sort: str, min_citations: int,
               study_type: str | None, study_types: str | None) -> None:
    """Search PubMed + EuropePMC in parallel, deduplicate, enrich, filter."""
    try:
        # Validate dates
        date_re = re.compile(r"^\d{4}-\d{2}-\d{2}$")
        for label, val in [("from_date", from_date), ("to_date", to_date)]:
            if val and not date_re.match(val):
                click.echo(json.dumps(
                    {"error": f"Invalid {label}: '{val}'. Use YYYY-MM-DD."},
                    separators=(",", ":")), err=True)
                return

        # Build study-type filter set
        allowed_types: set[str] | None = None
        if study_type or study_types:
            allowed_types = set()
            if study_type:
                allowed_types.add(study_type)
            if study_types:
                allowed_types.update(t.strip() for t in study_types.split(",") if t.strip())

        with ThreadPoolExecutor(max_workers=2) as ex:
            f_us = ex.submit(search_pubmed, query, max_results * 2, from_date, to_date)
            f_eu = ex.submit(search_europepmc, query, max_results * 2,
                             from_date, to_date, sort)
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
                # Overlay richer fields from the newer item
                for key in ("abstract", "journal", "cited_by", "authors",
                            "mesh_keywords", "pub_types", "in_epmc", "in_pmc",
                            "is_open_access"):
                    if item.get(key) and not existing.get(key):
                        existing[key] = item[key]
                # Upgrade study_type if currently Unknown
                if existing.get("study_type") == "Unknown" and item.get("study_type") != "Unknown":
                    existing["study_type"] = item["study_type"]
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

        # Apply filters
        if allowed_types:
            final = [r for r in final if r.get("study_type", "Unknown") in allowed_types]
        if min_citations > 0:
            final = [r for r in final
                     if r.get("cited_by") is None or r.get("cited_by", 0) >= min_citations]

        final = final[:max_results]

        # Clean up None cited_by for display (None = no data from PubMed-only results)
        for record in final:
            if record.get("cited_by") is None:
                record["cited_by"] = None  # explicit null in JSON

        click.echo(json.dumps(final, ensure_ascii=False, separators=(",", ":")))
    except Exception as exc:
        click.echo(json.dumps({"error": str(exc)}, separators=(",", ":")), err=True)


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
        pmids = [p.strip() for p in pmid.split(",") if p.strip()]
        if not pmids:
            click.echo(json.dumps({"error": "No valid PMIDs provided."},
                                  separators=(",", ":")), err=True)
            return

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


if __name__ == "__main__":
    cli()
