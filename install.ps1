<#
.SYNOPSIS
    Installs Interlock on this computer, or brings an existing install up to date.

.DESCRIPTION
    One command, safe to run again. It only does what is missing:

      1. checks the prerequisites (Python 3.12+, Node 22+, PostgreSQL 16) and,
         after asking, installs missing ones with winget
      2. creates the Python environment and installs Interlock into it
      3. builds the WhatsApp agent and the web app
      4. sets up the database (the postgres password is asked for once)
      5. connects your Google Sheet (you paste its link)
      6. connects WhatsApp (you accept the risk, then scan a QR code)
      7. makes Interlock start in the background when you log in
      8. starts it and opens it in your browser

    Steps 4 to 6 are skipped when already done, so running this on a working
    install just rebuilds and restarts. Nothing in your .env is overwritten, and
    your database and WhatsApp link are never recreated.

.PARAMETER CheckOnly
    Report what is installed and what this would do, and change nothing.
.PARAMETER Yes
    Answer yes to the "install this prerequisite?" questions. (It never answers the
    WhatsApp risk notice for you.)
.PARAMETER Reconfigure
    Ask again about the Google Sheet and WhatsApp even if they are already set up.
.PARAMETER SkipSheet
    Don't ask about the Google Sheet (connect it later in Settings).
.PARAMETER SkipWhatsApp
    Don't ask about WhatsApp (connect it later in Settings).

.EXAMPLE
    .\install.cmd
.EXAMPLE
    .\install.ps1 -CheckOnly
#>

[CmdletBinding()]
param(
    [switch]$CheckOnly,
    [switch]$Yes,
    [switch]$Reconfigure,
    [switch]$SkipSheet,
    [switch]$SkipWhatsApp
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
Set-Location $root

$venvPython = Join-Path $root ".venv\Scripts\python.exe"
$envFile = Join-Path $root ".env"
$agentDir = Join-Path $root "apps\agent"
$webDir = Join-Path $root "apps\web"
$script:PgSecure = $null    # the postgres password, asked for at most once
$script:Todo = 0

# -- small helpers --------------------------------------------------------------

function Write-Step([string]$Number, [string]$Text) {
    Write-Host ""
    Write-Host "[$Number] $Text" -ForegroundColor Cyan
}
function Write-Ok([string]$Text) { Write-Host "    ok     $Text" -ForegroundColor Green }
function Write-Note([string]$Text) { Write-Host "           $Text" -ForegroundColor DarkGray }
function Write-Todo([string]$Text) {
    $script:Todo++
    Write-Host "    to do  $Text" -ForegroundColor Yellow
}

function Confirm-Action([string]$Question) {
    if ($Yes) { return $true }
    $reply = Read-Host "$Question [Y/n]"
    return ($reply -eq "" -or $reply -match '^(y|yes)$')
}

function Update-SessionPath {
    # A program installed a moment ago isn't on this window's PATH yet.
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user"
}

function Invoke-Checked([string]$What, [scriptblock]$Command) {
    & $Command
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit code $LASTEXITCODE). See the messages above." }
}

function Invoke-Quiet([scriptblock]$Command) {
    # For a native program whose stderr should be thrown away (alembic logs its
    # progress there). In Windows PowerShell 5.1, "2>$null" on a native command is
    # itself an error while $ErrorActionPreference is "Stop", so relax it just here.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $Command } finally { $ErrorActionPreference = $previous }
}

function ConvertTo-PlainText([System.Security.SecureString]$Secure) {
    return [System.Net.NetworkCredential]::new("", $Secure).Password
}

function Read-NewPassword([string]$What) {
    # Letters, digits, - and _ only: it is passed to the PostgreSQL installer on a
    # command line, where anything fancier would need fragile quoting.
    while ($true) {
        $first = Read-Host "Choose a password for $What (letters, numbers, - and _ ; at least 8)" -AsSecureString
        $plain = ConvertTo-PlainText $first
        if ($plain -notmatch '^[A-Za-z0-9_-]{8,}$') {
            Write-Host "    That password isn't allowed: use at least 8 letters, numbers, - or _." -ForegroundColor Yellow
            continue
        }
        $again = Read-Host "Type it again" -AsSecureString
        if ((ConvertTo-PlainText $again) -ne $plain) {
            Write-Host "    They didn't match. Try again." -ForegroundColor Yellow
            continue
        }
        return $first
    }
}

