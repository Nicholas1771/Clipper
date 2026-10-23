# Production deployment

## Mandatory: preserve production seen IDs

**Never overwrite or modify production seen IDs during deployment, restart, rollback, testing, or cleanup.** This applies to every profile's effective `STATE_FILE` and its `seen_ids` history, regardless of where the file is stored.

- Never upload local seen-history files, clear or delete production history, replace it with an empty file, or run ClipSourcer with `--clear` in production.
- The only permitted way to add local IDs is the append-only `sync_seen_ids.py` (see "Syncing locally published clip IDs"); it never removes IDs.
- Do not reset or migrate seen history to accommodate application changes. Existing IDs must remain intact.
- Deploy only explicitly selected application files; exclude runtime state from every copy, archive extraction, and synchronization operation.
- If stopping services for deployment, record seen-history checksums after stopping and verify they are unchanged before starting services again. Legitimate successful publishes may update history after services resume.
- A request to deploy or restart is **not** permission to edit seen IDs. If any operation requires changing them, stop and obtain explicit user authorization for that specific history change.

## Host and access

- AWS region: `us-east-2`
- Host and EC2 instance ID are stored in the ignored root `.env` as `PRODUCTION_HOST` and `PRODUCTION_INSTANCE_ID`.
- SSH user: `ubuntu`
- SSH key: `%USERPROFILE%\.ssh\clipper-ec2.pem`
- Remote application directory: `/home/ubuntu/Clipper`
- SSH host-key verification is enabled; the host is already recorded in `%USERPROFILE%\.ssh\known_hosts`.

From PowerShell, run commands from the local repository root:

```powershell
$key = Join-Path $HOME '.ssh\clipper-ec2.pem'
$settings = @{}
Get-Content .env | ForEach-Object {
    if ($_ -match '^(PRODUCTION_HOST|PRODUCTION_PROFILES)=(.*)$') {
        $settings[$matches[1]] = $matches[2].Trim()
    }
}
$productionHost = $settings.PRODUCTION_HOST
if (-not $productionHost) { throw 'Set PRODUCTION_HOST in the ignored root .env.' }
ssh -i $key -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes "ubuntu@$productionHost"
```

Do not copy, print, or commit the private key.

## Applications and services

Each account named in `PRODUCTION_PROFILES` runs one five-app pipeline as a `clipper-<account>.service` systemd unit. `PRODUCTION_PROFILES` retains its existing deployment-variable name, but now lists account config filenames, not creator names:

- Both use `/home/ubuntu/Clipper` and `/home/ubuntu/venv/bin/python`

For the account/creator refactor, provision host-only `accounts\carootclips.env`,
`accounts\vrlclipmoments.env`, `creators\n3on.env`, `creators\navaeh.env`, and any
additional configured creator files (currently `creators\blueface.env`) from the
committed templates using **server-side** values. Merge each old profile and its local
secrets into the account file; move only sourcing/branding/streamer settings to the creator.
Keep `ACCOUNT_NAME` and all existing queue, state and token paths unchanged. Set
`CREATORS=n3on:8:1` for CarootClips or `CREATORS=navaeh:5:1,blueface:3:0` for
VrlClipMoments, and set creator `REQUIRE_SOURCE_KICK_BRANDING=false` for N3on
and `true` for Navaeh and Blueface. Deploy each creator's overlay asset as well.
Set `KICK_PLACEMENT_MODEL=gemini-3.1-pro-preview` for placement calls and
`KICK_OVERLAY_TOP_Y=0.1`, `KICK_OVERLAY_BOTTOM_Y=0.8` consistently for AI and Editor.
Remove the old profile/local-env files only after verifying the new host-only files.

Stop the old creator-named units before enabling account-named units; never run old and
new services against the same queue. Update each systemd `ExecStart` to run
`/home/ubuntu/venv/bin/python /home/ubuntu/Clipper/run_all_apps.py --carootclips`
(or `--vrlclipmoments`), update `PRODUCTION_PROFILES`, and run `systemctl daemon-reload`.
Perform this cutover before the next day's plan: existing YouTube plans retain the old
profile identity and safely block same-day reuse under a different account selector.
Do not rewrite plans, token state, or seen history to force a cutover.
New creator assignments are persisted in the shared account schedule. If multiple
creators' combined quotas cannot fit the posting window with the current minimum gap,
explicitly adjust `MIN_GAP_MINUTES` or the window before enabling that configuration.

After deploying shared application code, restart all listed services and verify that each is active:

```powershell
$profiles = $settings.PRODUCTION_PROFILES -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ }
if (-not $profiles) { throw 'Set PRODUCTION_PROFILES in the ignored root .env.' }
$services = $profiles | ForEach-Object { "clipper-$($_).service" }
$serviceList = $services -join ' '
ssh -i $key -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes "ubuntu@$productionHost" "sudo -n systemctl restart $serviceList"
ssh -i $key -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes "ubuntu@$productionHost" "systemctl is-active $serviceList"
```

For a change affecting only one profile, restart only its service. Check recent logs if a service does not return `active`:

```powershell
$unitList = $services -join ' -u '
ssh -i $key -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes "ubuntu@$productionHost" "sudo -n journalctl -u $unitList -n 100 --no-pager"
```

## Deploying code changes

### Rotating Instagram credentials

Code deployment does not copy or rotate tokens. Never upload local token-state files or secrets.
When a deployment request also calls for token replacement, deploy the Publisher CLI and token
manager changes first, then rotate on the server using a newly authorized, unexposed token.
Do not request tokens in chat or place them in deployment commands/logs.

In an interactive SSH session, from the remote application directory:

