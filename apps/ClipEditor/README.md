# ClipEditor

ClipEditor is the pipeline stage between ClipSourcer and ClipPublisher.

It watches the queue for runs that are already sourced and then:

- overlays the profile's configured branding image near the lower third of the clip
- adds a translucent white watermark using the profile's `ACCOUNT_NAME`
- writes `caption.txt`, `title.txt`, and `description.txt` to the queue item
- marks the queue item as `edited` so ClipPublisher can pick it up safely

## Run the app

```powershell
cd apps\ClipEditor
python main.py --once
```

Or keep it running in a loop:

```powershell
python main.py
```
