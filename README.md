# Med Search CLI

Twin-track parallel PubMed + EuropePMC CLI — token-optimized for AI agent consumption.

## Features

- **Dual-source search** — Queries PubMed and EuropePMC in parallel, deduplicates by PMID
- **Structured full-text** — Parses PMC Open Access XML into intro/methods/results/discussion sections
- **Smart truncation** — When text exceeds limit, keeps semantically dense sentences via keyword scoring, strips boilerplate (funding disclosures, conflict of interest, copyright)
- **Study type detection** — MeSH PublicationType tags from PubMed XML with regex fallback on title+abstract (RCT, Meta-Analysis, Systematic Review, Observational Study, etc.)
- **Dynamic backoff** — Per-endpoint rolling latency tracker adjusts timeouts; exponential backoff with jitter on transient errors; respects HTTP 429 Retry-After
- **SQLite cache** — TTL-based eviction (configurable per-fetch), FTS5 full-text search across cached papers with porter stemming and snippet highlights
- **Zero new dependencies** — Pure stdlib beyond `pymed` and `click`

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
python3 med_search_cli.py search --query "metformin diabetes cardiovascular RCT" --max-results 5
```

### Fetch paper by PMID

```bash
python3 med_search_cli.py fetch --pmid 38261728 --section all --limit 6000 --ttl 30
```

Options: `--section` (all/abstract/intro/methods/results/discussion), `--limit` (char limit, triggers smart truncation), `--ttl` (cache freshness in days)

### Cache stats

```bash
python3 med_search_cli.py cache-stats
```

### Full-text search cached papers (zero network calls)

```bash
python3 med_search_cli.py search-cache --query "SGLT2 inhibitor cardiovascular"
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

### Search response

```json
[{"pmid":"42132163","title":"Comparative Effectiveness of...","date":"2026-05-14","source":"pubmed","study_type":"Unknown"}]
```

### Fetch response

```json
{"pmid":"38261728","title":"...","source":"pubmed_ft","section":"all","study_type":"RCT","text":"...","truncated":true,"truncated_bytes":5980}
```

`source` values: `pubmed_ft` (PMC full-text), `europepmc_ft` (EuropePMC), `unpaywall` (OA PDF link), `fallback` (abstract only).

## AI agent skill

A companion skill for Claude Code / Hermes Agent is at `skills/med-search-cli/SKILL.md`. Copy it to `~/.claude/skills/` or `~/.hermes/skills/` for auto-discovery.
