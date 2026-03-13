#!/usr/bin/env python3
"""
Convert recovered Wayback tweets (JSONL) into a normalized CSV.

Fields reconstructed from the recovered dataset:
  tweet_id, author_username, author_id, text, created_at,
  retweet_count, quote_count, reply_count, like_count,
  lang, conversation_id, in_reply_to_status_id, in_reply_to_screen_name,
  is_retweet, is_quote, is_reply, hashtags, mentions, urls,
  source_quality, recovery_source, archive_capture_datetime
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


COLUMNS = [
    "tweet_id",
    "author_username",
    "author_id",
    "author_name",
    "text",
    "created_at",
    "lang",
    "retweet_count",
    "like_count",
    "reply_count",
    "quote_count",
    "view_count",
    "conversation_id",
    "in_reply_to_status_id",
    "in_reply_to_screen_name",
    "is_retweet",
    "is_quote",
    "is_reply",
    "hashtags",
    "mentions",
    "urls",
    "tweet_url",
    "source_quality",
    "recovery_source",
    "archive_capture_datetime",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-jsonl", type=Path, required=True)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--include-metadata-only", action="store_true")
    return parser.parse_args()


def int_or_empty(value):
    if value is None:
        return ""
    try:
        return int(value)
    except (TypeError, ValueError):
        return ""


def flatten(row: dict) -> dict:
    user = row.get("user") or {}
    return {
        "tweet_id": row.get("tweet_id", ""),
        "author_username": user.get("screen_name", ""),
        "author_id": user.get("user_id", ""),
        "author_name": user.get("name", ""),
        "text": (row.get("tweet_text") or "").replace("\n", " ").replace("\r", " "),
        "created_at": row.get("tweet_created_at", ""),
        "lang": row.get("lang", ""),
        "retweet_count": int_or_empty(row.get("retweet_count")),
        "like_count": int_or_empty(row.get("favorite_count")),
        "reply_count": int_or_empty(row.get("reply_count")),
        "quote_count": int_or_empty(row.get("quote_count")),
        "view_count": "",
        "conversation_id": row.get("conversation_id") or "",
        "in_reply_to_status_id": row.get("in_reply_to_status_id") or "",
        "in_reply_to_screen_name": row.get("in_reply_to_screen_name") or "",
        "is_retweet": row.get("is_retweet", False),
        "is_quote": row.get("is_quote", False),
        "is_reply": row.get("is_reply", False),
        "hashtags": "|".join(row.get("hashtags") or []),
        "mentions": "|".join(row.get("mentions") or []),
        "urls": "|".join(row.get("expanded_urls") or []),
        "tweet_url": row.get("tweet_url", ""),
        "source_quality": row.get("source_quality", ""),
        "recovery_source": row.get("recovery_source", ""),
        "archive_capture_datetime": row.get("archive_capture_datetime", ""),
    }


def main() -> None:
    args = parse_args()
    output_csv = args.output_csv or args.input_jsonl.with_suffix(".csv")
    rows = []
    skipped = 0
    with args.input_jsonl.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            raw = json.loads(line)
            if not args.include_metadata_only and raw.get("source_quality") == "metadata_only" and not raw.get("tweet_text"):
                skipped += 1
                continue
            rows.append(flatten(raw))

    rows.sort(key=lambda row: row["created_at"])
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, quoting=csv.QUOTE_MINIMAL)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {len(rows)} tweets to {output_csv}")
    print(f"Skipped {skipped} metadata-only rows")
    if rows:
        print(f"Date range: {rows[0]['created_at']} to {rows[-1]['created_at']}")


if __name__ == "__main__":
    main()
