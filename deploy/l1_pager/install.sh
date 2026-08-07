#!/bin/bash
# L1 pager install — autonomous-oversight tier 1.
# Run on Lenovo: sudo bash /home/charlie/deploy/l1_pager/install.sh
set -euo pipefail
cd "$(dirname "$0")"

install -m 0644 xrpld-l1-pager.service /etc/systemd/system/
install -m 0644 xrpld-l1-pager.timer   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now xrpld-l1-pager.timer
echo '--- timer status ---'
systemctl list-timers xrpld-l1-pager --no-pager
echo '--- first fire ---'
systemctl start xrpld-l1-pager.service
sleep 4
systemctl status xrpld-l1-pager.service --no-pager | head -20
echo
echo 'Tail log: sudo tail -f /home/charlie/xrpldashboard/logs/l1_pager.log'
