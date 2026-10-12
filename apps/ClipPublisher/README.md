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

A queue item is considered ready only if:

- the run directory exists
- there is a JSON manifest file
- there is an MP4 file
- the files have been stable for `READY_STABILITY_SECONDS`

The app then claims the item in place by setting its `queue_state.json` phase to `published`, so it is never picked twice. Failed or finished items are moved to `Archive/`.

This gives a simple safety check so the Publisher does not pick up a half-written queue item.

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
