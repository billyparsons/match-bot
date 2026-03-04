# cleo

A personal AI assistant that lives in Signal. Cleo connects Signal messages to the Anthropic Claude API with persistent memory, semantic search, tool use, and background task delegation.

## Architecture

```
Signal app (iOS/Android/Desktop)
    |  Signal protocol
signal-cli daemon (JSON-RPC over TCP)
    |
cleo (this project)
    |
Anthropic Claude API (claude-opus-4-6 / claude-sonnet-4-6)
```

`gateway.py` maintains a persistent TCP connection to signal-cli, checks incoming messages against an authorization whitelist, builds a context-enriched system prompt from memory files, and runs an agentic tool-use loop: call Claude, execute tools, loop until done, send reply.

## Features

- **Signal integration** -- DMs and group chats, with per-conversation history
- **Tool use** -- shell commands, file I/O (read/edit/write), code search (grep/glob), web search, web fetch, memory search
- **Background delegation** -- the main agent can spawn subagents for heavy tasks without blocking the conversation
- **Persistent memory** -- daily logs, project notes, curated long-term knowledge
- **Semantic retrieval** -- ChromaDB vector search surfaces relevant memories per message
- **Scheduled heartbeats** -- cron-based tasks (morning briefing, memory consolidation, etc.)
- **History compaction** -- old tool results are truncated to keep context lean

## Security

- **Sender/group allowlist** -- only authorized senders and group members are processed
- **OAuth authentication** -- uses Claude Max subscription token from `~/.claude/.credentials.json`
- **File path restrictions** -- read/write/edit tools only allow configured prefixes, with symlink traversal blocked
- **Command audit logging** -- every shell command is logged at WARNING level
- **No npm/Node.js** -- pure Python with minimal, vetted dependencies

## Prerequisites

