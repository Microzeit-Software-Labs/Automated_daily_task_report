<#
.SYNOPSIS
    One-time database provisioning for the local WhatsApp agent.

.DESCRIPTION
    Creates (or rotates) the interlock_agent role and grants it exactly
    SELECT/INSERT/UPDATE on whatsapp_agent_commands and whatsapp_agent_status
    -- nothing else. It cannot read tasks, reports, approvals, or audit_logs.
    This role is the trust boundary the design in
    docs/whatsapp-agent-setup.md relies on, in place of the Ed25519-signed
    transport the original architecture doc specified (see HANDOVER.md's
    WhatsApp section for why).

    Prerequisites, in order:
      1. scripts/setup-database.ps1 has already run (this script reads
         MIGRATION_DATABASE_URL from .env).
      2. Migrations are at head, so whatsapp_agent_commands and
         whatsapp_agent_status already exist:
           .\.venv\Scripts\python.exe -m alembic upgrade head

    Only touches the interlock database, never interlock_test -- nothing in
    the test suite runs a live agent against it; the FakeAgent in
    tests/integration/test_local_agent_provider.py uses the owner role's own
    fixtures instead.

    Your Postgres superuser password is needed once, only to create the
    role itself (interlock_owner has no CREATEROLE). It is read
    interactively and never written to disk. The grants themselves run as
    interlock_owner, using the credentials setup-database.ps1 already wrote
    to .env -- no second superuser step for those.

    Writes apps/agent/.env. Never touches the project root .env or any
    other role.

.PARAMETER SuperuserPassword
    The postgres password as a SecureString, so a caller (install.ps1) can ask for
    it once. Without it, you are asked.

.PARAMETER KeepEnv
    Reuse the password already in apps\agent\.env for the role and leave that file
    alone, instead of generating a new one.

.EXAMPLE
    .\scripts\setup-agent-role.ps1
#>

