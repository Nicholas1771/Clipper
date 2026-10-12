# ClipScheduler

ClipScheduler gates the pipeline so ClipSourcer only runs when a daily schedule slot opens. It treats `POSTS_PER_DAY` as a target, generates a humanized distribution of slot times for the day, and advances one slot at a time after each publish.

## Queue contract

When the scheduler decides the pipeline is ready, it writes queue-level scheduler files:

- `<QUEUE_DIR>/scheduler_plan.json` contains that day's generated slot timestamps
- `<QUEUE_DIR>/scheduler_state.json` contains current slot state (`scheduled`, `source_consumed`, `edited`, `published`, `source_failed`, `complete`) and the current slot index

ClipSourcer watches for that marker before it sources a clip.

## Scheduling behavior

- On startup (and at the start of each new local day) the scheduler generates that day's slot list. Slots are spread evenly across the `SCHEDULE_START_HOUR`–`SCHEDULE_END_HOUR` window, never closer than `MIN_GAP_MINUTES`.
- The slot count is `POSTS_PER_DAY` with a small random spread. If the scheduler starts before the window opens it plans the full day; if it starts partway through, the count is scaled to the remaining window (halfway through ≈ half the posts). Nothing is planned after the window closes.
- Each slot fires exactly once. A slot that comes due while the previous cycle is still active waits for that cycle to finish; overdue slots are coalesced into a single firing rather than fired back to back. A cycle with no progress for 3 hours is abandoned.
- `--now` still marks the queue ready immediately.

## Run the app

```powershell
cd apps\ClipScheduler
python main.py
```

Use `--once` to trigger a single schedule evaluation immediately.
