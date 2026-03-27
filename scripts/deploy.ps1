# =============================================================================
# deploy.ps1 -- Build, push, and deploy polymarket-insider-tracker.
#
# Default flow: push to main -> watch GH Actions build -> deploy to VM.
# With -Image: skip the build and deploy a specific image directly.
#
# All config is loaded from .env in the repo root.
# Copy .env.example to .env and fill in all values.
# Parameters override .env values; .env values override environment variables.
#
# Usage (interactive):  .\scripts\deploy.ps1
# Usage (scripted):     .\scripts\deploy.ps1 -Yes -AutomatedRun
# Usage (re-deploy):    .\scripts\deploy.ps1 -Image 'ghcr.io/.../polymarket-insider-tracker:main-abc1234'
# =============================================================================
[CmdletBinding()]
param(
    [switch]$Yes,
    [switch]$AutomatedRun,

    # VM SSH connection
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
$envFilePath = Join-Path (Join-Path $PSScriptRoot '..') '.env'
$dotenv = @{}
if (Test-Path $envFilePath) {
    Get-Content $envFilePath | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith('#') -and $line -match '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
            $dotenv[$Matches[1]] = $Matches[2]
        }
    }
} else {
    Write-Host "  WARNING: .env file not found at $envFilePath" -ForegroundColor Yellow
    Write-Host "  Copy .env.example to .env and fill in all values." -ForegroundColor Yellow
    Write-Host ""
}

# Resolve each setting: explicit parameter > .env file > environment variable
function Resolve-Setting([string]$ParamValue, [string]$DotEnvKey, [string]$EnvVarName) {
    if ($ParamValue) { return $ParamValue }
    if ($dotenv.ContainsKey($DotEnvKey) -and $dotenv[$DotEnvKey]) { return $dotenv[$DotEnvKey] }
    $envVal = [Environment]::GetEnvironmentVariable($EnvVarName)
    if ($envVal) { return $envVal }
    return ''
}

$VmHost         = Resolve-Setting $VmHost         'DEPLOY_VM_HOST'     'DEPLOY_VM_HOST'
$VmPort         = Resolve-Setting $VmPort         'DEPLOY_VM_PORT'     'DEPLOY_VM_PORT'
$VmUser         = Resolve-Setting $VmUser         'DEPLOY_VM_USER'     'DEPLOY_VM_USER'
$VmKeyFile      = Resolve-Setting $VmKeyFile      'DEPLOY_VM_KEY_FILE' 'DEPLOY_VM_KEY_FILE'
$RedisContainer = Resolve-Setting $RedisContainer  'REDIS_CONTAINER_NAME' 'REDIS_CONTAINER_NAME'
$GhcrUser       = Resolve-Setting $GhcrUser       'GHCR_USER'          'GHCR_USER'
$GhcrPat        = Resolve-Setting $GhcrPat        'GHCR_PAT'           'GHCR_PAT'
$VmAppDir       = Resolve-Setting $VmAppDir       'DEPLOY_VM_APP_DIR'  'DEPLOY_VM_APP_DIR'

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
if (-not $VmHost)         { $missingVars += 'DEPLOY_VM_HOST' }
if (-not $VmPort)         { $missingVars += 'DEPLOY_VM_PORT' }
if (-not $VmUser)         { $missingVars += 'DEPLOY_VM_USER' }
if (-not $RedisContainer) { $missingVars += 'REDIS_CONTAINER_NAME' }
if (-not $GhcrUser)       { $missingVars += 'GHCR_USER' }
if (-not $GhcrPat)        { $missingVars += 'GHCR_PAT' }
if ($missingVars.Count -gt 0) {
    Write-Host "`n  x Missing required config: $($missingVars -join ', ')" -ForegroundColor Red
    Write-Host ""
    Write-Host "  Fill in these values in .env:" -ForegroundColor Yellow
    Write-Host "    DEPLOY_VM_HOST=<vm-hostname>"
    Write-Host "    DEPLOY_VM_PORT=<ssh-port>"
    Write-Host "    DEPLOY_VM_USER=<ssh-user>"
    Write-Host "    REDIS_CONTAINER_NAME=<redis-container-name>"
    Write-Host "    GHCR_USER=<github-username>"
    Write-Host "    GHCR_PAT=<ghcr-pat-with-packages-read>"
    Write-Host ""
    exit 1
}

