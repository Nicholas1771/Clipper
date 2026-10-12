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

Load order is now: process env > `profiles\<name>.env` > `apps\<AppName>\.env` > `<repo>\.env`.

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
