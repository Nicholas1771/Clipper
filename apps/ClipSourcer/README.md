# ClipSourcer

Accounts select reusable creators with `CREATORS=name:ig_daily:yt_daily,...` in
`accounts\<account>.env`. Sourcing targets, filters, download authorization and branding
live in ignored `creators\<name>.env` files (copy `creators\template.env.example`).
The account scheduler assigns a creator to each randomized slot. Sourcing snapshots
that identity to `creator.json` for downstream stages and notifications.

`REQUIRE_SOURCE_KICK_BRANDING=true` requires the original downloaded clip to pass
the same strict visual Kick detector used by ClipAI **before it can enter the
candidate cache**. Missing branding is logged, skipped and remembered in attempted
history; unverified candidates are never cached. Detector outages fail the attempt
and may be retried, rather than permanently rejecting candidates. This uses shared
Gemini settings from the AI/root envs and adds download/AI work when fetching a fresh
batch. False allows sources without branding, with normal editor branding afterward.
If all scheduled sourcing attempts end without a candidate, a formatted failure email
reports the attempt results and why no clip could be queued.
N3on uses false; Navaeh uses true. Existing cache scopes are invalidated when the
branding policy changes; published seen history is not reset. Caches are isolated by
account and creator; the account's seen history remains shared.

Simple Python sourcer that polls TikTok for clips matching the scheduled creator, ranks them by a lightweight viral score, and writes the best new candidate into the account's shared queue. ClipScheduler is the only schedule owner; Sourcer no longer has an independent legacy daily schedule or sourcing cap. `--once` explicitly bypasses the scheduler for a single-creator account; multi-creator runs require an assigned scheduler slot.

I chose **TikTok** as the default source because Instagram's official hashtag access is more restricted and requires reviewed business-style access, while TikTok sourcing is easier to stand up through Apify's scraper API.

## What it does

- polls recent TikTok hashtag or profile results through Apify
- keeps only posts from the last `LOOKBACK_HOURS`
- filters for a keyword configured by the active profile
- can require a minimum play count before a clip is considered
- ranks candidates by engagement adjusted for age
- writes one new manifest per sourcing day into a numbered queue directory under the shared queue path
- can optionally download the raw video file for clips from explicitly authorized profiles with validated fallbacks
- remembers already sourced clip IDs in `data/state.json`
- uses a persisted once-daily randomized local-time sourcing window so the queue is populated before the publisher can post

## Setup

1. Create and activate a virtual environment.
2. Install dependencies:

   ```powershell
   pip install -r requirements.txt
   ```

3. Copy `.env.example` to `.env` and set `APIFY_TOKEN`.

## Configuration

| Variable | Purpose | Default |
| --- | --- | --- |
| `SOURCE_MODE` | `hashtag` or `profiles` | `hashtag` |
| `SOURCE_HASHTAG` | hashtag to poll in hashtag mode | profile setting |
| `SOURCE_PROFILES` | comma-separated TikTok profiles in profile mode | empty |
| `KEYWORD_FILTER` | text or hashtag match required in results | profile setting |
| `POLL_INTERVAL_MINUTES` | minutes between cycles | `60` |
| `LOOKBACK_HOURS` | max clip age | `4` |
| `RESULTS_PER_PAGE` | items requested per poll | `25` |
| `MINIMUM_VIEW_COUNT` | local minimum `playCount` required before a clip is eligible | `0` |
| `QUEUE_DIR` | account queue folder used by every pipeline stage | `data/state/{ACCOUNT_NAME}/queue` |
| `STATE_FILE` | successfully published seen-ID history | `data/state/{ACCOUNT_NAME}/state.json` |
| `CANDIDATE_CACHE_FILE` | persistent cache of eligible unused results; `{CREATOR_NAME}` is expanded per slot | `data/state/{ACCOUNT_NAME}/candidate_cache_{CREATOR_NAME}.json` |
| `APIFY_ACTOR_ID` | Apify actor slug for TikTok sourcing | `clockworks~tiktok-scraper` |
| `DOWNLOAD_VIDEO_FILES` | request and download raw video files when the clip author is authorized | `false` |
| `AUTHORIZED_DOWNLOAD_PROFILES` | comma-separated TikTok handles allowed for video download | empty |

## Authorized downloads

The best approach for raw video downloads is to source from your own profile, ask Apify for downloadable video URLs, and only save files when the clip author is on an explicit allowlist.

1. set `SOURCE_MODE=profiles`
2. set `SOURCE_PROFILES` to your own TikTok handle
3. set `AUTHORIZED_DOWNLOAD_PROFILES` to the same handle
4. set `DOWNLOAD_VIDEO_FILES=true`

Set these authorization options in the creator env. When enabled, the app saves the raw video next to its JSON manifest inside a numbered run folder under `QUEUE_DIR`, only when the selected author is authorized.

Download order:

1. `yt-dlp`
2. TikWM API
3. Apify-hosted media download

Each attempt is validated, and any partial file is deleted before the next fallback starts.

## Cost note

Apify's TikTok Scraper docs show `resultsPerPage`, hashtags, profiles, search, download toggles, and popularity filters based on likes, but **not a server-side minimum view filter**. So `MINIMUM_VIEW_COUNT` helps the app ignore low-view clips locally, but it does **not** reduce Apify billing by itself. The main cost levers are keeping `RESULTS_PER_PAGE` low and using narrower sources such as owned profiles instead of broad hashtag pulls. The fallback downloader also keeps Apify as the last resort so a successful `yt-dlp` or TikWM download avoids Apify's paid video-download add-on.

Each sourcing attempt logs the returned results' view-count distribution in ranges from `<1,000` through `>=10,000,000`, before applying the app's local eligibility filters. It also logs one diagnostic line per clip with its ID, author, engagement counts, view count, viral score, eligibility and selection ranks, filter outcomes, seen status, rejection reasons, URL, and caption snippet.

Eligible, unused results are persisted in `CANDIDATE_CACHE_FILE` and reused before making another Apify request. Cached clips are rechecked against the current lookback, keyword, and minimum-view filters. Before processing a selected clip, it is removed from the cache and reserved in a separate `<cache-stem>_attempted.json` file alongside the cache. Attempted clips are excluded from future selection, including after restarts or failures in download, AI, editing, or publishing. Only a successful publish to at least one assigned platform adds a clip to `seen_ids`; caching, queueing, and failed publishes do not. The cache is scoped to the actor and configured source, so changing the hashtag/profile or actor causes a fresh fetch.

After saving a fresh Apify batch to the cache, an INFO line identifies each saved clip by ID, author, views, current viral score, post date, and URL. When attempting a cached clip, an INFO line logs those same details as `Using candidate from cache`. Removing the selected clip logs only the remaining cache size, not misleading per-item save messages. Full per-result selection diagnostics are DEBUG-level logs.

Each sourcing attempt logs the unseen, unexpired cache size and whether candidates come from the cache or a new Apify request. Successful cache saves also log the resulting cache size, including zero when exhausted.

## Usage

Use `--once` for an immediate one-off run that bypasses the daily schedule:

```powershell
python main.py --once
```

Run continuously when you want the sourcer to wait for the daily randomized source window and queue one best item before the publisher window:

```powershell
python main.py
```

Each sourced manifest is a JSON file with:

- clip ID and TikTok URL
- caption text
- author handle
- timestamps
- engagement counts
- calculated viral score
- optional downloaded video path
- raw provider payload for later analysis
