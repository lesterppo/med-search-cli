# Med Search CLI

Twin-track parallel **PubMed + EuropePMC** search, full-text retrieval and
researcher-workflow tooling — one script, token-optimized for AI-agent and
clinician consumption.

```bash
python3 med_search_cli.py search  -q "gastric antral vascular ectasia" -m 5
python3 med_search_cli.py fetch   -p 38261728 -s all -l 6000
python3 med_search_cli.py mesh    "gastric antral vascular ectasia"
python3 med_search_cli.py citedby -p 38261728 -m 10
python3 med_search_cli.py export  -p 38261728,42543981 -F bibtex -o refs.bib
python3 med_search_cli.py watch   -n gave -q "gastric antral vascular ectasia" -m 20
```

Everything is a single-line JSON payload on stdout (errors on stderr), so an
agent can pipe it straight into its own reasoning.

## Why v3

v3 folds the old v1/v2 scripts into **one canonical CLI** and fixes the defects
found while running real hepatology screening sessions with it. The v1/v2 files
are gone (still in git history); every v2 flag keeps working.

Fixes (all reproduced live first, then verified after):

| # | Defect | Effect | Fix |
|---|--------|--------|-----|
| 1 | `--section all` returned an empty body when the full-text index existed but held no text | Silent data loss — a review built on those papers read "no data" | Mandatory abstract fallback + `text_from: "abstract"` marker |
| 2 | `source` reported `europepmc_ft` while returning nothing | Fabricated provenance | A full-text source is only claimed with ≥200 chars of real body |
| 3 | EuropePMC `citedByCount: 0` was dropped by a truthiness check | `-C/--min-citations` and `-S citations` were no-ops | None-aware field merge; the floor is pushed down as `CITED:[N TO *]` |
| 4 | `-T RCT` filtered a ~30-record page client-side and usually returned **0** | Study-type filtering looked broken | Pushed down to `[pt]` (PubMed) and `PUB_TYPE:"…"` (EuropePMC) |
| 5 | Whitespace / operator-only queries were forwarded verbatim | Returned **unrelated** papers with no error | `validate_query()` rejects them with exit code 2 |
| 6 | `9500320[pmid]` was not recognised as PubMed syntax | Same junk-results failure for the most common agent query | Full qualifier list + `europepmc_query()` translation (`[pmid]`→`EXT_ID:`, `[tiab]`→`TITLE_ABS:`, …) |
| 7 | pymed returns `article.xml` as an **Element**, but the code guarded with `isinstance(xml, str)` | Every publication type, MeSH heading and abstract label was silently dropped — so retractions were invisible and `study_type`/`mesh_keywords` were mostly empty | `_xml_root()` normalises Element/str/bytes |
| 8 | Retracted papers were indistinguishable from valid evidence | Clinical safety | `warning: RETRACTED` / `EXPRESSION_OF_CONCERN` / `CORRECTED` on search + fetch |
| 9 | Structured abstract labels (AIMS/METHODS/RESULTS/CONCLUSION) were flattened | Clinicians could not read an abstract's structure | Labels preserved in search and fetch |
| 10 | Batch fetch returned `date: null` for ahead-of-print records | Unsortable, unverifiable citations | ESummary `pubdate`/`epubdate` fallback |
| 11 | `fetch -p abc123` hit the network and returned a metadata error | Wasted calls, confusing failure | PMID validation (digits only, ≤50 per batch), exit code 2 |
| 12 | `search-cache` passed raw text into FTS5 `MATCH` | Operator input (`AND`, `x OR y`) raised an SQL error | Query sanitised into quoted terms |

## New researcher commands

| Command | Purpose |
|---------|---------|
| `mesh <term>` | MeSH descriptor lookup with UI, tree numbers, scope note — build/validate a search strategy before running it |
| `related -p <pmid>` | Similar articles (PubMed neighbour links) — snowballing from a seed paper |
| `citedby -p <pmid>` | Forward citation chasing (EuropePMC citations) — who cites this paper |
| `refs -p <pmid>` | Backward citation chasing; falls back to OpenAlex when EuropePMC's `/references` returns 503 |
| `export -p <pmids> -F bibtex\|ris\|csv\|json [-o file]` | Reference-manager export; `--screen` adds PRISMA screening columns |
| `trials -q <term> [-s STATUS] [--full]` | ClinicalTrials.gov registry leg of a systematic review (with `totalCount`) |
| `watch -n <name> -q <query>` | Standing-query surveillance: reports only records not seen before (SQLite-backed baseline) |

## Commands (core)

### search

```bash
python3 med_search_cli.py search -q "lupus nephritis" -m 10 -f 2024-01-01 -S date
python3 med_search_cli.py search -q "ulcerative colitis advanced therapy" -m 8 -T RCT
python3 med_search_cli.py search -q "helicobacter pylori eradication" -m 8 -C 50
python3 med_search_cli.py search -q '"Gastric Antral Vascular Ectasia"[MeSH]' -m 5
python3 med_search_cli.py search -q "resmetirom MASH" -m 3 -V     # pool/filter diagnostics
```

