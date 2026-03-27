# =============================================================================
# deploy.ps1 -- Interactive deploy helper for polymarket-insider-tracker.
#
# Pushes main, tracks the GitHub Actions build, then deploys to the VM.
#
# VM/GHCR connection details are loaded from .env in the repo root.
# Copy .env.example to .env and fill in the DEPLOY_* and GHCR_* values.
# Parameters override .env values; .env values override environment variables.
#
# Usage (interactive):  .\scripts\deploy.ps1
# Usage (scripted):     .\scripts\deploy.ps1 -Yes -AutomatedRun
#
# Parameters:
#   -Yes            Skip confirmation prompts (auto-confirm).
#   -AutomatedRun   Strict non-interactive mode.
#   -Image          Skip build and deploy a specific image directly.
# =============================================================================
[CmdletBinding()]
param(
    [switch]$Yes,
    [switch]$AutomatedRun,

    # VM SSH connection — defaults populated from .env / env vars below
    [string]$VmHost,
    [string]$VmPort,
    [string]$VmUser,
    [string]$VmKeyFile,
    [string]$VmAppDir,

    # Shared Redis container name on the VM
    [string]$RedisContainer,

    # GHCR credentials
    [string]$GhcrUser,
    [string]$GhcrPat,

    # Skip build, deploy a specific image directly
    [string]$Image = ''
)

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# Load .env file (KEY=VALUE lines, skipping comments and blanks)
# ---------------------------------------------------------------------------
$envFile = Join-Path (Join-Path $PSScriptRoot '..') '.env'
$dotenv = @{}
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith('#') -and $line -match '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
            $dotenv[$Matches[1]] = $Matches[2]
        }
    }
}

# Resolve each setting: explicit parameter > .env file > environment variable
function Resolve-Setting([string]$ParamValue, [string]$DotEnvKey, [string]$EnvVarName) {
    if ($ParamValue) { return $ParamValue }
    if ($dotenv.ContainsKey($DotEnvKey) -and $dotenv[$DotEnvKey]) { return $dotenv[$DotEnvKey] }
    $envVal = [Environment]::GetEnvironmentVariable($EnvVarName)
    if ($envVal) { return $envVal }
    return ''
}

$VmHost    = Resolve-Setting $VmHost    'DEPLOY_VM_HOST'    'DEPLOY_VM_HOST'
$VmPort    = Resolve-Setting $VmPort    'DEPLOY_VM_PORT'    'DEPLOY_VM_PORT'
$VmUser    = Resolve-Setting $VmUser    'DEPLOY_VM_USER'    'DEPLOY_VM_USER'
$VmKeyFile = Resolve-Setting $VmKeyFile 'DEPLOY_VM_KEY_FILE' 'DEPLOY_VM_KEY_FILE'
$RedisContainer = Resolve-Setting $RedisContainer 'REDIS_CONTAINER_NAME' 'REDIS_CONTAINER_NAME'
$GhcrUser  = Resolve-Setting $GhcrUser  'GHCR_USER'         'GHCR_USER'
$GhcrPat   = Resolve-Setting $GhcrPat   'GHCR_PAT'          'GHCR_PAT'
$VmAppDir  = Resolve-Setting $VmAppDir  'DEPLOY_VM_APP_DIR' 'DEPLOY_VM_APP_DIR'

# Default app dir derived from user if not explicitly set
if (-not $VmAppDir -and $VmUser) {
    $VmAppDir = "/home/$VmUser/polymarket-insider-tracker"
}

# ---------------------------------------------------------------------------
# Validate required config
# ---------------------------------------------------------------------------
function Fail([string]$Msg) {
    Write-Host "`n  x $Msg`n" -ForegroundColor Red
    exit 1
}

