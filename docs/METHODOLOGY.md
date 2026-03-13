# Recovering Deleted Twitter Content via the Wayback Machine

*A methodology guide based on an anonymized Twitter recovery case study*

---

## Overview

When a Twitter/X account is deleted or suspended, its content vanishes from the platform. But the Wayback Machine (web.archive.org) often holds far more than meets the eye. A naive search might surface a handful of timeline snapshots -- but with systematic excavation, it's possible to recover hundreds or even thousands of individual tweets, complete with metadata, media links, and account profile history.

This report documents the techniques used to recover **809 tweet IDs, including 538 with recovered text**, from a deleted account whose Wayback timeline pages showed only ~25 snapshots on first glance. The real corpus was hiding in **7,826 individually archived status-page captures** -- each one a separately crawled snapshot of a single tweet URL.

---

## Phase 1: CDX Inventory -- Finding What the Wayback Machine Actually Has

### The CDX API

The key tool is the **Wayback CDX Server API**, which lets you query the complete index of everything Wayback has ever crawled for a given URL pattern. This is where the "beyond what is visible on first sight" begins -- the CDX index is far richer than the Wayback Machine's web UI suggests.

**Endpoint:** `https://web.archive.org/cdx/search/cdx`

**Core query parameters:**
- `url` -- the URL to search (supports `*` wildcard suffix)
- `output=json` -- structured output instead of plain text
- `fl=timestamp,original,mimetype,statuscode,digest,length` -- which fields to return

### URL Variants to Query

Twitter content was archived under multiple URL patterns. All of these must be queried separately to build a complete picture:

| Target Pattern | Purpose |
|---|---|
| `twitter.com/{Handle}` | Desktop timeline page |
| `twitter.com/{handle}` | Lowercase variant (different CDX entries) |
| `mobile.twitter.com/{Handle}` | Mobile timeline page |
| `mobile.twitter.com/{handle}` | Mobile lowercase variant |
| `twitter.com/{Handle}/status/*` | Individual tweet pages (the goldmine) |
| `twitter.com/{handle}/status/*` | Lowercase tweet pages |
| `mobile.twitter.com/{Handle}/status/*` | Mobile tweet pages |
| `mobile.twitter.com/{handle}/status/*` | Mobile lowercase tweet pages |

The critical insight: **the status wildcard queries** (`/status/*`) return rows for every individually archived tweet URL. In this case, 25 visible timeline snapshots expanded to **7,826 status-page capture rows** covering **756 unique tweet IDs**.

### What the CDX Index Tells You

Each CDX row contains:

| Field | Use |
|---|---|
| `timestamp` | When Wayback crawled it (format: `YYYYMMDDHHmmss`) |
| `original` | The original URL that was crawled |
| `mimetype` | What format the capture is stored in |
| `statuscode` | HTTP status at time of crawl |
| `digest` | Content hash (for deduplication) |
| `length` | Payload size |

The **mimetype** and **statuscode** distributions reveal what kind of recoveries are possible:

**Status codes:**
- `200` (5,440 rows) -- the capture succeeded and content is available
- `301/302` (1,752 rows) -- redirects; often no cached payload, but sometimes the redirect target was also archived
- `404` (310 rows) -- the tweet was already deleted when Wayback tried to crawl it
- `429` (6 rows) -- rate limited during crawl
- `-` (318 rows) -- unknown/replay artifacts

**MIME types (for 200 responses):**
- `text/html` (5,780 rows) -- full rendered tweet pages
- `application/json` (276 rows) -- raw Twitter API JSON payloads (highest quality!)
- `text/plain` (148 rows) -- typically minimal content
- `warc/revisit` (42 rows) -- deduplicated WARC records pointing to earlier captures

### Ranking Captures per Tweet

Each tweet ID often has multiple captures at different timestamps. Rank them:

1. **HTTP 200 + `application/json`** -- best: direct tweet payload with full metadata
2. **HTTP 200 + `text/html`** -- good: parseable tweet page
3. **HTTP 200/302 + `text/plain`** -- partial: may contain some data
4. **Other success codes** -- marginal
5. **Errors/redirects** -- metadata-only (tweet ID, archive timestamp, URL)