function Test-PostgresPassword([System.Security.SecureString]$Secure) {
    $psql = Find-Psql
    if (-not $psql) { return $false }
    $env:PGPASSWORD = ConvertTo-PlainText $Secure
    try {
        # -w: fail instead of asking again. Output is discarded; only the exit code matters.
        Invoke-Quiet { & $psql -U postgres -h 127.0.0.1 -p 5432 -d postgres -w -t -A -c "SELECT 1;" 2>$null } | Out-Null
        return ($LASTEXITCODE -eq 0)
    } finally {
        Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue
    }
}

function Get-PostgresSecret {
    # Asked for at most once per run, and checked: a wrong password is caught here,
    # with three tries, instead of failing inside the database scripts.
    if (-not $script:PgSecure) {
        for ($attempt = 1; $attempt -le 3; $attempt++) {
            $candidate = Read-Host "PostgreSQL superuser (postgres) password" -AsSecureString
            if (Test-PostgresPassword $candidate) { $script:PgSecure = $candidate; break }
            Write-Host "    PostgreSQL did not accept that password (or it is not running). Try again." -ForegroundColor Yellow
        }
        if (-not $script:PgSecure) {
            throw "Could not sign in to PostgreSQL as 'postgres'. Check the password and that the postgresql-x64-16 service is running, then run this again."
        }
    }
    return $script:PgSecure
}

# -- 1. prerequisites ---------------------------------------------------------------

function Find-Python {
    # The project's own environment wins; otherwise any Python 3.12 or newer.
    $candidates = @()
    if (Test-Path $venvPython) { $candidates += , @($venvPython) }
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($v in @("3.13", "3.12")) { $candidates += , @("py", "-$v") }
    }
    if (Get-Command python -ErrorAction SilentlyContinue) { $candidates += , @("python") }
    foreach ($c in $candidates) {
        try {
            $exe = $c[0]
            $extra = @($c | Select-Object -Skip 1)
            $out = Invoke-Quiet { & $exe @extra -c "import sys; print(sys.executable); print(sys.version_info[0] * 100 + sys.version_info[1])" 2>$null }
            if ($LASTEXITCODE -eq 0 -and @($out).Count -ge 2 -and [int]$out[1] -ge 312) { return [string]$out[0] }
        } catch { }
    }
    return $null
}

function Get-NodeMajor {
    if (-not (Get-Command node -ErrorAction SilentlyContinue)) { return 0 }
    try {
        $v = (& node --version) -replace '^v', ''
        return [int]($v.Split('.')[0])
    } catch { return 0 }
}

function Find-Psql {
    foreach ($base in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
        if (-not $base) { continue }
        $candidate = Join-Path $base "PostgreSQL\16\bin\psql.exe"
        if (Test-Path $candidate) { return $candidate }
    }
    return $null
}

function Install-WithWinget([string]$Id, [string]$Name, [string]$Override) {
    Write-Host "    installing $Name ..." -ForegroundColor Yellow
    $wingetArgs = @("install", "--id", $Id, "-e", "--accept-package-agreements", "--accept-source-agreements")
    if ($Override) { $wingetArgs += @("--override", $Override) }
    & winget @wingetArgs
    Update-SessionPath
}

