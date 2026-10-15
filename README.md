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

Keep shared defaults in a root `.env` (copy `.env.example`) and app-specific settings in `apps\<AppName>\.env` (copy that app's `.env.example`). All real `.env` files are git-ignored; put account-specific values in `profiles\<name>.env` (copy `profiles\template.env.example`).

Load order is:

1. Real process environment variables
2. `apps\<AppName>\.env`
3. `<repo>\.env`

This means app-specific values override root shared defaults, and explicit exported env vars override both. This works the same for local Windows runs and Linux cloud deployment.

## Profiles (one config file per account)

Everything that differs per account lives in `profiles\<name>.env`: display name and hashtag, queue folder, state files, source filters, branding image, and Instagram credentials. Shared settings (schedule, API keys, alerts) stay in the root and per-app `.env` files.

```powershell
python run_all_apps.py --<name>    # available flags come from profiles\<name>.env files
```

To add an account, copy `profiles\template.env.example` to `profiles\<name>.env`, replace its sample values, and place the branding image in `data\assets` (or use a full path in `OVERLAY_IMAGE_PATH`). The flag `--<name>` becomes available automatically. Give each profile its own `QUEUE_DIR`, state files, and token state file so runs never mix. All real `*.env` files are git-ignored. When running a single app directly, set `CLIPPER_PROFILE=<name>`; if unset, the first available profile alphabetically is selected.

To simulate the start-of-day schedule creation (as production does around midnight), then exit:

```powershell
python run_all_apps.py --n3on --schedule
python run_all_apps.py --n3on --schedule --schedule-date=2026-10-15
```

Use `--navaeh` for the other account. The default day is tomorrow, simulated at 00:00 —
so YouTube ranking uses today's successful Instagram posts. This is a **read-only
simulation**: it logs the generated slots, platform assignments, YouTube slot, fetched
Instagram views and ranking, but writes no plan/history/token files, sends no emails,
refreshes no token, and starts no Sourcer, AI, Editor or Publisher. Slot times are random,
so production will pick different times. Only posts in this machine's local archive are
eligible (production-only archives are not available locally). Do not combine with
`--now`, `--stop-after`, `--test`, or `--publish`.

Load order is now: process env > `profiles\<name>.env` > `apps\<AppName>\.env` > `<repo>\.env`.

## YouTube Shorts publishing

Each profile can also upload to YouTube. Set `INSTAGRAM_POSTS_PER_DAY` and `YOUTUBE_POSTS_PER_DAY` in the profile (`YOUTUBE_POSTS_PER_DAY=0` disables YouTube). The supplied profiles use `YOUTUBE_SELECTION_MODE=instagram_top`: when the daily plan is first created, yesterday's successful Instagram posts are ranked by Instagram views and the top unused clip is scheduled in a separate 10 AM-noon YouTube window. No posts yesterday means no YouTube post today. Instagram keeps its existing sourcing and schedule. See [ClipScheduler](apps/ClipScheduler/README.md) for timing, permissions, and restart behavior.

`YOUTUBE_SELECTION_MODE=shared` retains the previous behavior: the scheduler plans `max(instagram, youtube)` slots per day and randomly assigns the lighter platform to a subset.

- The Short's title is the AI caption followed by the streamer hashtag and the AI hashtags, all in the title (100 characters max; hashtags that would not fit are dropped). Shorts have no description.
- One-time login per profile: `$env:CLIPPER_PROFILE='n3on'; python apps\ClipPublisher\main.py --youtube-auth` (needs `YOUTUBE_CLIENT_ID` / `YOUTUBE_CLIENT_SECRET` in the root `.env`). The refresh token is saved to `YOUTUBE_TOKEN_FILE` (git-ignored).
- `--publish youtube` or `--publish instagram` on `run_all_apps.py` restricts the run to one platform. In `instagram_top` mode, YouTube-only runs use the separate morning replay plan, not fresh sourcing or an immediate Instagram post. In `shared` mode, every slot targets only the selected platform.
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