---

## Phase 2: Fetching Archived Captures

### Constructing Replay URLs

To fetch an archived capture, construct a Wayback replay URL:

```
https://web.archive.org/web/{timestamp}id_/{original_url}
```

The `id_` suffix after the timestamp is important -- it tells Wayback to return the **raw original content** rather than the rewritten replay version (which inserts Wayback's toolbar and rewrites links).

### Rate Limiting and Checkpointing

Wayback rate-limits aggressive fetching. The extraction used:
- **0.2s base sleep** between requests with random jitter
- **Exponential backoff** on 429/5xx responses (up to 4 retries)
- **Checkpointed caching**: every fetched payload saved to disk immediately, so reruns skip already-fetched captures
- **Offline mode**: full re-analysis possible from cached payloads without any network access

### Organizing Raw Captures

Save raw payloads in format-specific directories for later re-analysis:

```
raw/
  cdx/                    # CDX inventory snapshots (8 JSON files)
  status_json/            # JSON captures (highest priority)
  status_html/            # HTML captures
  status_misc/            # text/plain and other formats
  timeline_html/          # Timeline page captures
```

Each file named `{tweet_id}_{timestamp}.{ext}` for deterministic deduplication.

---

## Phase 3: Parsing -- Extracting Tweet Content from Multiple Formats

This is where most of the "invisible" recoveries happen. Each capture format requires different extraction logic, applied in a cascade from highest to lowest quality.

### 3A: JSON Captures (Highest Quality)

When Wayback captured a raw Twitter API response (mimetype `application/json`), the content is a direct tweet object. Parse it with `json.loads()` and extract:

**Tweet content:**
- `full_text` or `text` (check `extended_tweet.full_text` for pre-280-character API responses)
- `created_at` (Twitter date format: `"Mon Jun 04 08:50:54 +0000 2018"`)
- `source` (posting client, e.g. "Twitter for iPhone")
- `lang`, `retweet_count`, `favorite_count`, `quote_count`, `reply_count`

**Threading/relationship flags:**
- `in_reply_to_status_id_str`, `in_reply_to_screen_name`
- `retweeted_status` (nested object if it's a retweet)
- `is_quote_status`, `quoted_status_id_str`
- `conversation_id_str`

**Entities (embedded links, media, mentions):**
- `entities.hashtags[].text`
- `entities.user_mentions[].screen_name`
- `entities.urls[].expanded_url`
- `extended_entities.media[].media_url_https` (images, video thumbnails)

**User metadata snapshot (attached to each tweet):**
- `user.screen_name`, `user.name`, `user.description`
- `user.followers_count`, `user.friends_count`, `user.statuses_count`
- `user.profile_image_url_https`, `user.profile_banner_url`

Each JSON-backed tweet gives you a **timestamped profile snapshot** for free -- over time, these build a time-series of account growth.

### 3B: HTML Captures -- Classic Twitter Markup

Most captures are full HTML tweet pages. These require regex-based extraction from Twitter's server-rendered markup (pre-React era, roughly 2016-2019). Key selectors:

**Tweet text** (try in order):
```
<p class="TweetTextSize...tweet-text"...>...</p>
<div class="js-tweet-text-container">...<p>...</p>
<div class="tweet-text">...</div>
```

**Timestamps:**
```
data-time-ms="1536984988000"      # Millisecond epoch
data-time="1536984988"             # Second epoch
```

**User data:**
```
data-screen-name="RecoveredHandle"
data-name="Recovered Account"
data-user-id="<user_id>"
```

**Structural markers:**
```
data-tweet-id="<tweet_id>"
data-item-id="<tweet_id>"
data-is-reply-to="true"
data-conversation-id="..."
```

**Embedded URLs and media:**
```
data-expanded-url="https://..."
data-url="https://..."
https://pbs.twimg.com/media/...
```

**Fallback -- OG meta tags:**
```html
<meta property="og:description" content="...tweet text preview...">
```

When direct text extraction fails, the `og:description` meta tag often preserves a truncated version of the tweet text.

#### Isolating the Target Tweet Block

A single HTML page may contain multiple tweets (quoted tweets, replies in thread). To avoid extracting the wrong tweet's text, first isolate the block containing the target tweet ID using structural markers:

```python
markers = [
    f'data-tweet-id="{tweet_id}"',
    f'data-item-id="{tweet_id}"',
    f'data-associated-tweet-id="{tweet_id}"',
]
```

Then search backward for the containing `<div class="tweet ..."` or `<li class="js-stream-item"` and forward for the next `</li>` or footer boundary.

### 3C: Embedded JSON in HTML (Hidden Gold)

Some HTML captures -- especially from later Twitter versions -- contain **JavaScript-embedded JSON payloads** that are richer than the visible HTML. These are assigned to window variables:

```javascript
window.__PREFETCH_DATA__ = { ... };
window.__INITIAL_STATE__ = { ... };
window.__META_DATA__ = { ... };
```

Extracting these requires a **balanced-brace matching algorithm** (not regex) because the JSON objects can be hundreds of kilobytes and contain nested braces, quoted strings, and escape sequences:

```python
def extract_balanced_object(text, brace_idx):
    depth = 0
    in_string = False
    escape = False
    for idx in range(brace_idx, len(text)):
        ch = text[idx]
        if in_string:
            if escape: escape = False
            elif ch == "\\": escape = True
            elif ch == quote: in_string = False
            continue
        if ch in {'"', "'"}:
            in_string = True; quote = ch; continue
        if ch == "{": depth += 1
        if ch == "}":
            depth -= 1
            if depth == 0:
                return text[brace_idx:idx+1]
```

Once extracted, these JSON blobs contain tweet objects in Twitter's internal API format. The tweet data may be nested several levels deep using a `legacy` wrapper:

```json
{
  "rest_id": "<tweet_id>",
  "core": {
    "user_results": {
      "result": {
        "rest_id": "<user_id>",
        "legacy": {
          "screen_name": "RecoveredHandle",
          "name": "Recovered Account",
          "followers_count": 27198
        }
      }
    }
  },
  "legacy": {
    "full_text": "Actual tweet text here...",
    "created_at": "Mon Sep 10 04:48:37 +0000 2018"
  }
}
```

The extractor walks the entire nested JSON tree looking for dicts matching the target tweet ID, then normalizes the `legacy` wrapper into a standard tweet object. This technique alone recovered **40 additional tweets** that pure HTML parsing missed.

### 3D: Timeline Page Extraction

Timeline pages (`twitter.com/{Handle}`, not individual status URLs) contain lists of tweets in `<li class="js-stream-item">` blocks. Each block can be parsed the same way as HTML status pages. These contribute:

- **Additional tweet IDs** not found in the status inventory (53 tweet IDs found only through timelines)
- **Profile metadata** from the page header (follower counts, bio, display name)
- **Enrichment** for tweets already in the status inventory but with missing fields

However, many timeline captures (especially mobile) are JavaScript shells with no pre-rendered content, so timelines are a supplement, not a primary source.

---

## Phase 4: Date Recovery

Tweet creation dates can be derived through multiple methods, applied as a fallback chain:

1. **JSON payload** `created_at` field -- most accurate, from Twitter's own metadata
2. **HTML `data-time-ms`** attribute -- millisecond Unix epoch embedded in page markup
3. **HTML `data-time`** attribute -- second-precision Unix epoch
4. **Twitter Snowflake ID** -- the tweet ID itself encodes a timestamp:

```python
TWITTER_EPOCH_MS = 1288834974657  # Nov 4, 2010 01:42:54.657 UTC

def snowflake_to_datetime(tweet_id):
    created_ms = (int(tweet_id) >> 22) + TWITTER_EPOCH_MS
    return datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc)
```

The Snowflake method works for every tweet ID and provides ~millisecond precision, making it a reliable last resort even when no payload was recovered.

---

## Phase 5: Text Cleaning

Archived content often has encoding issues. Key cleaning steps:

### Mojibake Repair

When Wayback stored UTF-8 content but served it as Latin-1 (or vice versa), you get garbled text like `â€™` instead of `'`. Detection and repair:

```python
def maybe_fix_mojibake(text):
    markers = ("Ã", "â€", "â€™", "â€œ", "ðŸ", "Â")
    if not any(m in text for m in markers):
        return text
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text
```

### HTML Tag Stripping

Tweet text from HTML captures contains embedded tags (`<a>`, `<span>`, `<img>` for emoji). The cleaner converts `<br>` to newlines, strips all other tags, then normalizes whitespace.

---

## Phase 6: Deduplication and Provenance

### Deduplication Rules

- **Primary key:** tweet ID
- **Prefer** JSON-sourced text over HTML-sourced text over timeline-sourced text
- **Preserve all captures** in a `capture_history` array per tweet (timestamp, URL, mimetype, status code)
- If multiple captures disagree on text content, prefer the JSON capture, then the earliest 200/HTML capture

### Provenance Tracking

Every recovered tweet links back to its specific archive capture:

```json
{
  "tweet_id": "<tweet_id>",
  "archive_capture_timestamp": "20180910050000",
  "archive_capture_url": "https://web.archive.org/web/20180910050000id_/...",
  "archive_capture_mimetype": "application/json",
  "raw_payload_path": "raw/status_json/<tweet_id>_20180910050000.json",
  "capture_history": [ ... all captures for this tweet ... ]
}
```

This makes every claim verifiable against the original archived content.

---

## Phase 7: Forensic Refresh -- Second-Pass Recovery

After the initial extraction, a **forensic refresh** script re-parses all cached payloads (no network access needed) using improved heuristics. This recovered **87 additional tweets**:

- 47 from improved HTML parsing (better regex patterns, broader block isolation)
- 40 from embedded JSON extraction (`__PREFETCH_DATA__`, `__INITIAL_STATE__`)

The key insight: by caching all raw payloads to disk during initial extraction, you can iterate on parsing logic indefinitely without re-fetching anything from Wayback.

---

## Phase 8: Residual Triage -- Understanding What Can't Be Recovered

After full extraction and forensic refresh, **271 tweet IDs** remained as metadata-only (ID known, text not recovered). Classifying these residuals reveals whether further effort is worthwhile:

| Category | Count | Description |
|---|---|---|
| `responsive_tombstone` | 136 | Modern Twitter/X HTML shell showing "This tweet is unavailable" |
| `cdx_redirect_no_raw` | 65 | CDX row is a 301/302 redirect with no cached payload |
| `cdx_not_found_no_raw` | 63 | Best capture was a 404 |
| `desktop_wrong_target` | 6 | Archived page resolved to a different tweet ID |
| `other` | 1 | Miscellaneous edge case |

**Key finding:** zero residual rows contained the target tweet's text in the cached payload. The remaining gaps are genuine -- the content was never captured or was already deleted when Wayback crawled it.

---

## Phase 9: Ecosystem Expansion -- Using Recovered Data to Find More

The recovered tweets themselves contain pointers to additional content:

### Network Analysis
Extract all `@mentions`, reply targets, and retweet sources from recovered tweets. Build an interaction graph to identify the account's most frequent conversation partners. Then search for *their* tweets that reference the deleted account:

```
from:{partner_handle} (@DeletedAccount OR to:DeletedAccount)
from:{partner_handle} (url:twitter.com/DeletedAccount/status)
```

### Conversation Thread Recovery
Extract `conversation_id` values from recovered tweets. Search for other participants in those threads:

```
conversation_id:{thread_root_id}
```

### URL-Seed Queries
Extract URLs shared in recovered tweets (YouTube links, blog posts, archived articles). Search for other accounts that shared the same URLs -- they may have quoted or replied to the deleted account's tweets about that content.

### Quote-Tweet Recovery
Search for tweets quoting specific recovered tweet URLs:

```
from:{partner} (url:twitter.com/DeletedAccount/status/{tweet_id})
```

These secondary searches, run via the Twitter/X search API (or scraping tools like Apify), can surface content from the deleted account that was quoted, screenshotted, or discussed by others.

### Additional Search-Widening Methods Not Yet Implemented Here

The current repo focuses on direct Wayback recovery from the supplied account handle plus offline reparsing of cached captures. Several useful widening strategies remain outside the shipped scripts:

- **Handle-history expansion.** If the account ever renamed itself, query CDX separately for all known historical handles and merge inventories. The current extractor only targets the supplied handle plus its lowercase form.
- **Additional permalink families.** Add CDX targets for modern `x.com` status URLs and internal permalink variants such as `twitter.com/i/web/status/{tweet_id}` when relevant. Some captures live under alternate host/path combinations even when the canonical desktop URL is sparse.
- **Relationship-driven second pass.** Use recovered `in_reply_to_status_id`, `quoted_status_id`, `retweeted_status_id`, and `conversation_id` fields to queue additional archive searches for thread roots, quoted tweets, and neighboring replies.
- **Media-side archive recovery.** Query archived `pbs.twimg.com`, `video.twimg.com`, and `pic.twitter.com` assets referenced by recovered tweets. Media sometimes survives even when the tweet page does not, and screenshots or thumbnails can preserve otherwise missing context.
- **Digest/revisit resolution.** Use CDX `digest` clusters and `warc/revisit` records to chase the original payload-bearing capture for duplicate rows instead of treating revisit entries as terminal metadata.
- **Automated ecosystem expansion.** This methodology describes mention-network, quote-tweet, conversation, and shared-URL expansion, but the repo does not yet ship scripts that operationalize those second-pass searches.

---

## Results Summary

| Metric | Value |
|---|---|
| Visible timeline snapshots (first glance) | 25 |
| Total CDX capture rows discovered | 7,826 |
| Unique tweet IDs in archive | 756 |
| Additional IDs from timeline pages | +53 |
| **Total tweets with recovered full text** | **809** |
| -- from JSON captures | 135 |
| -- from HTML captures | 311 |
| -- from timeline extraction | 92 |
| -- from forensic refresh | +87 |
| -- from embedded JSON in HTML | (40 of the 87) |
| Metadata-only (ID but no text) | 271 |
| Profile snapshots recovered | 472 |
| Date range covered | Nov 2016 -- Oct 2018 |

From 25 visible snapshots to 809 recovered tweets -- a **32x multiplier** -- using only the Wayback Machine's own data.

---

## Tools and Scripts Used

| Script | Purpose |
|---|---|
| `extract_twitter_wayback.py` | Main extractor: CDX inventory, fetch, parse, deduplicate (1,625 lines) |
| `forensic_refresh_twitter_wayback.py` | Offline re-parse of cached payloads with improved heuristics |
| `triage_twitter_wayback_residuals.py` | Classify unrecoverable rows |
| `twitter_wayback_to_csv.py` | Export to CSV format |

---

## Key Takeaways

1. **The CDX API is the real starting point.** The Wayback Machine web UI only shows timeline snapshots. The CDX index reveals individually archived tweet URLs -- often thousands of them.

2. **Status pages outnumber timeline pages by orders of magnitude.** For this account: 74 timeline captures vs 7,826 status captures.

3. **JSON captures are gold.** ~3.5% of captures were raw API JSON, but they provided the highest-quality recoveries with full metadata, engagement counts, and user profile snapshots.

4. **Embedded JSON in HTML is hidden gold.** Twitter's later frontend embedded `__PREFETCH_DATA__` and `__INITIAL_STATE__` JavaScript objects containing complete tweet payloads -- recoverable with balanced-brace extraction from HTML captures.

5. **Cache everything locally.** Raw payloads saved to disk allow unlimited re-analysis as parsing heuristics improve, without re-fetching from Wayback.

6. **Multiple parsing strategies stack.** JSON -> HTML -> embedded JSON -> timeline -> OG meta tags. Each layer recovers tweets the previous layers missed.

7. **Twitter Snowflake IDs encode timestamps.** Even when no payload is recovered, the tweet ID itself gives you the creation date to millisecond precision.

8. **Mojibake is common in archived content.** Always check for UTF-8-as-Latin-1 encoding corruption and attempt round-trip repair.

9. **Triage your residuals.** Understanding *why* remaining tweets can't be recovered (tombstones, 404s, missing payloads) tells you whether further effort is worthwhile and prevents wasted re-fetches.

10. **Recovered content seeds further discovery.** Mentions, conversation IDs, and shared URLs in recovered tweets point to secondary sources where additional content may survive.