function Step-Prerequisites {
    Write-Step "1/8" "Prerequisites"
    $python = Find-Python
    $nodeMajor = Get-NodeMajor
    $psql = Find-Psql

    if ($python) { Write-Ok "Python 3.12+ ($python)" } else { Write-Todo "Python 3.12 or newer is missing" }
    if ($nodeMajor -ge 22) { Write-Ok "Node.js $nodeMajor" } else { Write-Todo "Node.js 22 or newer is missing" }
    if ($psql) { Write-Ok "PostgreSQL 16" } else {
        $other = @(Get-Service -Name "postgresql*" -ErrorAction SilentlyContinue)
        if ($other.Count -gt 0) {
            throw ("Another PostgreSQL ($($other[0].Name)) is installed, but Interlock needs version 16 " +
                   "(its scripts expect C:\Program Files\PostgreSQL\16). Install PostgreSQL 16 alongside it on a " +
                   "different port, or uninstall the other one, then run this again.")
        }
        Write-Todo "PostgreSQL 16 is missing"
    }

    $missing = @()
    if (-not $python) { $missing += "python" }
    if ($nodeMajor -lt 22) { $missing += "node" }
    if (-not $psql) { $missing += "postgres" }
    if ($missing.Count -eq 0 -or $CheckOnly) { return $python }

    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Host ""
        Write-Host "winget (Windows Package Manager) isn't available, so these can't be installed automatically." -ForegroundColor Yellow
        Write-Host "  Python 3.12+   https://www.python.org/downloads/"
        Write-Host "  Node.js 22+    https://nodejs.org/"
        Write-Host "  PostgreSQL 16  https://www.postgresql.org/download/windows/"
        throw "Install the missing programs above, then run this again."
    }
    if (-not (Confirm-Action "Install the missing program(s) with winget now? (Windows may ask for permission)")) {
        throw "The missing prerequisites are needed. Install them and run this again."
    }

    if ($missing -contains "python") { Install-WithWinget "Python.Python.3.12" "Python 3.12" "" }
    if ($missing -contains "node") { Install-WithWinget "OpenJS.NodeJS.LTS" "Node.js (LTS)" "" }
    if ($missing -contains "postgres") {
        Write-Host ""
        Write-Host "PostgreSQL needs an administrator password. Choose one now; Interlock asks for it" -ForegroundColor Yellow
        Write-Host "again only if the database ever has to be set up again. Keep it somewhere safe." -ForegroundColor Yellow
        $script:PgSecure = Read-NewPassword "the PostgreSQL administrator (postgres)"
        $pw = ConvertTo-PlainText $script:PgSecure
        $override = "--mode unattended --unattendedmodeui none --superpassword $pw --servicename postgresql-x64-16 --serverport 5432"
        Install-WithWinget "PostgreSQL.PostgreSQL.16" "PostgreSQL 16" $override
        $deadline = (Get-Date).AddSeconds(90)
        while ((Get-Date) -lt $deadline) {
            $svc = Get-Service -Name "postgresql-x64-16" -ErrorAction SilentlyContinue
            if ($svc -and $svc.Status -eq "Running") { break }
            Start-Sleep -Seconds 2
        }
    }

    $python = Find-Python
    if (-not $python) { throw "Python was installed but isn't visible yet. Close this window, open a new one, and run the installer again." }
    if ((Get-NodeMajor) -lt 22) { throw "Node.js was installed but isn't visible yet. Close this window, open a new one, and run the installer again." }
    if (-not (Find-Psql)) { throw "PostgreSQL didn't finish installing (psql not found under C:\Program Files\PostgreSQL\16). Run the installer again, or install PostgreSQL 16 by hand." }
    Write-Ok "prerequisites installed"
    return $python
}

# -- 2. python environment -------------------------------------------------------------

function Step-PythonEnvironment([string]$Python) {
    Write-Step "2/8" "Python environment"
    if (-not (Test-Path $venvPython)) {
        if ($CheckOnly) { Write-Todo "create the Python environment (.venv) and install Interlock into it"; return }
        Invoke-Checked "Creating the Python environment" { & $Python -m venv (Join-Path $root ".venv") }
    }
    if ($CheckOnly) {
        Invoke-Quiet { & $venvPython -c "import interlock" 2>$null } | Out-Null
        if ($LASTEXITCODE -eq 0) { Write-Ok "Interlock is installed in .venv" } else { Write-Todo "install Interlock into .venv" }
        return
    }
    Write-Note "installing Python packages (quick when nothing changed) ..."
    Invoke-Checked "Installing Interlock" { & $venvPython -m pip install --disable-pip-version-check --quiet -e $root }
    Write-Ok "Interlock is installed in .venv"
}

# -- 3. builds --------------------------------------------------------------------------

function Step-NodeApp([string]$Dir, [string]$Name, [string]$BuiltFile) {
    $lock = Join-Path $Dir "package-lock.json"
    $modules = Join-Path $Dir "node_modules"
    $marker = Join-Path $modules ".interlock-lock"
    $built = Join-Path $Dir $BuiltFile
    $hash = (Get-FileHash $lock -Algorithm SHA256).Hash

    if ($CheckOnly) {
        if ((Test-Path $modules) -and (Test-Path $built)) { Write-Ok "$Name is built" } else { Write-Todo "install and build $Name" }
        return
    }

    Push-Location $Dir
    try {
        $current = if (Test-Path $marker) { (Get-Content $marker -Raw).Trim() } else { "" }
        if ($current -ne $hash) {
            if ((Test-Path $modules) -and $current -eq "") {
                # Installed by hand before this installer existed: trust it rather than
                # reinstalling a working set of packages. Delete node_modules to force it.
                Write-Note "$Name already has packages installed; keeping them"
            } else {
                Write-Note "installing $Name packages ..."
                Invoke-Checked "npm ci for $Name" { & npm ci --no-audit --no-fund }
            }
            Set-Content -Path $marker -Value $hash -Encoding ascii
        }
        Write-Note "building $Name ..."
        Invoke-Checked "Building $Name" { & npm run build --silent }
    } finally {
        Pop-Location
    }
    Write-Ok "$Name is built"
}

