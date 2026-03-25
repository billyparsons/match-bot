# Master Cheat Sheet

## Match Service
```bash
systemctl --user status cleo       # is Match running?
systemctl --user restart cleo      # restart Match
systemctl --user stop cleo         # stop Match
systemctl --user start cleo        # start Match
journalctl --user -u cleo | tail -20   # recent logs
journalctl --user -u cleo -f           # live logs
```

## Match Config Files
```bash
nano ~/cleo/config.yaml            # heartbeats, model, authorized senders
nano ~/cleo/.env                   # credentials (API key commented out)
nano ~/.cleo/workspace/SOUL.md     # Match personality
nano ~/.cleo/workspace/MEMORY.md   # Match long-term memory
nano ~/.cleo/workspace/USER.md     # who Billy is
nano ~/.cleo/workspace/CODEBASE.md # Match self-knowledge doc
```

## Match Memory
```bash
cat ~/.cleo/workspace/MEMORY.md
cat ~/.cleo/workspace/memory/$(date +%Y-%m-%d).md   # today's log
ls ~/.cleo/workspace/memory/summaries/               # dream summaries
wc -c ~/.cleo/workspace/consciousness.json           # consciousness size
wc -c ~/.cleo/workspace/feeds.json                   # feeds size
```

## Match Workspace Sizes (run after changes)
```bash
wc -c ~/.cleo/workspace/SOUL.md ~/.cleo/workspace/USER.md ~/.cleo/workspace/MEMORY.md ~/.cleo/workspace/feeds.json ~/.cleo/workspace/consciousness.json
```

## GitHub (from ~/cleo/)
```bash
cd ~/cleo
git status                              # see what changed
~/cleo/scripts/commit.sh "description"  # stage, commit, push, AND notify Match
git log --oneline                       # see commit history
```

## Commit → Match Auto-Notify
- `commit.sh` calls `scripts/notify_match.py` after every push
- `notify_match.py` writes to `scheduled:commit-notify` feed + sends SIGUSR1 to gateway
- Match wakes immediately and reviews commit for doc updates
- Circular guard: if Match itself commits (CLEO_PROCESS set), no notification fires

## Looper — Card Game Playtest Engine
### Via Match (Recommended)
```bash
"start looper for backprop"
"run 5 loops of backprop with a note about card balance"
"stop the looper"
```

### Manual (via shell)
```bash
python3 ~/game_design_session.py --game backprop          # run Backprop session
python3 ~/game_design_session.py --game backprop --loops 1  # 1 loop only
python3 ~/game_design_session.py --game backprop --loops 2 --note "focus on X"
python3 ~/game_design_session.py --list                   # list all games
cat ~/game-sessions/backprop/current_seed.md              # view current seed
cat ~/game-sessions/backprop/session_001_summary.md       # view summary
```

### Looper Tools
- `start_looper` — launches looper in background; `preflight_check()` now runs at session start (was defined but never called before). Auto-heals: restores corrupted seed from last approved doc, clears stale kill flags, clears stale task entries in usage.json. Logged to terminal as `[PREFLIGHT]`.
  - `loops` param: default **1** (changed from 3)
  - `api_delta` param: optional budget in dollars (e.g. `api_delta: 2.0`) — overrides default $1.00 kill threshold for this session. Say "api $2" and Match will pass it through.
  - `note` param: optional note to inject into the session
  - `players` param: **removed** (was default 4; now handled internally by game_design_session.py)
- `stop_looper` — kills a running looper session by game name
- Sessions numbered by finding highest existing session, not counting files
- Logs: ~/game-sessions/{game}/session_NNN_looper.log

### Looper Pipeline (game_design_session.py)
- **Critic step**: receives reports from ALL seated playtesters (not just newcomer + veteran). Hobbyist and solo_coop snippets are conditionally included based on player count (SEATING dict). Each report truncated to 250 chars.
- **knizia_final prompt**: now asks for substantive rule text (under 300 words, actual rule text per point, no summaries). Fed directly into ratification as `knizia_pos`.
- **Ratification call site**: passes `knizia_final` + `thematist_response` + `current_doc` separately (not a merged `designer_debate` string).
- **Agreement Gate** (`run_agreement_gate`): monitor calls monitor in MODE 3 to find consensus between knizia + thematist. Up to 2 dispute rounds. **Partial consensus proceeds**: if any rules are agreed, scribe applies those + skips disputed. Full consensus = directive is None. Partial = directive carries "Disputed rules left unchanged — resolve next loop." No agreed rules after max rounds = returns empty list.
- **Scribe** (`run_scribe`): applies agreed rules to the current rulebook via scribe agent. No-ops if agreed_rules is empty.
- **Seed update behavior**: On approved loops, `current_seed.md` is written with `final_doc`. On unapproved loops, if scribe produced output, that output is still written to seed so the next session builds on agreed rules (not the original seed). Gate failure returns best available doc (scribe output if it exists, else current_doc).

