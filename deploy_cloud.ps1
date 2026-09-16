$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Set-Location -LiteralPath $PSScriptRoot

function Write-Step([string]$s) { Write-Host "`n=== $s ===" -ForegroundColor Cyan }
function Refresh-Path {
    $machine = [Environment]::GetEnvironmentVariable('Path','Machine')
    $user = [Environment]::GetEnvironmentVariable('Path','User')
    $env:Path = "$machine;$user"
}
function Find-Program([string]$name) {
    return Get-Command $name -ErrorAction SilentlyContinue
}

Write-Host "Premier Residences Phu Quoc - 24h Cloud Watcher" -ForegroundColor Green
Write-Host "Target: 2026-12-25 -> 2026-12-28 / Adults 2 + Child age 4"
Write-Host "This setup creates a PUBLIC GitHub repository so 5-minute standard Actions runs do not consume private-repo minutes."
Write-Host "Telegram token/chat ID are stored as GitHub Secrets and are NOT committed to the repository."

Write-Step "Checking Git and GitHub CLI"
if (-not (Find-Program 'winget')) {
    throw 'winget was not found. Update/install App Installer from Microsoft Store, then run this file again.'
}
if (-not (Find-Program 'git')) {
    Write-Host 'Installing Git...'
    winget install --id Git.Git -e --source winget --accept-package-agreements --accept-source-agreements --silent
    Refresh-Path
}
if (-not (Find-Program 'gh')) {
    Write-Host 'Installing GitHub CLI...'
    winget install --id GitHub.cli -e --source winget --accept-package-agreements --accept-source-agreements --silent
    Refresh-Path
}
if (-not (Find-Program 'git')) { throw 'Git install was not detected. Close this window and run DEPLOY_CLOUD.bat again.' }
if (-not (Find-Program 'gh')) { throw 'GitHub CLI install was not detected. Close this window and run DEPLOY_CLOUD.bat again.' }
Write-Host "[OK] Git: $((Get-Command git).Source)"
Write-Host "[OK] gh:  $((Get-Command gh).Source)"

Write-Step "GitHub login"
$authCheck = 'gh auth status --hostname github.com >nul 2>nul'
& cmd.exe /d /s /c $authCheck
if ($LASTEXITCODE -ne 0) {
    Write-Host 'A browser will open once. Sign in to GitHub and approve the connection.' -ForegroundColor Yellow
    & gh auth login --hostname github.com --git-protocol https --web
    if ($LASTEXITCODE -ne 0) { throw 'GitHub login was not completed.' }
}
$owner = (& gh api user --jq '.login').Trim()
if (-not $owner) { throw 'Could not determine GitHub account.' }
Write-Host "[OK] GitHub account: $owner"

Write-Step "Finding existing Telegram watcher settings"
$roots = @(
    (Join-Path $env:USERPROFILE 'Downloads'),
    (Join-Path $env:USERPROFILE 'Desktop'),
    (Join-Path $env:USERPROFILE 'Documents')
) | Where-Object { Test-Path $_ }

$candidates = @()
foreach ($root in $roots) {
    try {
        $candidates += Get-ChildItem -Path $root -Filter 'config.json' -File -Recurse -ErrorAction SilentlyContinue |
          Where-Object { $_.FullName -match 'premier_residences_telegram_watcher' }
    } catch {}
}
$candidates = $candidates | Sort-Object LastWriteTime -Descending
$config = $null
$configPath = $null
foreach ($f in $candidates) {
    try {
        $j = Get-Content -LiteralPath $f.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($j.telegram_bot_token -and $j.telegram_chat_id) {
            $config = $j
            $configPath = $f.FullName
            break
        }
    } catch {}
}
if (-not $config) {
    Write-Host 'Could not automatically find the V3/V4 config.json.' -ForegroundColor Yellow
    Write-Host 'Copy config.json from your working premier_residences_telegram_watcher_v4 folder into this cloud folder, then run DEPLOY_CLOUD.bat again.' -ForegroundColor Yellow
    throw 'Telegram settings not found.'
}
Write-Host "[OK] Telegram settings found: $configPath"