| Flag | Default | Purpose |
|------|---------|---------|
| `--query` / `-q` | required | PubMed syntax is passed verbatim; plain keywords are `[TIAB]`-wrapped |
| `--max-results` / `-m` | `5` | Results returned (the fetch pool is 3×, then filtered) |
| `--from-date` / `-f`, `--to-date` / `-t` | none | `YYYY-MM-DD` bounds (validated, and order-checked) |
| `--sort` / `-S` | `relevance` | `relevance`, `date`, `citations` (citations re-sorted after merge) |
| `--min-citations` / `-C` | `0` | Citation floor, pushed down to EuropePMC |
| `--study-type` / `-T`, `--study-types` / `-U` | none | Single or comma-separated study types, pushed down to both APIs |
| `--verbose` / `-V` | off | stderr diagnostics: pool sizes, kept, dropped-for-missing-citations |

Record fields: `pmid`, `title`, `date`, `abstract`, `journal`, `cited_by`,
`authors`, `mesh_keywords`, `pub_types`, `doi`, `source`, `study_type`,
`in_pmc`, `in_epmc`, `is_open_access`, plus `warning` on retractions.

### fetch

```bash
python3 med_search_cli.py fetch -p 38261728 -s all -l 6000 --ttl 30
python3 med_search_cli.py fetch -p "38261728,38182299,38693734" -l 3000
```

Resolution order: PubMed Central OA XML → EuropePMC JATS XML → Unpaywall OA PDF
→ abstract. If a requested section does not exist, the abstract is returned with
a `note` rather than an empty body.

| Flag | Default | Purpose |
|------|---------|---------|
| `--pmid` / `-p` | required | One PMID or a comma-separated batch (≤50) |
| `--section` / `-s` | `all` | `all`, `abstract`, `intro`, `methods`, `results`, `discussion` |
| `--limit` / `-l` | `6000` | Character budget; triggers smart truncation |
| `--ttl` | `30` | Cache freshness in days (`0` forces a network refresh) |

### cache-stats, search-cache

```bash
python3 med_search_cli.py cache-stats
python3 med_search_cli.py search-cache -q "SGLT2 inhibitor cardiovascular"
```

## Install

```bash
pip install pymed click
export MED_SEARCH_EMAIL="you@example.org"     # NCBI/Unpaywall identification
export NCBI_API_KEY="..."                     # optional: 10 req/s instead of 3/s
alias med="python3 $PWD/med_search_cli.py"
```

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `NCBI_API_KEY` | none | NCBI E-utilities key (10 req/s vs 3/s) |
| `MED_SEARCH_EMAIL` | placeholder | Identification for NCBI/Unpaywall |
| `MED_SEARCH_DB` | `pubmed_agent_cache.db` | Cache path (cache + FTS5 + saved queries + watch state) |
| `MED_SEARCH_TTL` | `30` | Default cache TTL in days |
| `INSTITUTIONAL_PROXY_PREFIX` | none | Proxy prefix for DOI links (e.g. OpenAthens/EZProxy) |

## Output contract

* Single-line minified JSON on stdout; `{"error": "…"}` on stderr.
* Input errors (empty query, malformed PMID, bad date, reversed date range)
  exit **2**; upstream failures exit **1**; success exits **0**.
* Retracted records always carry `warning` — check it before quoting a paper.
* `note` explains degraded results (missing section, no OA text, fallback used).

## Study-type tags

`RCT`, `Meta-Analysis`, `Systematic Review`, `Review`, `Observational Study`,
`Case Reports`, `Clinical Trial`, `Practice Guideline`, `Unknown`.
Detection prefers MeSH publication types and falls back to title+abstract regex.

## Resilience

Exponential backoff with jitter (4 attempts, `429` honours `Retry-After`),
per-endpoint dynamic timeouts from rolling latency (clamped 4–30 s), SQLite
cache with FTS5 index, and background refresh of stale entries.

## Limits (read before citing)

* Full-text sections exist for roughly a third of PubMed (PMC OA coverage);
  everything else ends as `source: fallback` with `text_from: abstract`.
* `cited_by` is EuropePMC's count. Very recent records legitimately report `0`;
  `-C` therefore **drops** records with no citation data instead of passing
  them, and `-V` reports how many were dropped.
* `refs` depends on EuropePMC `/references`, which returns 503 during
  maintenance — the OpenAlex fallback keeps backward chasing working.
* This is a retrieval tool, not a systematic-review platform: no PRISMA flow
  diagram, dual screening, or risk-of-bias assessment. `export --screen`
  produces the screening spreadsheet to do that work elsewhere.

## AI-agent skill

`skills/med-search-cli/SKILL.md` is a ready-to-copy skill for Claude Code /
Hermes Agent:

```bash
cp -r skills/med-search-cli ~/.hermes/skills/
```

## License

MIT
