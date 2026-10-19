# Oracle2SSRS - offline Windows Server deployment.
#
# Does steps 1-6 of docs\WINDOWS_SERVER_DEPLOYMENT.md in one go. Safe to
# re-run: an upgrade is "unzip the new app over the old one, run again".
#
# Before running, put next to this script:
#   wheels\                      the Python libraries (required)
#   python-3.13.*-amd64.exe      the Python installer (only if Python is not installed)
#   tools\renderlab\lib\         the preview engine DLLs (optional)
#
# Run in PowerShell as Administrator:
#   powershell -ExecutionPolicy Bypass -File .\deploy.ps1
#
# Options:
#   -Port 5057         port to serve on
#   -SkipFirewall      do not open the port to other machines
#   -SkipTask          do not register the startup task; start the app yourself

param(
    [int]$Port = 5057,
    [switch]$SkipFirewall,
    [switch]$SkipTask
)

$ErrorActionPreference = 'Stop'

$AppDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$PyDir    = 'C:\Program Files\Python313'
$PyExe    = Join-Path $PyDir 'python.exe'
$VenvPy   = Join-Path $AppDir '.venv\Scripts\python.exe'
$Wheels   = Join-Path $AppDir 'wheels'
$LibDir   = Join-Path $AppDir 'tools\renderlab\lib'
$TaskName = 'Oracle2SSRS'

function Step($Msg) { Write-Host ""; Write-Host "== $Msg" -ForegroundColor Cyan }
function Ok($Msg)   { Write-Host "   OK    $Msg" -ForegroundColor Green }
function Warn($Msg) { Write-Host "   WARN  $Msg" -ForegroundColor Yellow }
function Fail($Msg) { Write-Host "   FAIL  $Msg" -ForegroundColor Red; exit 1 }

$me = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Fail 'Run PowerShell as Administrator.'
}
Set-Location $AppDir

# --- 0. Stop a running copy, so pip can replace files in .venv -------------
Step 'Stopping any running copy'
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName
}
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object { $_.CommandLine -like '*waitress*app:app*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2
Ok 'Nothing running.'

# --- 1. Unblock files copied from the internet ------------------------------
Step 'Unblocking files'
Get-ChildItem $AppDir -Recurse -File | Unblock-File
Ok $AppDir

# --- 2. Python --------------------------------------------------------------
Step 'Python'
if (Test-Path $PyExe) {
    Ok "Already installed: $PyExe"
} else {
    $installer = Get-ChildItem $AppDir -Filter 'python-3.13.*-amd64.exe' |
        Sort-Object Name | Select-Object -Last 1
    if (-not $installer) {
        Fail "Python is not installed and no python-3.13.*-amd64.exe is in $AppDir."
    }
    Write-Host "   Installing $($installer.Name) ..."
    $p = Start-Process -FilePath $installer.FullName -Wait -PassThru `
        -ArgumentList "/quiet InstallAllUsers=1 PrependPath=0 Include_test=0 TargetDir=`"$PyDir`""
    if ($p.ExitCode -ne 0 -or -not (Test-Path $PyExe)) {
        Fail "Python installer failed (exit $($p.ExitCode))."
    }
    Ok "Installed: $PyExe"
}

# --- 3. Libraries -----------------------------------------------------------
Step 'Python libraries'
if (-not (Test-Path $Wheels)) { Fail "Missing $Wheels" }
if (-not (Test-Path $VenvPy)) {
    & $PyExe -m venv (Join-Path $AppDir '.venv')
    if ($LASTEXITCODE -ne 0) { Fail 'Could not create .venv' }
}
& $VenvPy -m pip install --no-index --find-links $Wheels --disable-pip-version-check `
    -r (Join-Path $AppDir 'requirements.txt') waitress colorama
if ($LASTEXITCODE -ne 0) { Fail "pip install failed. A library is probably missing from $Wheels." }
Ok 'Installed from wheels\'

# --- 4. Session key ---------------------------------------------------------
Step 'Settings (.env)'
$envFile = Join-Path $AppDir '.env'
if (Test-Path $envFile) {
    Ok 'Keeping existing .env'
} else {
    $key = (& $VenvPy -c "import secrets; print(secrets.token_hex(32))").Trim()
    if ($LASTEXITCODE -ne 0 -or $key.Length -ne 64) { Fail 'Could not generate a secret key.' }
    @("O2S_SECRET_KEY=$key", "PORT=$Port") | Set-Content -Encoding ASCII $envFile
    Ok 'Created .env with a new secret key'
}

# --- Preview engine (optional) ----------------------------------------------
Step 'Preview engine DLLs'
$types = Join-Path $LibDir 'Microsoft.SqlServer.Types.dll'
if (-not (Test-Path (Join-Path $LibDir 'Microsoft.ReportViewer.WinForms.dll'))) {
    Warn "Not found in $LibDir. The app works; only the Preview tab is off."
} elseif (-not (Test-Path $types)) {
    Warn "Microsoft.SqlServer.Types.dll is missing from $LibDir. Preview will fail."
} else {
    $v = [Reflection.AssemblyName]::GetAssemblyName($types).Version
    if ($v.Major -ne 17) {
        Warn "Microsoft.SqlServer.Types.dll is version $v; this app needs 17.x."
        Warn "Replace $LibDir with a freshly fetched one, or Preview will fail."
    } else {
        Ok "Present (SqlServer.Types $v)"
    }
}

# --- Smoke test -------------------------------------------------------------
Step 'Converter smoke test'
& $VenvPy -c "import sys; sys.path.insert(0, 'backend'); from converter import convert; convert(open('samples/oracle/SAMPLE_INSPECTION.xml', 'rb').read()); print('converted')"
if ($LASTEXITCODE -ne 0) { Fail 'Converting the sample report failed.' }
Ok 'Sample report converts.'

# --- 5. serve.cmd + firewall ------------------------------------------------
Step 'Start script and firewall'
$serve = Join-Path $AppDir 'serve.cmd'
@(
    '@echo off',
    'cd /d "%~dp0backend"',
    "`"%~dp0.venv\Scripts\python.exe`" -m waitress --listen=0.0.0.0:$Port --threads=8 --channel-timeout=300 app:app >> `"%~dp0app.log`" 2>&1"
) | Set-Content -Encoding ASCII $serve
Ok "Wrote $serve"

if ($SkipFirewall) {
    Warn 'Firewall left closed (-SkipFirewall). Only this server can open the app.'
} else {
    $rule = 'Oracle2SSRS'
    Get-NetFirewallRule -DisplayName $rule -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    New-NetFirewallRule -DisplayName $rule -Direction Inbound -Protocol TCP `
        -LocalPort $Port -Action Allow | Out-Null
    Ok "Port $Port open to other machines"
}

# --- 6. Startup task --------------------------------------------------------
if ($SkipTask) {
    Step 'Done'
    Ok "Start the app with: $serve"
    exit 0
}

Step 'Startup task'
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}
$action   = New-ScheduledTaskAction -Execute $serve -WorkingDirectory $AppDir
$trigger  = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -User 'SYSTEM' -RunLevel Highest | Out-Null
Start-ScheduledTask -TaskName $TaskName
Ok "Registered and started '$TaskName'"

Step 'Checking the app answers'
$url = "http://localhost:$Port/"
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 2
    try {
        $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 5
        if ($r.StatusCode -eq 200) {
            Ok "$url is up. Others use http://$($env:COMPUTERNAME):$Port"
            exit 0
        }
    } catch {}  # not up yet; try again
}
Fail "No answer from $url after 60 s. See $(Join-Path $AppDir 'app.log')"
