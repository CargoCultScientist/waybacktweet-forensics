#!/usr/bin/env python3
"""
Recover archived Twitter/X account content from the Wayback Machine.

The extractor:
  - saves raw CDX inventories for account and status URLs
  - fetches the best archived capture per tweet ID
  - parses JSON-backed captures first, HTML tweet pages second
  - keeps full capture provenance per tweet
  - derives dated profile snapshots from tweet JSON user objects and timeline pages
  - writes deduplicated datasets, CSV indexes, and a markdown report
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import json
import random
import re
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import requests


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_BASE_DIR = REPO_ROOT / "data" / "twitter_wayback"
WAYBACK_CDX_URL = "https://web.archive.org/cdx/search/cdx"
WAYBACK_REPLAY_PREFIX = "https://web.archive.org/web"
USER_AGENT = "twitter-wayback-recovery/0.1 (+https://github.com/your-org/twitter-wayback-recovery)"
TWITTER_EPOCH_MS = 1288834974657

TIMELINE_TARGETS = {"timeline_main", "timeline_lower", "timeline_mobile_main", "timeline_mobile_lower"}
STATUS_TARGETS = {"status_main", "status_lower", "status_mobile_main", "status_mobile_lower"}


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def account_slug(account: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", (account or "").strip().lower()).strip("-")
    return normalized or "account"


def default_output_dir(account: str) -> Path:
    return DEFAULT_OUTPUT_BASE_DIR / account_slug(account)


def build_cdx_targets(account: str) -> list[dict[str, str]]:
    account = (account or "").strip()
    if not account:
        raise ValueError("account is required")

    variants: list[tuple[str, str]] = [
        ("main", account),
        ("lower", account.lower()),
    ]
    targets: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for suffix, handle in variants:
        for prefix, host in [("timeline", "twitter.com"), ("timeline_mobile", "mobile.twitter.com")]:
            key = (prefix, handle)
            if key in seen:
                continue
            seen.add(key)
            targets.append({"name": f"{prefix}_{suffix}", "url": f"{host}/{handle}"})
        for prefix, host in [("status", "twitter.com"), ("status_mobile", "mobile.twitter.com")]:
            key = (prefix, handle)
            if key in seen:
                continue
            seen.add(key)
            targets.append({"name": f"{prefix}_{suffix}", "url": f"{host}/{handle}/status/*"})
    return targets


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True, help="Twitter/X account name to inventory")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Dataset directory. Default: data/twitter_wayback/<account_slug>",
    )
    parser.add_argument("--max-tweets", type=int, default=None, help="Limit tweet capture fetches for a partial run")
    parser.add_argument("--max-timeline-captures", type=int, default=None, help="Limit fetched timeline captures")
    parser.add_argument(
        "--max-capture-attempts-per-tweet",
        type=int,
        default=None,
        help="Limit ranked status captures tried per tweet. Use 1-2 for faster overview runs.",
    )
    parser.add_argument("--sleep-seconds", type=float, default=0.2, help="Base sleep between Wayback requests")
    parser.add_argument("--retries", type=int, default=4, help="Retry count for Wayback requests")
    parser.add_argument(
        "--checkpoint-every",
        type=int,
        default=50,
        help="Write partial derived outputs every N processed status tweet IDs. Use 0 to disable.",
    )
    parser.add_argument(
        "--overview-only",
        action="store_true",
        help="Inventory CDX and write an acquisition overview without fetching replay captures.",
    )
    parser.add_argument(
        "--refresh-existing",
        action="store_true",
        help="Re-fetch captures even if a raw file already exists",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Reuse cached CDX snapshots and raw captures without making network requests",
    )
    return parser.parse_args()


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_json(path: Path, data: Any) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    ensure_dir(path.parent)
    with path.open("w", encoding="utf-8", newline="") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False))
            f.write("\n")


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    ensure_dir(path.parent)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            cleaned: dict[str, Any] = {}
            for key, value in row.items():
                if isinstance(value, (list, dict)):
                    cleaned[key] = json.dumps(value, ensure_ascii=False)
                else:
                    cleaned[key] = value
            writer.writerow(cleaned)


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def sleep_with_jitter(seconds: float) -> None:
    time.sleep(seconds + random.uniform(0, max(seconds * 0.25, 0.05)))


def maybe_sleep(seconds: float, *, diagnostics: dict[str, Any], enabled: bool) -> None:
    if not enabled or seconds <= 0:
        return
    diagnostics["sleep_events"] += 1
    diagnostics["sleep_seconds_requested"] += seconds
    sleep_with_jitter(seconds)


def request_with_retries(
    session: requests.Session,
    method: str,
    url: str,
    *,
    retries: int,
    sleep_seconds: float,
    **kwargs: Any,
) -> requests.Response:
    timeout = kwargs.pop("timeout", 45)
    attempt = 0
    while True:
        attempt += 1
        try:
            resp = session.request(method, url, timeout=timeout, **kwargs)
            if resp.status_code in {429, 500, 502, 503, 504} and attempt <= retries:
                sleep_with_jitter(sleep_seconds * attempt)
                continue
            resp.raise_for_status()
            return resp
        except requests.RequestException:
            if attempt > retries:
                raise
            sleep_with_jitter(sleep_seconds * attempt)


def parse_cdx_rows(payload: Any) -> list[dict[str, str]]:
    if not isinstance(payload, list) or not payload:
        return []
    header = payload[0]
    rows: list[dict[str, str]] = []
    for raw_row in payload[1:]:
        if not isinstance(raw_row, list):
            continue
        row: dict[str, str] = {}
        for idx, key in enumerate(header):
            row[str(key)] = str(raw_row[idx]) if idx < len(raw_row) else ""
        rows.append(row)
    return rows


def fetch_cdx_inventory(
    session: requests.Session,
    target: dict[str, str],
    *,
    retries: int,
    sleep_seconds: float,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    params = {
        "url": target["url"],
        "output": "json",
        "fl": "timestamp,original,mimetype,statuscode,digest,length",
    }
    resp = request_with_retries(
        session,
        "GET",
        WAYBACK_CDX_URL,
        retries=retries,
        sleep_seconds=sleep_seconds,
        params=params,
    )
    payload = resp.json()
    rows = parse_cdx_rows(payload)
    snapshot = {
        "target": target,
        "queried_at_utc": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "request_url": resp.url,
        "row_count": len(rows),
        "rows": rows,
    }
    return rows, snapshot


def load_cdx_snapshot(path: Path) -> tuple[list[dict[str, str]], dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("rows", []) if isinstance(payload, dict) else []
    snapshot = payload if isinstance(payload, dict) else {"rows": rows}
    return rows, snapshot


def parse_capture_timestamp(value: str) -> dt.datetime:
    return dt.datetime.strptime(value, "%Y%m%d%H%M%S").replace(tzinfo=dt.timezone.utc)


def tweet_id_from_url(url: str) -> str | None:
    match = re.search(r"/status(?:es)?/(\d+)", url, flags=re.IGNORECASE)
    return match.group(1) if match else None


def snowflake_to_iso(tweet_id: str | int | None) -> str | None:
    if not tweet_id:
        return None
    try:
        created_ms = (int(tweet_id) >> 22) + TWITTER_EPOCH_MS
    except Exception:
        return None
    created = dt.datetime.fromtimestamp(created_ms / 1000, tz=dt.timezone.utc)
    return created.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def clean_url(url: str) -> str:
    parsed = urlparse(url)
    path = re.sub(r"/+", "/", parsed.path or "")
    return f"{parsed.netloc.lower()}{path}".rstrip("/")


def canonical_status_url(original_url: str, tweet_id: str) -> str:
    parsed = urlparse(original_url)
    path_parts = [part for part in parsed.path.split("/") if part]
    screen_name = path_parts[0] if path_parts else "unknown"
    return f"https://twitter.com/{screen_name}/status/{tweet_id}"


def replay_url(timestamp: str, original_url: str) -> str:
    return f"{WAYBACK_REPLAY_PREFIX}/{timestamp}id_/{original_url}"


def derive_extension(mimetype: str, content: str) -> str:
    lower = (mimetype or "").lower()
    stripped = content.lstrip()
    if "json" in lower or stripped.startswith("{") or stripped.startswith("["):
        return ".json"
    if "html" in lower or stripped.startswith("<!DOCTYPE html") or stripped.startswith("<html"):
        return ".html"
    return ".txt"


def strip_tags(fragment: str) -> str:
    fragment = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.IGNORECASE)
    fragment = re.sub(r"</p\s*>", "\n", fragment, flags=re.IGNORECASE)
    fragment = re.sub(r"<[^>]+>", "", fragment)
    fragment = html.unescape(fragment)
    fragment = maybe_fix_mojibake(fragment)
    fragment = fragment.replace("\u200b", "")
    fragment = re.sub(r"\s+\n", "\n", fragment)
    fragment = re.sub(r"\n{3,}", "\n\n", fragment)
    fragment = re.sub(r"[ \t]{2,}", " ", fragment)
    return fragment.strip()


def regex_first(patterns: list[str], text: str, *, flags: int = 0) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, text, flags)
        if match:
            return match.group(1)
    return None


def extract_html_meta(text: str, attr: str, value: str) -> str | None:
    pattern = rf"<meta[^>]+{attr}=[\"']{re.escape(value)}[\"'][^>]+content=[\"'](.*?)[\"']"
    match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
    if match:
        return maybe_fix_mojibake(html.unescape(match.group(1)).strip())
    pattern = rf"<meta[^>]+content=[\"'](.*?)[\"'][^>]+{attr}=[\"']{re.escape(value)}[\"']"
    match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
    if match:
        return maybe_fix_mojibake(html.unescape(match.group(1)).strip())
    return None


def maybe_fix_mojibake(text: str | None) -> str | None:
    if not text:
        return text
    if not any(marker in text for marker in ("Ã", "â€", "â€™", "â€œ", "â€\x9d", "ðŸ", "Â")):
        return text
    try:
        fixed = text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text
    return fixed if fixed else text


def parse_twitter_created_at(value: str | None) -> str | None:
    if not value:
        return None
    for fmt in ("%a %b %d %H:%M:%S %z %Y", "%a %b %d %H:%M:%S %Y"):
        try:
            parsed = dt.datetime.strptime(value, fmt)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return parsed.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        except ValueError:
            continue
    return None


def parse_epoch_seconds(value: str | None) -> str | None:
    if not value:
        return None
    try:
        seconds = int(value)
    except ValueError:
        return None
    created = dt.datetime.fromtimestamp(seconds, tz=dt.timezone.utc)
    return created.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def flatten_entities(entities: dict[str, Any] | None) -> dict[str, Any]:
    entities = entities or {}
    hashtags = [item.get("text") for item in entities.get("hashtags", []) if isinstance(item, dict) and item.get("text")]
    mentions = [item.get("screen_name") for item in entities.get("user_mentions", []) if isinstance(item, dict) and item.get("screen_name")]
    urls: list[str] = []
    expanded_urls: list[str] = []
    for item in entities.get("urls", []):
        if not isinstance(item, dict):
            continue
        if item.get("url"):
            urls.append(item["url"])
        if item.get("expanded_url"):
            expanded_urls.append(item["expanded_url"])
    media_urls: list[str] = []
    media_expanded_urls: list[str] = []
    for item in entities.get("media", []):
        if not isinstance(item, dict):
            continue
        if item.get("media_url_https"):
            media_urls.append(item["media_url_https"])
        elif item.get("media_url"):
            media_urls.append(item["media_url"])
        if item.get("expanded_url"):
            media_expanded_urls.append(item["expanded_url"])
    return {
        "hashtags": sorted(dict.fromkeys(hashtags)),
        "mentions": sorted(dict.fromkeys(mentions)),
        "urls": sorted(dict.fromkeys(urls)),
        "expanded_urls": sorted(dict.fromkeys(expanded_urls)),
        "media_urls": sorted(dict.fromkeys(media_urls)),
        "media_expanded_urls": sorted(dict.fromkeys(media_expanded_urls)),
    }


def user_snapshot_from_json(user: dict[str, Any] | None, archive_timestamp: str) -> dict[str, Any] | None:
    if not isinstance(user, dict):
        return None
    return {
        "snapshot_timestamp": parse_capture_timestamp(archive_timestamp).isoformat().replace("+00:00", "Z"),
        "source_type": "tweet_json",
        "user_id": str(user.get("id_str") or user.get("id") or "") or None,
        "screen_name": user.get("screen_name"),
        "name": user.get("name"),
        "description": user.get("description"),
        "location": user.get("location"),
        "url": user.get("url"),
        "created_at": parse_twitter_created_at(user.get("created_at")),
        "followers_count": user.get("followers_count"),
        "friends_count": user.get("friends_count"),
        "listed_count": user.get("listed_count"),
        "favourites_count": user.get("favourites_count"),
        "statuses_count": user.get("statuses_count"),
        "verified": user.get("verified"),
        "protected": user.get("protected"),
        "profile_image_url_https": user.get("profile_image_url_https") or user.get("profile_image_url"),
        "profile_banner_url": user.get("profile_banner_url"),
        "profile_background_image_url_https": user.get("profile_background_image_url_https")
        or user.get("profile_background_image_url"),
        "profile_link_color": user.get("profile_link_color"),
        "profile_sidebar_fill_color": user.get("profile_sidebar_fill_color"),
        "profile_text_color": user.get("profile_text_color"),
        "geo_enabled": user.get("geo_enabled"),
        "lang": user.get("lang"),
    }


def extract_text_from_json_tweet(tweet: dict[str, Any]) -> str | None:
    if isinstance(tweet.get("full_text"), str) and tweet["full_text"].strip():
        return tweet["full_text"].strip()
    extended = tweet.get("extended_tweet")
    if isinstance(extended, dict) and isinstance(extended.get("full_text"), str) and extended["full_text"].strip():
        return extended["full_text"].strip()
    if isinstance(tweet.get("text"), str) and tweet["text"].strip():
        return tweet["text"].strip()
    return None


def parse_status_json_capture(content: str, capture: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return None, None

    if not isinstance(payload, dict):
        return None, None
    tweet = payload
    tweet_id = str(tweet.get("id_str") or tweet.get("id") or "") or capture["tweet_id"]
    entities = flatten_entities(tweet.get("extended_tweet", {}).get("entities") if isinstance(tweet.get("extended_tweet"), dict) else None)
    base_entities = flatten_entities(tweet.get("entities"))
    for key in entities:
        entities[key] = sorted(dict.fromkeys(base_entities[key] + entities[key]))
    extended_entities = tweet.get("extended_entities")
    if isinstance(extended_entities, dict):
        media_urls = entities.get("media_urls", [])
        media_expanded_urls = entities.get("media_expanded_urls", [])
        for item in extended_entities.get("media", []):
            if not isinstance(item, dict):
                continue
            media_url = item.get("media_url_https") or item.get("media_url")
            expanded = item.get("expanded_url")
            if media_url:
                media_urls.append(media_url)
            if expanded:
                media_expanded_urls.append(expanded)
        entities["media_urls"] = sorted(dict.fromkeys(media_urls))
        entities["media_expanded_urls"] = sorted(dict.fromkeys(media_expanded_urls))

    canonical = {
        "tweet_id": tweet_id,
        "tweet_url": canonical_status_url(capture["original"], tweet_id),
        "tweet_created_at": parse_twitter_created_at(tweet.get("created_at")) or snowflake_to_iso(tweet_id),
        "tweet_created_at_from": "payload" if tweet.get("created_at") else "snowflake",
        "tweet_text": extract_text_from_json_tweet(tweet),
        "tweet_text_source": "json",
        "lang": tweet.get("lang"),
        "source": strip_tags(tweet.get("source") or "") or None,
        "truncated": tweet.get("truncated"),
        "is_retweet": bool(tweet.get("retweeted_status")) or (extract_text_from_json_tweet(tweet) or "").startswith("RT @"),
        "is_quote": bool(tweet.get("is_quote_status")) or bool(tweet.get("quoted_status_id") or tweet.get("quoted_status_id_str")),
        "is_reply": bool(tweet.get("in_reply_to_status_id") or tweet.get("in_reply_to_status_id_str")),
        "in_reply_to_status_id": str(tweet.get("in_reply_to_status_id_str") or tweet.get("in_reply_to_status_id") or "") or None,
        "in_reply_to_user_id": str(tweet.get("in_reply_to_user_id_str") or tweet.get("in_reply_to_user_id") or "") or None,
        "in_reply_to_screen_name": tweet.get("in_reply_to_screen_name"),
        "conversation_id": str(tweet.get("conversation_id_str") or tweet.get("conversation_id") or "") or None,
        "retweet_count": tweet.get("retweet_count"),
        "favorite_count": tweet.get("favorite_count"),
        "quote_count": tweet.get("quote_count"),
        "reply_count": tweet.get("reply_count"),
        "possibly_sensitive": tweet.get("possibly_sensitive"),
        "hashtags": entities["hashtags"],
        "mentions": entities["mentions"],
        "urls": entities["urls"],
        "expanded_urls": entities["expanded_urls"],
        "media_urls": entities["media_urls"],
        "media_expanded_urls": entities["media_expanded_urls"],
        "user": {
            "user_id": str(tweet.get("user", {}).get("id_str") or tweet.get("user", {}).get("id") or "") or None,
            "screen_name": tweet.get("user", {}).get("screen_name"),
            "name": tweet.get("user", {}).get("name"),
            "description": tweet.get("user", {}).get("description"),
            "location": tweet.get("user", {}).get("location"),
            "verified": tweet.get("user", {}).get("verified"),
        },
        "retweeted_status_id": str(
            tweet.get("retweeted_status", {}).get("id_str") or tweet.get("retweeted_status", {}).get("id") or ""
        )
        or None,
        "quoted_status_id": str(tweet.get("quoted_status_id_str") or tweet.get("quoted_status_id") or "") or None,
        "archive_capture_timestamp": capture["timestamp"],
        "archive_capture_datetime": parse_capture_timestamp(capture["timestamp"]).isoformat().replace("+00:00", "Z"),
        "archive_capture_url": capture["replay_url"],
        "archive_capture_mimetype": capture["mimetype"],
        "archive_capture_statuscode": capture["statuscode"],
        "recovery_source": "json",
        "source_quality": "json",
        "raw_payload_path": capture["raw_path"],
    }
    snapshot = user_snapshot_from_json(tweet.get("user"), capture["timestamp"])
    return canonical, snapshot


def extract_target_tweet_block(content: str, tweet_id: str) -> str | None:
    markers = [
        f'data-tweet-id="{tweet_id}"',
        f'data-item-id="{tweet_id}"',
        f'data-associated-tweet-id="{tweet_id}"',
        f'"id_str":"{tweet_id}"',
        f'"rest_id":"{tweet_id}"',
    ]
    idx = min((content.find(marker) for marker in markers if content.find(marker) >= 0), default=-1)
    if idx < 0:
        return None
    start_candidates = [
        content.rfind('<div class="tweet ', 0, idx),
        content.rfind('<div class="tweet\n', 0, idx),
        content.rfind('<div class="permalink-inner', 0, idx),
        content.rfind('<li class="js-stream-item', 0, idx),
    ]
    start = max(candidate for candidate in start_candidates if candidate >= 0) if any(candidate >= 0 for candidate in start_candidates) else max(0, idx - 12000)
    end_markers = [
        content.find("</li>", idx),
        content.find('<ol class="hidden-replies-container">', idx),
        content.find('<div class="stream-item-footer"', idx),
        content.find('<div class="permalink-replies', idx),
    ]
    end_candidates = [candidate for candidate in end_markers if candidate >= 0]
    end = min(end_candidates) if end_candidates else min(len(content), idx + 24000)
    return content[start:end]


def extract_assignment_json(content: str, names: list[str]) -> Any | None:
    def extract_balanced_object(text: str, brace_idx: int) -> str | None:
        depth = 0
        in_string = False
        quote = ""
        escape = False
        for idx in range(brace_idx, len(text)):
            ch = text[idx]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == quote:
                    in_string = False
                continue
            if ch in {'"', "'"}:
                in_string = True
                quote = ch
                continue
            if ch == "{":
                depth += 1
                continue
            if ch == "}":
                depth -= 1
                if depth == 0:
                    return text[brace_idx : idx + 1]
        return None

    for name in names:
        for needle in (f"window.{name}", name):
            pos = content.find(needle)
            while pos >= 0:
                brace_idx = content.find("{", pos)
                if brace_idx >= 0:
                    payload = extract_balanced_object(content, brace_idx)
                    if payload:
                        try:
                            return json.loads(payload)
                        except json.JSONDecodeError:
                            pass
                pos = content.find(needle, pos + len(needle))
    return None


def iter_nested_dicts(root: Any) -> Iterable[dict[str, Any]]:
    stack = [root]
    seen: set[int] = set()
    while stack:
        current = stack.pop()
        current_id = id(current)
        if current_id in seen:
            continue
        seen.add(current_id)
        if isinstance(current, dict):
            yield current
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)


def find_embedded_user_dict(root: Any, user_id: str | None) -> dict[str, Any] | None:
    if not user_id:
        return None
    for candidate in iter_nested_dicts(root):
        candidate_id = str(candidate.get("id_str") or candidate.get("id") or candidate.get("rest_id") or "") or None
        if candidate_id != user_id:
            continue
        if candidate.get("screen_name") or candidate.get("name"):
            return candidate
        legacy = candidate.get("legacy")
        if isinstance(legacy, dict) and (legacy.get("screen_name") or legacy.get("name")):
            normalized = dict(legacy)
            normalized["id_str"] = candidate_id
            return normalized
    return None


def extract_embedded_tweet_payload(content: str, tweet_id: str) -> dict[str, Any] | None:
    for root_name in ["__PREFETCH_DATA__", "__INITIAL_STATE__", "__META_DATA__"]:
        root = extract_assignment_json(content, [root_name])
        if root is None:
            continue
        for candidate in iter_nested_dicts(root):
            candidate_id = str(candidate.get("id_str") or candidate.get("id") or candidate.get("rest_id") or "") or None
            if candidate_id != tweet_id:
                continue

            if isinstance(candidate.get("legacy"), dict):
                tweet = dict(candidate["legacy"])
                tweet["id_str"] = tweet_id
                core = candidate.get("core")
                if isinstance(core, dict):
                    user_result = ((core.get("user_results") or {}).get("result")) if isinstance(core.get("user_results"), dict) else None
                    if isinstance(user_result, dict):
                        user_legacy = user_result.get("legacy")
                        if isinstance(user_legacy, dict):
                            user = dict(user_legacy)
                            user["id_str"] = str(user_result.get("rest_id") or user_result.get("id") or user.get("id_str") or "") or None
                            tweet["user"] = user
                if not tweet.get("user") and tweet.get("user_id_str"):
                    user = find_embedded_user_dict(root, str(tweet.get("user_id_str")))
                    if user:
                        tweet["user"] = user
                return tweet

            if any(key in candidate for key in ("full_text", "text", "created_at", "user_id_str", "user")):
                tweet = dict(candidate)
                tweet["id_str"] = tweet_id
                if not isinstance(tweet.get("user"), dict):
                    user_id = str(tweet.get("user_id_str") or tweet.get("user_id") or "") or None
                    user = find_embedded_user_dict(root, user_id)
                    if user:
                        tweet["user"] = user
                return tweet
    return None


def parse_status_html_embedded_json_capture(
    content: str,
    capture: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    tweet = extract_embedded_tweet_payload(content, capture["tweet_id"])
    if tweet is None:
        return None, None
    canonical, snapshot = parse_status_json_capture(json.dumps(tweet, ensure_ascii=False), capture)
    if canonical is None:
        return None, None
    canonical["tweet_text_source"] = "html_embedded_json"
    canonical["recovery_source"] = "html"
    canonical["source_quality"] = "html" if canonical.get("tweet_text") else "metadata_only"
    canonical["archive_capture_mimetype"] = capture["mimetype"]
    if snapshot is not None:
        snapshot["source_type"] = "tweet_html_embedded_json"
    return canonical, snapshot


def parse_status_html_capture(content: str, capture: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    tweet_id = capture["tweet_id"]
    target_block = extract_target_tweet_block(content, tweet_id) or content
    text_html = regex_first(
        [
            r'<p class="TweetTextSize[^"]*?tweet-text[^"]*"[^>]*>(.*?)</p>',
            r'<div class="js-tweet-text-container[^"]*"[^>]*>\s*<p[^>]*>(.*?)</p>',
            r'<div class="tweet-text"[^>]*>\s*(.*?)\s*</div>',
        ],
        target_block,
        flags=re.IGNORECASE | re.DOTALL,
    )
    og_description = extract_html_meta(content, "property", "og:description")
    tweet_text = strip_tags(text_html) if text_html else None
    if not tweet_text and og_description:
        tweet_text = og_description.strip().strip("“”\"")

    data_time_ms = regex_first([r'data-time-ms="(\d+)"'], target_block, flags=re.IGNORECASE)
    created_at = None
    created_from = None
    if data_time_ms:
        try:
            created = dt.datetime.fromtimestamp(int(data_time_ms) / 1000, tz=dt.timezone.utc)
            created_at = created.replace(microsecond=0).isoformat().replace("+00:00", "Z")
            created_from = "data-time-ms"
        except ValueError:
            created_at = None
    if not created_at:
        created_at = parse_epoch_seconds(regex_first([r'data-time="(\d+)"'], target_block, flags=re.IGNORECASE))
        if created_at:
            created_from = "data-time"
    if not created_at:
        created_at = snowflake_to_iso(tweet_id)
        created_from = "snowflake"

    expanded_urls = sorted(
        dict.fromkeys(
            re.findall(r'data-expanded-url="([^"]+)"', target_block, flags=re.IGNORECASE)
            + re.findall(r'data-url="([^"]+)"', target_block, flags=re.IGNORECASE)
        )
    )
    media_urls = sorted(
        dict.fromkeys(
            re.findall(r'https://pbs\.twimg\.com/media/[^"\']+', target_block, flags=re.IGNORECASE)
            + re.findall(r'https://pbs\.twimg\.com/profile_images/[^"\']+', target_block, flags=re.IGNORECASE)
        )
    )
    hashtags = sorted(dict.fromkeys(re.findall(r"#([A-Za-z0-9_]+)", tweet_text or "")))
    mentions = sorted(dict.fromkeys(re.findall(r"@([A-Za-z0-9_]{1,15})", tweet_text or "")))
    user_name = regex_first(
        [
            r'data-name="(.*?)"',
            r'<div class="fullname">\s*.*?<strong>(.*?)</strong>',
            r'<span class="attr-fullname">(.*?)</span>',
        ],
        target_block,
        flags=re.IGNORECASE | re.DOTALL,
    )
    screen_name = regex_first(
        [
            r'data-screen-name="(.*?)"',
            r'<span class="username">\s*<span>@</span>\s*([^<]+)</span>',
            r'<span class="attr-username">@([^<]+)</span>',
        ],
        target_block,
        flags=re.IGNORECASE | re.DOTALL,
    )
    user_id = regex_first([r'data-user-id="(\d+)"'], target_block, flags=re.IGNORECASE)
    is_reply = bool(re.search(r'data-is-reply-to="true"', target_block, flags=re.IGNORECASE))
    is_quote = bool(re.search(r'quoted-tweet|QuoteTweet', target_block, flags=re.IGNORECASE))
    is_retweet = (
        (tweet_text or "").startswith("RT @")
        or bool(re.search(r'Retweeted|retweet-id|retweeter', target_block, flags=re.IGNORECASE))
    )

    canonical = {
        "tweet_id": tweet_id,
        "tweet_url": canonical_status_url(capture["original"], tweet_id),
        "tweet_created_at": created_at,
        "tweet_created_at_from": created_from,
        "tweet_text": tweet_text,
        "tweet_text_source": "html",
        "lang": regex_first([r'<p class="TweetTextSize[^"]*"[^>]*lang="([^"]+)"'], target_block, flags=re.IGNORECASE),
        "source": None,
        "truncated": None,
        "is_retweet": is_retweet,
        "is_quote": is_quote,
        "is_reply": is_reply,
        "in_reply_to_status_id": regex_first([r'data-conversation-id="(\d+)"'], target_block, flags=re.IGNORECASE),
        "in_reply_to_user_id": None,
        "in_reply_to_screen_name": None,
        "conversation_id": regex_first([r'data-conversation-id="(\d+)"'], target_block, flags=re.IGNORECASE),
        "retweet_count": None,
        "favorite_count": None,
        "quote_count": None,
        "reply_count": None,
        "possibly_sensitive": None,
        "hashtags": hashtags,
        "mentions": mentions,
        "urls": [],
        "expanded_urls": expanded_urls,
        "media_urls": media_urls,
        "media_expanded_urls": [],
        "user": {
            "user_id": user_id,
            "screen_name": html.unescape(screen_name) if screen_name else None,
            "name": strip_tags(user_name) if user_name else None,
            "description": None,
            "location": None,
            "verified": None,
        },
        "retweeted_status_id": None,
        "quoted_status_id": None,
        "archive_capture_timestamp": capture["timestamp"],
        "archive_capture_datetime": parse_capture_timestamp(capture["timestamp"]).isoformat().replace("+00:00", "Z"),
        "archive_capture_url": capture["replay_url"],
        "archive_capture_mimetype": capture["mimetype"],
        "archive_capture_statuscode": capture["statuscode"],
        "recovery_source": "html",
        "source_quality": "html" if tweet_text else "metadata_only",
        "raw_payload_path": capture["raw_path"],
    }

    snapshot = None
    if screen_name or user_name:
        snapshot = {
            "snapshot_timestamp": parse_capture_timestamp(capture["timestamp"]).isoformat().replace("+00:00", "Z"),
            "source_type": "tweet_html",
            "user_id": user_id,
            "screen_name": html.unescape(screen_name) if screen_name else None,
            "name": strip_tags(user_name) if user_name else None,
            "description": None,
            "location": None,
            "url": None,
            "created_at": None,
            "followers_count": None,
            "friends_count": None,
            "listed_count": None,
            "favourites_count": None,
            "statuses_count": None,
            "verified": None,
            "protected": None,
            "profile_image_url_https": None,
            "profile_banner_url": None,
            "profile_background_image_url_https": None,
            "profile_link_color": None,
            "profile_sidebar_fill_color": None,
            "profile_text_color": None,
            "geo_enabled": None,
            "lang": canonical["lang"],
        }
    return canonical, snapshot


def build_timeline_tweet_record(block: str, capture: dict[str, Any]) -> dict[str, Any] | None:
    tweet_id = regex_first([r'data-item-id="(\d+)"', r'data-tweet-id="(\d+)"'], block, flags=re.IGNORECASE)
    if not tweet_id or len(tweet_id) < 8:
        return None
    text_html = regex_first(
        [
            r'<p class="TweetTextSize[^"]*"[^>]*>(.*?)</p>',
            r'<div class="js-tweet-text-container[^"]*"[^>]*>\s*<p[^>]*>(.*?)</p>',
        ],
        block,
        flags=re.IGNORECASE | re.DOTALL,
    )
    tweet_text = strip_tags(text_html) if text_html else None
    if not tweet_text:
        return None
    data_time_ms = regex_first([r'data-time-ms="(\d+)"'], block, flags=re.IGNORECASE)
    created_at = None
    created_from = None
    if data_time_ms:
        try:
            created = dt.datetime.fromtimestamp(int(data_time_ms) / 1000, tz=dt.timezone.utc)
            created_at = created.replace(microsecond=0).isoformat().replace("+00:00", "Z")
            created_from = "data-time-ms"
        except ValueError:
            created_at = None
    if not created_at:
        created_at = parse_epoch_seconds(regex_first([r'data-time="(\d+)"'], block, flags=re.IGNORECASE))
        if created_at:
            created_from = "data-time"
    if not created_at:
        created_at = snowflake_to_iso(tweet_id)
        created_from = "snowflake"

    screen_name = regex_first([r'data-screen-name="(.*?)"'], block, flags=re.IGNORECASE)
    user_name = regex_first([r'data-name="(.*?)"'], block, flags=re.IGNORECASE)
    user_id = regex_first([r'data-user-id="(\d+)"'], block, flags=re.IGNORECASE)
    expanded_urls = sorted(dict.fromkeys(re.findall(r'data-expanded-url="([^"]+)"', block, flags=re.IGNORECASE)))
    media_urls = sorted(dict.fromkeys(re.findall(r'https://pbs\.twimg\.com/media/[^"\']+', block, flags=re.IGNORECASE)))
    hashtags = sorted(dict.fromkeys(re.findall(r"#([A-Za-z0-9_]+)", tweet_text)))
    mentions = sorted(dict.fromkeys(re.findall(r"@([A-Za-z0-9_]{1,15})", tweet_text)))
    return {
        "tweet_id": tweet_id,
        "tweet_url": canonical_status_url(
            f"https://twitter.com/{html.unescape(screen_name) if screen_name else 'unknown'}/status/{tweet_id}",
            tweet_id,
        ),
        "tweet_created_at": created_at,
        "tweet_created_at_from": created_from,
        "tweet_text": tweet_text,
        "tweet_text_source": "timeline_html",
        "lang": regex_first([r'<p class="TweetTextSize[^"]*"[^>]*lang="([^"]+)"'], block, flags=re.IGNORECASE),
        "source": None,
        "truncated": None,
        "is_retweet": bool(re.search(r'data-retweet-id="(\d+)"', block, flags=re.IGNORECASE)) or tweet_text.startswith("RT @"),
        "is_quote": bool(re.search(r'quoted-tweet|QuoteTweet|data-quoted-tweet-id=', block, flags=re.IGNORECASE)),
        "is_reply": bool(re.search(r'data-is-reply-to="true"', block, flags=re.IGNORECASE)),
        "in_reply_to_status_id": regex_first([r'data-conversation-id="(\d+)"'], block, flags=re.IGNORECASE),
        "in_reply_to_user_id": None,
        "in_reply_to_screen_name": None,
        "conversation_id": regex_first([r'data-conversation-id="(\d+)"'], block, flags=re.IGNORECASE),
        "retweet_count": None,
        "favorite_count": None,
        "quote_count": None,
        "reply_count": None,
        "possibly_sensitive": None,
        "hashtags": hashtags,
        "mentions": mentions,
        "urls": [],
        "expanded_urls": expanded_urls,
        "media_urls": media_urls,
        "media_expanded_urls": [],
        "user": {
            "user_id": user_id,
            "screen_name": html.unescape(screen_name) if screen_name else None,
            "name": html.unescape(user_name) if user_name else None,
            "description": None,
            "location": None,
            "verified": None,
        },
        "retweeted_status_id": regex_first([r'data-retweet-id="(\d+)"'], block, flags=re.IGNORECASE),
        "quoted_status_id": regex_first([r'data-quoted-tweet-id="(\d+)"'], block, flags=re.IGNORECASE),
        "archive_capture_timestamp": capture["timestamp"],
        "archive_capture_datetime": parse_capture_timestamp(capture["timestamp"]).isoformat().replace("+00:00", "Z"),
        "archive_capture_url": capture["replay_url"],
        "archive_capture_mimetype": capture["mimetype"],
        "archive_capture_statuscode": capture["statuscode"],
        "recovery_source": "timeline_html",
        "source_quality": "timeline_html",
        "raw_payload_path": capture["raw_path"],
    }


def extract_timeline_tweets(content: str, capture: dict[str, Any], account: str) -> list[dict[str, Any]]:
    matches = list(re.finditer(r'data-item-id="(\d+)"', content, flags=re.IGNORECASE))
    tweets: list[dict[str, Any]] = []
    account_lower = account.lower()
    seen: set[str] = set()
    for idx, match in enumerate(matches):
        start = match.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else min(len(content), start + 12000)
        block = content[start:end]
        candidate = build_timeline_tweet_record(block, capture)
        if not candidate:
            continue
        screen_name = (candidate.get("user", {}) or {}).get("screen_name")
        if not screen_name or screen_name.lower() != account_lower:
            continue
        if candidate["tweet_id"] in seen:
            continue
        seen.add(candidate["tweet_id"])
        tweets.append(candidate)
    return tweets


def parse_timeline_capture(content: str, capture: dict[str, Any], account: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    title = regex_first([r"<title>(.*?)</title>"], content, flags=re.IGNORECASE | re.DOTALL)
    name_html = regex_first([r'ProfileHeaderCard-nameLink[^>]*>(.*?)</a>'], content, flags=re.IGNORECASE | re.DOTALL)
    bio_html = regex_first([r'ProfileHeaderCard-bio[^>]*>(.*?)</p>'], content, flags=re.IGNORECASE | re.DOTALL)
    location_html = regex_first([r'ProfileHeaderCard-locationText[^>]*>(.*?)</span>'], content, flags=re.IGNORECASE | re.DOTALL)
    url_html = regex_first([r'ProfileHeaderCard-urlText[^>]*>.*?<a[^>]+title="([^"]+)"'], content, flags=re.IGNORECASE | re.DOTALL)
    join_html = regex_first([r'ProfileHeaderCard-joinDateText[^>]+title="([^"]+)"'], content, flags=re.IGNORECASE)
    profile_image = regex_first(
        [
            r'ProfileAvatar-image[^>]+src="([^"]+)"',
            r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"',
        ],
        content,
        flags=re.IGNORECASE,
    )
    description = strip_tags(bio_html) if bio_html else extract_html_meta(content, "property", "og:description")
    count_matches = re.findall(
        r'data-nav="(tweets|following|followers|favorites|lists|moments)"[^>]*>.*?ProfileNav-value[^>]*data-count="(\d+)"',
        content,
        flags=re.IGNORECASE | re.DOTALL,
    )
    counts = {label.lower(): int(value) for label, value in count_matches}
    timeline_tweets = extract_timeline_tweets(content, capture, account)
    tweet_ids = sorted(dict.fromkeys(row["tweet_id"] for row in timeline_tweets))
    snapshot = {
        "snapshot_timestamp": parse_capture_timestamp(capture["timestamp"]).isoformat().replace("+00:00", "Z"),
        "source_type": "timeline_html",
        "capture_name": capture["capture_name"],
        "raw_payload_path": capture["raw_path"],
        "archive_capture_url": capture["replay_url"],
        "screen_name": regex_first([r'@([A-Za-z0-9_]{1,15})\) \| Twitter'], title or ""),
        "name": strip_tags(name_html) if name_html else None,
        "title": strip_tags(title) if title else None,
        "description": description,
        "location": strip_tags(location_html) if location_html else None,
        "url": html.unescape(url_html) if url_html else None,
        "joined_display": html.unescape(join_html) if join_html else None,
        "profile_image_url_https": profile_image,
        "followers_count": counts.get("followers"),
        "friends_count": counts.get("following"),
        "statuses_count": counts.get("tweets"),
        "favourites_count": counts.get("favorites"),
        "listed_count": counts.get("lists"),
        "visible_tweet_ids": tweet_ids,
    }
    return snapshot, timeline_tweets


def parse_status_capture_candidate(
    content: str,
    capture: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    canonical: dict[str, Any] | None = None
    snapshot: dict[str, Any] | None = None
    lower_mimetype = (capture.get("mimetype") or "").lower()
    if "json" in lower_mimetype or content.lstrip().startswith("{"):
        canonical, snapshot = parse_status_json_capture(content, capture)
        if canonical is None:
            canonical, snapshot = parse_status_html_capture(content, capture)
    else:
        canonical, snapshot = parse_status_html_capture(content, capture)
        if canonical is None or not canonical.get("tweet_text"):
            embedded_canonical, embedded_snapshot = parse_status_html_embedded_json_capture(content, capture)
            if embedded_canonical is not None and embedded_canonical.get("tweet_text"):
                canonical, snapshot = embedded_canonical, embedded_snapshot
        if canonical is None or not canonical.get("tweet_text"):
            fallback_canonical, fallback_snapshot = parse_status_json_capture(content, capture)
            if fallback_canonical is not None:
                canonical, snapshot = fallback_canonical, fallback_snapshot
    if canonical is not None and canonical.get("tweet_id") != capture.get("tweet_id"):
        return None, None
    return canonical, snapshot


def capture_rank(capture: dict[str, Any]) -> tuple[int, int, str]:
    mimetype = (capture.get("mimetype") or "").lower()
    statuscode = capture.get("statuscode") or ""
    if statuscode in {"200", "-"} and "application/json" in mimetype:
        return (4, -int(capture["timestamp"]), capture["timestamp"])
    if statuscode in {"200", "-"} and "text/html" in mimetype:
        return (3, -int(capture["timestamp"]), capture["timestamp"])
    if statuscode in {"200", "302", "-"} and "text/plain" in mimetype:
        return (2, -int(capture["timestamp"]), capture["timestamp"])
    if statuscode in {"200", "302", "404", "-"}:
        return (1, -int(capture["timestamp"]), capture["timestamp"])
    return (0, -int(capture["timestamp"]), capture["timestamp"])


def choose_best_capture(captures: list[dict[str, Any]]) -> dict[str, Any]:
    return max(captures, key=capture_rank)


def format_duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "unknown"
    rounded = int(seconds)
    minutes, secs = divmod(rounded, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def estimate_eta(elapsed_seconds: float, completed: int, total: int) -> str:
    if completed <= 0 or elapsed_seconds <= 0 or total <= completed:
        return "unknown"
    per_item = elapsed_seconds / completed
    return format_duration(per_item * (total - completed))


def build_inventory_overview(
    *,
    account: str,
    output_dir: Path,
    all_cdx_rows: dict[str, list[dict[str, str]]],
    timeline_rows: list[dict[str, Any]],
    status_rows: list[dict[str, Any]],
    status_groups: dict[str, list[dict[str, Any]]],
    args: argparse.Namespace,
) -> dict[str, Any]:
    statuscode_counts = Counter(row.get("statuscode") or "" for row in status_rows)
    mimetype_counts = Counter((row.get("mimetype") or "unk").split(";")[0] for row in status_rows)
    tweet_ids_with_json = {
        row["tweet_id"]
        for row in status_rows
        if "application/json" in (row.get("mimetype") or "").lower() and (row.get("statuscode") or "") in {"200", "-"}
    }
    tweet_ids_with_html_200 = {
        row["tweet_id"]
        for row in status_rows
        if "text/html" in (row.get("mimetype") or "").lower() and (row.get("statuscode") or "") in {"200", "-"}
    }
    overview = {
        "account": account,
        "generated_at_utc": utc_now_iso(),
        "output_dir": str(output_dir),
        "run_parameters": {
            "offline": args.offline,
            "sleep_seconds": args.sleep_seconds,
            "max_tweets": args.max_tweets,
            "max_timeline_captures": args.max_timeline_captures,
            "max_capture_attempts_per_tweet": args.max_capture_attempts_per_tweet,
            "checkpoint_every": args.checkpoint_every,
        },
        "target_row_counts": {name: len(rows) for name, rows in all_cdx_rows.items()},
        "timeline_capture_rows": len(timeline_rows),
        "status_capture_rows": len(status_rows),
        "unique_tweet_ids": len(status_groups),
        "unique_tweet_ids_with_json_capture": len(tweet_ids_with_json),
        "unique_tweet_ids_with_html_200_capture": len(tweet_ids_with_html_200),
        "earliest_tweet_created_at_estimated": min((snowflake_to_iso(tweet_id) for tweet_id in status_groups), default=None),
        "latest_tweet_created_at_estimated": max((snowflake_to_iso(tweet_id) for tweet_id in status_groups), default=None),
        "statuscode_counts": dict(statuscode_counts.most_common()),
        "status_mimetype_counts": dict(mimetype_counts.most_common()),
        "recommended_quick_run": [
            "python scripts/extract_twitter_wayback.py --account <handle> --overview-only",
            "python scripts/extract_twitter_wayback.py --account <handle> --max-tweets 100 --max-timeline-captures 10 --max-capture-attempts-per-tweet 1",
        ],
        "recommended_exhaustive_run": "python scripts/extract_twitter_wayback.py --account <handle>",
    }
    return overview


def build_acquisition_plan(overview: dict[str, Any]) -> str:
    row_counts = overview.get("target_row_counts", {})
    statuscode_counts = overview.get("statuscode_counts", {})
    mimetype_counts = overview.get("status_mimetype_counts", {})
    lines = [
        f"# {overview['account']} Twitter Wayback Acquisition Plan",
        "",
        f"Target account: `https://twitter.com/{overview['account']}`",
        "",
        "## Preliminary Inventory",
        "",
        f"- Timeline capture rows across queried variants: {overview['timeline_capture_rows']}",
        f"- Status capture rows across queried variants: {overview['status_capture_rows']}",
        f"- Unique archived tweet IDs observed: {overview['unique_tweet_ids']}",
        f"- Tweet IDs with at least one JSON capture: {overview['unique_tweet_ids_with_json_capture']}",
        f"- Tweet IDs with at least one HTTP 200 HTML capture: {overview['unique_tweet_ids_with_html_200_capture']}",
        f"- Earliest recoverable tweet ID date estimate: {overview.get('earliest_tweet_created_at_estimated') or ''}",
        f"- Latest recoverable tweet ID date estimate: {overview.get('latest_tweet_created_at_estimated') or ''}",
        "",
        "## CDX Target Counts",
        "",
    ]
    for name, count in row_counts.items():
        lines.append(f"- `{name}`: {count}")
    lines.extend(["", "## Status Capture Mix", ""])
    for statuscode, count in statuscode_counts.items():
        lines.append(f"- status `{statuscode or 'blank'}`: {count}")
    for mimetype, count in mimetype_counts.items():
        lines.append(f"- mimetype `{mimetype}`: {count}")
    lines.extend(
        [
            "",
            "## Suggested Run Sequence",
            "",
            "- Overview first: `--overview-only` to estimate scope without replay fetches.",
            "- Fast sample second: `--max-tweets 100 --max-timeline-captures 10 --max-capture-attempts-per-tweet 1`.",
            "- Exhaustive crawl last: remove the limits once the overview justifies the cost.",
            "- Faster reruns: reuse the same output directory so cached raw captures are reused.",
            "",
            "## Speed Knobs",
            "",
            "- `--max-tweets`: cap how many status tweet IDs are materialized in this run.",
            "- `--max-timeline-captures`: cap timeline replay fetches. Timeline is useful, but status pages are the primary source.",
            "- `--max-capture-attempts-per-tweet`: try only the top-ranked 1-2 captures per tweet in quick runs.",
            "- `--offline`: parse only cached raw files and skip network replay fetches.",
            "- `--checkpoint-every`: keep partial derived outputs current during long runs.",
            "",
        ]
    )
    return "\n".join(lines)


def write_inventory_outputs(derived_dir: Path, overview: dict[str, Any]) -> None:
    write_json(derived_dir / "inventory_summary.json", overview)
    (derived_dir / "ACQUISITION_PLAN.md").write_text(build_acquisition_plan(overview), encoding="utf-8")


def print_inventory_overview(overview: dict[str, Any]) -> None:
    print(
        "[overview] "
        f"status_rows={overview['status_capture_rows']} "
        f"unique_tweet_ids={overview['unique_tweet_ids']} "
        f"json_tweet_ids={overview['unique_tweet_ids_with_json_capture']} "
        f"html_200_tweet_ids={overview['unique_tweet_ids_with_html_200_capture']}",
        flush=True,
    )
    print(
        "[overview] "
        "Quick sample knobs: --max-tweets 100 --max-timeline-captures 10 --max-capture-attempts-per-tweet 1. "
        "Exhaustive crawl: remove limits and keep the same output dir for cache reuse.",
        flush=True,
    )


def maybe_read_existing_capture(raw_path: Path) -> str | None:
    return raw_path.read_text(encoding="utf-8") if raw_path.exists() else None


def status_capture_filename(tweet_id: str, timestamp: str, extension: str) -> str:
    return f"{tweet_id}__{timestamp}{extension}"


def expected_status_capture_paths(raw_dir: Path, tweet_id: str, timestamp: str) -> list[Path]:
    return [
        raw_dir / "status_json" / status_capture_filename(tweet_id, timestamp, ".json"),
        raw_dir / "status_html" / status_capture_filename(tweet_id, timestamp, ".html"),
        raw_dir / "status_misc" / status_capture_filename(tweet_id, timestamp, ".txt"),
    ]


def find_existing_status_capture(raw_dir: Path, tweet_id: str, timestamp: str | None = None) -> Path | None:
    if timestamp:
        for path in expected_status_capture_paths(raw_dir, tweet_id, timestamp):
            if path.exists():
                return path
        return None
    candidates = [
        raw_dir / "status_json" / f"{tweet_id}.json",
        raw_dir / "status_html" / f"{tweet_id}.html",
        raw_dir / "status_misc" / f"{tweet_id}.txt",
    ]
    candidates.extend(sorted((raw_dir / "status_json").glob(f"{tweet_id}__*.json")))
    candidates.extend(sorted((raw_dir / "status_html").glob(f"{tweet_id}__*.html")))
    candidates.extend(sorted((raw_dir / "status_misc").glob(f"{tweet_id}__*.txt")))
    for path in candidates:
        if path.exists():
            return path
    return None


def fetch_and_store_capture(
    session: requests.Session,
    capture: dict[str, Any],
    *,
    raw_dir: Path,
    retries: int,
    sleep_seconds: float,
    refresh_existing: bool,
    offline: bool,
    diagnostics: dict[str, Any],
) -> dict[str, Any]:
    if not refresh_existing:
        existing_path = (
            Path(capture["raw_path"])
            if capture.get("raw_path")
            else find_existing_status_capture(raw_dir, capture["tweet_id"], capture.get("timestamp"))
        )
        if existing_path is not None:
            existing = maybe_read_existing_capture(existing_path)
            if existing is not None:
                capture["raw_path"] = str(existing_path)
                capture["content"] = existing
                capture["fetch_source"] = "cache"
                diagnostics["status_cache_hits"] += 1
                return capture

    mimetype = (capture.get("mimetype") or "").lower()
    statuscode = capture.get("statuscode") or ""
    fetchable = statuscode != "404" and (
        statuscode == "200" or "json" in mimetype or "html" in mimetype or "text/plain" in mimetype
    )
    if not fetchable:
        capture["raw_path"] = None
        capture["content"] = ""
        capture["fetch_source"] = "not_fetchable"
        return capture

    if offline:
        capture["raw_path"] = None
        capture["content"] = ""
        capture["fetch_error"] = "offline_missing_raw_capture"
        capture["fetch_source"] = "offline_missing_raw"
        return capture

    try:
        resp = request_with_retries(
            session,
            "GET",
            capture["replay_url"],
            retries=retries,
            sleep_seconds=sleep_seconds,
        )
    except requests.RequestException as exc:
        capture["raw_path"] = None
        capture["content"] = ""
        capture["fetch_error"] = str(exc)
        capture["fetch_source"] = "error"
        diagnostics["status_fetch_errors"] += 1
        return capture
    resp.encoding = "utf-8"
    content = resp.text
    extension = derive_extension(resp.headers.get("content-type", capture.get("mimetype", "")), content)
    subdir = raw_dir / ("status_json" if extension == ".json" else "status_html" if extension == ".html" else "status_misc")
    raw_path = subdir / status_capture_filename(capture["tweet_id"], capture["timestamp"], extension)
    ensure_dir(raw_path.parent)
    raw_path.write_text(content, encoding="utf-8")
    capture["mimetype"] = resp.headers.get("content-type", capture.get("mimetype", ""))
    capture["raw_path"] = str(raw_path)
    capture["content"] = content
    capture["fetch_source"] = "network"
    diagnostics["status_network_fetches"] += 1
    return capture


def fetch_and_store_timeline_capture(
    session: requests.Session,
    capture: dict[str, Any],
    *,
    raw_dir: Path,
    retries: int,
    sleep_seconds: float,
    refresh_existing: bool,
    offline: bool,
    diagnostics: dict[str, Any],
) -> dict[str, Any]:
    raw_path = raw_dir / "timeline_html" / f"{capture['capture_name']}_{capture['timestamp']}.html"
    if raw_path.exists() and not refresh_existing:
        capture["raw_path"] = str(raw_path)
        capture["content"] = raw_path.read_text(encoding="utf-8")
        capture["fetch_source"] = "cache"
        diagnostics["timeline_cache_hits"] += 1
        return capture

    if offline:
        capture["raw_path"] = None
        capture["content"] = ""
        capture["fetch_error"] = "offline_missing_raw_capture"
        capture["fetch_source"] = "offline_missing_raw"
        return capture

    try:
        resp = request_with_retries(
            session,
            "GET",
            capture["replay_url"],
            retries=retries,
            sleep_seconds=sleep_seconds,
        )
    except requests.RequestException as exc:
        capture["raw_path"] = None
        capture["content"] = ""
        capture["fetch_error"] = str(exc)
        capture["fetch_source"] = "error"
        diagnostics["timeline_fetch_errors"] += 1
        return capture
    resp.encoding = "utf-8"
    ensure_dir(raw_path.parent)
    raw_path.write_text(resp.text, encoding="utf-8")
    capture["raw_path"] = str(raw_path)
    capture["content"] = resp.text
    capture["fetch_source"] = "network"
    diagnostics["timeline_network_fetches"] += 1
    return capture


def summarize_capture_group(tweet_id: str, captures: list[dict[str, Any]], canonical: dict[str, Any] | None) -> dict[str, Any]:
    timestamps = sorted(capture["timestamp"] for capture in captures)
    mimetypes = Counter((capture.get("mimetype") or "").split(";")[0] for capture in captures)
    statuscodes = Counter(capture.get("statuscode") or "" for capture in captures)
    best = choose_best_capture(captures)
    return {
        "tweet_id": tweet_id,
        "canonical_tweet_url": canonical_status_url(best["original"], tweet_id),
        "tweet_created_at_estimated": canonical.get("tweet_created_at") if canonical else snowflake_to_iso(tweet_id),
        "capture_count": len(captures),
        "first_capture_timestamp": timestamps[0] if timestamps else None,
        "last_capture_timestamp": timestamps[-1] if timestamps else None,
        "best_capture_timestamp": best["timestamp"],
        "best_capture_original": best["original"],
        "best_capture_mimetype": best.get("mimetype"),
        "best_capture_statuscode": best.get("statuscode"),
        "capture_statuscodes": dict(statuscodes),
        "capture_mimetypes": dict(mimetypes),
    }


def dedupe_profile_snapshots(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    deduped: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda item: (item.get("snapshot_timestamp") or "", item.get("source_type") or "")):
        key = (
            row.get("snapshot_timestamp"),
            row.get("source_type"),
            row.get("screen_name"),
            row.get("name"),
            row.get("description"),
            row.get("followers_count"),
            row.get("friends_count"),
            row.get("statuses_count"),
            row.get("favourites_count"),
            row.get("listed_count"),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def timeline_candidate_rank(row: dict[str, Any]) -> tuple[int, int, str]:
    text_len = len(row.get("tweet_text") or "")
    created = row.get("tweet_created_at") or ""
    return (1 if text_len else 0, text_len, created)


def build_report(*, summary: dict[str, Any], tweets: list[dict[str, Any]], profile_snapshots: list[dict[str, Any]]) -> str:
    recovered = [row for row in tweets if row.get("tweet_text")]
    json_rows = [row for row in tweets if row.get("recovery_source") == "json" and row.get("tweet_text")]
    html_rows = [row for row in tweets if row.get("recovery_source") == "html" and row.get("tweet_text")]
    timeline_rows = [row for row in tweets if row.get("recovery_source") == "timeline_html" and row.get("tweet_text")]
    metadata_only = [row for row in tweets if row.get("source_quality") == "metadata_only"]
    lines = [
        "# Twitter Wayback Recovery Report",
        "",
        f"Source account: `{summary['account']}`",
        "Primary source: https://web.archive.org",
        "",
        "## Scope",
        "",
        f"- Timeline capture rows inventoried: {summary['timeline_capture_rows']}",
        f"- Status capture rows inventoried: {summary['status_capture_rows']}",
        f"- Unique tweet IDs in status inventory: {summary['unique_tweet_ids']}",
        f"- Tweets with recovered content: {len(recovered)}",
        f"- JSON-backed recoveries: {len(json_rows)}",
        f"- HTML-backed recoveries: {len(html_rows)}",
        f"- Timeline-only or timeline-enriched recoveries: {len(timeline_rows)}",
        f"- Metadata-only tweet rows: {len(metadata_only)}",
        f"- Profile snapshots recovered: {len(profile_snapshots)}",
        f"- Timeline-only tweet IDs added beyond status inventory: {summary.get('timeline_only_recovered_tweet_ids', 0)}",
        "",
        "## Date Ranges",
        "",
        f"- Earliest tweet date: {summary.get('earliest_tweet_created_at') or ''}",
        f"- Latest tweet date: {summary.get('latest_tweet_created_at') or ''}",
        f"- Earliest archive capture: {summary.get('earliest_archive_capture') or ''}",
        f"- Latest archive capture: {summary.get('latest_archive_capture') or ''}",
        "",
        "## Capture Mix",
        "",
    ]
    for mimetype, count in summary.get("status_mimetype_counts", {}).items():
        lines.append(f"- `{mimetype}`: {count}")
    lines.extend(["", "## Recent Recovered Tweets", ""])
    for row in sorted(recovered, key=lambda item: (item.get("tweet_created_at") or "", item.get("tweet_id") or ""), reverse=True)[:25]:
        preview = (row.get("tweet_text") or "").replace("\n", " ").strip()
        if len(preview) > 140:
            preview = preview[:137] + "..."
        lines.append(f"- {row.get('tweet_created_at') or ''} | `{row.get('tweet_id')}` | {preview}")
    lines.extend(["", "## Profile Snapshot Samples", ""])
    for row in profile_snapshots[:15]:
        desc = (row.get("description") or "").replace("\n", " ").strip()
        if len(desc) > 120:
            desc = desc[:117] + "..."
        lines.append(
            f"- {row.get('snapshot_timestamp') or ''} | `{row.get('source_type')}` | "
            f"{row.get('name') or ''} @{row.get('screen_name') or ''} | "
            f"followers={row.get('followers_count')} following={row.get('friends_count')} "
            f"tweets={row.get('statuses_count')} | {desc}"
        )
    return "\n".join(lines).strip() + "\n"


def build_runtime_diagnostics(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "run_mode": "overview_only" if args.overview_only else "crawl",
        "offline": args.offline,
        "refresh_existing": args.refresh_existing,
        "requested_max_tweets": args.max_tweets,
        "requested_max_timeline_captures": args.max_timeline_captures,
        "max_capture_attempts_per_tweet": args.max_capture_attempts_per_tweet,
        "checkpoint_every": args.checkpoint_every,
        "sleep_seconds": args.sleep_seconds,
        "sleep_events": 0,
        "sleep_seconds_requested": 0.0,
        "cdx_network_requests": 0,
        "timeline_cache_hits": 0,
        "timeline_network_fetches": 0,
        "timeline_fetch_errors": 0,
        "status_cache_hits": 0,
        "status_network_fetches": 0,
        "status_fetch_errors": 0,
        "status_capture_attempts": 0,
        "status_capture_attempts_skipped_by_limit": 0,
        "status_capture_attempts_without_text": 0,
        "tweets_recovered_from_second_or_later_capture": 0,
        "timings": {},
    }


def build_summary(
    *,
    args: argparse.Namespace,
    output_dir: Path,
    derived_dir: Path,
    cdx_dir: Path,
    raw_dir: Path,
    timeline_rows: list[dict[str, Any]],
    status_rows: list[dict[str, Any]],
    status_groups: dict[str, list[dict[str, Any]]],
    recovered_tweets: list[dict[str, Any]],
    profile_snapshots: list[dict[str, Any]],
    timeline_discovered_ids: set[str],
    timeline_limit: int,
    timeline_processed_count: int,
    status_requested_count: int,
    status_processed_count: int,
    timeline_enriched_count: int,
    timeline_only_count: int,
    diagnostics: dict[str, Any],
    run_completion: str,
    timeline_merge_applied: bool,
) -> dict[str, Any]:
    requested_all_status_ids = status_requested_count == len(status_groups)
    complete = (
        run_completion == "complete"
        and timeline_processed_count >= min(timeline_limit, len(timeline_rows))
        and status_processed_count >= status_requested_count
        and requested_all_status_ids
        and timeline_merge_applied
    )
    return {
        "account": args.account,
        "output_dir": str(output_dir),
        "generated_at_utc": utc_now_iso(),
        "run_completion": run_completion,
        "timeline_merge_applied": timeline_merge_applied,
        "status_processing_complete": complete,
        "timeline_capture_rows": len(timeline_rows),
        "status_capture_rows": len(status_rows),
        "unique_tweet_ids": len(status_groups),
        "status_tweet_ids_requested": status_requested_count,
        "status_tweet_ids_processed": status_processed_count,
        "materialized_tweet_ids": len(recovered_tweets),
        "timeline_capture_rows_materialized": timeline_processed_count,
        "tweets_with_recovered_content": sum(1 for row in recovered_tweets if row.get("tweet_text")),
        "json_recoveries": sum(1 for row in recovered_tweets if row.get("recovery_source") == "json" and row.get("tweet_text")),
        "html_recoveries": sum(1 for row in recovered_tweets if row.get("recovery_source") == "html" and row.get("tweet_text")),
        "timeline_recoveries": sum(
            1 for row in recovered_tweets if row.get("recovery_source") == "timeline_html" and row.get("tweet_text")
        ),
        "metadata_only_rows": sum(1 for row in recovered_tweets if row.get("source_quality") == "metadata_only"),
        "profile_snapshot_count": len(profile_snapshots),
        "timeline_discovered_tweet_ids": len(timeline_discovered_ids),
        "timeline_only_discovered_tweet_ids": len(timeline_discovered_ids - set(status_groups.keys())),
        "timeline_only_recovered_tweet_ids": timeline_only_count,
        "status_inventory_tweets_enriched_from_timeline": timeline_enriched_count,
        "earliest_tweet_created_at": min((row.get("tweet_created_at") for row in recovered_tweets if row.get("tweet_created_at")), default=None),
        "latest_tweet_created_at": max((row.get("tweet_created_at") for row in recovered_tweets if row.get("tweet_created_at")), default=None),
        "earliest_archive_capture": min((row["timestamp"] for row in status_rows + timeline_rows), default=None),
        "latest_archive_capture": max((row["timestamp"] for row in status_rows + timeline_rows), default=None),
        "status_mimetype_counts": dict(Counter((row.get("mimetype") or "unk").split(";")[0] for row in status_rows).most_common()),
        "statuscode_counts": dict(Counter(row.get("statuscode") or "" for row in status_rows).most_common()),
        "raw_files": {
            "cdx_dir": str(cdx_dir),
            "status_json_dir": str(raw_dir / "status_json"),
            "status_html_dir": str(raw_dir / "status_html"),
            "timeline_html_dir": str(raw_dir / "timeline_html"),
        },
        "derived_files": {
            "inventory_summary_json": str(derived_dir / "inventory_summary.json"),
            "acquisition_plan_md": str(derived_dir / "ACQUISITION_PLAN.md"),
            "tweets_json": str(derived_dir / "tweets_recovered_deduped.json"),
            "tweets_jsonl": str(derived_dir / "tweets_recovered.jsonl"),
            "capture_index_csv": str(derived_dir / "tweet_capture_index.csv"),
            "profile_snapshots_csv": str(derived_dir / "profile_snapshots.csv"),
            "dataset_json": str(derived_dir / "twitter_wayback_dataset.json"),
            "report_md": str(derived_dir / "REPORT.md"),
        },
        "diagnostics": diagnostics,
    }


def write_recovery_outputs(
    *,
    args: argparse.Namespace,
    output_dir: Path,
    derived_dir: Path,
    cdx_dir: Path,
    raw_dir: Path,
    timeline_rows: list[dict[str, Any]],
    status_rows: list[dict[str, Any]],
    status_groups: dict[str, list[dict[str, Any]]],
    recovered_tweets: list[dict[str, Any]],
    profile_snapshots: list[dict[str, Any]],
    capture_index_rows: list[dict[str, Any]],
    timeline_discovered_ids: set[str],
    timeline_limit: int,
    timeline_processed_count: int,
    status_requested_count: int,
    status_processed_count: int,
    timeline_enriched_count: int,
    timeline_only_count: int,
    diagnostics: dict[str, Any],
    run_completion: str,
    timeline_merge_applied: bool,
) -> dict[str, Any]:
    summary = build_summary(
        args=args,
        output_dir=output_dir,
        derived_dir=derived_dir,
        cdx_dir=cdx_dir,
        raw_dir=raw_dir,
        timeline_rows=timeline_rows,
        status_rows=status_rows,
        status_groups=status_groups,
        recovered_tweets=recovered_tweets,
        profile_snapshots=profile_snapshots,
        timeline_discovered_ids=timeline_discovered_ids,
        timeline_limit=timeline_limit,
        timeline_processed_count=timeline_processed_count,
        status_requested_count=status_requested_count,
        status_processed_count=status_processed_count,
        timeline_enriched_count=timeline_enriched_count,
        timeline_only_count=timeline_only_count,
        diagnostics=diagnostics,
        run_completion=run_completion,
        timeline_merge_applied=timeline_merge_applied,
    )
    dataset_bundle = {
        "summary": summary,
        "tweets": recovered_tweets,
        "profile_snapshots": profile_snapshots,
        "capture_index": capture_index_rows,
        "timeline_discovered_tweet_ids": sorted(timeline_discovered_ids),
    }
    write_json(derived_dir / "extraction_summary.json", summary)
    write_json(derived_dir / "tweets_recovered_deduped.json", recovered_tweets)
    write_jsonl(derived_dir / "tweets_recovered.jsonl", recovered_tweets)
    write_json(derived_dir / "twitter_wayback_dataset.json", dataset_bundle)
    write_csv(
        derived_dir / "tweet_capture_index.csv",
        capture_index_rows,
        [
            "tweet_id",
            "canonical_tweet_url",
            "tweet_created_at_estimated",
            "capture_count",
            "first_capture_timestamp",
            "last_capture_timestamp",
            "best_capture_timestamp",
            "best_capture_original",
            "best_capture_mimetype",
            "best_capture_statuscode",
            "capture_statuscodes",
            "capture_mimetypes",
        ],
    )
    write_csv(
        derived_dir / "profile_snapshots.csv",
        profile_snapshots,
        [
            "snapshot_timestamp",
            "source_type",
            "capture_name",
            "screen_name",
            "name",
            "title",
            "description",
            "location",
            "url",
            "joined_display",
            "followers_count",
            "friends_count",
            "statuses_count",
            "favourites_count",
            "listed_count",
            "user_id",
            "profile_image_url_https",
            "profile_banner_url",
            "profile_background_image_url_https",
            "verified",
            "protected",
            "lang",
            "visible_tweet_ids",
            "archive_capture_url",
            "raw_payload_path",
        ],
    )
    (derived_dir / "REPORT.md").write_text(build_report(summary=summary, tweets=recovered_tweets, profile_snapshots=profile_snapshots), encoding="utf-8")
    return summary


def main() -> None:
    args = parse_args()
    output_dir = ensure_dir(args.output_dir or default_output_dir(args.account))
    raw_dir = ensure_dir(output_dir / "raw")
    derived_dir = ensure_dir(output_dir / "derived")
    cdx_dir = ensure_dir(raw_dir / "cdx")
    ensure_dir(raw_dir / "status_json")
    ensure_dir(raw_dir / "status_html")
    ensure_dir(raw_dir / "status_misc")
    ensure_dir(raw_dir / "timeline_html")

    diagnostics = build_runtime_diagnostics(args)
    run_started = time.perf_counter()
    session = make_session()
    all_cdx_rows: dict[str, list[dict[str, str]]] = {}
    cdx_targets = build_cdx_targets(args.account)
    cdx_started = time.perf_counter()
    for target in cdx_targets:
        snapshot_path = cdx_dir / f"{target['name']}.json"
        if args.offline:
            rows, snapshot = load_cdx_snapshot(snapshot_path)
        else:
            rows, snapshot = fetch_cdx_inventory(session, target, retries=args.retries, sleep_seconds=args.sleep_seconds)
            diagnostics["cdx_network_requests"] += 1
        all_cdx_rows[target["name"]] = rows
        if not args.offline:
            write_json(snapshot_path, snapshot)
            maybe_sleep(args.sleep_seconds, diagnostics=diagnostics, enabled=True)
    diagnostics["timings"]["cdx_inventory_seconds"] = round(time.perf_counter() - cdx_started, 3)

    timeline_rows: list[dict[str, Any]] = []
    for name, rows in all_cdx_rows.items():
        if name not in TIMELINE_TARGETS:
            continue
        for row in rows:
            timeline_rows.append({**row, "capture_name": name, "replay_url": replay_url(row["timestamp"], row["original"])})
    timeline_rows.sort(key=lambda row: row["timestamp"])

    status_rows: list[dict[str, Any]] = []
    for name, rows in all_cdx_rows.items():
        if name not in STATUS_TARGETS:
            continue
        for row in rows:
            tweet_id = tweet_id_from_url(row["original"])
            if not tweet_id:
                continue
            status_rows.append(
                {
                    **row,
                    "capture_name": name,
                    "tweet_id": tweet_id,
                    "clean_original": clean_url(row["original"]),
                    "replay_url": replay_url(row["timestamp"], row["original"]),
                }
            )
    status_rows.sort(key=lambda row: (int(row["tweet_id"]), row["timestamp"]))
    status_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in status_rows:
        status_groups[row["tweet_id"]].append(row)

    overview = build_inventory_overview(
        account=args.account,
        output_dir=output_dir,
        all_cdx_rows=all_cdx_rows,
        timeline_rows=timeline_rows,
        status_rows=status_rows,
        status_groups=status_groups,
        args=args,
    )
    write_inventory_outputs(derived_dir, overview)
    print_inventory_overview(overview)
    if args.overview_only:
        diagnostics["timings"]["total_runtime_seconds"] = round(time.perf_counter() - run_started, 3)
        summary = {
            "account": args.account,
            "output_dir": str(output_dir),
            "generated_at_utc": utc_now_iso(),
            "run_completion": "overview_only",
            "timeline_capture_rows": len(timeline_rows),
            "status_capture_rows": len(status_rows),
            "unique_tweet_ids": len(status_groups),
            "derived_files": {
                "inventory_summary_json": str(derived_dir / "inventory_summary.json"),
                "acquisition_plan_md": str(derived_dir / "ACQUISITION_PLAN.md"),
            },
            "diagnostics": diagnostics,
        }
        write_json(derived_dir / "extraction_summary.json", summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return

    capture_index_rows: list[dict[str, Any]] = []
    recovered_tweets: list[dict[str, Any]] = []
    profile_snapshots: list[dict[str, Any]] = []
    timeline_snapshots: list[dict[str, Any]] = []
    timeline_discovered_ids: set[str] = set()
    timeline_tweet_candidates: dict[str, dict[str, Any]] = {}
    timeline_candidate_history: dict[str, list[dict[str, Any]]] = defaultdict(list)

    timeline_limit = args.max_timeline_captures or len(timeline_rows)
    timeline_processed_count = 0
    timeline_started = time.perf_counter()
    for idx, capture in enumerate(timeline_rows[:timeline_limit], start=1):
        fetched = fetch_and_store_timeline_capture(
            session,
            dict(capture),
            raw_dir=raw_dir,
            retries=args.retries,
            sleep_seconds=args.sleep_seconds,
            refresh_existing=args.refresh_existing,
            offline=args.offline,
            diagnostics=diagnostics,
        )
        timeline_processed_count = idx
        if not fetched.get("content"):
            continue
        snapshot, visible_tweets = parse_timeline_capture(fetched["content"], fetched, args.account)
        timeline_snapshots.append(snapshot)
        for candidate in visible_tweets:
            tweet_id = candidate["tweet_id"]
            timeline_discovered_ids.add(tweet_id)
            timeline_candidate_history[tweet_id].append(
                {
                    "timestamp": candidate["archive_capture_timestamp"],
                    "archive_datetime": candidate["archive_capture_datetime"],
                    "original": fetched["original"],
                    "replay_url": candidate["archive_capture_url"],
                    "mimetype": candidate["archive_capture_mimetype"],
                    "statuscode": candidate["archive_capture_statuscode"],
                    "digest": fetched.get("digest"),
                    "length": fetched.get("length"),
                    "raw_payload_path": candidate["raw_payload_path"],
                }
            )
            current = timeline_tweet_candidates.get(tweet_id)
            if current is None or timeline_candidate_rank(candidate) > timeline_candidate_rank(current):
                timeline_tweet_candidates[tweet_id] = candidate
        if idx % 10 == 0 or idx == timeline_limit:
            elapsed = time.perf_counter() - timeline_started
            print(
                "[timeline] "
                f"{idx}/{timeline_limit} captures | discovered_ids={len(timeline_tweet_candidates)} "
                f"cache={diagnostics['timeline_cache_hits']} network={diagnostics['timeline_network_fetches']} "
                f"errors={diagnostics['timeline_fetch_errors']} eta={estimate_eta(elapsed, idx, timeline_limit)}",
                flush=True,
            )
        maybe_sleep(
            args.sleep_seconds,
            diagnostics=diagnostics,
            enabled=fetched.get("fetch_source") == "network",
        )
    diagnostics["timings"]["timeline_processing_seconds"] = round(time.perf_counter() - timeline_started, 3)

    tweet_ids = sorted(status_groups.keys(), key=lambda value: int(value))
    if args.max_tweets is not None:
        tweet_ids = tweet_ids[: args.max_tweets]
    status_requested_count = len(tweet_ids)
    status_processed_count = 0
    interrupted = False
    status_started = time.perf_counter()

    def checkpoint(run_completion: str, *, timeline_merge_applied: bool, timeline_enriched_count: int = 0, timeline_only_count: int = 0) -> None:
        summary = write_recovery_outputs(
            args=args,
            output_dir=output_dir,
            derived_dir=derived_dir,
            cdx_dir=cdx_dir,
            raw_dir=raw_dir,
            timeline_rows=timeline_rows,
            status_rows=status_rows,
            status_groups=status_groups,
            recovered_tweets=recovered_tweets,
            profile_snapshots=profile_snapshots,
            capture_index_rows=capture_index_rows,
            timeline_discovered_ids=timeline_discovered_ids,
            timeline_limit=timeline_limit,
            timeline_processed_count=timeline_processed_count,
            status_requested_count=status_requested_count,
            status_processed_count=status_processed_count,
            timeline_enriched_count=timeline_enriched_count,
            timeline_only_count=timeline_only_count,
            diagnostics=diagnostics,
            run_completion=run_completion,
            timeline_merge_applied=timeline_merge_applied,
        )
        print(
            "[checkpoint] "
            f"run_completion={run_completion} status_ids={status_processed_count}/{status_requested_count} "
            f"materialized={summary['materialized_tweet_ids']} with_text={summary['tweets_with_recovered_content']}",
            flush=True,
        )

    try:
        for idx, tweet_id in enumerate(tweet_ids, start=1):
            captures = status_groups[tweet_id]
            canonical: dict[str, Any] | None = None
            snapshot: dict[str, Any] | None = None
            prepared_capture: dict[str, Any] | None = None
            ranked_captures = sorted(captures, key=capture_rank, reverse=True)
            attempts_for_tweet = 0
            for capture_idx, candidate_capture in enumerate(ranked_captures, start=1):
                if args.max_capture_attempts_per_tweet is not None and capture_idx > args.max_capture_attempts_per_tweet:
                    diagnostics["status_capture_attempts_skipped_by_limit"] += len(ranked_captures) - args.max_capture_attempts_per_tweet
                    break
                diagnostics["status_capture_attempts"] += 1
                attempts_for_tweet += 1
                prepared_capture = fetch_and_store_capture(
                    session,
                    dict(candidate_capture),
                    raw_dir=raw_dir,
                    retries=args.retries,
                    sleep_seconds=args.sleep_seconds,
                    refresh_existing=args.refresh_existing,
                    offline=args.offline,
                    diagnostics=diagnostics,
                )
                content = prepared_capture.get("content", "")
                maybe_sleep(
                    args.sleep_seconds,
                    diagnostics=diagnostics,
                    enabled=prepared_capture.get("fetch_source") == "network",
                )
                if not content:
                    continue
                candidate_canonical, candidate_snapshot = parse_status_capture_candidate(content, prepared_capture)
                if candidate_canonical is None:
                    continue
                canonical = candidate_canonical
                snapshot = candidate_snapshot
                if canonical.get("tweet_text"):
                    if attempts_for_tweet > 1:
                        diagnostics["tweets_recovered_from_second_or_later_capture"] += 1
                    break

            if canonical is None or not canonical.get("tweet_text"):
                diagnostics["status_capture_attempts_without_text"] += attempts_for_tweet

            if canonical is None:
                prepared_capture = prepared_capture or dict(choose_best_capture(captures))
                canonical = {
                    "tweet_id": tweet_id,
                    "tweet_url": canonical_status_url(prepared_capture["original"], tweet_id),
                    "tweet_created_at": snowflake_to_iso(tweet_id),
                    "tweet_created_at_from": "snowflake",
                    "tweet_text": None,
                    "tweet_text_source": None,
                    "lang": None,
                    "source": None,
                    "truncated": None,
                    "is_retweet": None,
                    "is_quote": None,
                    "is_reply": None,
                    "in_reply_to_status_id": None,
                    "in_reply_to_user_id": None,
                    "in_reply_to_screen_name": None,
                    "conversation_id": None,
                    "retweet_count": None,
                    "favorite_count": None,
                    "quote_count": None,
                    "reply_count": None,
                    "possibly_sensitive": None,
                    "hashtags": [],
                    "mentions": [],
                    "urls": [],
                    "expanded_urls": [],
                    "media_urls": [],
                    "media_expanded_urls": [],
                    "user": {"user_id": None, "screen_name": None, "name": None, "description": None, "location": None, "verified": None},
                    "retweeted_status_id": None,
                    "quoted_status_id": None,
                    "archive_capture_timestamp": prepared_capture["timestamp"],
                    "archive_capture_datetime": parse_capture_timestamp(prepared_capture["timestamp"]).isoformat().replace("+00:00", "Z"),
                    "archive_capture_url": prepared_capture["replay_url"],
                    "archive_capture_mimetype": prepared_capture["mimetype"],
                    "archive_capture_statuscode": prepared_capture["statuscode"],
                    "recovery_source": "metadata_only",
                    "source_quality": "metadata_only",
                    "raw_payload_path": prepared_capture["raw_path"],
                }

            capture_history = [
                {
                    "timestamp": capture["timestamp"],
                    "archive_datetime": parse_capture_timestamp(capture["timestamp"]).isoformat().replace("+00:00", "Z"),
                    "original": capture["original"],
                    "replay_url": capture["replay_url"],
                    "mimetype": capture.get("mimetype"),
                    "statuscode": capture.get("statuscode"),
                    "digest": capture.get("digest"),
                    "length": capture.get("length"),
                }
                for capture in sorted(captures, key=lambda row: row["timestamp"])
            ]
            canonical["capture_history"] = capture_history
            canonical["capture_count"] = len(capture_history)
            canonical["first_capture_timestamp"] = capture_history[0]["timestamp"] if capture_history else None
            canonical["last_capture_timestamp"] = capture_history[-1]["timestamp"] if capture_history else None
            canonical["first_capture_datetime"] = capture_history[0]["archive_datetime"] if capture_history else None
            canonical["last_capture_datetime"] = capture_history[-1]["archive_datetime"] if capture_history else None
            canonical["snowflake_created_at"] = snowflake_to_iso(tweet_id)
            canonical["account_handle_observed"] = regex_first([r"twitter\.com/([^/]+)/status"], prepared_capture["original"], flags=re.IGNORECASE)
            canonical["capture_inventory_originals"] = sorted(dict.fromkeys(capture["original"] for capture in captures))
            recovered_tweets.append(canonical)
            if snapshot is not None:
                profile_snapshots.append(snapshot)
            capture_index_rows.append(summarize_capture_group(tweet_id, captures, canonical))
            status_processed_count = idx
            if idx % 25 == 0 or idx == len(tweet_ids):
                elapsed = time.perf_counter() - status_started
                recovered_texts = sum(1 for row in recovered_tweets if row.get("tweet_text"))
                metadata_only = sum(1 for row in recovered_tweets if row.get("source_quality") == "metadata_only")
                print(
                    "[tweets] "
                    f"{idx}/{len(tweet_ids)} status IDs | with_text={recovered_texts} metadata={metadata_only} "
                    f"attempts={diagnostics['status_capture_attempts']} cache={diagnostics['status_cache_hits']} "
                    f"network={diagnostics['status_network_fetches']} errors={diagnostics['status_fetch_errors']} "
                    f"eta={estimate_eta(elapsed, idx, len(tweet_ids))}",
                    flush=True,
                )
            if args.checkpoint_every and idx % args.checkpoint_every == 0:
                diagnostics["timings"]["status_processing_seconds"] = round(time.perf_counter() - status_started, 3)
                diagnostics["timings"]["total_runtime_seconds"] = round(time.perf_counter() - run_started, 3)
                checkpoint("checkpoint", timeline_merge_applied=False)
    except KeyboardInterrupt:
        interrupted = True
        print("[interrupt] received Ctrl+C; writing partial derived outputs from completed work", flush=True)

    diagnostics["timings"]["status_processing_seconds"] = round(time.perf_counter() - status_started, 3)

    recovered_by_id = {row["tweet_id"]: row for row in recovered_tweets}
    timeline_enriched_count = 0
    timeline_only_count = 0
    for tweet_id, candidate in timeline_tweet_candidates.items():
        history = sorted(timeline_candidate_history[tweet_id], key=lambda row: row["timestamp"])
        existing = recovered_by_id.get(tweet_id)
        if existing is not None:
            if not existing.get("tweet_text") and candidate.get("tweet_text"):
                existing["tweet_text"] = candidate["tweet_text"]
                existing["tweet_text_source"] = "timeline_html"
                existing["recovery_source"] = "timeline_html"
                existing["source_quality"] = "timeline_html"
                existing["timeline_capture_timestamp"] = candidate["archive_capture_timestamp"]
                existing["timeline_capture_datetime"] = candidate["archive_capture_datetime"]
                existing["timeline_capture_url"] = candidate["archive_capture_url"]
                existing["timeline_raw_payload_path"] = candidate["raw_payload_path"]
                for field in [
                    "lang",
                    "is_retweet",
                    "is_quote",
                    "is_reply",
                    "in_reply_to_status_id",
                    "conversation_id",
                    "retweeted_status_id",
                    "quoted_status_id",
                ]:
                    if existing.get(field) in (None, "", []):
                        existing[field] = candidate.get(field)
                for field in ["hashtags", "mentions", "expanded_urls", "media_urls", "media_expanded_urls"]:
                    if not existing.get(field):
                        existing[field] = candidate.get(field, [])
                if existing.get("user") and candidate.get("user"):
                    for key, value in candidate["user"].items():
                        if not existing["user"].get(key) and value:
                            existing["user"][key] = value
                timeline_enriched_count += 1
            existing["timeline_capture_history"] = history
            continue

        timeline_only = dict(candidate)
        timeline_only["capture_history"] = history
        timeline_only["capture_count"] = len(history)
        timeline_only["first_capture_timestamp"] = history[0]["timestamp"] if history else None
        timeline_only["last_capture_timestamp"] = history[-1]["timestamp"] if history else None
        timeline_only["first_capture_datetime"] = history[0]["archive_datetime"] if history else None
        timeline_only["last_capture_datetime"] = history[-1]["archive_datetime"] if history else None
        timeline_only["snowflake_created_at"] = snowflake_to_iso(tweet_id)
        timeline_only["account_handle_observed"] = (timeline_only.get("user", {}) or {}).get("screen_name")
        timeline_only["capture_inventory_originals"] = sorted(dict.fromkeys(item["original"] for item in history))
        timeline_only["timeline_only"] = True
        recovered_tweets.append(timeline_only)
        recovered_by_id[tweet_id] = timeline_only
        timeline_only_count += 1

    profile_snapshots.extend(timeline_snapshots)
    profile_snapshots = dedupe_profile_snapshots(profile_snapshots)
    recovered_tweets.sort(key=lambda row: (row.get("tweet_created_at") or "", row["tweet_id"]))
    capture_index_rows.sort(key=lambda row: (row.get("tweet_created_at_estimated") or "", row["tweet_id"]))
    diagnostics["timings"]["total_runtime_seconds"] = round(time.perf_counter() - run_started, 3)
    run_completion = "interrupted" if interrupted else "complete"
    if args.max_tweets is not None or args.max_timeline_captures is not None or args.max_capture_attempts_per_tweet is not None:
        if run_completion == "complete":
            run_completion = "partial"
    summary = write_recovery_outputs(
        args=args,
        output_dir=output_dir,
        derived_dir=derived_dir,
        cdx_dir=cdx_dir,
        raw_dir=raw_dir,
        timeline_rows=timeline_rows,
        status_rows=status_rows,
        status_groups=status_groups,
        recovered_tweets=recovered_tweets,
        profile_snapshots=profile_snapshots,
        capture_index_rows=capture_index_rows,
        timeline_discovered_ids=timeline_discovered_ids,
        timeline_limit=timeline_limit,
        timeline_processed_count=timeline_processed_count,
        status_requested_count=status_requested_count,
        status_processed_count=status_processed_count,
        timeline_enriched_count=timeline_enriched_count,
        timeline_only_count=timeline_only_count,
        diagnostics=diagnostics,
        run_completion=run_completion,
        timeline_merge_applied=True,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
