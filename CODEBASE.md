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
| MAX_TOKENS | 16384 | Max output tokens per API call |
| MAX_AGENTIC_ITERATIONS | 50 | Safety limit on tool loops |
| WAKE_DEBOUNCE_DM | 0 | Seconds to wait before waking for DMs |
| WAKE_DEBOUNCE_GROUP | 3.5 | Seconds to wait before waking for group messages |
| PRICE_INPUT_PER_M | 3.00 | Sonnet 4.6 input token price per million |
| PRICE_OUTPUT_PER_M | 15.00 | Sonnet 4.6 output token price per million |

## Usage Tracking (gateway.py)
- `_load_usage()` / `_save_usage()` — read/write `usage.json` at startup and after each API call
- `_update_usage(tokens_in, tokens_out, task_id)` — accumulates session cost + token counts; per-task breakdown if task_id provided
- `_check_task_limits(task_id)` — checks if a specific task exceeded its delta limits (OAuth or API); returns violation dict or None
- `_check_limits()` — iterates all active tasks, returns first violation or None
- `check_usage` tool — dynamically injected into wake_loop tools; returns OAuth %, `api_spend_today` (API credits spent since last dream), per-task delta usage, violation. Daily spend counter resets at the 3am dream cycle.
- Limits are **delta-based** (per-task): configurable in `usage.json` under `limits`: `oauth_delta` (fraction of 5h window, default 0.15 = 15%), `api_delta` (dollars, default $1.00)
- Each subagent task snapshots `baseline_oauth_5h` and `baseline_api_cost` at start; limits measured from baseline
- Passive kill: if a subagent exceeds its delta limits mid-iteration, it is killed and a `system:kill:<task_id>` feed is injected to notify Match
- **Safe push on kill**: before killing a subagent, gateway iterates known repos and runs their `safe_push.sh` if it exists — `~/cleo/scripts/safe_push.sh`, `~/match-spark/scripts/safe_push.sh`, and `~/murmur-looper/scripts/safe_push.sh`. Each is called with `"autosave before kill"`. Timeout 30s per script. Failure is logged as warning but does not block the kill.
- **Looper 80% warning**: when a looper hits 80% of its `api_delta` budget, a `system:warn:<task_id>` feed is injected (fires once per session via `.warned80_<task_id>` flag file). Flag cleared on normal session completion. Gives Match a chance to raise the limit before the kill.
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

## Known Edge Cases
- **Midnight looper span**: if a looper session starts before midnight and runs past it, the API spend appears across two daily memory logs but only one dream consolidation processes it. `looper_spend_today` in usage.json is still accurate (single counter), but the daily log narrative may be split. Not a bug — just worth knowing.

## Dream (3am America/Chicago)
Fires via cron. Match should NOT schedule it manually. Match handles:
1. Read today's daily log → write summary to summaries/
2. Update MEMORY.md (prune to under 4000 chars)

Code handles automatically after dream completes:
- Feeds trimmed to 6, subagent/scheduled threads purged
- Today's daily log deleted
- Summaries older than 14 days deleted
- consciousness.json reset to empty
- Daily looper spend counter reset (`looper_spend_today` → 0.0)

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
check_quota, restart_self, check_usage, set_usage_limit,
advance_game, expand_game

**web_fetch**: truncates at **50,000 chars** (raised from 8,000).
**vote_poll**: excluded from subagent tool set (added to `_SUBAGENT_EXCLUDED_TOOLS`).
**delegate_task souls**: engineer (code/build/fix), researcher (web research, 3+ searches), consolidator (memory logs/MEMORY.md pruning), planner (architecture/design decisions before building), game_designer (Backprop rules, Murmur session design, game analysis). Soul routing guidance is embedded in the tool description itself.

## Subagent System
- `delegate_task` launches background subagents with their own isolated conversation
- Subagent souls loaded from `~/.cleo/workspace/subagent_souls/` (engineer.md, researcher.md, consolidator.md, planner.md, game_designer.md)
- Billing header is injected into subagent system prompts (same OAuth fix as main loop)
- **Cache tagging**: system prompt soul block tagged with `cache_control: ephemeral`. Last user message in history is tagged each iteration (old tags cleared first) to maximize prompt cache hits across long subagent runs.
- **Context management**: subagent API calls pass `extra_body={"context_management": {...}}` with two triggers: `compact_20260112` fires at 90k input tokens (summarizes context); `clear_tool_uses_20250919` fires at 60k tokens (clears old tool uses, keeps last 5, must clear ≥15k tokens). Prevents runaway context growth on long tasks.
- Results injected as `subagent:<task_id>` feed → wakes main loop for reporting
- `cancel_tasks` tool cancels all running asyncio subagent tasks (dynamically injected per wake)
- **Subagent timeout watchdog**: if a subagent hasn't completed within 45 minutes, gateway injects a `system:timeout:<task_id>` feed to warn Match. Allows Match to investigate or cancel hung tasks before they idle indefinitely.

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
5. Update CODEBASE.md if constants or structure changed
6. Confirm to Billy what was changed and pushed

