# ClipScheduler

From the repository root, `python run_all_apps.py --carootclips --schedule` simulates the
start-of-day plan creation (default: tomorrow at 00:00; override with
`--schedule-date=YYYY-MM-DD`) and logs the slots, YouTube slot, Instagram views and
ranking. It is read-only: no plan, history or token writes, no alerts, no sourcing or
publishing. Use `--vrlclipmoments` for that account. The direct Scheduler entry point also
accepts `--schedule` with `CLIPPER_PROFILE` set.

ClipScheduler runs one shared schedule per account. `CREATORS=name:ig_daily:yt_daily,...` in `accounts\<account>.env` supplies separate per-creator platform quotas. Creators are randomly mixed across the generated slots and each slot advances after publish or source failure. Creator assignments and display names persist in the plan, queue marker, simulations and schedule emails.

## Queue contract

When the scheduler decides the pipeline is ready, it writes queue-level scheduler files:

- `<QUEUE_DIR>/scheduler_plan.json` contains that day's generated slot timestamps
- `<QUEUE_DIR>/scheduler_state.json` contains current slot state (`scheduled`, `source_consumed`, `edited`, `published`, `source_failed`, `complete`) and the current slot index

ClipSourcer watches for that marker before it sources a clip.

## Scheduling behavior

- On startup (and at the start of each new local day) the scheduler generates that day's slot list. Slots are spread evenly across the `SCHEDULE_START_HOUR`–`SCHEDULE_END_HOUR` window, never closer than `MIN_GAP_MINUTES`.
- A full-day plan uses the exact per-creator quotas (`sum(Instagram)` in replay mode; `sum(max(Instagram, YouTube))` in shared mode). Impossible full-day quotas raise a clear error; widen the window or reduce `MIN_GAP_MINUTES`. Starting partway through scales the plan down (halfway through ≈ half the slots), randomly retaining creator tickets without exceeding any quota. Nothing is planned after the window closes.
- Each slot fires exactly once. A slot that comes due while the previous cycle is still active waits for that cycle to finish; overdue slots are coalesced into a single firing rather than fired back to back. A cycle with no progress for 3 hours is abandoned.
- `--now` still marks the queue ready immediately.
- `--stop-after` is a fresh-cycle test run: it archives existing queue items before firing, preventing old clips from falsely satisfying the requested stage. Normal production restarts use the saved plan to retry the interrupted slot and archive its unfinished queue items first.

## YouTube from yesterday's Instagram posts

The migrated accounts use `YOUTUBE_SELECTION_MODE=instagram_top` with a YouTube quota of 1 in each creator entry. Daily plan creation prepares a separate YouTube shortlist from yesterday's successful Instagram posts, ranking within each creator by Instagram Insights views and selecting up to that creator's quota. Selected creators are randomly mixed into the account's morning slots. No creator borrows another's unused quota or reserves.

- YouTube uses `YOUTUBE_SCHEDULE_START_HOUR=10` and `YOUTUBE_SCHEDULE_END_HOUR=12`, with randomized spacing and the quota capped by eligible clips and window capacity. Unused peak-hour settings have been removed.
- Instagram keeps its existing window, randomized quota, sourcing, AI, and editing flow. YouTube is no longer randomly assigned to an Instagram slot in this mode.
- Selected archived videos are uploaded directly to YouTube; the replay path never publishes to Instagram or sources a new TikTok clip.
- In this mode ClipAI writes YouTube title/description for every clip (one extra Gemini call each), since any Instagram post may be replayed. For older clips, run `python apps\ClipAI\main.py --carootclips --backfill-youtube YYYY-MM-DD` (archived, successfully published clips since that date lacking metadata; existing files are kept). Creator snapshots select the correct identity during backfill. Clips without metadata fall back to Instagram text and log a warning.
- No successful Instagram posts yesterday means no YouTube uploads today. No earlier-day or random fallback is used.
- Missing Insights permissions or unavailable view counts are logged and retried every five minutes while the YouTube window remains open; Instagram scheduling continues. The token needs the applicable Insights permissions (`instagram_manage_insights` for Facebook Login).
- Daily selection and upload progress persist independently in `youtube_plan.json` and `youtube_history.json`. Restarts preserve these files. Starting after noon does not trigger a late YouTube upload.
- Existing saved Instagram slots are retained on rollout; the new morning slots start with the next newly generated daily plan. `YOUTUBE_SELECTION_MODE=shared` retains the previous shared-slot behavior.

## Run the app

```powershell
cd apps\ClipScheduler
python main.py
```

Use `--once` to trigger a single schedule evaluation immediately.

## Daily report email

Set `DAILY_REPORT_TIME=23:00` in the root `.env` to choose the daily report time
(24-hour `HH:MM`, default 11 PM in `TIMEZONE_NAME`). Keep the report time and timezone
shared across profiles. The running scheduler sends one consolidated email for all
profiles, including each scheduled platform's recorded outcome, skipped/failed posts,
immediate runs, and the day's total estimated Apify cost. A pending post is not treated
as successful just because its scheduler slot has advanced. The report is a snapshot;
posts scheduled after the report time remain pending.

Delivery uses the existing `ALERT_EMAIL_*` settings. A restart after the report time
sends today's report if it has not already been delivered; failed delivery retries
once per minute. Test mode and schedule simulation do not send reports.

Apify requests mean **scraped clips returned**, including filtered-out results and
extra actor calls for download fallbacks, at **CAD $4.26 per 1,000 clips**. Cache-only
runs and YouTube replays cost zero. Failed actor calls with no usable dataset have
unknown usage and are called out rather than counted as free. Costs are estimates,
not an Apify invoice. Tracking begins with deployment (no historical backfill).
Usage and post outcomes persist in `<QUEUE_DIR parent>/<QUEUE_DIR name>_reporting.sqlite3`,
outside the queue's startup cleanup.
