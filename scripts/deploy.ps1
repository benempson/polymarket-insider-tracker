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
    # Leave VmKeyFile empty to use Pageant for authentication (recommended).
    # VmKeyFile must be a .ppk (PuTTY Private Key) if provided.
    [string]$VmHost,
    [string]$VmPort,
    [string]$VmUser,
    [string]$VmKeyFile = "",
    [string]$VmAppDir,

    # Shared Redis container name on the VM
    [string]$RedisContainer,

    # GHCR credentials
    [string]$GhcrUser,
    [string]$GhcrPat,

    # Skip build, deploy a specific image directly
    [string]$Image = '',

    # Collect diagnostics from the running bot on the VM and save to a local file
    [switch]$Diagnose,

    # Push .env changes to the VM and restart the tracker
    [switch]$UpdateConfig,

    # Show recent container logs
    [switch]$Logs
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
# VmKeyFile intentionally not resolved from .env — Pageant is the default.
# Pass -VmKeyFile explicitly only if you need a .ppk file instead of Pageant.
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
if (-not $Diagnose) {
    if (-not $RedisContainer) { $missingVars += 'REDIS_CONTAINER_NAME' }
}
if (-not $Diagnose -and -not $UpdateConfig) {
    if (-not $GhcrUser)       { $missingVars += 'GHCR_USER' }
    if (-not $GhcrPat)        { $missingVars += 'GHCR_PAT' }
}
if ($missingVars.Count -gt 0) {
    Write-Host "`n  x Missing required config: $($missingVars -join ', ')" -ForegroundColor Red
    Write-Host ""
    Write-Host "  Fill in these values in .env:" -ForegroundColor Yellow
    Write-Host "    DEPLOY_VM_HOST=<vm-hostname>"
    Write-Host "    DEPLOY_VM_PORT=<ssh-port>"
    Write-Host "    DEPLOY_VM_USER=<ssh-user>"
    if (-not $Diagnose) {
        Write-Host "    REDIS_CONTAINER_NAME=<redis-container-name>"
    }
    if (-not $Diagnose -and -not $UpdateConfig) {
        Write-Host "    GHCR_USER=<github-username>"
        Write-Host "    GHCR_PAT=<ghcr-pat-with-packages-read>"
    }
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

function Get-SshOutput([string]$Cmd) {
    $prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    if ($script:VmKeyFile) {
        $out = & plink -batch -P $script:VmPort -i $script:VmKeyFile "$($script:VmUser)@$($script:VmHost)" $Cmd 2>&1
    } else {
        $out = & plink -batch -agent -P $script:VmPort "$($script:VmUser)@$($script:VmHost)" $Cmd 2>&1
    }
    $ErrorActionPreference = $prev
    return $out | ForEach-Object {
        if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.Exception.Message } else { "$_" }
    }
}

function Write-DiagSection([string]$Title) {
    $line = '-' * [Math]::Max(0, 62 - $Title.Length)
    Write-Host ""
    Write-Host "  -- $Title $line" -ForegroundColor Cyan
}