### Looper Kill Behavior
- **Intra-loop kill**: usage checked after EACH agent step (not just between loops) — killed mid-loop if limit breached
- **Summarizer skipped on kill**: if a session is killed, the end-of-session summarizer is skipped entirely
- **Iteration tokens**: 1200 per agent call (patch/doc-update calls get 4000)
- **Loop cost**: reported as per-loop cost (tokens used in that loop only), not cumulative session cost

## Looper — Start a New Game
```bash
mkdir -p ~/game-sessions/my-game
nano ~/game-sessions/my-game/current_seed.md
python3 ~/game_design_session.py --game my-game --loops 1
```

## Looper Auth
- API key lives in ~/looper.env (NOT in ~/cleo/.env)
- Match uses OAuth (session limit), looper uses API credits
- Never uncomment ANTHROPIC_API_KEY in ~/cleo/.env

## SCP — Copy Files Between Windows and Haskins
Run in Windows PowerShell:
```powershell
# Windows → Haskins
scp C:\Users\billy\Desktop\file.py billy@172.31.202.15:~/file.py
# Haskins → Windows
scp billy@172.31.202.15:~/game-sessions/backprop/session_001_summary.md C:\Users\billy\Desktop\
```

## Signal / Match Contacts
- Match bot number: +13463460886
- Billy UUID: d9ffd4d4-0738-46e1-a1fe-cfc95ebdd525
- Layla UUID: 311d1fcf-d571-4883-abe1-05e7fc196133

## Dream (runs 3am America/Chicago)
- Summarizes daily log → summaries/
- Prunes MEMORY.md to under 4000 chars
- Trims feeds to 6 messages, deletes system threads
- Deletes today's daily log
- Deletes summaries older than 14 days
- Resets consciousness.json to empty (in code)

## Key Gateway Constants
- MAX_CONSCIOUSNESS_MESSAGES = 20
- MAX_FEED_MESSAGES = 6
- MAX_TOKENS = 8192
- WAKE_DEBOUNCE_GROUP = 3.5 seconds

## Usage Tracking
- Match tracks OAuth %, API spend today, and token counts in `~/.cleo/workspace/usage.json`
- `check_usage` tool returns current session stats + any limit violations
- Tell Match "check usage" to see current utilization
- **API spend**: `check_usage` shows `api_spend_today` — total API credits spent since last dream (resets nightly at 3am dream). No manual balance tracking needed.
- Limits are **delta-based per task**: `oauth_delta` (default 15% of 5h window), `api_delta` (default $1.00)
- Each task snapshots its baseline at start; limits measured from that baseline, not total session
- **Subagents** monitored against `oauth_delta` (they use OAuth); **Loopers** monitored against `api_delta` (they use API credits)
- Passive kill: tasks auto-killed if they exceed their delta limit mid-run; system feed injected to notify Match
- Per-task delta usage shown in `check_usage` output (`active_tasks` field)

## OAuth Billing Fix (applied 2026-03-17)
- User-agent: claude-code/2.1.76
- Salt: 59cf53e54c78
- Billing header computed per-wake and injected as first system block
- Subagents also get billing header in their system prompts (fixed 2026-03-18)
- If Match gets 400 errors again: check latest Claude Code version and updated salt in clewdr repo
- Reference: https://github.com/anomalyco/opencode/issues/17910

## Subagents
- `delegate_task` → spawns background subagent (engineer/researcher/consolidator/planner)
- Soul files: ~/.cleo/workspace/subagent_souls/{engineer,researcher,consolidator}.md
- `cancel_tasks` → cancels all running subagents (dynamically injected tool, not in tools.py)
- Results come back as subagent:<task_id> feed → Match wakes and reports

## Vector Memory
- ChromaDB-backed semantic search over all workspace memory files
- Auto-indexed on startup; top 5 chunks injected per wake cycle
- memory_search tool: semantic mode = vector query; exact mode = grep
- Vectorstore lives in ~/.cleo/workspace/ (ChromaDB collection)
