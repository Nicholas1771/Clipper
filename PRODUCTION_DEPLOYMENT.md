# Production deployment

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

The remote application directory is not a Git checkout. Deploy only the changed, required application files from the repository root with `scp`, preserving their paths. For example:

```powershell
scp -i $key -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes .\apps\ClipAI\src\clipai\service.py "ubuntu@${productionHost}:/home/ubuntu/Clipper/apps/ClipAI/src/clipai/service.py"
```

Repeat for each changed runtime file, then restart the affected service or both services if the change is shared. Confirm the service state and review logs when needed. Do not use a destructive sync or delete remote-only files.

Never deploy local `.env` files, `profiles\*.local.env`, production state/token files, queue or archive data, or test output over their production counterparts. Shareable `profiles\<name>.env` files contain no credentials; keep production secrets in host-only `profiles\<name>.local.env` files or the service environment.

The root `.env.example` lists the deployment settings. Add their real values to the ignored root `.env`; never place the host address or profile names in tracked files.

## AWS CLI

The configured CLI profile is `clipper`, in region `us-east-2`. AWS CLI API calls require a current browser login; SSH access to the host does not. To renew AWS CLI access when needed:

```powershell
aws login --region us-east-2 --profile clipper
aws sts get-caller-identity --region us-east-2 --profile clipper
```
