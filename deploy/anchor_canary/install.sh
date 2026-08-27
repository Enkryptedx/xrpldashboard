#!/bin/bash
# Anchor canary install — L1.5 tier (the strongest forgery detection).
# Reads the on-ledger anchor memo via the Lenovo's own rippled (sovereign),
# compares against the live chain.json fetched via Render origin, alerts on
# divergence. This is the one check that works even if the site itself is
# compromised, because the attacker can't rewrite the ledger.
#
# Run on Lenovo: sudo bash /home/charlie/xrpldashboard/deploy/anchor_canary/install.sh
#
# Prereqs (add to /home/charlie/.config/xrpldashboard/env, chmod 0600):
#   ANCHOR_CANARY_TELEGRAM_BOT_TOKEN=<bot token>    (reuses L1's bot is fine)
#   ANCHOR_CANARY_TELEGRAM_CHAT_ID=<Charlie's chat id>
#   XRPL_LOCAL_NODE=http://localhost:5005           (usually already set)
#   ANCHOR_CANARY_SITE_URL=https://xrpldashboard.onrender.com   (optional; default)
set -euo pipefail
cd "$(dirname "$0")"

install -m 0644 xrpld-anchor-canary.service /etc/systemd/system/
install -m 0644 xrpld-anchor-canary.timer   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now xrpld-anchor-canary.timer
echo '--- timer status ---'
systemctl list-timers xrpld-anchor-canary --no-pager
echo '--- first fire ---'
systemctl start xrpld-anchor-canary.service
sleep 4
systemctl status xrpld-anchor-canary.service --no-pager | head -20
echo
echo 'Tail log:  sudo tail -f /home/charlie/xrpldashboard/logs/anchor_canary.log'
echo 'Dry-run:   /home/charlie/xrpldashboard/.venv/bin/python /home/charlie/xrpldashboard/tools/anchor_canary.py --dry-run'
echo 'Test-msg:  /home/charlie/xrpldashboard/.venv/bin/python /home/charlie/xrpldashboard/tools/anchor_canary.py --test-message'
