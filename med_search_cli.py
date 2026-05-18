#!/usr/bin/env python3
"""Twin-track parallel PubMed / EuropePMC CLI — token-optimized for AI agent consumption."""

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

# Map MeSH PublicationType UI labels to our tags
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
# Smart truncation — keyword-density scoring
# ---------------------------------------------------------------------------
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


def _tokenize(text: str) -> set[str]:
    return set(re.findall(r"[a-z]{3,}", text.lower()))


def smart_truncate(text: str, limit: int, keywords: set[str] | None = None) -> tuple[str, bool, int]:
    """Keep highest-scoring sentences that fit within *limit* characters.

    Scoring = count of keyword hits per sentence.  Sentences flagged as
    boilerplate earn a heavy penalty so they sort to the bottom.
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
        score = sum(1 for kw in keywords if kw in s.lower())
        if _is_boilerplate(s):
            score -= 10  # heavy penalty
        scored.append((score, s))

    # Sort descending by score, stable so original order preserved among ties
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
            # Try to squeeze in a partial for the last slot
            remaining = limit - total - 4  # room for "..."
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
    # Mirror to FTS5 index
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
    """Full-text search across locally cached papers."""
    conn = _connect()
    # FTS5 snippet() gives highlighted context
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
    """Compute timeout from rolling average latency for *endpoint*."""
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
    """Perform an HTTP GET with exponential backoff + jitter on transient errors.

    Returns response body bytes, or *None* after exhausting retries.
    """
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
                return None  # 4xx (non-429) are not retried
        except Exception:
            if attempt < attempts - 1:
                time.sleep(2**attempt + random.uniform(0, 2**attempt))
    return None


# ---------------------------------------------------------------------------
# PubMed helpers (via pymed + raw HTTP for full-text)
# ---------------------------------------------------------------------------
def _pubmed_client() -> pymed.PubMed:
    pubmed = pymed.PubMed(tool="AgentCLI", email=AGENT_EMAIL)
    api_key = os.getenv("NCBI_API_KEY")
    if api_key:
        pubmed.parameters["api_key"] = api_key
    return pubmed


def search_pubmed(query: str, max_results: int) -> list[dict]:
    pubmed = _pubmed_client()
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
        record = {
            "pmid": pmid,
            "title": art.title,
            "date": str(art.publication_date) if art.publication_date else None,
            "source": "pubmed",
        }
        articles.append(record)
    return articles


def search_europepmc(query: str, max_results: int) -> list[dict]:
    encoded = urllib.parse.quote(query)
    url = (
        f"https://www.ebi.ac.uk/europepmc/webservices/rest/search"
        f"?query={encoded}&format=json&resultType=lite&pageSize={max_results}"
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
        articles.append(
            {
                "pmid": str(pmid),
                "title": art.get("title", "Untitled"),
                "date": art.get("firstPublicationDate"),
                "source": "europepmc",
            }
        )
    return articles


def pmid_metadata(pmid: str) -> dict | None:
    """Resolve title, abstract, DOI, and MeSH publication types for a PMID."""
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
    # Attempt to extract MeSH publication types from the XML representation
    mesh_types: list[str] = []
    try:
        xml_str = art.xml if hasattr(art, "xml") else None
        if xml_str is not None and xml_str != "":
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
    # ID convert PMID → PMCID
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


def fetch_europepmc_fulltext(pmid: str) -> dict | None:
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
    abstract = paper.get("abstractText", "")
    if "METHODS" in abstract or "RESULTS" in abstract:
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
# CLI
# ---------------------------------------------------------------------------
@click.group()
def cli():
    init_db()


@cli.command("search")
@click.option("--query", "-q", required=True)
@click.option("--max-results", "-m", default=5, type=int)
def search_cmd(query: str, max_results: int) -> None:
    """Search PubMed + EuropePMC in parallel, deduplicate, return top N."""
    try:
        with ThreadPoolExecutor(max_workers=2) as ex:
            f_us = ex.submit(search_pubmed, query, max_results * 2)
            f_eu = ex.submit(search_europepmc, query, max_results * 2)
            res_us = f_us.result() or []
            res_eu = f_eu.result() or []

        deduped: dict[str, dict] = {}
        for item in res_us + res_eu:
            pid = item.get("pmid")
            if pid and pid not in deduped:
                deduped[pid] = item
            elif pid and pid in deduped:
                # Merge: if both sources have it, keep the one with better metadata, tag as dual
                existing = deduped[pid]
                if item.get("title") and not existing.get("title"):
                    deduped[pid] = item
                existing["source"] = "both"

        final = list(deduped.values())[:max_results]

        # Enrich with study types where we already have abstract data
        for record in final:
            if "study_type" not in record:
                record["study_type"] = "Unknown"

        click.echo(json.dumps(final, ensure_ascii=False, separators=(",", ":")))
    except Exception as exc:
        click.echo(json.dumps({"error": str(exc)}, separators=(",", ":")), err=True)


@cli.command("fetch")
@click.option("--pmid", "-p", required=True)
@click.option(
    "--section",
    "-s",
    type=click.Choice(["all", "abstract", "intro", "methods", "results", "discussion"]),
    default="all",
)
@click.option("--limit", "-l", default=6000, type=int, help="Character limit for returned text")
@click.option("--ttl", default=DEFAULT_TTL_DAYS, type=int, help="Cache TTL in days")
def fetch_cmd(pmid: str, section: str, limit: int, ttl: int) -> None:
    """Fetch a paper by PMID, with twin-track full-text and smart truncation."""
    # 1. Try cache
    cached = get_cached_data(pmid)
    if cached and not cached["stale"]:
        out = cached["data"]
        source = cached["source"]
    else:
        # 2. Resolve metadata
        meta = pmid_metadata(pmid)
        if not meta:
            click.echo(json.dumps({"error": "Metadata resolution failed."}, separators=(",", ":")))
            return

        doi = meta["doi"]
        keywords = _tokenize(f"{meta['title']} {meta['abstract']}")
        out = {
            "pmid": pmid,
            "doi": doi,
            "title": meta["title"],
            "abstract": meta["abstract"],
            "study_type": meta.get("study_type", "Unknown"),
            "sections": None,
            "open_access": None,
            "proxy_url": None,
        }
        source = "fallback"

        # 3. Twin-track full-text fetch
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

        # 4. Save to cache (even if stale — provides a baseline)
        save_to_cache(pmid, source, out, ttl)

    # 5. Build response
    resp: dict = {
        "pmid": pmid,
        "title": out["title"],
        "source": source,
        "section": section,
        "study_type": out.get("study_type", "Unknown"),
    }
    if out.get("proxy_url"):
        resp["proxy_url"] = out["proxy_url"]
    if cached and cached.get("stale"):
        resp["stale"] = True

    # 6. Select text
    raw: str = ""
    if section == "abstract":
        raw = out.get("abstract", "")
    elif section == "all":
        secs = out.get("sections")
        if secs:
            raw = json.dumps(secs, ensure_ascii=False, separators=(",", ":"))
        else:
            raw = out.get("abstract", "")
            if not secs and out.get("open_access"):
                resp["pdf"] = out["open_access"]["pdf"]
    else:
        secs = out.get("sections")
        if secs and section in secs:
            raw = secs[section]
        else:
            raw = f"Section '{section}' not available."
            if out.get("open_access"):
                resp["pdf"] = out["open_access"]["pdf"]

    # 7. Smart truncation
    if raw and len(raw) > limit:
        keywords = _tokenize(f"{out.get('title', '')} {out.get('abstract', '')}")
        trimmed, was_truncated, kept_bytes = smart_truncate(raw, limit, keywords)
        resp["text"] = trimmed
        resp["truncated"] = was_truncated
        if was_truncated:
            resp["truncated_bytes"] = kept_bytes
    else:
        resp["text"] = raw

    click.echo(json.dumps(resp, ensure_ascii=False, separators=(",", ":")))


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