[CmdletBinding()]
param(
    [string]$PsqlPath = "C:\Program Files\PostgreSQL\16\bin\psql.exe",
    [string]$PgHost   = "127.0.0.1",
    [int]   $Port     = 5432,
    [string]$Database = "interlock",
    [System.Security.SecureString]$SuperuserPassword,
    [switch]$KeepEnv
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $PsqlPath)) {
    throw "psql not found at '$PsqlPath'. Pass -PsqlPath with the correct location."
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$envFile  = Join-Path $repoRoot ".env"
$agentDir = Join-Path $repoRoot "apps\agent"
$agentEnv = Join-Path $agentDir ".env"

if (-not (Test-Path $envFile)) {
    throw ".env not found. Run scripts\setup-database.ps1 first."
}

# --- Reuse the owner credentials setup-database.ps1 already generated ------
$ownerLine = Get-Content $envFile | Where-Object { $_ -match '^MIGRATION_DATABASE_URL=' }
if (-not $ownerLine) {
    throw "MIGRATION_DATABASE_URL not found in .env. Run scripts\setup-database.ps1 first."
}
if ($ownerLine -notmatch 'postgresql\+psycopg://([^:]+):([^@]+)@') {
    throw "Could not parse MIGRATION_DATABASE_URL out of .env."
}
$ownerUser = $Matches[1]
$ownerPw   = $Matches[2]

function Invoke-PsqlAs {
    param([string]$User, [string]$Password, [string]$Db, [string]$Sql, [switch]$AllowFailure)
    # See setup-database.ps1's identical helper for why stderr is left
    # unmerged under Windows PowerShell 5.1's $ErrorActionPreference = "Stop".
    # -w: never stop and ask for a password (see setup-database.ps1).
    $env:PGPASSWORD = $Password
    try {
        $out = & $PsqlPath -U $User -h $PgHost -p $Port -d $Db -w `
                           -v ON_ERROR_STOP=1 -q -t -A -c $Sql
        if ($LASTEXITCODE -ne 0 -and -not $AllowFailure) {
            throw "psql failed (exit $LASTEXITCODE): $out"
        }
        return $out
    } finally {
        Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue
    }
}

# --- The tables this role is scoped to must already exist -------------------
$tableCheck = Invoke-PsqlAs -User $ownerUser -Password $ownerPw -Db $Database `
    -Sql "SELECT 1 FROM pg_tables WHERE tablename = 'whatsapp_agent_commands';"
if ($tableCheck -notmatch "1") {
    throw ("whatsapp_agent_commands does not exist yet in '$Database'. Run migrations first: " +
           "'.\.venv\Scripts\python.exe -m alembic upgrade head'")
}

Write-Host ""
Write-Host "Provisioning interlock_agent..." -ForegroundColor Cyan

# --- Role: needs the Postgres superuser --------------------------------------
$secure = $SuperuserPassword
if (-not $secure) {
    $secure = Read-Host "PostgreSQL superuser (postgres) password" -AsSecureString
}
$bstr   = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
    $superPw = [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
}

# Fail once, clearly, on a wrong password, before anything is changed.
Invoke-PsqlAs -User "postgres" -Password $superPw -Db "postgres" -Sql "SELECT 1;" -AllowFailure | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Could not sign in to PostgreSQL as 'postgres' (see the message above). Check the password, and that the PostgreSQL service is running. Nothing was changed."
}

$alphabet = (48..57) + (65..90) + (97..122)
$agentPw  =-join ($alphabet | Get-Random -Count 32 | ForEach-Object { [char]$_ })
$writeAgentEnv = $true
if ($KeepEnv -and (Test-Path $agentEnv)) {
    $kept = [regex]::Match((Get-Content $agentEnv -Raw), '(?m)^DATABASE_URL=postgresql://interlock_agent:([^@\s]+)@')
    if (-not $kept.Success) { throw "-KeepEnv: apps\agent\.env has no interlock_agent DATABASE_URL to keep." }
    $agentPw = $kept.Groups[1].Value
    $writeAgentEnv = $false
}

$roleSql = @"
DO `$`$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'interlock_agent') THEN
        ALTER ROLE interlock_agent WITH LOGIN PASSWORD '$agentPw';
    ELSE
        CREATE ROLE interlock_agent WITH LOGIN PASSWORD '$agentPw';
    END IF;
END
`$`$;
"@
Invoke-PsqlAs -User "postgres" -Password $superPw -Db "postgres" -Sql $roleSql | Out-Null
Write-Host "  role interlock_agent" -ForegroundColor Green

$superPw = $null
[System.GC]::Collect()

# --- Grants: interlock_owner already owns these tables, so it can grant on -
# --- them itself -- no further superuser use needed from here. -------------
$grantSql = @"
GRANT CONNECT ON DATABASE $Database TO interlock_agent;
GRANT USAGE ON SCHEMA public TO interlock_agent;
GRANT SELECT, INSERT, UPDATE ON whatsapp_agent_commands TO interlock_agent;
GRANT SELECT, INSERT, UPDATE ON whatsapp_agent_status TO interlock_agent;
"@
Invoke-PsqlAs -User $ownerUser -Password $ownerPw -Db $Database -Sql $grantSql | Out-Null
Write-Host "  privileges (whatsapp_agent_commands, whatsapp_agent_status only)" -ForegroundColor Green

# --- apps/agent/.env ---------------------------------------------------------
if ($writeAgentEnv) {
if (-not (Test-Path $agentDir)) {
    New-Item -ItemType Directory -Path $agentDir | Out-Null
}
$agentUrl = "postgresql://interlock_agent:$agentPw@${PgHost}:${Port}/$Database"
$agentEnvContent = @"
# Written by scripts\setup-agent-role.ps1 -- gitignored, holds a real
# generated password. Re-running this script rotates it.
DATABASE_URL=$agentUrl
LOG_LEVEL=info
"@
Set-Content -Path $agentEnv -Value $agentEnvContent -Encoding utf8
Write-Host "  wrote apps\agent\.env" -ForegroundColor Green
} else {
    Write-Host "  kept the existing apps\agent\.env" -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "Done." -ForegroundColor Cyan
Write-Host "  interlock_agent can read/write only whatsapp_agent_commands and"
Write-Host "  whatsapp_agent_status. It cannot see tasks, reports, approvals, or"
Write-Host "  audit_logs. apps\agent\.env is gitignored; re-running this script"
Write-Host "  rotates the password."
