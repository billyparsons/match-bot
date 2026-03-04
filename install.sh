#!/bin/bash
set -e

cd "$(dirname "$0")"

echo "Creating virtual environment..."
python3 -m venv --without-pip venv

echo "Bootstrapping pip..."
curl -sS https://bootstrap.pypa.io/get-pip.py | ./venv/bin/python3

echo "Installing dependencies..."
./venv/bin/pip install -r requirements.txt

if [ ! -f .env ]; then
    cp .env.example .env
    echo ""
    echo "Created .env from template."
fi

echo "Installing systemd user service..."
mkdir -p ~/.config/systemd/user
cp cleo.service ~/.config/systemd/user/
systemctl --user daemon-reload

echo ""
echo "Done. Next steps:"
echo "  1. Edit .env with your Anthropic API key"
echo "  2. systemctl --user enable --now cleo"
echo "  3. journalctl --user -u cleo -f   # to watch logs"
