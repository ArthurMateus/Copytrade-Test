#!/usr/bin/env bash
# One-time setup of a fresh Ubuntu/Debian server for the bot (docs/PRODUCTION_PLAN.md Phase 2). Run as root:
#   bash setup_server.sh
# Then put the secrets in /etc/copybot.env (see deploy/copybot.env.example), copy data/ from the old PC, and
#   systemctl start copybot
# NEVER run the bot on two machines at once: stop the Windows task first (tools/install_autostart.ps1 -Remove).
set -euo pipefail

REPO=https://github.com/ArthurMateus/Copytrade-Test.git
BRANCH=${BRANCH:-live/phase1}

apt-get update -y
apt-get install -y git curl ca-certificates
id copybot >/dev/null 2>&1 || useradd --create-home --shell /bin/bash copybot
if [ ! -d /opt/copybot/.git ]; then
    git clone --branch "$BRANCH" "$REPO" /opt/copybot
fi
chown -R copybot:copybot /opt/copybot
sudo -u copybot bash -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
sudo -u copybot bash -c 'cd /opt/copybot && ~/.local/bin/uv sync'
if [ ! -f /etc/copybot.env ]; then
    install -m 600 -o root -g root /opt/copybot/deploy/copybot.env.example /etc/copybot.env
fi
install -m 644 /opt/copybot/deploy/copybot.service /etc/systemd/system/copybot.service
systemctl daemon-reload
systemctl enable copybot
echo
echo "Done. Next:"
echo "  1. nano /etc/copybot.env        (fill in the secrets)"
echo "  2. copy the old PC's data/ folder to /opt/copybot/data and run: chown -R copybot:copybot /opt/copybot/data"
echo "  3. systemctl start copybot       (logs: journalctl -u copybot -f, and /opt/copybot/logs/copybot.log)"
echo "Updates later:  cd /opt/copybot && sudo -u copybot git pull && systemctl restart copybot"
