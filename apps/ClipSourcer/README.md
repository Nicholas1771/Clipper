# ClipSourcer

Simple Python sourcer that polls TikTok for recent clips matching the active profile's hashtag, ranks them by a lightweight viral score, and writes the best new candidate into the shared queue before ClipPublisher's posting window opens.

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
| `QUEUE_DIR` | shared queue folder used by the pipeline | `data/queue` |
| `OUTPUT_DIR` | optional override for the shared queue folder consumed by ClipPublisher | same as `QUEUE_DIR` |
| `STATE_FILE` | sourced-ID state file | `data/state.json` |
| `SOURCE_HISTORY_FILE` | daily sourcing history file | `data/source_history.json` |
| `SOURCE_SCHEDULE_FILE` | persisted randomized schedule for the current sourcing day | `data/source_schedule.json` |
| `APIFY_ACTOR_ID` | Apify actor slug for TikTok sourcing | `clockworks~tiktok-scraper` |
| `DOWNLOAD_VIDEO_FILES` | request and download raw video files when the clip author is authorized | `false` |
| `AUTHORIZED_DOWNLOAD_PROFILES` | comma-separated TikTok handles allowed for video download | empty |
| `MAX_SOURCE_RUNS_PER_DAY` | max successful sourcing runs per local day | `1` |
| `MIN_SOURCE_GAP_MINUTES` | minimum spacing between sourcing runs | `1200` |
| `SOURCE_TARGET_POSTING_START_HOUR` | local posting-window start hour that sourcing should lead | `18` |
| `SOURCE_LEAD_MINUTES` | target minutes before the posting window when sourcing should run | `30` |
| `SOURCE_JITTER_MINUTES` | randomized variation around the lead-time target | `15` |
| `SOURCE_GRACE_MINUTES` | how long the sourcer may still run after that day's randomized target time | `90` |
| `SOURCE_TIMEZONE_NAME` | IANA timezone used for daily sourcing schedule | `America/New_York` |

## Authorized downloads

The best approach for raw video downloads is to source from your own profile, ask Apify for downloadable video URLs, and only save files when the clip author is on an explicit allowlist.

1. set `SOURCE_MODE=profiles`
2. set `SOURCE_PROFILES` to your own TikTok handle
3. set `AUTHORIZED_DOWNLOAD_PROFILES` to the same handle
4. set `DOWNLOAD_VIDEO_FILES=true`

When enabled, the app saves the raw video file next to the clip's JSON manifest inside a single numbered run folder under `OUTPUT_DIR` (for example `sourced_clips/0001_20261002T232000Z_winner_12345/...`) only if the selected clip belongs to one of the authorized handles.

Download order:

1. `yt-dlp`
2. TikWM API
3. Apify-hosted media download

Each attempt is validated, and any partial file is deleted before the next fallback starts.

## Cost note

Apify's TikTok Scraper docs show `resultsPerPage`, hashtags, profiles, search, download toggles, and popularity filters based on likes, but **not a server-side minimum view filter**. So `MINIMUM_VIEW_COUNT` helps the app ignore low-view clips locally, but it does **not** reduce Apify billing by itself. The main cost levers are keeping `RESULTS_PER_PAGE` low and using narrower sources such as owned profiles instead of broad hashtag pulls. The fallback downloader also keeps Apify as the last resort so a successful `yt-dlp` or TikWM download avoids Apify's paid video-download add-on.

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
