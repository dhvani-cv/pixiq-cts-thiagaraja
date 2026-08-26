#!/bin/sh
# Full restart of the Sieger services, run as a detached systemd transient
# unit by POST /system/restart (src/api/main.py):
#
#   sudo systemd-run --unit=sieger-restart --collect /usr/local/bin/sieger-restart.sh
#
# The sleep between stop and start outlives the GigE camera control-channel
# lock (GevHeartbeatTimeout = 10 s wedge fix) so a new instance never sees
# "The device is controlled by another application" (0xE1018006), even if
# the old process died without cleanly closing the cameras.
# See docs/restart.txt and docs/material_id_wise_plan.md §8.
#
# Install (as root):
#   install -m 0755 deploy/sieger-restart.sh /usr/local/bin/sieger-restart.sh
#   install -m 0440 deploy/sudoers/sieger-restart /etc/sudoers.d/sieger-restart

set -eu

systemctl stop sieger-inspection.service sieger-api.service
sleep 12
systemctl start sieger-inspection.service sieger-api.service
