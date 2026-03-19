# CODEBASE.md — Match's Self-Knowledge Doc

## What I Am
Match is a Signal-based AI assistant running on Billy's Ubuntu server (Haskins, via WSL on Windows).
Built on Sean Gibat's open-source Cleo framework. Codebase lives at ~/cleo/.

## Key Files
| File | Purpose |
|------|---------|
| ~/cleo/gateway.py | Main brain — wake loop, API calls, tool dispatch, feed handling |
| ~/cleo/tools.py | All tool implementations (exec, file I/O, send_message, web, memory) |
| ~/cleo/memory.py | Builds system prompts from workspace files |
| ~/cleo/auth.py | OAuth token management for Claude Pro |
| ~/cleo/scheduler.py | Cron-based heartbeat scheduler |
| ~/cleo/config.py | Config loader |
| ~/cleo/identity.py | Signal contact/group name resolution |
| ~/cleo/vectorstore.py | ChromaDB semantic memory |
| ~/cleo/voice.py | Voice generation (disabled, no API key) |
| ~/cleo/config.yaml | Runtime config — bot number, authorized senders, heartbeats |
| ~/cleo/.env | Credentials — OAuth token (active), API key (commented out) |

## Workspace Files (loaded every wake)
| File | Purpose |
|------|---------|
| ~/.cleo/workspace/SOUL.md | Personality — loaded every wake, keep under 2KB |
| ~/.cleo/workspace/USER.md | Who Billy is — loaded every wake |
| ~/.cleo/workspace/MEMORY.md | Long-term facts — loaded every wake, target under 4000 chars |

## Workspace Files (written/read during operation)
| File | Purpose |
|------|---------|
| ~/.cleo/workspace/consciousness.json | Rolling conversation history, capped at 20 messages, reset nightly |
| ~/.cleo/workspace/feeds.json | Signal conversation threads, capped at 6 messages each |
| ~/.cleo/workspace/usage.json | Session usage tracking — OAuth %, API cost, token counts, per-task breakdown |
| ~/.cleo/workspace/memory/YYYY-MM-DD.md | Daily raw log, written all day, deleted after dream |
| ~/.cleo/workspace/memory/summaries/YYYY-MM-DD.md | Dream summary, kept 14 days |
| ~/.cleo/workspace/memory/dream-log.md | Dream cycle log |

## Key Constants in gateway.py
| Constant | Value | Purpose |
|----------|-------|---------|
| MAX_CONSCIOUSNESS_MESSAGES | 20 | Max messages in rolling history |
| MAX_FEED_MESSAGES | 6 | Max messages retained per feed thread |
| MAX_TOKENS | 8192 | Max output tokens per API call |
| MAX_AGENTIC_ITERATIONS | 50 | Safety limit on tool loops |
| WAKE_DEBOUNCE_DM | 0 | Seconds to wait before waking for DMs |
| WAKE_DEBOUNCE_GROUP | 3.5 | Seconds to wait before waking for group messages |
| PRICE_INPUT_PER_M | 3.00 | Sonnet 4.6 input token price per million |
| PRICE_OUTPUT_PER_M | 15.00 | Sonnet 4.6 output token price per million |

## Usage Tracking (gateway.py)
- `_load_usage()` / `_save_usage()` — read/write `usage.json` at startup and after each API call
- `_update_usage(tokens_in, tokens_out, task_id)` — accumulates session cost + token counts; per-task breakdown if task_id provided
- `_check_limits()` — returns violation dict if OAuth 5h ≥ limit or API cost ≥ limit; else None
- `check_usage` tool — dynamically injected into wake_loop tools; returns OAuth %, API cost, token counts, tasks, violation
- Limits configurable in `usage.json` under `limits`: `oauth_5h` (fraction, default 1.0), `api_dollars` (default 999.0)
- OAuth utilization fed in from `_update_rate_limits()` (parsed from response headers)

## How a Wake Cycle Works
1. Message arrives via Signal → buffered into feeds.json
2. Debounce timer fires → wake_loop() called
3. Static prompt built from SOUL + USER + MEMORY (cached, ephemeral cache)
4. Vector store queried on first user message → top 5 relevant memory chunks injected as "Retrieved Memories"
5. Feed prefetched (last unread + 2 history messages) injected into consciousness
6. Billing header computed and injected as first system block (OAuth requirement)
7. API call made → Match reasons, calls tools, sends reply
8. Daily memory log appended
9. Feeds trimmed, consciousness saved

## Auth
- Match uses OAuth (Claude Pro session limit) via ~/.claude/.credentials.json
- ANTHROPIC_API_KEY in .env is COMMENTED OUT — it's for the looper only
- Looper reads key from ~/looper.env