```bash
sudo -n systemctl stop clipper-carootclips.service
/home/ubuntu/venv/bin/python apps/ClipPublisher/main.py --refresh-token --carootclips --token-prompt
```

Enter the new token at the hidden prompt. Only after the command succeeds:

```bash
sudo -n systemctl start clipper-carootclips.service
systemctl is-active clipper-carootclips.service
```

Use `--vrlclipmoments` and `clipper-vrlclipmoments.service` for that account. The same CLI also supports
`--token=TOKEN`, but the hidden prompt avoids credentials in shell history/process arguments.
For automated rotation from a workstation, pipe the token over SSH standard input with
`--token-stdin` (never as a command-line argument), with the service stopped as above.
The server uses its own profile configuration, app credentials and `TOKEN_STATE_FILE` path.
No local state is transferred. The token command does not publish or modify seen IDs, queues,
candidate caches, or YouTube credentials. Starting the service resumes normal scheduled work.
If rotation fails, the previous saved token remains; repair authorization/configuration and retry
before restarting. A revoked previous token will not become usable just because its file remains.

### Copying application files

The remote application directory is not a Git checkout. Deploy only the changed, required application files from the repository root with `scp`, preserving their paths. For example:

```powershell
scp -i $key -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes .\apps\ClipAI\src\clipai\service.py "ubuntu@${productionHost}:/home/ubuntu/Clipper/apps/ClipAI/src/clipai/service.py"
```

Repeat for each changed runtime file, then restart the affected service or both services if the change is shared. Confirm the service state and review logs when needed. Do not use a destructive sync or delete remote-only files.

Code deployment does not update ignored application configuration. Compare the effective
non-secret settings for every affected app/profile, and update approved settings on the
server explicitly rather than copying local `.env` files. In particular, verify
`MINIMUM_VIEW_COUNT=100000` for ClipSourcer and that ClipPublisher can load the existing
server-side Gemini credentials and `youtube_policy_screening.txt` prompt.
Check cached candidates against the effective threshold; clearing candidate caches must
never clear published seen IDs, attempted-candidate history, or YouTube upload history.

Never deploy local `.env` files, account/creator env files, production state/token files, queue or archive data, or test output over their production counterparts. Both account and creator envs are ignored and host-only; templates contain no credentials. `.local.env` overrides are no longer supported. Preserve host-specific credentials in each account's single env file or service environment.

The root `.env.example` lists the deployment settings. Add their real values to the ignored root `.env`; never place the host address or profile names in tracked files.

### Syncing locally published clip IDs

A real publish from a workstation records its clip ID only in the local seen history. To stop
production from posting the same clip again, append local seen IDs to production:

```powershell
python sync_seen_ids.py --carootclips --dry-run   # report how many IDs would be added
python sync_seen_ids.py --carootclips             # append them (use --vrlclipmoments for that account)
```

The IDs travel over SSH standard input to `sync_seen_ids.py --merge-stdin` on the server, which
validates them, locks and atomically rewrites the profile's `STATE_FILE`, and verifies that no
existing ID was removed. With `SYNC_SEEN_TO_PRODUCTION=true` in the workstation's root `.env`, the
Publisher runs this automatically after each successful local publish and logs the manual command
if it fails. Never set that flag on the server. No service restart is needed.

## Backups

Amazon Data Lifecycle Manager policy `clipper-daily-snapshots` takes a daily EBS snapshot of the
instance's root volume (tag `Backup=clipper-daily`) and keeps the latest 7 (about $0.50/month).
One-time setup after `aws login` (see AWS CLI below), from the repository root:

```powershell
$p = @('--region','us-east-2','--profile','clipper')
$instance = ((Get-Content .env) -match '^PRODUCTION_INSTANCE_ID=' -replace '^PRODUCTION_INSTANCE_ID=','').Trim()
$volume = aws ec2 describe-instances @p --instance-ids $instance --query 'Reservations[0].Instances[0].BlockDeviceMappings[0].Ebs.VolumeId' --output text
aws ec2 create-tags @p --resources $volume --tags Key=Backup,Value=clipper-daily
aws dlm create-default-role @p --resource-type snapshot 2>$null | Out-Null   # no-op error if it already exists
$role = aws iam get-role @p --role-name AWSDataLifecycleManagerDefaultRole --query 'Role.Arn' --output text
$details = '{"ResourceTypes":["VOLUME"],"TargetTags":[{"Key":"Backup","Value":"clipper-daily"}],"Schedules":[{"Name":"daily-7","CreateRule":{"Interval":24,"IntervalUnit":"HOURS","Times":["08:00"]},"RetainRule":{"Count":7},"CopyTags":true}]}'
$details | Set-Content -Encoding ascii "$env:TEMP\clipper-dlm.json"
aws dlm create-lifecycle-policy @p --description clipper-daily-snapshots --state ENABLED --execution-role-arn $role --policy-details "file://$env:TEMP\clipper-dlm.json"
aws dlm get-lifecycle-policies @p --query 'Policies[].[PolicyId,Description,State]' --output table
```

`08:00` UTC is 4 AM Eastern (outside posting hours). Console alternative: EC2 → Elastic Block
Store → Lifecycle Manager → Create lifecycle policy → EBS snapshot policy, target the instance's
volume, every 24 hours, retain 7. To restore, create a volume from a snapshot (EC2 → Snapshots)
and swap it in as the root volume.

## AWS CLI

The configured CLI profile is `clipper`, in region `us-east-2`. AWS CLI API calls require a current browser login; SSH access to the host does not. To renew AWS CLI access when needed:

```powershell
aws login --region us-east-2 --profile clipper
aws sts get-caller-identity --region us-east-2 --profile clipper
```
