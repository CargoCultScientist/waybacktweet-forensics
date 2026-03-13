#!/usr/bin/env python3
"""
Forensic refresh for a cached Twitter Wayback dataset.

This script does not hit the network. It reloads the existing derived dataset,
reparses metadata-only rows from cached raw payloads using the latest extractor
heuristics, and rewrites the derived JSON/report outputs.
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = None
EXTRACTOR_PATH = REPO_ROOT / "scripts" / "extract_twitter_wayback.py"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Dataset directory produced by extract_twitter_wayback.py",
    )
    return parser.parse_args()


def load_extractor() -> Any:
    spec = importlib.util.spec_from_file_location("twitter_wayback_extractor", EXTRACTOR_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False))
            handle.write("\n")


def merge_recovered_row(existing: dict[str, Any], recovered: dict[str, Any]) -> dict[str, Any]:
    preserved = {
        key: existing[key]
        for key in [
            "capture_history",
            "capture_count",
            "first_capture_timestamp",
            "last_capture_timestamp",
            "first_capture_datetime",
            "last_capture_datetime",
            "snowflake_created_at",
            "account_handle_observed",
            "capture_inventory_originals",
            "timeline_capture_history",
            "timeline_capture_timestamp",
            "timeline_capture_datetime",
            "timeline_capture_url",
            "timeline_raw_payload_path",
            "timeline_only",
        ]
        if key in existing
    }
    merged = dict(existing)
    merged.update(recovered)
    merged.update(preserved)
    return merged


def recompute_summary(
    extractor: Any,
    summary: dict[str, Any],
    tweets: list[dict[str, Any]],
    profile_snapshots: list[dict[str, Any]],
) -> dict[str, Any]:
    refreshed = dict(summary)
    refreshed["generated_at_utc"] = (
        dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )
    refreshed["materialized_tweet_ids"] = len(tweets)
    refreshed["tweets_with_recovered_content"] = sum(1 for row in tweets if row.get("tweet_text"))
    refreshed["json_recoveries"] = sum(
        1 for row in tweets if row.get("recovery_source") == "json" and row.get("tweet_text")
    )
    refreshed["html_recoveries"] = sum(
        1 for row in tweets if row.get("recovery_source") == "html" and row.get("tweet_text")
    )
    refreshed["timeline_recoveries"] = sum(
        1 for row in tweets if row.get("recovery_source") == "timeline_html" and row.get("tweet_text")
    )
    refreshed["metadata_only_rows"] = sum(1 for row in tweets if row.get("source_quality") == "metadata_only")
    refreshed["profile_snapshot_count"] = len(profile_snapshots)
    refreshed["earliest_tweet_created_at"] = min(
        (row.get("tweet_created_at") for row in tweets if row.get("tweet_created_at")),
        default=None,
    )
    refreshed["latest_tweet_created_at"] = max(
        (row.get("tweet_created_at") for row in tweets if row.get("tweet_created_at")),
        default=None,
    )
    return refreshed


def main() -> None:
    args = parse_args()
    derived_dir = args.output_dir / "derived"
    dataset_path = derived_dir / "twitter_wayback_dataset.json"
    if not dataset_path.exists():
        raise FileNotFoundError(f"Missing dataset bundle: {dataset_path}")

    extractor = load_extractor()
    bundle = json.loads(dataset_path.read_text(encoding="utf-8"))
    summary = bundle["summary"]
    tweets = bundle["tweets"]
    profile_snapshots = bundle["profile_snapshots"]
    capture_index = bundle.get("capture_index", [])
    timeline_discovered_ids = bundle.get("timeline_discovered_tweet_ids", [])

    updated_tweets: list[dict[str, Any]] = []
    recovered_count = 0
    recovered_by_source: dict[str, int] = {}
    for idx, row in enumerate(tweets, start=1):
        refreshed = dict(row)
        raw_payload_path = row.get("raw_payload_path")
        should_reparse = (
            row.get("source_quality") == "metadata_only"
            and raw_payload_path
            and str(row.get("archive_capture_statuscode")) == "200"
            and "status_html" in str(raw_payload_path)
        )
        if should_reparse:
            raw_path = Path(raw_payload_path)
            if raw_path.exists():
                content = raw_path.read_text(encoding="utf-8", errors="replace")
                tweet_id = row["tweet_id"]
                likely_recoverable = any(
                    marker in content
                    for marker in [
                        f'data-tweet-id="{tweet_id}"',
                        f'data-item-id="{tweet_id}"',
                        f'data-associated-tweet-id="{tweet_id}"',
                        f'"id_str":"{tweet_id}"',
                        f'"rest_id":"{tweet_id}"',
                        'property="og:description"',
                        "window.__PREFETCH_DATA__",
                    ]
                )
                if likely_recoverable:
                    capture = {
                        "tweet_id": tweet_id,
                        "timestamp": row.get("archive_capture_timestamp"),
                        "original": row.get("tweet_url"),
                        "replay_url": row.get("archive_capture_url"),
                        "mimetype": row.get("archive_capture_mimetype") or "text/html",
                        "statuscode": row.get("archive_capture_statuscode") or "",
                        "raw_path": str(raw_path),
                    }
                    recovered, snapshot = extractor.parse_status_capture_candidate(content, capture)
                    if recovered is not None and recovered.get("tweet_text"):
                        refreshed = merge_recovered_row(row, recovered)
                        recovered_count += 1
                        source = refreshed.get("tweet_text_source") or "unknown"
                        recovered_by_source[source] = recovered_by_source.get(source, 0) + 1
                        if snapshot is not None:
                            profile_snapshots.append(snapshot)
        if idx % 100 == 0:
            print(f"[forensic-refresh] scanned {idx}/{len(tweets)} rows", flush=True)
        updated_tweets.append(refreshed)

    updated_tweets.sort(key=lambda row: (row.get("tweet_created_at") or "", row["tweet_id"]))
    profile_snapshots = extractor.dedupe_profile_snapshots(profile_snapshots)
    summary = recompute_summary(extractor, summary, updated_tweets, profile_snapshots)
    summary["forensic_refresh"] = {
        "refreshed_at_utc": summary["generated_at_utc"],
        "new_recoveries_from_cached_raw": recovered_count,
        "new_recoveries_by_tweet_text_source": recovered_by_source,
    }

    bundle = {
        "summary": summary,
        "tweets": updated_tweets,
        "profile_snapshots": profile_snapshots,
        "capture_index": capture_index,
        "timeline_discovered_tweet_ids": timeline_discovered_ids,
    }

    write_json(derived_dir / "extraction_summary.json", summary)
    write_json(derived_dir / "tweets_recovered_deduped.json", updated_tweets)
    write_jsonl(derived_dir / "tweets_recovered.jsonl", updated_tweets)
    write_json(derived_dir / "twitter_wayback_dataset.json", bundle)
    (derived_dir / "REPORT.md").write_text(
        extractor.build_report(summary=summary, tweets=updated_tweets, profile_snapshots=profile_snapshots),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