function Step-Builds {
    Write-Step "3/8" "WhatsApp agent and web app"
    Step-NodeApp $agentDir "the WhatsApp agent" "dist\src\index.js"
    Step-NodeApp $webDir "the web app" "dist\index.html"
}

# -- 4. database ----------------------------------------------------------------------

function Get-DbStatus {
    $result = @{ app = "failed"; agent = "failed" }
    if (-not (Test-Path $venvPython)) { return $result }
    $lines = Invoke-Quiet { & $venvPython -m interlock.setup_cli db-status 2>$null }
    foreach ($line in @($lines)) {
        if ($line -match '^(app|agent): (.*)$') { $result[$Matches[1]] = $Matches[2] }
    }
    return $result
}

function Test-MigrationsCurrent {
    $heads = Invoke-Quiet { & $venvPython -m alembic heads 2>$null }
    $current = Invoke-Quiet { & $venvPython -m alembic current 2>$null }
    $head = [regex]::Match(($heads -join " "), '\b[0-9a-f]{12}\b').Value
    return ($head -ne "" -and (($current -join " ") -match $head))
}

function Step-Database {
    Write-Step "4/8" "Database"
    $status = Get-DbStatus
    $haveEnv = Test-Path $envFile

    if ($status.app -eq "ok") {
        Write-Ok "the database is set up and reachable"
    } elseif ($CheckOnly) {
        Write-Todo "create the database, its roles and .env (asks for the postgres password once)"
        return
    } else {
        $psql = Find-Psql
        $keep = $haveEnv -and ((Get-Content $envFile -Raw) -match '(?m)^DATABASE_URL=')
        $dbArgs = @{ PsqlPath = $psql; SuperuserPassword = (Get-PostgresSecret) }
        if ($keep) {
            Write-Note "found an existing .env: recreating the roles with its passwords and keeping the file"
            $dbArgs.KeepEnv = $true
        }
        & (Join-Path $root "scripts\setup-database.ps1") @dbArgs
        if (-not (Test-Path $envFile)) { throw "The database setup didn't produce a .env." }
    }

    if ($CheckOnly) {
        if (Test-MigrationsCurrent) { Write-Ok "database tables are up to date" } else { Write-Todo "bring the database tables up to date" }
    } else {
        Invoke-Checked "Updating the database tables" { & $venvPython -m alembic upgrade head }
        Write-Ok "database tables are up to date"
    }

    $status = Get-DbStatus
    if ($status.agent -eq "ok") {
        Write-Ok "the WhatsApp agent's database access is set up"
    } elseif ($CheckOnly) {
        Write-Todo "give the WhatsApp agent its own limited database role"
    } else {
        $agentEnv = Join-Path $agentDir ".env"
        $roleArgs = @{ PsqlPath = (Find-Psql); SuperuserPassword = (Get-PostgresSecret) }
        if (Test-Path $agentEnv) { $roleArgs.KeepEnv = $true }
        & (Join-Path $root "scripts\setup-agent-role.ps1") @roleArgs
        Write-Ok "the WhatsApp agent's database access is set up"
    }
}

# -- 5. google sheet ---------------------------------------------------------------------

function Step-Sheet {
    Write-Step "5/8" "Google Sheet"
    if ($SkipSheet) { Write-Note "skipped (connect it later in Settings > Google Sheet)"; return }
    if ($CheckOnly) { Write-Note "would ask for your sheet's link if none is connected"; return }
    $sheetArgs = @("-m", "interlock.setup_cli", "sheet")
    if (-not $Reconfigure) { $sheetArgs += "--keep-existing" }
    & $venvPython @sheetArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Host "    No sheet is connected yet. You can do it any time in Settings > Google Sheet." -ForegroundColor Yellow
    }
}

# -- 6. whatsapp ----------------------------------------------------------------------------

function Test-WhatsAppSetUp {
    if (-not (Test-Path $envFile)) { return $false }
    $text = Get-Content $envFile -Raw
    $enabled = ($text -match '(?m)^WHATSAPP_PROVIDER=local_agent\s*$') -and ($text -match '(?im)^WHATSAPP_LOCAL_AGENT_TOS_ACK=true\s*$')
    $paired = @(Get-ChildItem -Path $agentDir -Filter "creds.json" -Recurse -Depth 3 -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -match '\.wa-session' }).Count -gt 0
    return ($enabled -and $paired)
}

