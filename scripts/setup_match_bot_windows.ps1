param(
  [string]$WorkspaceDir = "$HOME\dev",
  [string]$RepoName = "match-bot",
  [ValidateSet("private","public")]
  [string]$Visibility = "private",
  [string]$SourceRepoUrl = "https://github.com/seangibat/cleo.git",
  [switch]$SkipRepoCreate
)

$ErrorActionPreference = "Stop"

function Write-Info($msg) { Write-Host "[setup-win] $msg" }
function Write-Warn2($msg) { Write-Warning "[setup-win] $msg" }

function Replace-IfPresent {
  param(
    [Parameter(Mandatory = $true)][string]$File,
    [Parameter(Mandatory = $true)][string]$Old,
    [Parameter(Mandatory = $true)][string]$New
  )

  if (-not (Test-Path $File)) {
    Write-Warn2 "Skip missing file: $File"
    return
  }

  $content = Get-Content -Raw -Path $File
  if ($content.Contains($Old)) {
    Write-Info "Patch: $File :: '$Old' -> '$New'"
    $escaped = [regex]::Escape($Old)
    $updated = [regex]::Replace($content, $escaped, [System.Text.RegularExpressions.MatchEvaluator]{ param($m) $New })
    Set-Content -Path $File -Value $updated -NoNewline
  } else {
    Write-Warn2 "Pattern not found in $File: $Old"
  }
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
  throw "git is not installed. Install Git for Windows first."
}

$targetDir = Join-Path $WorkspaceDir $RepoName
New-Item -ItemType Directory -Path $WorkspaceDir -Force | Out-Null

if (-not $SkipRepoCreate) {
  if (-not $env:GITHUB_TOKEN) {
    throw "GITHUB_TOKEN is not set. Set `$env:GITHUB_TOKEN first (repo scope)."
  }

  Write-Info "Creating GitHub repo '$RepoName' ($Visibility) via REST API"
  $privateFlag = if ($Visibility -eq "private") { $true } else { $false }
  $body = @{ name = $RepoName; private = $privateFlag } | ConvertTo-Json

  try {
    Invoke-RestMethod -Method Post -Uri "https://api.github.com/user/repos" `
      -Headers @{ Authorization = "Bearer $($env:GITHUB_TOKEN)"; Accept = "application/vnd.github+json" } `
      -Body $body -ContentType "application/json" | Out-Null
    Write-Info "Repo created (or returned success)."
  } catch {
    $msg = $_.Exception.Message
    if ($msg -match "422") {
      Write-Warn2 "Repo likely already exists (HTTP 422). Continuing."
    } else {
      throw
    }
  }
}

if (Test-Path (Join-Path $targetDir ".git")) {
  Write-Warn2 "Target repo already exists at $targetDir. Reusing."
} else {
  if (Test-Path $targetDir) { Remove-Item -Recurse -Force $targetDir }
  Write-Info "Cloning Cleo from $SourceRepoUrl"
  git clone $SourceRepoUrl $targetDir
}

Push-Location $targetDir
try {
  Write-Info "Creating/switching to branch efficiency-hardening"
  git checkout -B efficiency-hardening

  # Patch common config locations.
  Replace-IfPresent -File "src/config/defaults.ts" -Old "readFeedLimit: 50" -New "readFeedLimit: 10"
  Replace-IfPresent -File "src/config/defaults.ts" -Old "webFetchMaxChars: 50000" -New "webFetchMaxChars: 8000"
  Replace-IfPresent -File "src/config/defaults.ts" -Old "retrievalTopK: 10" -New "retrievalTopK: 4"
  Replace-IfPresent -File "src/config/defaults.ts" -Old "mainLoopMaxIterations: 50" -New "mainLoopMaxIterations: 10"
  Replace-IfPresent -File "src/config/defaults.ts" -Old "subagentMaxIterations: 100" -New "subagentMaxIterations: 15"
  Replace-IfPresent -File "src/config/defaults.ts" -Old "maxConcurrentSubagents: 3" -New "maxConcurrentSubagents: 1"
  Replace-IfPresent -File "src/config/defaults.ts" -Old "subagentsEnabledByDefault: true" -New "subagentsEnabledByDefault: false"

  Replace-IfPresent -File "src/config.ts" -Old "readFeedLimit: 50" -New "readFeedLimit: 10"
  Replace-IfPresent -File "src/config.ts" -Old "webFetchMaxChars: 50000" -New "webFetchMaxChars: 8000"
  Replace-IfPresent -File "src/config.ts" -Old "retrievalTopK: 10" -New "retrievalTopK: 4"
  Replace-IfPresent -File "src/config.ts" -Old "mainLoopMaxIterations: 50" -New "mainLoopMaxIterations: 10"
  Replace-IfPresent -File "src/config.ts" -Old "subagentMaxIterations: 100" -New "subagentMaxIterations: 15"
  Replace-IfPresent -File "src/config.ts" -Old "maxConcurrentSubagents: 3" -New "maxConcurrentSubagents: 1"
  Replace-IfPresent -File "src/config.ts" -Old "subagentsEnabledByDefault: true" -New "subagentsEnabledByDefault: false"

  @"
profile=claude-pro-lean
changes=read_feed_50_to_10,web_fetch_50000_to_8000,retrieval_10_to_4,main_loop_50_to_10,subagent_100_to_15,subagents_default_off
"@ | Set-Content -Path ".efficiency-profile-applied"

  Write-Info "Done. Next: git status; git add -A; git commit; git push -u origin efficiency-hardening"
} finally {
  Pop-Location
}
