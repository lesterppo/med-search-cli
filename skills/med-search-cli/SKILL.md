---
name: med-search-cli
description: Search and fetch biomedical literature via PubMed + EuropePMC with smart truncation, study-type tagging, dynamic backoff, and local FTS5 cache search. Use for literature search, paper retrieval, evidence synthesis, and citation lookups — especially when token efficiency matters.
triggers:
  keywords:
    - PubMed
    - EuropePMC
    - PMID
    - literature search
    - medical literature
    - paper fetch
    - biomedical search
    - med search
    - pubmed
    - evidence synthesis
    - clinical trial search
    - study type
    - FTS5
    - cached papers
  context:
    - User wants to search PubMed or EuropePMC for papers
    - User needs to fetch a paper by PMID
    - User wants structured full-text sections (intro/methods/results/discussion)
    - User wants study type classification (RCT, Meta-Analysis, etc.)
    - User wants local full-text search across previously fetched papers
    - Token-efficient literature retrieval for AI agent consumption
---

# Med Search CLI

Two versions: `med_search_cli.py` (stable v1) and `med_search_cli_v2.py` (v2 with date filtering, batch fetch, sort control, study-type filtering, citation metadata, interleaved merge). Both at `/home/peter/`. **Prefer v2** for all new work — same cache DB, backward compatible output, richer metadata.

# Med Search CLI

Single-script twin-track parallel PubMed + EuropePMC CLI at `/home/peter/med_search_cli.py`. Designed for token-efficient AI agent consumption — smart truncation preserves semantically dense content, study types are auto-tagged, and cache + FTS5 eliminate redundant network calls.

## Commands

### search — Find papers

```bash
python3 /home/peter/med_search_cli_v2.py search --query "metformin diabetes RCT" --max-results 5
```

- Queries PubMed and EuropePMC in parallel, deduplicates by PMID.
- Interleaved merge: "both" sources first, then zigzag PubMed/EuropePMC so both sources are represented.
- Each result: `pmid`, `title`, `date`, `abstract`, `journal`, `cited_by`, `authors`, `mesh_keywords`, `source`, `study_type`.
- `study_type` is detected at search time via regex on title+abstract (no fetch needed).
- `cited_by` comes from EuropePMC; `null` for PubMed-only results.

**v2 search flags:**

| Flag | Default | Purpose |
|------|---------|---------|
| `--query` / `-q` | required | Search query; if it contains `[MeSH]`, `[TIAB]`, `AND`/`OR`/`NOT` → passed verbatim to PubMed |
| `--max-results` / `-m` | `5` | Maximum results to return |
| `--from-date` / `-f` | none | Lower bound YYYY-MM-DD |
| `--to-date` / `-t` | none | Upper bound YYYY-MM-DD |
| `--sort` / `-S` | `relevance` | `relevance`, `date`, or `citations` (EuropePMC only) |
| `--min-citations` / `-C` | `0` | Minimum citation count filter |
| `--study-type` / `-T` | none | Single study type filter (RCT, Meta-Analysis, etc.) |
| `--study-types` / `-U` | none | Comma-separated study types (e.g., `"RCT,Meta-Analysis"`) |

### fetch — Get full paper data

```bash
# Single PMID (backward compatible)
python3 /home/peter/med_search_cli_v2.py fetch --pmid 38261728 --section all --limit 6000 --ttl 30

# Batch fetch multiple PMIDs
python3 /home/peter/med_search_cli_v2.py fetch --pmid "38261728,38182299,38693734" --limit 3000
```

| Flag | Default | Purpose |
|------|---------|---------|
| `--pmid` / `-p` | required | Single PMID or comma-separated list for batch fetch |
| `--section` / `-s` | `all` | One of: `all`, `abstract`, `intro`, `methods`, `results`, `discussion` |
| `--limit` / `-l` | `6000` | Character limit; triggers smart truncation when exceeded |
| `--ttl` | `30` | Cache freshness in days (`7` for fast-moving topics, `90` for stable) |

**Full-text resolution order:** PubMed Central Open Access XML → EuropePMC JATS XML → Unpaywall OA PDF link → fallback (abstract only).

**Output fields:** `pmid`, `title`, `source`, `section`, `study_type`, `text`, plus `doi`, `journal`, `authors`, `mesh_keywords`, `cached`/`cached_stale`, `proxy_url`, `pdf` (when available), `truncated`, `truncated_bytes`.

### cache-stats — Inspect local cache

```bash
python3 /home/peter/med_search_cli.py cache-stats
```

Returns: `total_records`, `stale_records`, `fts_indexed`.

### search-cache — Full-text search without networking

```bash
python3 /home/peter/med_search_cli.py search-cache --query "SGLT2 inhibitor cardiovascular" --limit 20
```

- Searches title + abstract + full-text of all cached papers using SQLite FTS5 (porter stemming).
- Returns PMIDs with `<b>`-highlighted snippets. Zero API calls.

## Study type detection

Every `fetch` response includes a `study_type` field. Detection order:

