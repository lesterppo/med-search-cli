---
name: med-search-cli
description: Search and fetch biomedical literature via PubMed + EuropePMC with smart truncation, retraction warnings, citation chasing, MeSH lookup, reference export, trials and standing-query surveillance. Use for literature search, paper retrieval, evidence synthesis, systematic-review screening, and citation lookups — especially when token efficiency matters.
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
    - citation chasing
    - citedby
    - MeSH
    - PRISMA
    - systematic review
    - FTS5
    - cached papers
  context:
    - User wants to search PubMed or EuropePMC for papers
    - User needs a paper's full text or abstract by PMID
    - User wants citation chasing (who cites it / what it cites)
    - User is building or validating a search strategy with MeSH terms
    - User needs BibTeX/RIS/CSV export for a reference manager
    - User wants to be told when a paper is retracted
    - User wants a standing query that reports only new records
    - Token-efficient literature retrieval for AI agent consumption
---

# Med Search CLI (v3)

Single-script twin-track **PubMed + EuropePMC** CLI. One canonical file — the
old v1/v2 scripts were merged into it.

Repo: https://github.com/lesterppo/med-search-cli
Local: `~/med-search-cli/med_search_cli.py`

```bash
med() { python3 ~/med-search-cli/med_search_cli.py "$@"; }
```

## Command map

| Need | Command |
|------|---------|
| Find papers | `search -q "…" -m 10` |
| Restrict by study design | `search -q "…" -T RCT` (also `-U "RCT,Meta-Analysis"`) |
| Restrict by date | `search -q "…" -f 2024-01-01 -t 2024-12-31 -S date` |
| High-impact only | `search -q "…" -C 50` |
| Get text | `fetch -p 38261728 -s all -l 6000` |
| Batch text | `fetch -p "1,2,3" -l 3000` |
| Validate a MeSH term | `mesh "gastric antral vascular ectasia"` |
| Similar papers | `related -p 38261728` |
| Who cites it | `citedby -p 38261728 -m 20` |
| What it cites | `refs -p 38261728 -m 20` |
| Reference manager export | `export -p "1,2,3" -F bibtex -o refs.bib` |
| PRISMA screening sheet | `export -p "1,2,3" -F csv --screen -o screen.csv` |
| Trial registry leg | `trials -q "resmetirom MASH" -s RECRUITING --full` |
| "What's new on my topic?" | `watch -n gave -q "gastric antral vascular ectasia"` |
| Already-cached full text | `search-cache -q "SGLT2 cardiovascular"` |
| Cache health | `cache-stats` |

## Token discipline

* All output is single-line minified JSON → pipe into `python3 -c` or `jq`, never
  re-print raw.
* Abstracts are the default payload (`-s abstract`); `-s all` for PMC OA text.
* Cache TTL defaults to 30 days — repeat fetches are free. `--ttl 0` forces a
  refresh; `--ttl 7` for fast-moving topics.
* `search` returns full records (title + abstract). Ask for metadata only via
  `export -F csv` when you do not need abstracts.

## Safety rules (clinical use)

1. **Check `warning` on every record.** `RETRACTED` /
   `EXPRESSION_OF_CONCERN` / `CORRECTED` appear on both search hits and fetch
   responses. Never quote a flagged paper as evidence.
2. **Missing citation data is not zero.** `--min-citations` deliberately drops
   records whose citation count is unknown (recent papers); `-V` reports the
   count dropped, so you can tell recall loss from a genuine empty result.
3. **Empty text is a failure signal, not "no data".** v3 always falls back to
   the abstract; if a response has neither `text` nor `note`, re-issue with
   `--ttl 0`.
4. **Exit codes matter.** `2` = your input was invalid (empty query, malformed
   PMID, reversed date range) — fix the call, do not retry the network.

## Query construction

```bash
# Plain keywords → wrapped as "kw"[TIAB] automatically
med search -q "helicobacter pylori eradication therapy" -m 5

# PubMed syntax → passed verbatim to NCBI
med search -q '("Gastric Antral Vascular Ectasia"[MeSH]) AND (2025[dp] : 2026[dp])' -m 10

# Single record by PMID (translated to EXT_ID: for the EuropePMC leg)
med search -q "9500320[pmid]"
```

Supported PubMed qualifiers are translated for EuropePMC (`[pmid]`→`EXT_ID:`,
`[tiab]`→`TITLE_ABS:`, `[mh]`→`MESH:`, `[pt]`→`PUB_TYPE:`, `[dp]`→`FIRST_PDATE:`,
`[au]`→`AUTH:`, `[ta]`→`JOURNAL:`); unsupported tags are stripped so they can no
longer drag unrelated records into the merge.

## Workflows

### Evidence question → cited answer

```bash
med search -q "inebilizumab IgG4-related disease" -m 10 -T RCT
med fetch -p 34003330 -s all -l 8000
med citedby -p 34003330 -m 20      # check what has challenged it since
```

### Systematic-review screening

```bash
med search -q "predictive factors gastric antral vascular ectasia" -m 50 -S date > hits.json
med export -p "$(python3 -c "import json;print(','.join(r['pmid'] for r in json.load(open('hits.json'))))")" \
    -F csv --screen -o screen.csv
# snowball
med refs    -p <seed> -m 30
med citedby -p <seed> -m 30
med trials  -q "<intervention> <condition>" --full
```

### Surveillance

```bash
med watch -n gave -q "gastric antral vascular ectasia" -m 20   # first run = baseline
med watch -n gave -m 20                                        # later runs: only new records
med watch -n x --list                                          # saved queries
med watch -n gave --forget                                     # delete
```

### Strategy building

```bash
med mesh "gastric antral vascular ectasia"
# → ui/tree/scope/entry_terms; then search with [MeSH] and compare hit counts
med search -q '"Gastric Antral Vascular Ectasia"[MeSH]' -m 5 -V
```

## Data sources

PubMed E-utilities, EuropePMC REST (+ JATS full text), PMC OA, Unpaywall,
NLM MeSH lookup, OpenAlex (reference-list fallback), ClinicalTrials.gov v2.

## Anti-patterns

* **Do not invent PMIDs.** Always take them from `search` output.
* **Do not loop `fetch` on the same PMID with different `--section` values** —
  the cache stores one entry per paper; slice locally instead.
* **Do not expect full text for every paper** (~⅓ PMC OA coverage). `source`
  tells you what resolved: `pubmed_ft`, `europepmc_ft`, `unpaywall`, `fallback`.
* **Do not treat this as a PRISMA engine** — it produces the screening sheet and
  the citation graph, not risk-of-bias judgements.
