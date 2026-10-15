# ClipScheduler

From the repository root, `python run_all_apps.py --n3on --schedule` simulates the
start-of-day plan creation (default: tomorrow at 00:00; override with
`--schedule-date=YYYY-MM-DD`) and logs the slots, YouTube slot, Instagram views and
ranking. It is read-only: no plan, history or token writes, no alerts, no sourcing or
publishing. Use `--navaeh` for that account. The direct Scheduler entry point also
accepts `--schedule` with `CLIPPER_PROFILE` set.

ClipScheduler gates the pipeline so ClipSourcer only runs when a daily schedule slot opens. It treats `POSTS_PER_DAY` as a target, generates a humanized distribution of slot times for the day, and advances one slot at a time after each publish.

## Queue contract

When the scheduler decides the pipeline is ready, it writes queue-level scheduler files:

- `<QUEUE_DIR>/scheduler_plan.json` contains that day's generated slot timestamps
- `<QUEUE_DIR>/scheduler_state.json` contains current slot state (`scheduled`, `source_consumed`, `edited`, `published`, `source_failed`, `complete`) and the current slot index

ClipSourcer watches for that marker before it sources a clip.

## Scheduling behavior

- On startup (and at the start of each new local day) the scheduler generates that day's slot list. Slots are spread evenly across the `SCHEDULE_START_HOUR`–`SCHEDULE_END_HOUR` window, never closer than `MIN_GAP_MINUTES`.
- The slot count is exactly `POSTS_PER_DAY` (no random spread). If the scheduler starts before the window opens it plans the full day; if it starts partway through, the count is scaled to the remaining window (halfway through ≈ half the posts). Nothing is planned after the window closes.
- Each slot fires exactly once. A slot that comes due while the previous cycle is still active waits for that cycle to finish; overdue slots are coalesced into a single firing rather than fired back to back. A cycle with no progress for 3 hours is abandoned.
- `--now` still marks the queue ready immediately.
- `--stop-after` is a fresh-cycle test run: it archives existing queue items before firing, preventing old clips from falsely satisfying the requested stage. Normal production restarts use the saved plan to retry the interrupted slot and archive its unfinished queue items first.

## YouTube from yesterday's Instagram posts

The supplied profiles use `YOUTUBE_SELECTION_MODE=instagram_top` and `YOUTUBE_POSTS_PER_DAY=1`. When the daily schedule is first created, the scheduler prepares a separate YouTube shortlist from yesterday's successful Instagram posts, using the account's timezone and current Instagram Insights view counts. This runs during normal plan initialization, not at a hardcoded midnight trigger.

- YouTube uses `YOUTUBE_SCHEDULE_START_HOUR=10`, `YOUTUBE_SCHEDULE_END_HOUR=12`, and `YOUTUBE_SCHEDULE_PEAK_HOUR=11`, with the existing randomized window/spacing algorithm and an exact quota capped by available clips and window capacity.
- Instagram keeps its existing window, randomized quota, sourcing, AI, and editing flow. YouTube is no longer randomly assigned to an Instagram slot in this mode.
- Selected archived videos are uploaded directly to YouTube; the replay path never publishes to Instagram or sources a new TikTok clip.
- In this mode ClipAI writes YouTube title/description for every clip (one extra Gemini call each), since any Instagram post may be replayed. For clips published before that change, run `python apps\ClipAI\main.py --n3on --backfill-youtube YYYY-MM-DD` (archived, successfully published clips since that date lacking metadata; existing files are kept). Clips without it fall back to Instagram text and log a warning.
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