$missingVars = @()
if (-not $VmHost) { $missingVars += 'DEPLOY_VM_HOST' }
if (-not $VmPort) { $missingVars += 'DEPLOY_VM_PORT' }
if (-not $VmUser) { $missingVars += 'DEPLOY_VM_USER' }
if (-not $RedisContainer) { $missingVars += 'REDIS_CONTAINER_NAME' }
if ($missingVars.Count -gt 0) {
    Write-Host "`n  x Missing required config: $($missingVars -join ', ')" -ForegroundColor Red
    Write-Host ""
    Write-Host "  Copy .env.example to .env and fill in the deployment section:" -ForegroundColor Yellow
    Write-Host "    DEPLOY_VM_HOST=<vm-hostname>"
    Write-Host "    DEPLOY_VM_PORT=<ssh-port>"
    Write-Host "    DEPLOY_VM_USER=<ssh-user>"
    Write-Host "    GHCR_USER=<github-username>    (for -Image deploys)"
    Write-Host "    GHCR_PAT=<ghcr-pat>            (for -Image deploys)"
    Write-Host ""
    exit 1
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
function Invoke-Ssh([string]$Cmd) {
    if ($script:VmKeyFile) {
        & plink -batch -P $script:VmPort -i $script:VmKeyFile "$($script:VmUser)@$($script:VmHost)" $Cmd | Out-Host
    } else {
        & plink -batch -agent -P $script:VmPort "$($script:VmUser)@$($script:VmHost)" $Cmd | Out-Host
    }
    return $LASTEXITCODE
}

function Copy-ToVm([string]$LocalPath, [string]$RemotePath) {
    Write-Host "  > pscp $LocalPath -> $RemotePath"
    $prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    if ($script:VmKeyFile) {
        $out = & pscp -batch -P $script:VmPort -i $script:VmKeyFile $LocalPath "$($script:VmUser)@$($script:VmHost):$RemotePath" 2>&1
    } else {
        $out = & pscp -batch -agent -P $script:VmPort $LocalPath "$($script:VmUser)@$($script:VmHost):$RemotePath" 2>&1
    }
    $ec = $LASTEXITCODE; $ErrorActionPreference = $prev
    $out | ForEach-Object {
        $line = if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.Exception.Message } else { "$_" }
        if ($line.Trim()) { Write-Host "    $line" }
    }
    return $ec
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
$CHK = [char]0x2713

Write-Host ""
Write-Host "  Polymarket Insider Tracker - Deploy" -ForegroundColor Cyan
Write-Host "  ====================================" -ForegroundColor Cyan
Write-Host ""

# Confirm
if (-not $Yes -and -not $AutomatedRun) {
    $confirm = Read-Host "  Deploy to production? (y/N)"
    if ($confirm -ne 'y') { Fail "Aborted by user." }
}

if ($Image) {
    Write-Host "  Using provided image: $Image" -ForegroundColor Cyan
} else {
    # Push and trigger GH Actions build
    Write-Host "  Pushing main to trigger build..."
    & git push origin main
    if ($LASTEXITCODE -ne 0) { Fail "git push failed" }

    # Watch the GH Actions run
    Write-Host "`n  Watching GitHub Actions build..."
    & gh run watch --repo (git remote get-url origin) --exit-status
    if ($LASTEXITCODE -ne 0) { Fail "GitHub Actions build failed" }

    # The GH Actions workflow handles the full deploy including SSH.
    # If we reach here, the deploy succeeded.
    Write-Host "`n  $CHK Build and deploy complete.`n" -ForegroundColor Green
    exit 0
}

# ---------------------------------------------------------------------------
# Direct image deploy (skip build)
# ---------------------------------------------------------------------------
Write-Host "`n  Syncing deploy files to VM..."

$rc = Invoke-Ssh "mkdir -p $VmAppDir/scripts $VmAppDir/vars"
if ($rc -ne 0) { Fail "Failed to create directories on VM" }

$rc = Copy-ToVm "docker-compose.prod.yml" "$VmAppDir/docker-compose.prod.yml"
if ($rc -ne 0) { Fail "Failed to sync docker-compose.prod.yml" }

$rc = Copy-ToVm "scripts/deploy-tracker.sh" "$VmAppDir/scripts/deploy-tracker.sh"
if ($rc -ne 0) { Fail "Failed to sync deploy-tracker.sh" }

$rc = Invoke-Ssh "sed -i 's/\r//' $VmAppDir/scripts/deploy-tracker.sh && chmod +x $VmAppDir/scripts/deploy-tracker.sh"
if ($rc -ne 0) { Fail "Failed to prepare deploy-tracker.sh" }

if ($GhcrPat -and $GhcrUser) {
    Write-Host "`n  Refreshing GHCR credentials on VM..."
    $rc = Invoke-Ssh "echo '$GhcrPat' | docker login ghcr.io -u $GhcrUser --password-stdin"
    if ($rc -ne 0) { Fail "GHCR login failed on VM" }
}

Write-Host "`n  Deploying on VM..."
$rc = Invoke-Ssh "{ TRACKER_IMAGE='$Image' TRACKER_APP_DIR='$VmAppDir' REDIS_CONTAINER='$RedisContainer' $VmAppDir/scripts/deploy-tracker.sh; } 2>&1"
if ($rc -ne 0) { Fail "Deploy failed on VM" }

Write-Host "`n  $CHK Deployment complete.`n" -ForegroundColor Green