# Validate that .env has the required application config for the tracker
$requiredAppVars = @('POSTGRES_PASSWORD', 'POLYGON_RPC_URL')
$missingApp = @()
foreach ($key in $requiredAppVars) {
    if (-not $dotenv.ContainsKey($key) -or -not $dotenv[$key]) {
        $missingApp += $key
    }
}
if ($missingApp.Count -gt 0) {
    Write-Host "`n  x Missing required application config in .env: $($missingApp -join ', ')" -ForegroundColor Red
    Write-Host ""
    Write-Host "  These are needed to generate the production env file on the VM:" -ForegroundColor Yellow
    Write-Host "    POSTGRES_PASSWORD=<strong-random-password>"
    Write-Host "    POLYGON_RPC_URL=<alchemy-or-infura-polygon-rpc-url>"
    Write-Host ""
    exit 1
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
$CHK  = [char]0x2713

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
# Generate .env.production from the local .env
#
# Strips deployment-only keys (DEPLOY_*, GHCR_*) and overrides DATABASE_URL
# and REDIS_URL to point at the compose-managed containers.
# ---------------------------------------------------------------------------
function New-EnvProduction {
    $prodFile = Join-Path $env:TEMP '.env.production'
    $lines = @(
        "# Auto-generated by deploy.ps1 — do not edit on the VM.",
        "# To change values, update .env locally and re-deploy.",
        ""
    )

    # Keys that are deployment-only (not needed by the application)
    $skipPrefixes = @('DEPLOY_', 'GHCR_', 'ADMINER_', 'REDIS_INSIGHT_', 'REDIS_HOST', 'REDIS_PORT',
                      'POSTGRES_HOST', 'POSTGRES_PORT', 'POSTGRES_DB', 'POSTGRES_USER')

    foreach ($key in ($dotenv.Keys | Sort-Object)) {
        $skip = $false
        foreach ($prefix in $skipPrefixes) {
            if ($key -eq $prefix -or $key.StartsWith($prefix)) { $skip = $true; break }
        }
        if ($skip) { continue }

        # Override DATABASE_URL and REDIS_URL for the container network
        if ($key -eq 'DATABASE_URL') {
            $pw = $dotenv['POSTGRES_PASSWORD']
            $lines += "DATABASE_URL=postgresql://tracker:${pw}@polymarket-tracker-postgres:5432/polymarket_tracker"
        } elseif ($key -eq 'REDIS_URL') {
            $lines += "REDIS_URL=redis://${RedisContainer}:6379"
        } else {
            $lines += "$key=$($dotenv[$key])"
        }
    }

    $lines | Set-Content -Path $prodFile -Encoding UTF8
    return $prodFile
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "  Polymarket Insider Tracker - Deploy" -ForegroundColor Cyan
Write-Host "  ====================================" -ForegroundColor Cyan
Write-Host ""

# Confirm
if (-not $Yes -and -not $AutomatedRun) {
    $confirm = Read-Host "  Deploy to production? (y/N)"
    if ($confirm -ne 'y') { Fail "Aborted by user." }
}

# ---------------------------------------------------------------------------
# Step 1: Build — push to main and watch GH Actions, or use provided image
# ---------------------------------------------------------------------------
if ($Image) {
    Write-Host "  Using provided image: $Image" -ForegroundColor Cyan
} else {
    Write-Host "  Pushing main to trigger build..."
    & git push origin main
    if ($LASTEXITCODE -ne 0) { Fail "git push failed" }

    Write-Host "`n  Watching GitHub Actions build..."
    & gh run watch --repo (git remote get-url origin) --exit-status
    if ($LASTEXITCODE -ne 0) { Fail "GitHub Actions build failed" }

    Write-Host "`n  $CHK Image built and pushed." -ForegroundColor Green

    # Derive the image tag: ghcr.io/<user>/polymarket-insider-tracker:main
    $Image = "ghcr.io/$GhcrUser/polymarket-insider-tracker:main"
    Write-Host "  Image: $Image" -ForegroundColor Cyan
}

# ---------------------------------------------------------------------------
# Step 2: Prepare VM
# ---------------------------------------------------------------------------
Write-Host "`n  Creating VM directories..."
$rc = Invoke-Ssh "mkdir -p $VmAppDir/scripts $VmAppDir/vars"
if ($rc -ne 0) { Fail "Failed to create directories on VM" }

# GHCR login on VM so docker pull works
Write-Host "`n  Refreshing GHCR credentials on VM..."
$rc = Invoke-Ssh "echo '$GhcrPat' | docker login ghcr.io -u $GhcrUser --password-stdin"
if ($rc -ne 0) { Fail "GHCR login failed on VM" }

# Generate and upload .env.production
Write-Host "`n  Generating .env.production from local .env..."
$prodEnvFile = New-EnvProduction
$rc = Copy-ToVm $prodEnvFile "$VmAppDir/vars/.env.production"
Remove-Item $prodEnvFile -ErrorAction SilentlyContinue
if ($rc -ne 0) { Fail "Failed to upload .env.production" }

# Sync compose and deploy script
Write-Host "`n  Syncing deploy files..."
$rc = Copy-ToVm "docker-compose.prod.yml" "$VmAppDir/docker-compose.prod.yml"
if ($rc -ne 0) { Fail "Failed to sync docker-compose.prod.yml" }

$rc = Copy-ToVm "scripts/deploy-tracker.sh" "$VmAppDir/scripts/deploy-tracker.sh"
if ($rc -ne 0) { Fail "Failed to sync deploy-tracker.sh" }

$rc = Invoke-Ssh "sed -i 's/\r//' $VmAppDir/scripts/deploy-tracker.sh && chmod +x $VmAppDir/scripts/deploy-tracker.sh"
if ($rc -ne 0) { Fail "Failed to prepare deploy-tracker.sh" }

# ---------------------------------------------------------------------------
# Step 3: Deploy on VM
# ---------------------------------------------------------------------------
Write-Host "`n  Deploying on VM..."
$rc = Invoke-Ssh "{ TRACKER_IMAGE='$Image' TRACKER_APP_DIR='$VmAppDir' REDIS_CONTAINER='$RedisContainer' $VmAppDir/scripts/deploy-tracker.sh; } 2>&1"
if ($rc -ne 0) { Fail "Deploy failed on VM" }

Write-Host "`n  $CHK Deployment complete.`n" -ForegroundColor Green
