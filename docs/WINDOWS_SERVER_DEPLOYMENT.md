# Oracle2SSRS — Running on Windows Server

Steps to install and run Oracle2SSRS on a Windows Server with no internet
access. Get the files first:
[WINDOWS_SERVER_PREREQUISITES.md](WINDOWS_SERVER_PREREQUISITES.md).

The examples install to `D:\Apps\OFR2SSRS`. Run the commands in
**PowerShell as Administrator**.

**Shortcut:** after step 1, `deploy.ps1` does steps 2–6 for you. Put the
Python installer in the app folder too, then run as Administrator:

```powershell
cd D:\Apps\OFR2SSRS
powershell -ExecutionPolicy Bypass -File .\deploy.ps1
```

Run it again after unzipping a new version over the old one to upgrade.
`-SkipFirewall` keeps the port closed; `-SkipTask` skips the startup task.

## Single-file option

Instead of the steps below, one wheel can carry the app and every Python
library. Download the one matching the server's Python on a machine with
internet (`3.13` or `3.15`):

```powershell
pip download oracle2ssrs --no-deps --only-binary=:all: --platform win_amd64 --python-version 3.15 -d .
```

Or build it from the source: `python tools\build_offline_wheel.py --python-version 3.15`
(needs `pip install build wheel`). Copy the `.whl` to the server, then:

```powershell
& "C:\Program Files\Python315\python.exe" -m pip install --no-index oracle2ssrs-1.0.2-cp315-cp315-win_amd64.whl
oracle2ssrs --host 0.0.0.0 --port 5057
```

`oracle2ssrs-convert <folder> --out <folder>` converts without the web UI.
Put `.env` in the folder you start it from.

## 1. Copy the files to the server

Unzip the app to `D:\Apps\OFR2SSRS`, then copy in:

* the Python libraries → `D:\Apps\OFR2SSRS\wheels\`
* the preview engine DLLs (optional) → `D:\Apps\OFR2SSRS\tools\renderlab\lib\`

If you downloaded the three `.nupkg` files by hand instead of using
`fetch_reportviewer.py`: rename each one to `.zip`, open it, and copy every
`.dll` it contains into `tools\renderlab\lib\`.

Then unblock everything, because Windows refuses to load DLLs copied from
the internet:

```powershell
Get-ChildItem D:\Apps\OFR2SSRS -Recurse | Unblock-File
```

## 2. Install Python

Run `python-3.13.16-amd64.exe`, tick **Install for all users**, and
install to `C:\Program Files\Python313`. Or install silently:

```powershell
.\python-3.13.16-amd64.exe /quiet InstallAllUsers=1 TargetDir="C:\Program Files\Python313"
```

## 3. Install the libraries

```powershell
cd D:\Apps\OFR2SSRS
& "C:\Program Files\Python313\python.exe" -m venv .venv
.\.venv\Scripts\python.exe -m pip install --no-index --find-links .\wheels --disable-pip-version-check -r requirements.txt waitress colorama
```

`--no-index` makes pip install from the `wheels` folder only, without
going online.

## 4. Set the session key

Create `D:\Apps\OFR2SSRS\.env` with a fixed secret so users stay signed in
to their work across restarts:

```powershell
$key = .\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_hex(32))"
"O2S_SECRET_KEY=$key" | Set-Content -Encoding ASCII .env
```

Optional settings for the same file: `O2S_MAX_UPLOAD_MB=64` (upload size
limit) and `PORT=5057`. Never set `O2S_DEBUG` on a server.

## 5. Start the app

```powershell
cd D:\Apps\OFR2SSRS\backend
..\.venv\Scripts\python.exe -m waitress --listen=0.0.0.0:5057 --threads=8 --channel-timeout=300 app:app
```

Open `http://localhost:5057` on the server and drop
`samples\oracle\SAMPLE_INSPECTION.xml` onto the page. If the conversion
shows up, the app works. Press `Ctrl+C` to stop it.

To let other machines reach it, open the port:

```powershell
New-NetFirewallRule -DisplayName "Oracle2SSRS" -Direction Inbound -Protocol TCP -LocalPort 5057 -Action Allow
```

Users then browse to `http://<server-name>:5057`. The app has no login of
its own, so anyone who can reach the port can use it.

## 6. Keep it running after restarts (optional)

Create `D:\Apps\OFR2SSRS\serve.cmd`:

```bat
@echo off
cd /d "%~dp0backend"
"%~dp0.venv\Scripts\python.exe" -m waitress --listen=0.0.0.0:5057 --threads=8 --channel-timeout=300 app:app >> "%~dp0app.log" 2>&1
```

Register it as a startup task:

```powershell
$action   = New-ScheduledTaskAction -Execute "D:\Apps\OFR2SSRS\serve.cmd"
$trigger  = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit 0 -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "Oracle2SSRS" -Action $action -Trigger $trigger -Settings $settings -User "SYSTEM" -RunLevel Highest
Start-ScheduledTask -TaskName "Oracle2SSRS"
```

Stop it with `Stop-ScheduledTask -TaskName Oracle2SSRS`. Errors go to
`D:\Apps\OFR2SSRS\app.log`.

## Common problems

| Problem | Fix |
|---------|-----|
| `No module named ...` | A library is missing from `wheels\`. Add it and repeat step 3. |
| Preview says the engine DLLs could not be fetched | `tools\renderlab\lib\` is empty. Copy the DLLs in (step 1). |
| Preview fails with *"Operation is not supported"* | The DLLs are still blocked. Run the `Unblock-File` command from step 1 and restart the app. |
| Other machines can't open the page | Check the firewall rule from step 5 and that the app is running. |
| Large uploads are rejected | Raise `O2S_MAX_UPLOAD_MB` in `.env` and restart. |