## start_looper Tool
The `start_looper` tool handles both game design and campaign loopers. Uses a `_LOOPER_SCRIPTS` lookup table inside `gateway.py` — add new games there.

| game | script | extra params |
|------|--------|-------------|
| `backprop` | `~/cleo/game_design_session.py` | `loops`, `note` |
| `murmur` | `~/murmur-looper/scripts/looper.py` | `campaign` (default: infinite-costco), `players` (default: 3), `loops` = turns |

Unknown game names return an error listing known games. Murmur looper passes `--api-delta` flag directly so it can self-enforce its budget.

## Looper Pipeline Functions (game_design_session.py)
Key functions in the looper's design session pipeline:
- `run_agreement_gate(client, knizia_pos, thematist_pos, current_doc, transcript_lines, tokens, task_id)` — calls monitor in MODE 3 to find consensus between knizia and thematist designers. Up to 2 rounds of dispute resolution. Returns `(agreed_rules, directive_or_None)`. `agreed_rules` is a list of `{"section": ..., "rule_text": ...}` dicts. **Partial consensus proceeds**: if any rules are agreed, returns them immediately with a directive noting disputed rules are left unchanged. Only returns empty list if zero agreement after all rounds.
- `run_scribe(client, current_doc, agreed_rules, transcript_lines, tokens, task_id)` — calls scribe agent to apply `agreed_rules` to `current_doc`. Returns complete updated rulebook string. No-ops (returns doc unchanged) if `agreed_rules` is empty.
- `run_ratification(client, loop_num, knizia_pos, thematist_pos, current_doc, transcript_lines, tokens, phase, task_id)` — wrapper around `run_agreement_gate` + `run_scribe`. Takes separate `knizia_pos` and `thematist_pos` strings (not a merged `designer_debate`). `knizia_pos` is now the knizia_final response (substantive rule text, not a summary position). Returns `(ratified_doc, gate_directive_or_None)`.
- `preflight_check(game)` — runs at session start (top of `run_session()`). Diagnoses and auto-fixes common issues from previously killed/crashed loops: (1) restores `current_seed.md` from last approved design doc if seed is >40% shorter (corrupted mid-write); (2) clears stale `.killed_{game}-*` flag files; (3) clears stale task entries in `usage.json`. Returns list of actions taken; logs them to terminal as `[PREFLIGHT]`.
- `run_completeness_check(...)` — existing completeness check
- **Critic step**: receives truncated design doc + ALL seated playtester reports (newcomer, veteran, and — if seated — hobbyist and solo_coop snippets). Each report truncated to 250 chars. Hobbyist and solo snippets are only included if those playtesters were actually seated (based on player count via SEATING dict).
- **Runner notes step**: receives newcomer, veteran, and critic reports (200 chars each). Produces facilitator note: top 2 rules causing confusion, top 1 house-rule invented, and overall experience summary.
- `run_loop(...)` — main loop orchestrator; calls the above in sequence. **Seed update behavior**: approved loops write `final_doc` to `current_seed.md`. Unapproved loops still write scribe's output to seed if it differs from `current_doc`, so next session always builds on the latest agreed rules. Gate failure returns best available doc (scribe output if present, else `current_doc`).

## Doc Update Workflow
After any significant code change is committed, Billy will text Match to update docs.
Match should then update ALL of the following in one shot:
- ~/cleo/CODEBASE.md — add/update the relevant section
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
- Structural changes (new tools, new flows, new constants, new files) → update CODEBASE.md + MEMORY.md as needed
- Typos, refactors, minor fixes → no doc update needed; tell Billy "nothing to document here"

**SIGUSR1 handler location:** `gateway.py` in `main()`, after `_load_feeds()`, before `_load_consciousness()`

**Circular commit guard:** `commit.sh` checks for `$CLEO_PROCESS` env var — if set, skips feed injection so Match's own doc-update commits don't trigger another notification.
