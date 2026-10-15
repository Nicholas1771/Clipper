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

Each profile named in `PRODUCTION_PROFILES` runs the five-app pipeline as a `clipper-<profile>.service` systemd unit:

- Both use `/home/ubuntu/Clipper` and `/home/ubuntu/venv/bin/python`

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
sudo -n systemctl stop clipper-n3on.service
/home/ubuntu/venv/bin/python apps/ClipPublisher/main.py --refresh-token --n3on --token-prompt
```

Enter the new token at the hidden prompt. Only after the command succeeds:

```bash
sudo -n systemctl start clipper-n3on.service
systemctl is-active clipper-n3on.service
```

Use `--navaeh` and `clipper-navaeh.service` for that account. The same CLI also supports
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

Never deploy local `.env` files, `profiles\*.local.env`, production state/token files, queue or archive data, or test output over their production counterparts. Shareable `profiles\<name>.env` files contain no credentials; keep production secrets in host-only `profiles\<name>.local.env` files or the service environment.

The root `.env.example` lists the deployment settings. Add their real values to the ignored root `.env`; never place the host address or profile names in tracked files.

### Syncing locally published clip IDs

A real publish from a workstation records its clip ID only in the local seen history. To stop
production from posting the same clip again, append local seen IDs to production:

```powershell
python sync_seen_ids.py --n3on --dry-run   # report how many IDs would be added
python sync_seen_ids.py --n3on             # append them (use --navaeh for that account)
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
