<#
.SYNOPSIS
    Reset a forgotten PostgreSQL superuser password. Run as Administrator.

.DESCRIPTION
    There is no way to recover a forgotten PostgreSQL password -- it is stored
    as a SCRAM verifier, not recoverably. The only route is to temporarily tell
    the server to accept local connections without a password, set a new one,
    and put the original configuration back.

    WHAT THIS DOES, IN ORDER:
      1. Backs up pg_hba.conf next to the original, timestamped.
      2. Rewrites the local/loopback auth method to `trust`.
      3. Restarts the PostgreSQL service.
      4. Connects with no password and sets the new one.
      5. Restores the original pg_hba.conf.
      6. Restarts again and verifies the new password works.

    SECURITY CAVEAT: between steps 3 and 5 -- a few seconds -- any local process
    on this machine could connect to PostgreSQL as a superuser without a
    password. On a single-user laptop that is a small and brief exposure, but it
    is real. Close other applications that might auto-connect (pgAdmin, for
    instance) before running this.

    Step 5 runs in a `finally` block, so the original configuration is restored
    even if the password change fails partway through.

.EXAMPLE
    # From an ELEVATED PowerShell prompt:
    .\scripts\reset-postgres-password.ps1
#>

[CmdletBinding()]
param(
    [string]$PgRoot      = "C:\Program Files\PostgreSQL\16",
    [string]$ServiceName = "postgresql-x64-16",
    [int]   $Port        = 5432
)

$ErrorActionPreference = "Stop"

# --- Preconditions ---------------------------------------------------------
$identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "This script must be run from an elevated (Administrator) PowerShell prompt."
}

$dataDir = Join-Path $PgRoot "data"
$hbaPath = Join-Path $dataDir "pg_hba.conf"
$psql    = Join-Path $PgRoot "bin\psql.exe"
$isready = Join-Path $PgRoot "bin\pg_isready.exe"

foreach ($path in @($hbaPath, $psql)) {
    if (-not (Test-Path $path)) { throw "Not found: $path. Check -PgRoot." }
}

function Wait-ForPostgres {
    <#
        Poll until the server actually accepts connections. A fixed sleep is a
        guess; on a cold start it can be too short, which would fail the reset
        for no real reason. pg_isready reports readiness rather than liveness.
    #>
    param([int]$TimeoutSeconds = 45)

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-Path $isready) {
            & $isready -h 127.0.0.1 -p $Port -q 2>&1 | Out-Null
            if ($LASTEXITCODE -eq 0) { return }
        } else {
            # pg_isready missing (unusual): fall back to a real connection probe.
            & $psql -U postgres -h 127.0.0.1 -p $Port -d postgres -c "SELECT 1;" 2>&1 | Out-Null
            if ($LASTEXITCODE -eq 0) { return }
        }
        Start-Sleep -Milliseconds 500
    }
    throw "PostgreSQL did not become ready within $TimeoutSeconds seconds."
}

Write-Host ""
Write-Host "This will briefly allow passwordless local superuser access to PostgreSQL." -ForegroundColor Yellow
Write-Host "Close pgAdmin and any other database clients first." -ForegroundColor Yellow
$reply = Read-Host "Continue? (y/N)"
if ($reply -ne "y") { Write-Host "Aborted. Nothing changed."; return }

# --- New password ----------------------------------------------------------
$secure = Read-Host "New password for the 'postgres' superuser" -AsSecureString
$bstr   = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
    $newPw = [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
}
if ([string]::IsNullOrWhiteSpace($newPw)) { throw "Password cannot be empty." }
if ($newPw -match "'") { throw "Password cannot contain a single quote (breaks the SQL literal)." }

# --- Backup ----------------------------------------------------------------
$stamp  = Get-Date -Format "yyyyMMdd-HHmmss"
$backup = "$hbaPath.backup-$stamp"
Copy-Item $hbaPath $backup
Write-Host "  backed up pg_hba.conf -> $(Split-Path -Leaf $backup)" -ForegroundColor Green

$restored = $false
try {
    # --- Trust mode --------------------------------------------------------
    # Only the auth method on local + loopback lines changes. Any other host
    # rules are left exactly as they were.
    $lines = Get-Content $hbaPath
    $patched = $lines | ForEach-Object {
        if ($_ -match '^\s*local\s+' -or $_ -match '^\s*host\s+\S+\s+\S+\s+(127\.0\.0\.1/32|::1/128)\s+') {
            $_ -replace '(scram-sha-256|md5|password|peer|ident)\s*$', 'trust'
        } else { $_ }
    }
    Set-Content -Path $hbaPath -Value $patched -Encoding ascii
    Write-Host "  set local auth to trust (temporary)" -ForegroundColor DarkYellow

    Restart-Service $ServiceName -Force
    Wait-ForPostgres
    Write-Host "  service restarted and accepting connections" -ForegroundColor Green

    # --- Set the password --------------------------------------------------
    $sql = "ALTER ROLE postgres WITH PASSWORD '$newPw';"
    $out = & $psql -U postgres -h 127.0.0.1 -p $Port -d postgres -v ON_ERROR_STOP=1 -q -c $sql
    if ($LASTEXITCODE -ne 0) { throw "Failed to set password (exit $LASTEXITCODE): $out" }
    Write-Host "  password updated" -ForegroundColor Green
}
finally {
    # Always put the original configuration back, even if the above failed.
    Copy-Item $backup $hbaPath -Force
    $restored = $true
    Restart-Service $ServiceName -Force
    Wait-ForPostgres
    Write-Host "  restored original pg_hba.conf and restarted" -ForegroundColor Green
}

# --- Verify ----------------------------------------------------------------
if ($restored) {
    $env:PGPASSWORD = $newPw
    try {
        $check = & $psql -U postgres -h 127.0.0.1 -p $Port -d postgres -t -A -c "SELECT 'ok';"
        if ($LASTEXITCODE -eq 0 -and $check -match "ok") {
            Write-Host ""
            Write-Host "Verified: the new password works and password auth is enforced again." -ForegroundColor Cyan
            Write-Host "Next: .\scripts\setup-database.ps1"
        } else {
            Write-Warning "Password was set but verification failed. Check $hbaPath manually."
        }
    } finally {
        Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue
    }
}

$newPw = $null
[System.GC]::Collect()