1. **MeSH PublicationType tags** from PubMed XML (authoritative when present).
2. **Regex heuristics** on title + abstract when MeSH tags are absent.

| Tag | Matches |
|-----|---------|
| `RCT` | Randomized controlled trial |
| `Meta-Analysis` | Meta-analysis |
| `Systematic Review` | Systematic review |
| `Review` | General review |
| `Observational Study` | Cohort, case-control, cross-sectional |
| `Case Reports` | Case report or series |
| `Clinical Trial` | Non-randomized clinical trial |
| `Practice Guideline` | Guideline, consensus statement |
| `Unknown` | None of the above detected |

## Smart truncation

When text exceeds `--limit`, the script:
1. Strips boilerplate sentences (funding disclosures, conflict of interest, copyright, correspondence, etc.).
2. Scores remaining sentences by keyword overlap with the paper's title + abstract.
3. Keeps top-N sentences that fit within the character limit.
4. Marks `"truncated": true` and reports `truncated_bytes` (actual bytes kept).

No mid-word cuts. Semantically dense content always wins over boilerplate.

## Resilience

All HTTP calls go through exponential backoff with jitter (4 attempts, 0s → 1s+jitter → 2s+jitter → 4s+jitter). HTTP 429 responses respect `Retry-After` headers. Timeouts adapt dynamically based on rolling average latency per endpoint (NCBI, EuropePMC, Unpaywall), clamped to [4, 30] seconds.

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `NCBI_API_KEY` | none | NCBI E-utilities API key (10 req/s with key vs 3/s without) |
| `MED_SEARCH_EMAIL` | placeholder | Your email for NCBI/Unpaywall identification |
| `MED_SEARCH_DB` | `pubmed_agent_cache.db` | Custom cache database path |
| `MED_SEARCH_TTL` | `30` | Default cache TTL in days |
| `INSTITUTIONAL_PROXY_PREFIX` | none | Proxy prefix for DOI links (e.g., `https://proxy.library.edu/login?url=`) |

## Routing rules

- **Literature search for a clinical question:** `search` first, shortlist relevant PMIDs, then `fetch` each one (batch if multiple).
- **Need recent papers:** Use `--from-date` + `--to-date` + `-S date` in v2 search. No more stuffing dates into query strings.
- **Need only guidelines/RCTs/meta-analyses:** Use `--study-type` or `--study-types` filter in v2 search. Study type is now detected at search time.
- **Need high-impact papers:** Use `--min-citations N` to filter by citation count (EuropePMC data).
- **Need full-text sections (methods/results):** `fetch --section all` — PMC Open Access XML and EuropePMC JATS XML are parsed into intro/methods/results/discussion sections when available.
- **Checking if you already have a paper:** `search-cache --query "keyword"` first before hitting the network.
- **Fast-moving topic (last 7 days):** `fetch --ttl 7` to force cache turnover.
- **Citation chaining:** Search once, `fetch` each candidate. Use batch fetch for efficiency. The cache means subsequent fetches on related papers may hit locally.
- **PubMed expert query:** Use `[MeSH]`, `[TIAB]`, `AND`/`OR`/`NOT` directly in the query — v2 passes them verbatim to PubMed.

## Anti-patterns

### Don't fetch the same PMID repeatedly

The cache makes repeat fetches instant. If you need fresh data on a stale cached paper, use `--ttl 0` to force a network refresh.

### Don't search-cache before you have papers cached

`search-cache` only searches locally cached papers. If the cache is empty, run `search` + `fetch` first to populate it.

### Don't expect full-text sections for every paper

PMC Open Access availability is ~30% of PubMed. When sections aren't available, the response falls back to abstract text. The `source` field tells you what was resolved: `pubmed_ft` (full-text from PMC), `europepmc_ft` (EuropePMC structured), `unpaywall` (OA PDF link), `fallback` (abstract only).

### Don't use med-search-cli as a replacement for systematic review tools

This is a retrieval tool for AI agent consumption. It does not implement PRISMA flow diagrams, dual-screening, or risk-of-bias assessment. For systematic reviews, export PMIDs to a dedicated tool.

### Don't guess PMIDs

Always get PMIDs from `search` output. Invented PMIDs will hit the network and fail at metadata resolution.

## Integration with BioMCP

BioMCP (`biomcp`) is the broader biomedical data CLI. `med_search_cli.py` is the literature-specific sharp tool. Use BioMCP for gene/disease/drug/trial/variant structured data; use `med_search_cli.py` for literature retrieval and full-text. The two are complementary:

- BioMCP `search article` → fast typed-filter search with Semantic Scholar enrichment
- `med_search_cli.py search` → twin-track PubMed + EuropePMC, token-minimized output
- BioMCP `get article` → PubTator annotations, TLDR summaries
- `med_search_cli.py fetch` → structured full-text sections, smart truncation, local caching

## Output contract

- All output is single-line JSON to stdout (no raw tracebacks in the stream).
- `separators=(',', ':')` — no whitespace in JSON, maximizing token density.
- On error, stderr gets `{"error": "message"}`.
- On success, the output is a flat JSON object (fetch) or array (search).
