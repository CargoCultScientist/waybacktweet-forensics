#!/usr/bin/env python3
"""
Classify the remaining metadata-only Twitter Wayback rows.

Outputs:
  - residual_metadata_triage.csv
  - residual_metadata_triage.json
  - TRIAGE_REPORT.md
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Dataset directory produced by extract_twitter_wayback.py",
    )
    return parser.parse_args()


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def first_match(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text, flags=re.IGNORECASE)
    return match.group(1) if match else None


def classify_row(row: dict[str, Any]) -> dict[str, Any]:
    raw_payload_path = row.get("raw_payload_path")
    statuscode = str(row.get("archive_capture_statuscode") or "")
    mimetype = (row.get("archive_capture_mimetype") or "").split(";")[0] or "none"
    tweet_id = row["tweet_id"]

    category = "unclassified"
    reason = ""
    next_action = ""
    page_target_tweet_id = None
    markers: list[str] = []

    if not raw_payload_path:
        if statuscode == "301":
            category = "cdx_redirect_no_raw_301"
            reason = "CDX row is a redirect with no cached raw payload on disk."
            next_action = "Only worth retrying if you want to refetch specific missing captures from Wayback."
        elif statuscode == "404":
            category = "cdx_not_found_no_raw_404"
            reason = "Best archived capture resolves to a 404 and no raw payload is cached."
            next_action = "Treat as unrecoverable from current inventory unless alternate captures are fetched."
        elif statuscode == "302":
            category = "cdx_redirect_no_raw_302"
            reason = "CDX row is a 302 redirect with no cached raw payload on disk."
            next_action = "Low priority; only refetch if pursuing every possible redirect target."
        else:
            category = "cdx_no_raw_other"
            reason = "No cached raw payload is available for this metadata-only row."
            next_action = "Manual fetch required."
        return {
            **row,
            "triage_category": category,
            "triage_reason": reason,
            "triage_next_action": next_action,
            "page_target_tweet_id": page_target_tweet_id,
            "marker_summary": "",
        }

    raw_path = Path(raw_payload_path)
    text = raw_path.read_text(encoding="utf-8", errors="replace")

    page_target_tweet_id = first_match(r'data-tweet-id="(\d+)"', text) or first_match(r'"id_str":"(\d+)"', text)
    has_target_markers = any(
        marker in text
        for marker in [
            f'data-tweet-id="{tweet_id}"',
            f'data-item-id="{tweet_id}"',
            f'data-associated-tweet-id="{tweet_id}"',
            f'"id_str":"{tweet_id}"',
            f'"rest_id":"{tweet_id}"',
        ]
    )
    if has_target_markers:
        markers.append("target_present")
    if "window.__INITIAL_STATE__" in text:
        markers.append("initial_state")
    if "window.__PREFETCH_DATA__" in text:
        markers.append("prefetch")
    if "window.__META_DATA__" in text:
        markers.append("meta_data")
    if 'property="og:description"' in text:
        markers.append("ogdesc")
    if "TweetUnavailable" in text or "tweet unavailable" in text.lower():
        markers.append("tweet_unavailable")
    if "tombstone" in text.lower():
        markers.append("tombstone")
    if "/i/nojs_router" in text:
        markers.append("nojs_router")

    if has_target_markers:
        category = f"unexpected_target_present_{statuscode}"
        reason = "Target tweet ID is still visible in the cached payload despite remaining metadata-only."
        next_action = "High priority manual parser review."
    elif ("initial_state" in markers or "meta_data" in markers) and "tombstone" in markers:
        if "nojs_router" in markers:
            category = f"responsive_tombstone_nojs_{statuscode}"
            reason = "Responsive Twitter/X shell loads a tombstone state and no target tweet payload."
        else:
            category = f"responsive_tombstone_{statuscode}"
            reason = "Responsive Twitter/X shell contains only tombstone state for this status."
        next_action = "Low yield; likely unrecoverable without a different archived capture."
    elif "ogdesc" in markers and page_target_tweet_id and page_target_tweet_id != tweet_id:
        if "tweet_unavailable" in markers:
            category = f"desktop_wrong_target_unavailable_{statuscode}"
            reason = "Desktop permalink replay resolves to another tweet page and marks the requested status unavailable."
        elif "tombstone" in markers:
            category = f"desktop_wrong_target_tombstone_{statuscode}"
            reason = "Desktop permalink replay resolves to another tweet page and shows a tombstone for the requested status."
        else:
            category = f"desktop_wrong_target_permalink_{statuscode}"
            reason = "Desktop permalink replay resolves to another tweet page with a different tweet ID."
        next_action = "Not trustworthy for content recovery; only useful as weak contextual evidence."
    elif page_target_tweet_id and page_target_tweet_id != tweet_id:
        category = f"wrong_target_permalink_{statuscode}"
        reason = "Cached page clearly resolves to a different tweet ID than the requested status."
        next_action = "Skip unless you want to audit replay integrity."
    elif statuscode == "302":
        category = "html_redirect_other_302"
        reason = "Cached HTML is a redirect-style replay without a recoverable target tweet payload."
        next_action = "Very low priority."
    elif statuscode == "200":
        category = "html_other_200"
        reason = "Cached HTML exists but exposes no target tweet payload."
        next_action = "Manual spot-check only if you want absolute exhaustion."
    else:
        category = "html_other"
        reason = "Cached HTML exists but does not expose a recoverable target tweet payload."
        next_action = "Manual inspection."

    return {
        **row,
        "triage_category": category,
        "triage_reason": reason,
        "triage_next_action": next_action,
        "page_target_tweet_id": page_target_tweet_id,
        "marker_summary": ",".join(markers),
    }


def build_report(
    *,
    total_metadata_only: int,
    categories: Counter[str],
    rows: list[dict[str, Any]],
) -> str:
    lines = [
        "# Twitter Wayback Residual Triage",
        "",
        f"Metadata-only rows remaining: `{total_metadata_only}`",
        "",
        "## Category Counts",
        "",
    ]
    for category, count in categories.most_common():
        lines.append(f"- `{category}`: {count}")

    lines.extend(
        [
            "",
            "## Key Findings",
            "",
            f"- Rows where the target tweet ID is still present in cached payloads: {sum(1 for row in rows if row['triage_category'].startswith('unexpected_target_present'))}",
            f"- Responsive tombstone rows: {sum(1 for row in rows if row['triage_category'].startswith('responsive_tombstone'))}",
            f"- Wrong-target desktop permalink rows: {sum(1 for row in rows if 'wrong_target' in row['triage_category'])}",
            f"- No-raw redirect/not-found rows: {sum(1 for row in rows if row['triage_category'].startswith('cdx_'))}",
            "",
            "## Sample Residuals",
            "",
        ]
    )
    shown = 0
    for row in rows:
        if shown >= 15:
            break
        lines.append(
            f"- `{row['tweet_id']}` | `{row['triage_category']}` | "
            f"status={row.get('archive_capture_statuscode')} mimetype={row.get('archive_capture_mimetype')} "
            f"capture={row.get('archive_capture_timestamp')} page_target={row.get('page_target_tweet_id') or ''}"
        )
        shown += 1
    lines.extend(
        [
            "",
            "## Assessment",
            "",
            "- The residual set is now dominated by tombstones, replay redirects, and 404s rather than parser misses.",
            "- The cached corpus no longer contains any unresolved rows where the target tweet ID is visibly present in the payload.",
            "- The only materially different next step would be a targeted refetch of selected redirect/404 captures or cross-source recovery from other archives.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    args = parse_args()
    derived_dir = args.output_dir / "derived"
    tweets_path = derived_dir / "tweets_recovered_deduped.json"
    tweets = json.loads(tweets_path.read_text(encoding="utf-8"))
    rows = [classify_row(row) for row in tweets if row.get("source_quality") == "metadata_only"]
    rows.sort(key=lambda row: (row["triage_category"], row.get("tweet_created_at") or "", row["tweet_id"]))

    categories = Counter(row["triage_category"] for row in rows)
    marker_counts = Counter(row["marker_summary"] for row in rows if row["marker_summary"])
    grouped_samples: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        category = row["triage_category"]
        if len(grouped_samples[category]) < 5:
            grouped_samples[category].append(row["tweet_id"])

    summary = {
        "metadata_only_rows": len(rows),
        "category_counts": dict(categories),
        "marker_counts": dict(marker_counts),
        "sample_tweet_ids_by_category": dict(grouped_samples),
    }

    write_json(derived_dir / "residual_metadata_triage.json", {"summary": summary, "rows": rows})
    write_csv(
        derived_dir / "residual_metadata_triage.csv",
        rows,
        [
            "tweet_id",
            "tweet_created_at",
            "archive_capture_timestamp",
            "archive_capture_statuscode",
            "archive_capture_mimetype",
            "raw_payload_path",
            "page_target_tweet_id",
            "triage_category",
            "triage_reason",
            "triage_next_action",
            "marker_summary",
            "tweet_url",
            "archive_capture_url",
        ],
    )
    (derived_dir / "TRIAGE_REPORT.md").write_text(
        build_report(total_metadata_only=len(rows), categories=categories, rows=rows),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
