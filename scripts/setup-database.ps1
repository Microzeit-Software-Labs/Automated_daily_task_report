<#
.SYNOPSIS
    One-time database provisioning for Interlock.

.DESCRIPTION
    Creates two roles and two databases, then writes .env with a generated
    application password.

    Two roles on purpose (least privilege, per the architecture):

      interlock_owner  owns the schema and runs migrations. Has DDL rights.
      interlock_app    the application connects as this. No DDL. Later
                       migrations additionally REVOKE UPDATE and DELETE on the
                       audit and snapshot tables from this role, which is what
                       makes the audit log genuinely append-only rather than
                       append-only by convention.

    Your postgres superuser password is read interactively and is never written
    to disk, never logged, and never passed on a command line.

.EXAMPLE
    .\scripts\setup-database.ps1
#>

[CmdletBinding()]
param(
    [string]$PsqlPath = "C:\Program Files\PostgreSQL\16\bin\psql.exe",
    [string]$PgHost   = "127.0.0.1",
    [int]   $Port     = 5432,
    [string]$Database = "interlock",
    [string]$TestDatabase = "interlock_test"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $PsqlPath)) {
    throw "psql not found at '$PsqlPath'. Pass -PsqlPath with the correct location."
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$envFile  = Join-Path $repoRoot ".env"

if (Test-Path $envFile) {
    Write-Host ".env already exists. Provisioning would overwrite it." -ForegroundColor Yellow
    $reply = Read-Host "Overwrite? (y/N)"
    if ($reply -ne "y") { Write-Host "Aborted. Nothing changed."; return }
}

# --- Credentials -----------------------------------------------------------
$secure = Read-Host "PostgreSQL superuser (postgres) password" -AsSecureString
$bstr   = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
    $superPw = [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
}

# Generated, not chosen. 32 chars from a URL-safe alphabet so it survives being
# embedded in a connection string without escaping.
$alphabet = (48..57) + (65..90) + (97..122)
$appPw    = -join ($alphabet | Get-Random -Count 32 | ForEach-Object { [char]$_ })
$ownerPw  = -join ($alphabet | Get-Random -Count 32 | ForEach-Object { [char]$_ })

function Invoke-Psql {
    param([string]$Db, [string]$Sql, [switch]$AllowFailure)
    # No `2>&1` here: in Windows PowerShell 5.1, merging a native exe's stderr
    # wraps every line (WARNING included) in a terminating NativeCommandError
    # under $ErrorActionPreference = "Stop", aborting the script on a harmless
    # warning. psql's own stderr prints straight to the console untouched;
    # $out only captures stdout, and $LASTEXITCODE is still the real exit code.
    $env:PGPASSWORD = $superPw
    try {
        $out = & $PsqlPath -U postgres -h $PgHost -p $Port -d $Db `
                           -v ON_ERROR_STOP=1 -q -t -A -c $Sql
        if ($LASTEXITCODE -ne 0 -and -not $AllowFailure) {
            throw "psql failed (exit $LASTEXITCODE): $out"
        }
        return $out
    } finally {
        Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue
    }
}

Write-Host ""
Write-Host "Provisioning..." -ForegroundColor Cyan

# --- Collation bookkeeping ---------------------------------------------------
# A Windows Update can silently bump the OS's ICU/NLS collation library. New
# databases are created from `template1` by default, and PostgreSQL refuses
# CREATE DATABASE when template1's recorded collation version doesn't match
# what the OS reports now -- exactly the error seen here. REFRESH COLLATION
# VERSION only updates that bookkeeping; it does not touch data, and there is
# no data yet in databases this script owns. Pre-existing databases with actual
# indexed text data would separately want a REINDEX after this, but that is out
# of scope for a fresh provisioning run.
foreach ($db in @("template1", "postgres")) {
    Invoke-Psql -Db $db -Sql "ALTER DATABASE $db REFRESH COLLATION VERSION;" -AllowFailure | Out-Null
}
Write-Host "  refreshed collation bookkeeping (Windows Update side effect)" -ForegroundColor DarkGray

# --- Roles -----------------------------------------------------------------
# Idempotent: ALTER if present, CREATE if not. Re-running rotates the password,
# which is why .env is rewritten in the same run.
foreach ($role in @(@{n="interlock_owner"; p=$ownerPw}, @{n="interlock_app"; p=$appPw})) {
    $sql = @"
DO `$`$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '$($role.n)') THEN
        ALTER ROLE $($role.n) WITH LOGIN PASSWORD '$($role.p)';
    ELSE
        CREATE ROLE $($role.n) WITH LOGIN PASSWORD '$($role.p)';
    END IF;
END
`$`$;
"@
    Invoke-Psql -Db "postgres" -Sql $sql | Out-Null
    Write-Host "  role $($role.n)" -ForegroundColor Green
}

# --- Databases -------------------------------------------------------------
# CREATE DATABASE cannot run inside a DO block, so existence is checked first.
foreach ($db in @($Database, $TestDatabase)) {
    $exists = Invoke-Psql -Db "postgres" -Sql "SELECT 1 FROM pg_database WHERE datname = '$db';"
    if ($exists -match "1") {
        Write-Host "  database $db (already present)" -ForegroundColor DarkGray
    } else {
        Invoke-Psql -Db "postgres" -Sql "CREATE DATABASE $db OWNER interlock_owner ENCODING 'UTF8';" | Out-Null
        Write-Host "  database $db" -ForegroundColor Green
    }
    # PostgreSQL's timestamptz converts to/from the CLIENT SESSION's TimeZone
    # setting, which defaults to this machine's OS locale (Asia/Calcutta) --
    # not UTC. Every value written as (say) 17:04 UTC would otherwise read
    # back as 22:34+05:30: the same instant, but a different ISO string,
    # which silently breaks anything that hashes or string-compares a
    # timestamp (the audit chain's entry_hash, most consequentially). The
    # application also pins this per-connection in code (see
    # adapters/persistence/base.py's make_engine) -- this database-level
    # default is defense in depth for anyone connecting directly via psql.
    Invoke-Psql -Db "postgres" -Sql "ALTER DATABASE $db SET TimeZone TO 'UTC';" | Out-Null
}

# --- Privileges ------------------------------------------------------------
foreach ($db in @($Database, $TestDatabase)) {
    $grants = @"
GRANT CONNECT ON DATABASE $db TO interlock_app;
GRANT USAGE ON SCHEMA public TO interlock_app;
ALTER SCHEMA public OWNER TO interlock_owner;
-- Tables created later by migrations must be reachable by the app role without
-- another manual grant, so set the default now.
ALTER DEFAULT PRIVILEGES FOR ROLE interlock_owner IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO interlock_app;
ALTER DEFAULT PRIVILEGES FOR ROLE interlock_owner IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO interlock_app;
"@
    Invoke-Psql -Db $db -Sql $grants | Out-Null
    Write-Host "  privileges on $db" -ForegroundColor Green
}

# --- .env ------------------------------------------------------------------
$template = Get-Content (Join-Path $repoRoot ".env.example") -Raw
$appUrl   = "postgresql+psycopg://interlock_app:$appPw@${PgHost}:$Port/$Database"
$ownerUrl = "postgresql+psycopg://interlock_owner:$ownerPw@${PgHost}:$Port/$Database"
$testUrl  = "postgresql+psycopg://interlock_owner:$ownerPw@${PgHost}:$Port/$TestDatabase"

$content = $template -replace '(?m)^DATABASE_URL=.*$', "DATABASE_URL=$appUrl"
$content += @"

# --- Generated by scripts/setup-database.ps1 --------------------------------
# Migrations run as the owner; the application runs as the less-privileged app
# role. Do not collapse these into one URL.
MIGRATION_DATABASE_URL=$ownerUrl
TEST_DATABASE_URL=$testUrl
"@

Set-Content -Path $envFile -Value $content -Encoding utf8
Write-Host "  wrote .env" -ForegroundColor Green

$superPw = $null
[System.GC]::Collect()

Write-Host ""
Write-Host "Done." -ForegroundColor Cyan
Write-Host "  .env is gitignored and holds generated passwords. Back it up if you"
Write-Host "  care about keeping this database; re-running this script rotates them."
