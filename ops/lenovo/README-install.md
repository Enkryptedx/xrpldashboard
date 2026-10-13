# Lenovo rippled units — PREPARED, NOT INSTALLED

Nothing here is deployed. Four files, all local to this repo.

## Pre-flight (answers the one open question in the timer)
    systemctl --version | head -1      # need >= 240 for the timezone suffix

If < 240, edit rippled-weekly-restart.timer to:
    OnCalendar=Sun 07:00:00 UTC
...and accept that it becomes 02:00 ET once EST begins 2026-11-01.

## Install (run as root ON THE LENOVO)
    sudo install -m 0644 rippled-rss-log.service        /etc/systemd/system/
    sudo install -m 0644 rippled-rss-log.timer          /etc/systemd/system/
    sudo install -m 0644 rippled-weekly-restart.service /etc/systemd/system/
    sudo install -m 0644 rippled-weekly-restart.timer   /etc/systemd/system/
    sudo systemctl daemon-reload

## Dry-check BEFORE enabling anything
    systemd-analyze verify /etc/systemd/system/rippled-rss-log.service
    systemd-analyze verify /etc/systemd/system/rippled-weekly-restart.service
    systemd-analyze calendar 'Sun 03:00:00 America/New_York'   # confirm next 5 fires
    sudo systemctl start rippled-rss-log.service && tail -3 /var/log/rippled-rss.log

## Enable the LOGGER first, restart timer later
    sudo systemctl enable --now rippled-rss-log.timer
    # let it run >= 1 week, then decide leak vs cache from the trend
    sudo systemctl enable --now rippled-weekly-restart.timer   # only when ready

## Rollback
    sudo systemctl disable --now rippled-weekly-restart.timer rippled-rss-log.timer
    sudo rm /etc/systemd/system/rippled-{rss-log,weekly-restart}.{service,timer}
    sudo systemctl daemon-reload