- Python 3.11+
- [signal-cli](https://github.com/AsamK/signal-cli) running as a daemon with JSON-RPC over TCP + HTTP
- Claude Max subscription (OAuth token) or Anthropic API key

### signal-cli setup

```bash
# Register a phone number (one-time)
signal-cli -a +1XXXXXXXXXX register --voice
signal-cli -a +1XXXXXXXXXX verify CODE

# Run as daemon with both HTTP and TCP endpoints
signal-cli -a +1XXXXXXXXXX daemon \
  --http 127.0.0.1:8080 \
  --tcp 127.0.0.1:7583 \
  --no-receive-stdout
```

Cleo connects to TCP port 7583 for incoming messages and queries HTTP port 8080 for contacts/groups/sending.

## Setup

```bash
git clone <repo-url> ~/code/cleo
cd ~/code/cleo
./setup.sh
```

`setup.sh` will:
1. Create a Python venv and install dependencies
2. Copy `config.yaml.example` to `config.yaml`
3. Copy `.env.example` to `.env`
4. Create workspace directories
5. Copy identity file templates (SOUL.md, USER.md, MEMORY.md) to workspace
6. Optionally install a systemd user service

Then configure:
```bash
vi config.yaml   # set bot number, authorized senders/groups
vi .env           # set API key (or use: claude login)

# Customize the bot's personality and knowledge:
vi ~/.cleo/workspace/SOUL.md
vi ~/.cleo/workspace/USER.md
vi ~/.cleo/workspace/MEMORY.md

# Start
systemctl --user enable --now cleo

# Monitor
journalctl --user -u cleo -f
```

## Configuration

### config.yaml

```yaml
bot_number: '+1XXXXXXXXXX'
workspace: '~/.cleo/workspace'
model: 'claude-sonnet-4-5'
timezone: 'America/New_York'
allowed_paths: []               # extra path prefixes for file tools

authorized_senders:
  - '+1XXXXXXXXXX'

authorized_groups: []           # signal group IDs

heartbeats:                     # scheduled cron tasks (optional)
  morning-briefing:
    cron: "0 7 * * 1-5"
    prompt: |
      Good morning. Check the weather, review TODOs, summarize yesterday.
```

### .env

```bash
# Auth -- choose one, or leave both commented to auto-read from ~/.claude/.credentials.json
# ANTHROPIC_AUTH_TOKEN=sk-ant-oat01-...   # Claude Code OAuth token
# ANTHROPIC_API_KEY=sk-ant-api03-...      # Standard API key

# Optional overrides
# SIGNAL_CLI_URL=http://127.0.0.1:8080
# SIGNAL_CLI_TCP=127.0.0.1:7583
```

### Workspace

The workspace directory holds Cleo's memory and identity:

```
workspace/
  SOUL.md              # Personality and behavioral guidelines
  USER.md              # Information about the user
  MEMORY.md            # Curated long-term knowledge
  memory/              # Daily logs (YYYY-MM-DD.md) and project notes
    summaries/         # Consolidated memory summaries
  vectordb/            # ChromaDB semantic index (auto-generated)
  private/             # Credentials for optional integrations (gitignored)
  subagent_souls/      # Custom personalities for delegated tasks
  scripts/             # Helper scripts
```

### Optional integrations

These tools work out of the box if configured, and fail gracefully with a helpful message if not:

| Integration | Config file | Tools |
|-------------|-------------|-------|
| Emby | `private/emby.json` | `emby_admin` |
| Audiobookshelf | `private/audiobookshelf.json` | `audiobookshelf_admin` |
| IPTorrents + Transmission | `private/trackers.json` | `search_torrents`, `download_torrent`, `list_torrents` |
| Twilio SMS | `private/twilio.json` | `check_sms` |
| Gemini (image gen) | `private/gemini.json` | `generate_image` |
| Inworld (TTS) | `private/inworld.json` | `generate_voice` |
| OpenAI (Whisper STT) | `private/openai.json` | Voice transcription |

## Files

| File | Purpose |
|------|---------|
| `gateway.py` | Async main loop: TCP listener, agentic tool loop, subagent delegation |
| `tools.py` | Tool implementations and definitions for the Claude API |
| `memory.py` | System prompt assembly: base identity + semantic retrieval |
| `vectorstore.py` | ChromaDB wrapper: indexing, chunking, semantic search |
| `config.py` | Config loader + OAuth credential management |
| `auth.py` | Sender/group authorization with cached group membership |
| `identity.py` | Signal contact/group name resolution |
| `scheduler.py` | APScheduler: heartbeat checks and maintenance jobs |
| `voice.py` | Whisper STT + Inworld TTS |

## Tools

| Tool | Description |
|------|-------------|
| `exec_command` | Run shell commands (audit logged) |
| `read_file` | Read files with line numbers |
| `edit_file` | Surgical string replacement in files |
| `write_file` | Create or overwrite files |
| `grep_search` | Regex search across files with context |
| `find_files` | Find files by glob pattern |
| `web_search` | DuckDuckGo search |
| `web_fetch` | Fetch URL, strip HTML |
| `send_message` | Send Signal message with Markdown formatting |
| `send_reaction` | React to messages with emoji |
| `send_poll` / `vote_poll` | Create and vote on Signal polls |
| `memory_search` | Semantic or exact search over workspace memories |
| `read_daily_log` | Read raw daily interaction logs |
| `delegate_task` | Spawn a background subagent for complex tasks |
| `generate_image` | Generate images via Gemini (optional) |
| `generate_voice` | Text-to-speech via Inworld (optional) |
| `check_weather` | Weather lookup by location |
| `check_sms` | Read SMS via Twilio (optional) |
| `emby_admin` | Emby media server admin (optional) |
| `audiobookshelf_admin` | Audiobook server admin (optional) |
| `search_torrents` / `download_torrent` / `list_torrents` | Torrent management (optional) |
| `schedule_reminder` | Schedule one-time reminders |
| `check_quota` | Check API rate limits |

## Monitoring

```bash
alias cleo-status='systemctl --user status cleo'
alias cleo-logs='journalctl --user -u cleo -n 50'
alias cleo-follow='journalctl --user -u cleo -f'
alias cleo-restart='systemctl --user restart cleo'
```
