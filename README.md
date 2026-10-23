# Workspace

This repository contains five pipeline apps plus one shared library app under `apps\`:

- `apps\ClipScheduler`
- `apps\ClipSourcer`
- `apps\ClipAI`
- `apps\ClipEditor`
- `apps\ClipPublisher`
- `apps\Common` (shared helpers used by all apps)

Run them in separate terminals in order:

```powershell
cd apps\ClipScheduler
python main.py
```

```powershell
cd apps\ClipSourcer
python main.py
```

```powershell
cd apps\ClipAI
python main.py
```

```powershell
cd apps\ClipEditor
python main.py
```

```powershell
cd apps\ClipPublisher
python main.py
```

Each app has its own `main.py`, `.env.example`, `README.md`, `requirements.txt`, and package directory.

## Shared root `.env` + per-app `.env`

Keep shared defaults in a root `.env` (copy `.env.example`) and app-specific settings in `apps\<AppName>\.env` (copy that app's `.env.example`). Account credentials and creator quotas live in `accounts\<account>.env`; reusable sourcing and branding settings live in `creators\<creator>.env`. All real env files are git-ignored. Only templates are committed; there are no `.local.env` overrides.

Load order is:

1. Real process environment variables
2. `accounts\<account>.env`
3. `apps\<AppName>\.env`
4. `<repo>\.env`

This means app-specific values override root shared defaults, and explicit exported env vars override both. This works the same for local Windows runs and Linux cloud deployment.

## Accounts and creators

Copy `accounts\template.env.example` and `creators\template.env.example` to create ignored real files. Account files contain the account name, Instagram credentials, publishing mode, optional state-path overrides, and a comma-separated creator list:

```dotenv
ACCOUNT_NAME=ExampleAccount
CREATORS=n3on:8:1,navaeh:4:2
YOUTUBE_SELECTION_MODE=instagram_top
```

Each entry is `creator:Instagram posts per day:YouTube posts per day`. Zero disables that platform for that creator. Quotas belong only in the account file; do not set `POSTS_PER_DAY`, `INSTAGRAM_POSTS_PER_DAY`, or `YOUTUBE_POSTS_PER_DAY` separately. The latter two are derived internally from the creator list. A creator can be used by multiple accounts with different quotas.

Creator files define `STREAMER_NAME`, `STREAMER_HASHTAG`, `OVERLAY_IMAGE_PATH`, sourcing targets/filters, download authorization, and `REQUIRE_SOURCE_KICK_BRANDING`. N3on uses `false`; Navaeh uses `true`. Required branding is checked visually on downloaded source frames **before cache admission**: missing branding skips the candidate, and detector failures block admission rather than assuming approval. `false` allows unbranded sources; the editor can add branding as before. Keep API keys in shared root/app envs, not creator files. Place branding assets in `data\assets`.

```powershell
python run_all_apps.py --carootclips
python run_all_apps.py --vrlclipmoments
```

Adding `creators\another.env` and `another:3:1` to an account is sufficient to include it. Each account runs **one five-app pipeline and one shared schedule**. Creator assignments are shuffled across that account's slots, not scheduled in creator blocks. The full-day plan honors each creator/platform quota; mid-window starts scale the plan down as before. Minimum spacing still applies: if full quotas cannot fit, widen the schedule window or reduce `MIN_GAP_MINUTES`; the scheduler reports a configuration error instead of silently underfilling.

Account queue, seen history, and tokens default to `data\state\{ACCOUNT_NAME}`. Candidate caches are isolated by creator within each account; published seen history stays account-wide. Existing account files retain the original paths, so account renaming does not reset seen IDs, archives or credentials. For a legacy cache path without `{CREATOR_NAME}`, the first creator retains that cache and attempted history; additional creators get suffixed cache files. Each new run stores a `creator.json` snapshot so downstream processing and archived YouTube replays retain the correct creator identity and branding. Schedule, success/failure, and daily-report emails include creator names.

The `--<account>` flag appears automatically. For a single app, use `CLIPPER_PROFILE=<account>` (the existing environment variable now selects an account); if unset, the first available account alphabetically is selected. Changing quotas or adding/removing creators should be done for the next day's plan; same-day saved assignments survive restarts. Legacy queue/archive items without a creator snapshot use the account's first creator, so keep the original creator first when adding more.

To simulate the start-of-day schedule creation (as production does around midnight), then exit:

```powershell
python run_all_apps.py --carootclips --schedule
python run_all_apps.py --carootclips --schedule --schedule-date=2026-10-15
```

Use `--vrlclipmoments` for the other account. The default day is tomorrow, simulated at 00:00 —
so YouTube ranking uses today's successful Instagram posts. This is a **read-only
simulation**: it logs the generated slots, platform assignments, YouTube slot, fetched
Instagram views and ranking, but writes no plan/history/token files, sends no emails,
refreshes no token, and starts no Sourcer, AI, Editor or Publisher. Slot times are random,
so production will pick different times. Only posts in this machine's local archive are
eligible (production-only archives are not available locally). Do not combine with
`--now`, `--stop-after`, or `--test`.

The creator selected for a slot supplies its sourcing, identity and branding settings; these are snapshotted on the queued run, never switched globally in another worker.

Obsolete configuration has been removed: Publisher retry/cadence knobs (publishing is single-attempt and Scheduler-owned), scheduler peak-hour knobs (slots use stratified random spacing), Sourcer's legacy independent daily schedule/caps/history files and `OUTPUT_DIR` alias, and old watermark/trim aliases. Existing runtime history files are left untouched.

## YouTube Shorts publishing

Each account can also upload to YouTube. Set separate per-creator quotas in `CREATORS`. The migrated accounts use `YOUTUBE_SELECTION_MODE=instagram_top`: yesterday's successful Instagram posts are ranked by views **within each creator**, then the top unused clips up to that creator's YouTube quota are randomly mixed into the account's separate 10 AM-noon YouTube window. Insufficient eligible posts for one creator do not increase another's quota. Reserve replacements stay within the scheduled creator. No posts yesterday means no YouTube post today. See [ClipScheduler](apps/ClipScheduler/README.md) for timing, permissions, and restart behavior.

`YOUTUBE_SELECTION_MODE=shared` plans `max(instagram, youtube)` fresh-source slots **per creator**, randomly assigns each platform within that creator's quota, and shuffles all creators into one account schedule.

- The Short's title is the AI caption followed by the streamer hashtag and the AI hashtags, all in the title (100 characters max; hashtags that would not fit are dropped). Shorts have no description.
- One-time login per account: `python apps\ClipPublisher\main.py --carootclips --youtube-auth` (needs `YOUTUBE_CLIENT_ID` / `YOUTUBE_CLIENT_SECRET` in the root `.env`). The refresh token is saved to `YOUTUBE_TOKEN_FILE` (git-ignored).
- Platform selection comes from each creator's `CREATORS` quotas; set a platform's quota to zero to disable it.
- A slot assigned to YouTube while no token exists is skipped with a warning. Platforms publish independently, so one failing does not block the other.

## Run all 5 apps together (recommended)

Use the cross-platform launcher from the repo root:

```powershell
python run_all_apps.py
```

Linux/macOS:

```bash
./run_all_apps.sh
```

The launcher starts apps in this startup order:

1. ClipScheduler
2. ClipSourcer
3. ClipAI
4. ClipEditor
5. ClipPublisher

### Passing flags

Run one pipeline cycle immediately through a selected stage:

```powershell
python run_all_apps.py --now --stop-after=editor
```

In PowerShell, prefer the `--flag=value` form for these launcher args.

Useful launcher options:

- `--now` to start the first cycle immediately
- `--stop-after=<app>` to run a single cycle and stop after the selected stage
- `--test` to run within the queue's `Test` directory
- `--dry-run` to print commands without launching

## Local vs cloud

- **Local Windows testing** is still supported directly from PowerShell; you do not need Git Bash.
- **Linux cloud deployment** is supported by overriding paths/fonts via `.env`:
  - set Linux queue and overlay paths
  - set `WATERMARK_FONT_FILE` or `WATERMARK_FONT_NAME`
  - for ClipPublisher, prefer Cloudflare R2 via `R2_*` settings instead of a local tunnel
- Everything file-based lives under the root `data\` folder: `data\assets` (branding PNGs) and `data\prompts` (AI prompts) are tracked in Git, while per-profile queues and state (`data\state\<ACCOUNT_NAME>\...`) are ignored and stay local to each environment.
- After a real publish from a workstation, `python sync_seen_ids.py --<profile>` appends local seen IDs to production (append-only; automatic when `SYNC_SEEN_TO_PRODUCTION=true` in the workstation `.env`). See `PRODUCTION_DEPLOYMENT.md`.

## Email alerts

Each app supports SMTP alert settings in its `.env`:

- `ALERT_EMAIL_ENABLED`
- `ALERT_EMAIL_SMTP_HOST`
- `ALERT_EMAIL_SMTP_PORT`
- `ALERT_EMAIL_USE_TLS`
- `ALERT_EMAIL_USERNAME`
- `ALERT_EMAIL_PASSWORD`
- `ALERT_EMAIL_FROM`
- `ALERT_EMAIL_TO` (recipient email address)

Alerts are disabled in the examples. When enabling them, configure both a sender and `ALERT_EMAIL_TO`; the app skips delivery rather than using a placeholder recipient.

Behavior:

- ClipPublisher sends a success alert when a clip is posted.
- ClipScheduler, ClipSourcer, ClipAI, ClipEditor, and ClipPublisher send failure alerts when an unhandled runtime failure occurs.
