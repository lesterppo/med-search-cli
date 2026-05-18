# Med Search CLI

Twin-track parallel PubMed + EuropePMC CLI — token-optimized for AI agent consumption.

**Two versions available:**
- `med_search_cli.py` — stable v1 (707 lines)
- `med_search_cli_v2.py` — v2 with date filtering, batch fetch, sort control, study-type filtering, citation/journal metadata, interleaved merge, and position-aware truncation (1167 lines)

Same cache database, same output contract, fully backward compatible.

## v2 Features (beyond v1)

| Feature | Flag(s) |
|---------|---------|
| **Date filtering** | `--from-date YYYY-MM-DD`, `--to-date YYYY-MM-DD` |
| **Sort control** | `--sort relevance\|date\|citations` |
| **Study type at search time** | Auto-detected from title+abstract (no fetch needed) |
| **Study type filter** | `--study-type RCT`, `--study-types "RCT,Meta-Analysis"` |
| **Citation filter** | `--min-citations N` |
| **Batch fetch** | `fetch --pmid "123,456,789"` (comma-separated) |
| **PubMed query passthrough** | `[MeSH]`, `[TIAB]`, `AND`/`OR`/`NOT` → verbatim to PubMed |
| **Journal & citation metadata** | `journal`, `cited_by` in search results |
| **Author & MeSH metadata** | `authors`, `mesh_keywords` in search + fetch |
| **Interleaved merge** | EuropePMC results no longer buried by PubMed ordering |
| **Cache transparency** | `"cached": true` on cache hits |
| **Position-aware truncation** | Discussion/conclusion sentences get scoring bonus |
| **EuropePMC XML full-text** | JATS `<sec>` parsing (mirrors PMC parser) |

## Install

```bash
pip install pymed click
# Set your email for NCBI E-utilities
export MED_SEARCH_EMAIL="your@email.com"
# Optional: NCBI API key for higher rate limits
export NCBI_API_KEY="your-key"
```

## Usage

### Search papers

```bash
# Basic search
python3 med_search_cli_v2.py search --query "SLE guideline" --max-results 5

# With date filter and sort
python3 med_search_cli_v2.py search -q "lupus nephritis" -m 10 -f 2024-01-01 -S date

# Filter by study type
python3 med_search_cli_v2.py search -q "SLE" -m 10 -T "RCT"

# PubMed syntax passthrough
python3 med_search_cli_v2.py search -q '"lupus nephritis"[MeSH] AND (RCT OR meta-analysis)' -m 5

# Min citations filter
python3 med_search_cli_v2.py search -q "belimumab" -m 5 -C 10
```

### Fetch paper(s) by PMID

```bash
# Single fetch (backward compatible)
python3 med_search_cli_v2.py fetch --pmid 38261728 --section all --limit 6000 --ttl 30

# Batch fetch (returns JSON array)
python3 med_search_cli_v2.py fetch --pmid "38261728,38182299,38693734" --limit 3000
```

Options: `--section` (all/abstract/intro/methods/results/discussion), `--limit` (char limit, triggers smart truncation), `--ttl` (cache freshness in days)

### Cache stats

```bash
python3 med_search_cli_v2.py cache-stats
```

### Full-text search cached papers (zero network calls)

```bash
python3 med_search_cli_v2.py search-cache --query "SGLT2 inhibitor cardiovascular"
```

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `NCBI_API_KEY` | none | NCBI E-utilities API key (10 req/s vs 3/s) |
| `MED_SEARCH_EMAIL` | placeholder | Email for NCBI/Unpaywall identification |
| `MED_SEARCH_DB` | `pubmed_agent_cache.db` | Custom cache path |
| `MED_SEARCH_TTL` | `30` | Default cache TTL in days |
| `INSTITUTIONAL_PROXY_PREFIX` | none | Institutional proxy for DOI links |

## Output format

All output is single-line minified JSON to stdout. On error, stderr gets `{"error": "message"}`.

### Search response (v2)

```json
[{"pmid":"42132163","title":"Comparative Effectiveness of...","date":"2026-05-14","source":"both","abstract":"...","journal":"Arthritis Rheumatol","cited_by":42,"authors":["Smith J","Lee K"],"mesh_keywords":["Lupus Nephritis"],"study_type":"RCT"}]
```

### Fetch response

```json
{"pmid":"38261728","title":"...","source":"pubmed_ft","section":"all","study_type":"RCT","text":"...","cached":true,"truncated":true,"truncated_bytes":5980}
```

`source` values: `pubmed_ft` (PMC full-text), `europepmc_ft` (EuropePMC XML), `unpaywall` (OA PDF link), `fallback` (abstract only).

## AI agent skill

A companion skill for Claude Code / Hermes Agent is at `skills/med-search-cli/SKILL.md`. Copy it to `~/.claude/skills/` or `~/.hermes/skills/` for auto-discovery.
