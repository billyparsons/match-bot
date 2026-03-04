#!/bin/bash
set -e

cd "$(dirname "$0")"
REPO_DIR="$(pwd)"

echo "=== Cleo Setup ==="
echo ""

# 1. Python venv
if [ ! -d venv ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

echo "Installing dependencies..."
./venv/bin/pip install -q -r requirements.txt

# 2. Config files
if [ ! -f config.yaml ]; then
    cp config.yaml.example config.yaml
    echo "Created config.yaml from example — edit it with your values."
else
    echo "config.yaml already exists, skipping."
fi

if [ ! -f .env ]; then
    cp .env.example .env
    echo "Created .env from example — add your API key or use Claude CLI OAuth."
else
    echo ".env already exists, skipping."
fi

# 3. Workspace directories
# Read workspace from config.yaml if possible, else default
WORKSPACE=$(python3 -c "
import yaml, os
try:
    with open('config.yaml') as f:
        ws = yaml.safe_load(f).get('workspace', '~/.cleo/workspace')
    print(os.path.expanduser(ws))
except:
    print(os.path.expanduser('~/.cleo/workspace'))
" 2>/dev/null || echo "$HOME/.cleo/workspace")

echo "Setting up workspace at $WORKSPACE ..."
mkdir -p "$WORKSPACE"/{memory/summaries,private,scripts,vectordb,subagent_souls,notes,skills}

# 4. Identity files
for f in SOUL.md USER.md MEMORY.md; do
    if [ ! -f "$WORKSPACE/$f" ]; then
        cp "${f}.example" "$WORKSPACE/$f"
        echo "Created $WORKSPACE/$f from example — customize it."
    else
        echo "$WORKSPACE/$f already exists, skipping."
    fi
done

# 5. Subagent souls
if [ ! -f "$WORKSPACE/subagent_souls/engineer.md" ]; then
    cat > "$WORKSPACE/subagent_souls/engineer.md" << 'SOUL'
You are an expert software engineer executing a delegated task autonomously.

## Principles

- **Ship working code.** Validate everything before declaring done. Use `exec_command` to run syntax checks, tests, or quick verifications. Never hand back untested work.
- **Read before writing.** Use `read_file` to understand existing code patterns before modifying. Use `grep_search` to find usages and `find_files` to locate files — don't shell out for these.
- **Edit, don't rewrite.** Use `edit_file` for targeted changes instead of rewriting whole files with `write_file`. Only use `write_file` for new files.
- **Debug, don't bail.** If something breaks, fix it. You have tools. Use them in a loop until it works.
- **Keep it simple.** Prefer standard libraries, minimal dependencies, straightforward solutions. Don't over-engineer.
- **Be concise.** Your final summary should state what you did, what files you changed, and any issues. Skip narration.
SOUL
    echo "Created default engineer subagent soul."
fi

# 6. Systemd service (optional)
echo ""
read -p "Install systemd user service? [y/N] " install_service
if [[ "$install_service" =~ ^[Yy] ]]; then
    mkdir -p ~/.config/systemd/user

    cat > ~/.config/systemd/user/cleo.service << EOF
[Unit]
Description=Cleo Signal Gateway
After=network.target signal-cli.service
Requires=signal-cli.service

[Service]
Type=simple
WorkingDirectory=$REPO_DIR
ExecStart=$REPO_DIR/venv/bin/python gateway.py
Restart=on-failure
RestartSec=10
EnvironmentFile=$REPO_DIR/.env

[Install]
WantedBy=default.target
EOF

    systemctl --user daemon-reload
    echo "Installed cleo.service."
fi

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit config.yaml — set bot_number, authorized_senders, workspace path"
echo "  2. Edit .env — set your Anthropic API key (or use Claude CLI: claude login)"
echo "  3. Edit $WORKSPACE/SOUL.md — customize personality"
echo "  4. Edit $WORKSPACE/USER.md — tell Cleo about yourself"
echo "  5. Edit $WORKSPACE/MEMORY.md — add long-term context"
echo "  6. Ensure signal-cli is running (see README.md)"
echo "  7. Start: systemctl --user enable --now cleo"
echo "     Or run directly: ./venv/bin/python gateway.py"
