# match-bot GitHub + Cleo efficiency status

If you're on Windows, start with `WINDOWS_QUICKSTART.md`.

## Recommended order (Windows)

1. Set `GITHUB_TOKEN` in PowerShell.
2. Run `scripts\setup_match_bot_windows.ps1`.
3. Push `efficiency-hardening` branch.

The PowerShell script handles:
- creating `match-bot` repo,
- cloning Cleo base,
- applying lean Claude Pro defaults.

## Script docs

- Windows quickstart: `WINDOWS_QUICKSTART.md`
- PowerShell setup script: `scripts/setup_match_bot_windows.ps1`
- Bash equivalents (if needed):
  - `scripts/create_match_bot_repo.sh`
  - `scripts/bootstrap_match_bot.sh`
  - `scripts/patch_cleo_pro_efficiency.sh`
