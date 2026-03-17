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
| ~/.cleo/workspace/CODEBASE.md | This file — Match's self-knowledge |

## Workspace Files (written/read during operation)
| File | Purpose |
|------|---------|
| ~/.cleo/workspace/consciousness.json | Rolling conversation history, capped at 20 messages, reset nightly |
| ~/.cleo/workspace/feeds.json | Signal conversation threads, capped at 6 messages each |
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

## How a Wake Cycle Works
1. Message arrives via Signal → buffered into feeds.json
2. Debounce timer fires → wake_loop() called
3. Static prompt built from SOUL + USER + MEMORY (cached)
4. Feed prefetched (last unread + 2 history messages) injected into consciousness
5. API call made → Match reasons, calls tools, sends reply
6. Daily memory log appended
7. Feeds trimmed, consciousness saved

## Auth
- Match uses OAuth (Claude Pro session limit) via ~/.claude/.credentials.json
- ANTHROPIC_API_KEY in .env is COMMENTED OUT — it's for the looper only
- Looper reads key from ~/looper.env

## Dream (3am America/Chicago)
Fires via cron. Match should NOT schedule it manually. Steps:
1. Read today's daily log → write summary to summaries/
2. Update MEMORY.md (prune to under 4000 chars)
3. Trim feeds to 6, delete subagent/scheduled threads
4. Delete today's daily log
5. Delete summaries older than 14 days
6. consciousness.json reset to empty IN CODE (gateway.py, not by Match manually)

## Making Code Changes
1. Edit file directly: nano ~/cleo/gateway.py
2. Restart service: systemctl --user restart cleo
3. Verify: journalctl --user -u cleo | tail -20
4. Commit: cd ~/cleo && git add -A && git commit -m "description" && git push
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
memory_search, find_files, delegate_task, list_feeds, read_feed, send_reaction,
schedule_reminder, list_reminders, cancel_reminder

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
4. Commit and push: exec_command("cd ~/cleo && git add -A && git commit -m 'description' && git push")
5. Update CODEBASE.md and CHEATSHEET.md if constants or structure changed
6. Confirm to Billy what was changed and pushed
