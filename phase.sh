#!/usr/bin/env bash
# Change de phase SANS redéployer : le serveur relit config/phase à chaque requête.
# Usage : ./phase.sh 0|1|2|3
set -eu
cd "$(dirname "$0")"

case "${1:-}" in
  0) desc="serveur HTTP local : 200 ou 404" ;;
  1) desc="fichier absent -> image par défaut" ;;
  2) desc="cache : miss -> origin -> mise en cache, puis hit" ;;
  3) desc="plusieurs edges : cache, puis voisins, puis origin" ;;
  *) echo "Usage : ./phase.sh 0|1|2|3" >&2; exit 1 ;;
esac

printf '%s\n' "$1" > config/phase

# On repart d'un cache vide pour que les démos soient reproductibles.
for n in edge1 edge2; do
  if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "clab-cdn-$n"; then
    docker exec "clab-cdn-$n" sh -c 'find /app/cache -mindepth 1 ! -name .gitkeep -delete'
  fi
done

echo "Phase $1 active ($desc). Caches vidés."
echo "Test automatique : ./test.sh"