function Invoke-Diagnose {
    Write-Host "  Polymarket Insider Tracker - Diagnostics" -ForegroundColor Cyan
    Write-Host "  =========================================" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  VM:        $VmUser@$VmHost`:$VmPort"
    Write-Host "  Collected: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') UTC"

    $script:_diagLines = [System.Collections.Generic.List[string]]::new()
    $script:_diagLines.Add("Polymarket Insider Tracker - Diagnostics")
    $script:_diagLines.Add("Collected: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') UTC")
    $script:_diagLines.Add("VM: $VmUser@$VmHost`:$VmPort")
    $script:_diagLines.Add("")

    function Collect([string]$section, [string]$cmd) {
        Write-DiagSection $section
        $out = Get-SshOutput $cmd
        $out | ForEach-Object { Write-Host "    $_" }
        $script:_diagLines.Add("-- $section " + ('-' * [Math]::Max(0, 62 - $section.Length)))
        $out | ForEach-Object { $script:_diagLines.Add($_) }
        $script:_diagLines.Add("")
    }

    Collect "Container Status" `
        "docker ps --filter name=polymarket-tracker --format 'table {{.Names}}`t{{.Status}}`t{{.Image}}'"

    Collect "Restart Counts" `
        "docker inspect polymarket-tracker polymarket-tracker-postgres --format '{{.Name}}  status={{.State.Status}}  restarts={{.RestartCount}}  exitCode={{.State.ExitCode}}' 2>/dev/null || echo 'One or both containers not found'"

    Collect "Health Endpoint (:8085/health)" `
        "curl -sf http://localhost:8085/health | python3 -m json.tool 2>/dev/null || echo 'UNREACHABLE'"

    Collect "Prometheus Metrics (:8085/metrics)" `
        "curl -sf http://localhost:8085/metrics 2>/dev/null | grep -E '^polymarket_[^#]' || echo 'UNREACHABLE'"

    Collect "Database Tables & Row Counts" `
        "docker exec polymarket-tracker-postgres psql -U tracker -d polymarket_tracker -c 'SELECT relname AS table, n_live_tup AS rows FROM pg_stat_user_tables ORDER BY n_live_tup DESC;' 2>/dev/null || echo 'Database unavailable'"

    Collect "Active Redis Dedup Keys (polymarket:dedup:*)" `
        "n=`$(docker exec `$(docker ps -qf name=nuera-backend-redis) redis-cli KEYS 'polymarket:dedup:*' 2>/dev/null | wc -l); echo `"`$n dedup keys active`""

    Collect "VM Resource Usage" `
        "free -h && echo '' && df -h / && echo '' && docker stats --no-stream --format 'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}' 2>/dev/null"

    Collect "Recent Logs - last 100 lines (stdout+stderr)" `
        "docker logs polymarket-tracker --tail 100 2>&1"

    $outDir  = Join-Path (Join-Path $PSScriptRoot '..') 'diagnostics'
    $null    = New-Item -ItemType Directory -Force -Path $outDir
    $outFile = Join-Path $outDir "diag-$(Get-Date -Format 'yyyyMMdd-HHmmss').txt"
    $script:_diagLines | Set-Content -Path $outFile -Encoding UTF8

    Write-Host ""
    Write-Host "  $CHK Diagnostics saved to: $outFile" -ForegroundColor Green
    Write-Host ""
}