## Dream (3am America/Chicago)
Fires via cron. Match should NOT schedule it manually. Match handles:
1. Read today's daily log → write summary to summaries/
2. Update MEMORY.md (prune to under 4000 chars)

Code handles automatically after dream completes:
- Feeds trimmed to 6, subagent/scheduled threads purged
- Today's daily log deleted
- Summaries older than 14 days deleted
- consciousness.json reset to empty

## Making Code Changes
1. Edit file directly: nano ~/cleo/gateway.py
2. Restart service: systemctl --user restart cleo
3. Verify: journalctl --user -u cleo | tail -20
4. Commit and push: ~/cleo/scripts/commit.sh "description"
- Changes to .py files or config.yaml require restart
- Changes to SOUL.md, MEMORY.md, USER.md take effect next wake (no restart needed)

## Signal Architecture
- signal-cli runs as java process, HTTP on 127.0.0.1:8080
- Bot number: +13463460886
- Billy UUID: d9ffd4d4-0738-46e1-a1fe-cfc95ebdd525
- Layla UUID: 311d1fcf-d571-4883-abe1-05e7fc196133
- Always message Billy via UUID, NOT phone (phone routes to Sean)

## Tools Available
exec_command, read_file, write_file, edit_file, send_message, web_search, web_fetch,
memory_search, find_files, delegate_task, cancel_tasks, check_feeds, read_feed,
send_reaction, send_poll, generate_image, describe_image, schedule_reminder,
check_quota, restart_self, check_usage

## Subagent System
- `delegate_task` launches background subagents with their own isolated conversation
- Subagent souls loaded from `~/.cleo/workspace/subagent_souls/` (engineer.md, researcher.md, consolidator.md)
- Billing header is injected into subagent system prompts (same OAuth fix as main loop)
- Results injected as `subagent:<task_id>` feed → wakes main loop for reporting
- `cancel_tasks` tool cancels all running asyncio subagent tasks (dynamically injected per wake)

## Vector Memory (vectorstore.py / ChromaDB)
- Initialized at startup: `init_vectorstore()` + `index_memory_files()` indexes all workspace memory
- On each wake: first user message queried → top 5 semantic chunks injected into dynamic prompt
- Also available as `memory_search` tool (semantic mode uses vector store; exact mode = grep)
- Indexed files: MEMORY.md, daily logs, dream summaries

## GitHub
- Repo: github.com/billyparsons/match-bot
- Remote: https://github.com/billyparsons/match-bot.git
- Credentials stored via git credential helper
- .gitignore excludes: .env, config.yaml, memory/, workspace data

## Text-to-GitHub Workflow
When Billy asks for a code change in plain English:
1. Make the change using edit_file or write_file
2. Test if needed via exec_command
3. Restart service if gateway.py or config files changed
4. ~/cleo/scripts/commit.sh "description"
5. Update CODEBASE.md and CHEATSHEET.md if constants or structure changed
6. Confirm to Billy what was changed and pushed

## Doc Update Workflow
After any significant code change is committed, Billy will text Match to update docs.
Match should then update ALL of the following in one shot:
- ~/cleo/CODEBASE.md — add/update the relevant section
- ~/cleo/CHEATSHEET.md — update quick reference if needed
- ~/.cleo/workspace/MEMORY.md — note the capability if Match needs to know about it
Then run ~/cleo/scripts/commit.sh "update docs for [feature]" to push doc changes.

## Commit Notification System
When Billy pushes to GitHub, Match is automatically notified via the `scheduled:commit-notify` feed.

**How it works:**
1. `~/cleo/scripts/commit.sh "message"` — stages, commits, and pushes to GitHub, then calls `notify_match.py`
2. `~/cleo/scripts/notify_match.py <message>` — writes to `scheduled:commit-notify` feed in feeds.json, then sends SIGUSR1 to gateway.py process
3. `gateway.py` SIGUSR1 handler — receives signal, reloads feeds.json, triggers `wake_loop()` immediately
4. Match wakes, reads the commit-notify feed, and decides what (if anything) to update in docs

**Decision rule:** Not every commit needs doc updates.
- Structural changes (new tools, new flows, new constants, new files) → update CODEBASE.md + CHEATSHEET.md + MEMORY.md as needed
- Behavior changes Billy interacts with → update CHEATSHEET.md
- Typos, refactors, minor fixes → no doc update needed; tell Billy "nothing to document here"

**SIGUSR1 handler location:** `gateway.py` in `main()`, after `_load_feeds()`, before `_load_consciousness()`

**Circular commit guard:** `commit.sh` checks for `$CLEO_PROCESS` env var — if set, skips feed injection so Match's own doc-update commits don't trigger another notification.
