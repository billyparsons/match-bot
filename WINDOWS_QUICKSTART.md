# Windows Quickstart (match-bot from Cleo)

This is the exact order to run things on Windows.

## 0) Prerequisites (one-time)

1. Install **Git for Windows** (includes `git`).
2. Install **PowerShell 7** (recommended).
3. Create a GitHub Personal Access Token with `repo` scope.

## 1) Open PowerShell in this repo

```powershell
cd C:\path\to\Friend-Slots
```

## 2) Set your GitHub token for this shell session

```powershell
$env:GITHUB_TOKEN = "ghp_xxx"
```

## 3) Run the all-in-one Windows script

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup_match_bot_windows.ps1 \
  -WorkspaceDir "$env:USERPROFILE\dev" \
  -RepoName "match-bot" \
  -Visibility "private" \
  -SourceRepoUrl "https://github.com/seangibat/cleo.git"
```

What this does in order:
1. Creates GitHub repo `match-bot` (if token has rights).
2. Clones Cleo into `WorkspaceDir\match-bot`.
3. Creates branch `efficiency-hardening`.
4. Applies lean Claude Pro defaults.

## 4) Push branch to GitHub

```powershell
cd "$env:USERPROFILE\dev\match-bot"
git push -u origin efficiency-hardening
```

## 5) If repo creation says "already exists"

That is okay. The script continues with clone/patch.

## 6) If your network blocks GitHub

You will see 403/connect errors. In that case:
1. Download Cleo zip on a network that can access GitHub.
2. Extract to `C:\Users\<you>\dev\match-bot`.
3. Run script again with `-SkipRepoCreate`.

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup_match_bot_windows.ps1 \
  -WorkspaceDir "$env:USERPROFILE\dev" \
  -RepoName "match-bot" \
  -SkipRepoCreate
```
