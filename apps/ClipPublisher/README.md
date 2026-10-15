# ClipPublisher

Simple queue consumer for post-processing ClipSourcer output and publishing ready short-form videos to Instagram.

## Best automation approach

The best way to automate Instagram posting is not browser automation. Use the Meta Graph API for Instagram Business Accounts.

Recommended flow:

1. Use an Instagram Business account linked to a Facebook Page.
2. Create a Meta app and add the Instagram Graph API product.
3. Generate a long-lived access token for the Business account or Page.
4. Upload a public video URL using the Graph API.
5. Publish that media object to the Instagram account.

The API flow is:

- `POST /{ig-user-id}/media` with `media_type=VIDEO`, `video_url`, `caption`, `access_token`
- then `POST /{ig-user-id}/media_publish` with the returned `creation_id`

This is far more reliable than automation via Selenium or browser macros, which are fragile and often blocked by Instagram's anti-bot protections.

## Posting cadence

ClipPublisher itself has no independent scheduling window logic. It publishes when an edited queue item is ready and when ClipScheduler has advanced the pipeline.

## Queue behavior

This app watches the configured queue directory and waits for a run directory to be fully ready before claiming it.

`seen_ids` records only clips successfully published to at least one assigned platform. Failed or skipped publishes do not mark a clip seen. Failed items are still quarantined without retry; ClipSourcer tracks attempted candidates separately from published history.

A queue item is considered ready only if:

- the run directory exists
- there is a JSON manifest file
- there is an MP4 file
- the files have been stable for `READY_STABILITY_SECONDS`

The app then claims the item in place by setting its `queue_state.json` phase to `published`, so it is never picked twice. Failed or finished items are moved to `Archive/`.

This gives a simple safety check so the Publisher does not pick up a half-written queue item.

## YouTube replay of yesterday's best Instagram clips

`clippublisher.youtube_replay.YouTubeReplay(config)` replays yesterday's most-viewed Instagram clips to YouTube. ClipScheduler drives it (loading the Publisher `AppConfig` lazily):

- `prepare(now, slots) -> bool`: call when the day's schedule is first generated. "Yesterday" is the previous calendar day in `TIMEZONE_NAME`. It scans `Archive/` (and `processing/`) for successful Instagram `publisher/publish_state.json` records (legacy single-IG `result` or nested `result.instagram`; timestamp `published_at.instagram`, falling back to `updated_at`), requires archived local media, skips clips already on YouTube, reads Meta media insights `metric=views`, and ranks by views (ties: earlier IG publish, then clip id). Returns `True` when a complete snapshot is saved (empty, without API calls, when there are no eligible posts); returns `False` when any eligible clip's views are unknown. A completed snapshot for the same day is never re-ranked.
- `process_due(now) -> bool`: uploads at most one due planned clip with `publish_to_youtube` (never Instagram), using the archived video and the same YouTube metadata. The entry is persisted as `attempted` (plan and `youtube_history.json`) before uploading; failures are logged, alerted and never retried. Success is written to history before the success notification. Sourcer `seen_ids` are not touched.

Both files are written atomically in `QUEUE_DIR`. Publish states now also record `published_at` per platform.

Preparation completes only when every eligible clip has a known views count. Missing metrics and unavailable insights now return `False` for retry, even if other clips have known counts; they are not excluded to produce an incomplete ranking. All-missing insights do not create an empty completed plan. An actual reported count of zero is valid.

Replay honors `YOUTUBE_POSTS_PER_DAY` (default `0`, disabled), `YOUTUBE_SELECTION_MODE` (default `shared`), and `YOUTUBE_SCHEDULE_START_HOUR`/`YOUTUBE_SCHEDULE_END_HOUR` (defaults `10`/`12`). At or after the end hour, remaining pending entries become `failed` with a `missed_window` error without uploading or adding upload history. Daily attempt counts in both plan and history enforce the quota. Plans persist `profile` and `instagram_user_id`; a same-day snapshot from another account, or without account identity, blocks preparation and uploads rather than being reused or reranked. Keep each profile's queue separate.

`INSTAGRAM_INSIGHTS_API_VERSION` pins the dedicated insights endpoint (default `v24.0`; accepts `v23.0` or newer). It does not change the existing Instagram publishing API version. Token and permission failures block preparation for retry after credentials/permissions are repaired; there is no random or zero-view fallback.

Plan entries expose `scheduled_for` (ISO datetime; `slot` is retained for compatibility), `status` (`pending`, `attempted`, `published`, or `failed`), `clip_id`, `media_id`, `run_dir`, `views`, `instagram_published_at`, and `rank`. Account-matched older snapshots with `slot` and `planned` remain usable without re-ranking. When an archived clip has no editor description, replay metadata uses `ai/summary.txt`; dedicated YouTube metadata and nonempty editor descriptions still take precedence. No new AI generation is required.

ClipPublisher calls `process_due` itself on every poll (`run_forever`) and before each `--once` run, only when `YOUTUBE_SELECTION_MODE=instagram_top` (set by the profiles), `CLIPPER_TEST_MODE` is off and `CLIPPER_PUBLISH_ONLY` is not `instagram`. ClipScheduler only calls `prepare`. In that mode shared queue items are never uploaded to YouTube, even if a slot lists it; the default `shared` mode keeps the previous behavior. Replay errors are logged and never block the Instagram queue. Queue discovery only looks at run directories, so the plan/history files (and their temp/backup files) in `QUEUE_DIR` are ignored.

## Required config

Copy `.env.example` to `.env` and set:

