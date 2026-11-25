#!/bin/bash
# Copy ~/yolo_model/cira_tools_yolo11n into the CiRA container again (overwrites, keeps owner = cira).
#   sudo bash ~/kitcheck_v2/cira-sync-model.sh
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
SRC=/home/sear/yolo_model/cira_tools_yolo11n
docker exec ciracore mkdir -p /home/cira/Models
docker cp "$SRC/." ciracore:/home/cira/Models/cira_tools_yolo11n/
CIRA_UID=$(docker exec ciracore id -u cira 2>/dev/null || echo 1000)
# the container runs with --cap-drop ALL, so root inside cannot chown; docker cp already keeps
# the source owner (sear = uid 1000 = cira), so this is only a best effort
docker exec -u 0 ciracore chown -R "$CIRA_UID:$CIRA_UID" /home/cira/Models 2>/dev/null || true
docker exec ciracore ls -la /home/cira/Models/cira_tools_yolo11n