function Step-WhatsApp {
    Write-Step "6/8" "WhatsApp"
    if ($SkipWhatsApp) { Write-Note "skipped (connect it later in Settings > WhatsApp)"; return }
    if ((Test-WhatsAppSetUp) -and -not $Reconfigure) { Write-Ok "WhatsApp is enabled and a phone is linked"; return }
    if ($CheckOnly) { Write-Todo "ask you to accept the WhatsApp risk, then show a QR code to scan"; return }

    & $venvPython -m interlock.setup_cli whatsapp-enable
    if ($LASTEXITCODE -ne 0) {
        Write-Host "    WhatsApp stays off for now. Enable it later in Settings > WhatsApp." -ForegroundColor Yellow
        return
    }
    # The agent reads .env when it starts: (re)start the service so it is running and enabled.
    Write-Note "starting Interlock so the QR code can be shown ..."
    & (Join-Path $root "scripts\start-interlock.ps1") -Restart -NoBrowser
    & $venvPython -m interlock.setup_cli whatsapp-link
    if ($LASTEXITCODE -ne 0) {
        Write-Host "    No phone is linked yet. Do it any time in Settings > WhatsApp." -ForegroundColor Yellow
    }
}

# -- 7. start at login ---------------------------------------------------------------------

function Step-Autostart {
    Write-Step "7/8" "Start at login"
    $task = Get-ScheduledTask -TaskName "Interlock" -ErrorAction SilentlyContinue
    $shortcut = Join-Path ([Environment]::GetFolderPath("Startup")) "Interlock.lnk"
    $registered = ($null -ne $task) -or (Test-Path $shortcut)
    if ($CheckOnly) {
        if ($registered) { Write-Ok "Interlock starts when you log in" } else { Write-Todo "make Interlock start when you log in" }
        return
    }
    & (Join-Path $root "scripts\register-autostart.ps1") | Out-Null
    Write-Ok "Interlock will start by itself when you log in"
}

# -- 8. start ----------------------------------------------------------------------------------

function Step-Start {
    Write-Step "8/8" "Starting Interlock"
    if ($CheckOnly) { Write-Note "would (re)start Interlock and open it in your browser"; return }
    & (Join-Path $root "scripts\start-interlock.ps1") -Restart -NoBrowser
    $page = "#/"
    try {
        $groups = Invoke-RestMethod -Uri "http://127.0.0.1:8000/whatsapp/groups" -TimeoutSec 5
        if (@($groups).Count -eq 0) { $page = "#/groups" }
    } catch { }
    Start-Process "http://127.0.0.1:8000/app/$page"
    if ($page -eq "#/groups") {
        Write-Note "no WhatsApp groups are pinned yet: pick the groups reports go to on the Groups page"
    }
}

# -- run -------------------------------------------------------------------------------------

Write-Host ""
Write-Host "Interlock installer" -ForegroundColor Cyan
if ($CheckOnly) { Write-Host "(check only: nothing will be changed)" -ForegroundColor DarkGray }

$python = Step-Prerequisites
if ($CheckOnly -and $script:Todo -gt 0 -and -not (Test-Path $venvPython) -and -not $python) {
    Write-Host ""
    Write-Host "Prerequisites are missing, so the later steps can't be checked yet." -ForegroundColor Yellow
    Write-Host "Run .\install.cmd to install them."
    exit 0
}
Step-PythonEnvironment $python
Step-Builds
Step-Database
Step-Sheet
Step-WhatsApp
Step-Autostart
Step-Start

Write-Host ""
if ($CheckOnly) {
    if ($script:Todo -eq 0) {
        Write-Host "Everything is already set up. Nothing was changed." -ForegroundColor Green
    } else {
        Write-Host "$($script:Todo) thing(s) would be done by .\install.cmd. Nothing was changed." -ForegroundColor Yellow
    }
    exit 0
}
Write-Host "Interlock is ready." -ForegroundColor Green
Write-Host "  Open it:       http://127.0.0.1:8000/"
Write-Host "  Its state:     .\scripts\start-interlock.ps1 -Status"
Write-Host "  Stop / start:  .\scripts\start-interlock.ps1 -Stop   /   .\scripts\start-interlock.ps1"
Write-Host "  Logs:          $root\logs"
Write-Host "  It starts by itself in the background whenever you log in."
