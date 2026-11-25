#!/bin/bash
# Move CiRA CORE project files (.npj / .flow) between the container and sear's home.
#   sudo bash ~/kitcheck_v2/cira-projects.sh export   # container /home/cira/Projects -> ~/cira_projects (readable by sear)
#   sudo bash ~/kitcheck_v2/cira-projects.sh import   # ~/cira_projects/import/*.npj -> container /home/cira/Projects
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
OUT=/home/sear/cira_projects
case "${1:-}" in
export)
  rm -rf "$OUT/Projects"; mkdir -p "$OUT"
  docker cp ciracore:/home/cira/Projects "$OUT/Projects"
  docker exec ciracore sh -c 'ls -la /home/cira/*.npj /home/cira/Desktop/*.npj 2>/dev/null' || true
  for f in $(docker exec ciracore sh -c 'ls /home/cira/*.npj /home/cira/Desktop/*.npj /home/cira/Models/*.npj 2>/dev/null' || true); do
    docker cp "ciracore:$f" "$OUT/Projects/" || true
  done
  chown -R sear:sear "$OUT"
  find "$OUT/Projects" -maxdepth 2 -type f | head -n 50
  ;;
import)
  ls "$OUT"/import/*.npj >/dev/null
  for f in "$OUT"/import/*.npj; do docker cp "$f" ciracore:/home/cira/Projects/; echo "imported $(basename "$f")"; done
  ;;
*) echo "usage: sudo bash $0 export|import"; exit 1 ;;
esac
