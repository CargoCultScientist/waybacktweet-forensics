# Twitter Wayback Recovery

Portable extraction scripts for recovering deleted or suspended Twitter/X account content from the Wayback Machine.

This repository packages the recovery workflow as a standalone Python project for reusable Twitter/X Wayback analysis.

## Included Scripts

- `scripts/extract_twitter_wayback.py`
  Main extractor: inventories CDX, fetches raw captures, parses JSON/HTML/timeline snapshots, deduplicates tweets, and writes derived outputs.
- `scripts/forensic_refresh_twitter_wayback.py`
  Offline refresh pass for re-parsing cached raw payloads with updated extractor heuristics.
- `scripts/triage_twitter_wayback_residuals.py`
  Classifies metadata-only rows that remain unresolved after extraction.
- `scripts/twitter_wayback_to_csv.py`
  Converts recovered JSONL into a normalized CSV.

## Setup

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e .
```

Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .
```

## Typical Workflow

Run a fresh extraction for an account:

```bash
python scripts/extract_twitter_wayback.py --account someaccount
```

Outputs default to:

```text
data/twitter_wayback/<account-slug>/
```

Re-run the offline refresh on cached payloads:

```bash
python scripts/forensic_refresh_twitter_wayback.py --output-dir data/twitter_wayback/someaccount
```

Classify remaining metadata-only rows:

```bash
python scripts/triage_twitter_wayback_residuals.py --output-dir data/twitter_wayback/someaccount
```

Convert recovered JSONL to CSV:

```bash
python scripts/twitter_wayback_to_csv.py   --input-jsonl data/twitter_wayback/someaccount/derived/tweets_recovered.jsonl
```

## Output Layout

```text
data/twitter_wayback/<account-slug>/
  raw/
    cdx/
    status_json/
    status_html/
    status_misc/
    timeline_html/
  derived/
    extraction_summary.json
    tweets_recovered.jsonl
    tweets_recovered_deduped.json
    twitter_wayback_dataset.json
    tweet_capture_index.csv
    profile_snapshots.csv
    REPORT.md
```

## Notes

- The extractor is networked; the refresh and triage scripts are offline.
- Wayback rate limits aggressive querying. Use the built-in throttling and cached reruns.
- `docs/METHODOLOGY.md` includes an anonymized case study plus generalized recovery tactics.