function Invoke-UpdateConfig {
    Write-Host "  Polymarket Insider Tracker - Update Config" -ForegroundColor Cyan
    Write-Host "  ===========================================" -ForegroundColor Cyan
    Write-Host ""

    # Read .env.production from the VM
    Write-Host "  Reading .env.production from VM..."
    $fileContent = (Get-SshOutput "cat $VmAppDir/vars/.env.production 2>/dev/null || echo '___FILE_NOT_FOUND___'") -join "`n"

    if ($fileContent -eq '___FILE_NOT_FOUND___') {
        Fail "No .env.production found on VM at $VmAppDir/vars/.env.production"
    }

    # Parse .env.production into key=value pairs
    $fileVars = @{}
    $fileContent -split "`n" | ForEach-Object {
        $l = $_.Trim()
        if ($l -and -not $l.StartsWith('#') -and $l -match '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
            $fileVars[$Matches[1]] = $Matches[2]
        }
    }

    if ($fileVars.Count -eq 0) {
        Fail ".env.production on VM is empty or has no valid key=value pairs"
    }

    # Dump all env vars from the running container and parse the ones we care about
    Write-Host "  Comparing .env.production against running container..."
    $containerDump = Get-SshOutput "docker exec polymarket-tracker env 2>/dev/null"

    if (-not $containerDump) {
        Fail "Could not read environment from running container. Is polymarket-tracker running?"
    }

    # Parse container env output into hashtable
    $containerVars = @{}
    $containerDump | ForEach-Object {
        $l = "$_".Trim()
        if ($l -match '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
            $containerVars[$Matches[1]] = $Matches[2]
        }
    }

    # Find differences
    $changes = @()
    foreach ($key in ($fileVars.Keys | Sort-Object)) {
        $fileVal = $fileVars[$key]
        $containerVal = if ($containerVars.ContainsKey($key)) { $containerVars[$key] } else { $null }

        if ($containerVal -eq $null) {
            $changes += "  + $key (new - not set in container)"
        } elseif ($fileVal -ne $containerVal) {
            $isSensitive = $key -match '(?i)(password|secret|token|pat|key|url)'
            if ($isSensitive) {
                $changes += "  ~ $key (changed)"
            } else {
                $changes += "  ~ $key : $containerVal -> $fileVal"
            }
        }
    }

    if ($changes.Count -eq 0) {
        Write-Host ""
        Write-Host "  No differences found. The running container already matches .env.production." -ForegroundColor Green
        Write-Host ""
        return
    }

    Write-Host ""
    Write-Host "  Changes to apply ($($changes.Count)):" -ForegroundColor Yellow
    $changes | ForEach-Object { Write-Host $_ }
    Write-Host ""

    # Recreate tracker to pick up new env_file (restart alone won't re-read it)
    Write-Host "  Recreating tracker container..."
    $restartCmd = @(
        "cd $VmAppDir",
        "export POSTGRES_PASSWORD=`$(grep -E '^POSTGRES_PASSWORD=' vars/.env.production | cut -d= -f2- | tr -d '\\r')",
        "export TRACKER_IMAGE=`$(docker inspect polymarket-tracker --format='{{.Config.Image}}' 2>/dev/null)",
        "export REDIS_CONTAINER='$RedisContainer'",
        "export REDIS_NETWORK=`$(docker inspect '$RedisContainer' --format='{{range `$k,`$v := .NetworkSettings.Networks}}{{`$k}}{{end}}' 2>/dev/null | head -1)",
        "export TRACKER_APP_DIR='$VmAppDir'",
        "docker compose -f docker-compose.prod.yml stop tracker",
        "docker compose -f docker-compose.prod.yml rm -f tracker",
        "docker compose -f docker-compose.prod.yml up -d tracker"
    ) -join ' && '
    $rc = Invoke-Ssh "{ $restartCmd; } 2>&1"
    if ($rc -ne 0) { Fail "Failed to recreate tracker" }

    # Quick health check
    Write-Host "  Waiting for health check..."
    Start-Sleep -Seconds 10
    $healthOut = Get-SshOutput "curl -sf http://localhost:8085/health 2>/dev/null || echo 'UNREACHABLE'"
    $healthStr = $healthOut -join ' '
    if ($healthStr -match '"status"') {
        Write-Host "`n  $CHK Config updated and tracker restarted.`n" -ForegroundColor Green
    } else {
        Write-Host "`n  WARNING: Tracker may not be healthy after restart. Run diagnostics to check." -ForegroundColor Yellow
        Write-Host ""
    }
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
Write-Host ""

if ($Diagnose) { Invoke-Diagnose; exit 0 }
if ($UpdateConfig) { Invoke-UpdateConfig; exit 0 }
if ($Logs) { Invoke-Ssh "curl -sf 'http://localhost:8085/logs?lines=100' 2>/dev/null || docker logs polymarket-tracker --tail 100 2>&1"; exit 0 }

Write-Host "  Polymarket Insider Tracker - Deploy" -ForegroundColor Cyan
Write-Host "  ====================================" -ForegroundColor Cyan
Write-Host ""

# ---------------------------------------------------------------------------
# Step 1: Build or skip — choose action
# ---------------------------------------------------------------------------
if ($Image) {
    Write-Host "  Using provided image: $Image" -ForegroundColor Cyan
} else {
    $defaultImage = "ghcr.io/$GhcrUser/polymarket-insider-tracker:main"

    if (-not $Yes -and -not $AutomatedRun) {
        Write-Host "  1) Build and deploy  (push, build image, then deploy to VM)"
        Write-Host "  2) Deploy only       (use latest image, skip build)"
        Write-Host "  3) Update config     (push .env changes, restart tracker)"
        Write-Host "  4) Show recent logs  (last 100 lines)"
        Write-Host "  5) Collect diagnostics from the running bot"
        Write-Host ""
        $choice = Read-Host "  Choose (1/2/3/4/5)"
        if ($choice -notin @('1','2','3','4','5')) { Fail "Invalid choice '$choice'. Please enter 1, 2, 3, 4, or 5." }
    } else {
        $choice = '1'
    }

    if ($choice -eq '5') { Invoke-Diagnose; exit 0 }
    if ($choice -eq '4') { Invoke-Ssh "curl -sf 'http://localhost:8085/logs?lines=100' 2>/dev/null || docker logs polymarket-tracker --tail 100 2>&1"; exit 0 }
    if ($choice -eq '3') { Invoke-UpdateConfig; exit 0 }

    if ($choice -eq '2') {
        $Image = $defaultImage
        Write-Host "`n  Skipping build, using latest image: $Image" -ForegroundColor Cyan
    } else {
        Write-Host "`n  Pushing main..."
        & git push origin main
        if ($LASTEXITCODE -ne 0) { Fail "git push failed" }

        $repo = git remote get-url origin
        Write-Host "`n  Triggering build workflow..."
        & gh workflow run deploy.yml --repo $repo
        if ($LASTEXITCODE -ne 0) { Fail "Failed to trigger workflow" }

        # Poll until GH registers the run (max ~30s)
        $runId = $null
        for ($i = 0; $i -lt 10; $i++) {
            Start-Sleep -Seconds 3
            $runId = & gh run list --repo $repo --workflow=deploy.yml --status=in_progress --json databaseId --jq '.[0].databaseId' 2>$null
            if ($runId) { break }
            $runId = & gh run list --repo $repo --workflow=deploy.yml --status=queued --json databaseId --jq '.[0].databaseId' 2>$null
            if ($runId) { break }
        }
        if (-not $runId) { Fail "Could not find triggered workflow run" }

        Write-Host "  Watching build run $runId..."
        & gh run watch $runId --repo $repo --exit-status
        if ($LASTEXITCODE -ne 0) { Fail "GitHub Actions build failed" }

        Write-Host "`n  $CHK Image built and pushed." -ForegroundColor Green
        $Image = $defaultImage
        Write-Host "  Image: $Image" -ForegroundColor Cyan
    }
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

# Ensure .env.production exists on VM — seed from .env.example if not
$envExists = Get-SshOutput "test -f $VmAppDir/vars/.env.production; if [ `$? -eq 0 ]; then echo EXISTS; else echo MISSING; fi"
$envExists = ($envExists -join '').Trim()
if ($envExists -eq 'MISSING') {
    Write-Host "`n  No .env.production on VM — seeding from .env.example..."
    $rc = Copy-ToVm ".env.example" "$VmAppDir/vars/.env.production"
    if ($rc -ne 0) { Fail "Failed to upload .env.example as .env.production" }
    $rc = Invoke-Ssh "sed -i 's/\r//' $VmAppDir/vars/.env.production"
    if ($rc -ne 0) { Fail "Failed to fix line endings in .env.production" }
    Write-Host ""
    Write-Host "  WARNING: .env.production was seeded from .env.example." -ForegroundColor Yellow
    Write-Host "  You MUST edit it on the VM before the tracker will work correctly:" -ForegroundColor Yellow
    Write-Host "    ssh $VmUser@$VmHost -p $VmPort"
    Write-Host "    nano $VmAppDir/vars/.env.production"
    Write-Host ""
    Fail "Edit .env.production on the VM, then re-deploy."
} else {
    # Warn if .env.production looks like an unedited .env.example
    $prodContent = (Get-SshOutput "cat $VmAppDir/vars/.env.production") -join "`n"
    $exampleContent = (Get-Content ".env.example" -Raw) -replace "`r`n", "`n"
    if ($prodContent.Trim() -eq $exampleContent.Trim()) {
        Write-Host ""
        Write-Host "  WARNING: .env.production on the VM is identical to .env.example." -ForegroundColor Yellow
        Write-Host "  It likely has placeholder values. Edit it on the VM:" -ForegroundColor Yellow
        Write-Host "    ssh $VmUser@$VmHost -p $VmPort"
        Write-Host "    nano $VmAppDir/vars/.env.production"
        Write-Host ""
        Fail "Edit .env.production on the VM, then re-deploy."
    }
    Write-Host "`n  .env.production exists on VM $CHK"
}

# Sync compose and deploy script
Write-Host "`n  Syncing deploy files..."
$rc = Copy-ToVm "docker-compose.prod.yml" "$VmAppDir/docker-compose.prod.yml"
if ($rc -ne 0) { Fail "Failed to sync docker-compose.prod.yml" }

$rc = Copy-ToVm "scripts/deploy-tracker.sh" "$VmAppDir/scripts/deploy-tracker.sh"
if ($rc -ne 0) { Fail "Failed to sync deploy-tracker.sh" }

$rc = Invoke-Ssh "sed -i 's/\r//' $VmAppDir/scripts/deploy-tracker.sh"
if ($rc -ne 0) { Fail "Failed to fix line endings in deploy-tracker.sh" }
$rc = Invoke-Ssh "chmod +x $VmAppDir/scripts/deploy-tracker.sh"
if ($rc -ne 0) { Fail "Failed to make deploy-tracker.sh executable" }

# ---------------------------------------------------------------------------
# Step 3: Deploy on VM
# ---------------------------------------------------------------------------
Write-Host "`n  Deploying on VM..."
$rc = Invoke-Ssh "{ TRACKER_IMAGE='$Image' TRACKER_APP_DIR='$VmAppDir' REDIS_CONTAINER='$RedisContainer' $VmAppDir/scripts/deploy-tracker.sh; } 2>&1"
if ($rc -ne 0) { Fail "Deploy failed on VM" }

Write-Host "`n  $CHK Deployment complete.`n" -ForegroundColor Green