- `QUEUE_DIR` — the directory that ClipSourcer writes to, for example `../../data/queue`
- `INSTAGRAM_USER_ID` — your Instagram Business account user ID
- `INSTAGRAM_ACCESS_TOKEN` — a valid Meta user token with permission to publish to Instagram
- `FACEBOOK_APP_ID` — your Meta app ID (required for automatic token refresh)
- `FACEBOOK_APP_SECRET` — your Meta app secret (required for automatic token refresh)
- `TOKEN_STATE_FILE` — where the refreshed token is persisted (default `..\..\data\publisher_token_state.json`)
- `AUTO_REFRESH_TOKEN` — set `true` to auto-inspect and refresh token before expiry
- `REFRESH_BUFFER_DAYS` — refresh threshold in days before expiry (default `10`)
- `MEDIA_BASE_URL` — optional if you have Cloudflared installed; otherwise it must be set manually to a public HTTPS base URL that serves the queue folders, such as `https://example.com/queue`
- `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET_NAME`, `R2_PUBLIC_BASE_URL` — preferred cloud deploy path; if set, Publisher uploads the final MP4 to Cloudflare R2 and uses that public URL for Meta
- `R2_KEY_PREFIX` — optional key prefix inside the bucket (default `processing`)
- `TIMEZONE_NAME` — your IANA timezone, such as `America/New_York`
- `ALERT_EMAIL_*` — optional SMTP settings for publish-success and failure alert emails

## Meta setup (specific steps)

1. In Meta for Developers, create/select your app and add **Facebook Login** + **Instagram Graph API**.
2. Add your account as a tester/developer role in the app while testing.
3. In Facebook Login settings, set a valid OAuth redirect URI for your backend callback.
4. Request these permissions in login: `instagram_basic`, `instagram_content_publish`, `pages_show_list`, `pages_read_engagement` (and `business_management` if your setup needs it).
5. Ensure your Instagram account is Business/Creator and linked to the correct Facebook Page.
6. Complete OAuth once to obtain a user token, then set that as `INSTAGRAM_ACCESS_TOKEN`.
7. Set `FACEBOOK_APP_ID` and `FACEBOOK_APP_SECRET`.
8. Run:

```powershell
python main.py --refresh-token
```

This exchanges/persists a long-lived token into `TOKEN_STATE_FILE`. After that, normal publisher runs will auto-refresh before expiry.

### Replacing a token (including newly granted permissions)

From the repository root, select the account explicitly:

```powershell
python apps\ClipPublisher\main.py --refresh-token --n3on --token=TOKEN
python apps\ClipPublisher\main.py --refresh-token --navaeh --token-prompt
```

`--token-prompt` reads the replacement without displaying it or putting it in shell history.
Prefer it over `--token`, which can expose the credential through history/process arguments.
Never paste credentials into chat or commit them. Both options require `--refresh-token`.

Stop the affected pipeline before replacing credentials, then restart it after success to avoid
concurrent token-state writes. The explicit replacement bypasses the old saved token and
`AUTO_REFRESH_TOKEN`, validates its Meta app and validity, exchanges it for a long-lived token,
validates the result, and atomically replaces only the selected profile's `TOKEN_STATE_FILE`.
It requires `FACEBOOK_APP_ID` and `FACEBOOK_APP_SECRET`. Failures before saving leave the old
file intact. There is no need to edit token JSON or `.env` files for this operation.
The command exits without starting the queue publisher, media server, or tunnel.

Without either replacement option, `--refresh-token` retains the existing renewal behavior:
saved token first, then `INSTAGRAM_ACCESS_TOKEN`. Refreshing does not grant new permissions;
authorize them through Meta first and supply that newly authorized token. Verify required scopes
and read-only Insights access separately. See `PRODUCTION_DEPLOYMENT.md` for server rotation.

### Recommended cloud media hosting (Lightsail + R2)

For a 24/7 cloud deployment, the preferred setup is:

1. Run all five apps on one Linux VM.
2. Keep the queue on local disk.
3. Configure Cloudflare R2 for final edited MP4 uploads.
4. Set `R2_*` variables and leave `MEDIA_BASE_URL` blank.

When R2 is configured, ClipPublisher uploads the edited MP4 before publishing and gives Meta the R2 public URL. This avoids temporary tunnels and keeps the local/shared queue design intact.

### Automatic public URL (recommended for a local PC)

For a local machine, the best no-manual setup is:

1. Install Cloudflared (`cloudflared`) on your PC.
2. Leave `MEDIA_BASE_URL` blank in `.env`.
3. Start ClipPublisher; the app will automatically serve the queue folder locally and create a free Cloudflare Tunnel URL for it.

That gives you a dynamic public HTTPS URL like `https://abc123.trycloudflare.com` without static IPs or manual DNS changes.

Important:

- The Graph API requires a public URL for the raw video file; a local file path does not work directly.
- If your queue is on a private local or cloud-synced folder, you will need a public URL or a small file-hosting step before calling the Instagram API.
- This app expects the queue items to be served over a public HTTP(S) endpoint; otherwise the Meta API will not be able to fetch the MP4.
- The publisher is tuned for a conservative creator-style cadence by default: 1 post per day, evening local-time window, and a persisted randomized daily publish time.

## Run the app

```powershell
python main.py --once
```

`--once` ignores the natural posting schedule and runs immediately if a queue item is ready.

or keep it running:

```powershell
python main.py
```

To force a token renewal at any time:

```powershell
python main.py --refresh-token
```