Write-Step "Preparing GitHub repository"
$baseName = 'premier-residences-phuquoc-watcher'
$repoName = $baseName
$exists = $false
$repoCheck = 'gh repo view "{0}/{1}" >nul 2>nul' -f $owner, $repoName
& cmd.exe /d /s /c $repoCheck
if ($LASTEXITCODE -eq 0) { $exists = $true }
if ($exists) {
    $suffix = Get-Date -Format 'yyyyMMdd-HHmmss'
    $repoName = "$baseName-$suffix"
}
$repo = "$owner/$repoName"

if (Test-Path '.git') { Remove-Item -Recurse -Force '.git' }
& git init -b main | Out-Null
& git config user.name $owner
& git config user.email "$owner@users.noreply.github.com"
& git add .
& git commit -m 'Initial cloud watcher' | Out-Null

& gh repo create $repoName --public --source . --remote origin --push
if ($LASTEXITCODE -ne 0) { throw 'GitHub repository creation/push failed.' }
Write-Host "[OK] Repository created: https://github.com/$repo"

Write-Step "Saving Telegram secrets"
$token = [string]$config.telegram_bot_token
$chat = [string]$config.telegram_chat_id
$token | & gh secret set TELEGRAM_BOT_TOKEN --repo $repo
if ($LASTEXITCODE -ne 0) { throw 'Failed to save TELEGRAM_BOT_TOKEN secret.' }
$chat | & gh secret set TELEGRAM_CHAT_ID --repo $repo
if ($LASTEXITCODE -ne 0) { throw 'Failed to save TELEGRAM_CHAT_ID secret.' }
Write-Host '[OK] Telegram secrets saved securely.'

Write-Step "Starting first cloud test"
# GitHub can take a short time to index a brand-new workflow after the first push.
$started = $false
for ($i = 1; $i -le 18; $i++) {
    $runCmd = 'gh workflow run watch.yml --repo "{0}" >nul 2>nul' -f $repo
    & cmd.exe /d /s /c $runCmd
    if ($LASTEXITCODE -eq 0) {
        $started = $true
        break
    }
    Write-Host "Waiting for GitHub Actions workflow to become available... ($i/18)"
    Start-Sleep -Seconds 5
}
if (-not $started) {
    Start-Process "https://github.com/$repo/actions"
    throw 'Could not start the first workflow run after waiting for GitHub to index it.'
}

Write-Host 'Waiting for the run to appear...'
$runId = ''
for ($i = 1; $i -le 12; $i++) {
    try {
        $runId = (& gh run list --repo $repo --workflow watch.yml --limit 1 --json databaseId --jq '.[0].databaseId' 2>$null).Trim()
    } catch {
        $runId = ''
    }
    if ($runId) { break }
    Start-Sleep -Seconds 5
}
if (-not $runId) {
    Start-Process "https://github.com/$repo/actions"
    throw 'The workflow was started, but its run ID did not appear in time.'
}

Write-Host "Cloud run started. Run ID: $runId"
Write-Host 'Watching until it finishes (usually 1-3 minutes)...'
$oldEap = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
& gh run watch $runId --repo $repo --exit-status
$runExit = $LASTEXITCODE
$ErrorActionPreference = $oldEap
if ($runExit -ne 0) {
    Write-Host 'The first cloud run failed. Opening the Actions page so the error can be seen.' -ForegroundColor Yellow
    Start-Process "https://github.com/$repo/actions"
    throw 'First cloud test failed.'
}

Write-Step "DONE"
Write-Host 'PC can now be turned off. GitHub Actions will check approximately every 5 minutes.' -ForegroundColor Green
Write-Host 'If a room opens, your Telegram bot will send the alert.' -ForegroundColor Green
Write-Host "Repository: https://github.com/$repo"
Start-Process "https://github.com/$repo/actions"
exit 0
