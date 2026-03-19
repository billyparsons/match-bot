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
~/cleo/scripts/commit.sh "description"  # stage, commit, and push in one step
git log --oneline                       # see commit history
```

## Looper — Card Game Playtest Engine
```bash
python3 ~/game_design_session.py --game backprop          # run Backprop session
python3 ~/game_design_session.py --game backprop --loops 1  # 1 loop only
python3 ~/game_design_session.py --game backprop --loops 2 --note "focus on X"
python3 ~/game_design_session.py --list                   # list all games
cat ~/game-sessions/backprop/current_seed.md              # view current seed
cat ~/game-sessions/backprop/session_001_summary.md       # view summary
```

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
