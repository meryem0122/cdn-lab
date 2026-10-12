#!/usr/bin/env bash
# Teste la phase active (config/phase) sur le labo déployé.
# Usage : ./test.sh
set -u
cd "$(dirname "$0")"

LAB=clab-cdn
E1=10.0.1.2   # edge1, vu du client
E2=10.0.2.2   # edge2, vu du client
PHASE=$(tr -d ' \n' < config/phase)
PASS=0
FAIL=0

client() { docker exec "${LAB}-client" "$@"; }

# Une requête, en-têtes affichés (wget -S écrit les en-têtes sur stderr).
fetch() { client wget -S -q -O /dev/null "$1" 2>&1 | tr -d '\r'; }

# check "description" url statut_attendu [X-Cache attendu] [X-Source contient]
check() {
  local desc=$1 url=$2 want_status=$3 want_cache=${4:-} want_src=${5:-}
  local out status cache src ok=1
  out=$(fetch "$url")
  status=$(printf '%s\n' "$out" | awk '/^ *HTTP\//{s=$2} END{print s}')
  cache=$(printf '%s\n' "$out" | awk 'tolower($1)=="x-cache:"{v=$2} END{print v}')
  src=$(printf '%s\n' "$out" | awk 'tolower($1)=="x-source:"{v=$2} END{print v}')

  [ "$status" = "$want_status" ] || ok=0
  [ -z "$want_cache" ] || [ "$cache" = "$want_cache" ] || ok=0
  [ -z "$want_src" ] || [[ "$src" == *"$want_src"* ]] || ok=0

  if [ "$ok" = 1 ]; then
    PASS=$((PASS + 1)); printf '  \033[32m✔\033[0m %-48s [%s %s %s]\n' "$desc" "$status" "$cache" "$src"
  else
    FAIL=$((FAIL + 1)); printf '  \033[31m✘\033[0m %-48s attendu [%s %s %s], reçu [%s %s %s]\n' \
      "$desc" "$want_status" "$want_cache" "$want_src" "${status:-aucune réponse}" "$cache" "$src"
  fi
}

# Nombre de requêtes GET <chemin> reçues par l'origin (preuve que le cache évite l'origin).
origin_hits() { docker logs "${LAB}-origin" 2>&1 | grep -c "GET $1 " || true; }

expect_count() { # description attendu obtenu
  if [ "$2" = "$3" ]; then
    PASS=$((PASS + 1)); printf '  \033[32m✔\033[0m %-48s [%s]\n' "$1" "$3"
  else
    FAIL=$((FAIL + 1)); printf '  \033[31m✘\033[0m %-48s attendu %s, reçu %s\n' "$1" "$2" "$3"
  fi
}

# --- attendre que les serveurs répondent
ready=0
for _ in $(seq 1 20); do
  if client wget -q -O /dev/null "http://$E1/hello.txt" 2>/dev/null &&
     client wget -q -O /dev/null "http://$E2/hello.txt" 2>/dev/null; then ready=1; break; fi
  sleep 1
done
if [ "$ready" != 1 ]; then
  echo "Les serveurs ne répondent pas (phase $PHASE). Regarde :"
  echo "  docker logs ${LAB}-edge1"
  echo "  docker logs ${LAB}-edge2"
  exit 1
fi

# --- repartir d'un cache vide
for n in edge1 edge2; do
  docker exec "${LAB}-$n" sh -c 'find /app/cache -mindepth 1 ! -name .gitkeep -delete'
done

echo "Test de la phase $PHASE"
case "$PHASE" in
  0)
    check "fichier local -> 200"                  "http://$E1/hello.txt"          200 LOCAL local
    check "fichier absent -> 404"                 "http://$E1/cat.jpg"            404
    check "fichier inconnu -> 404"                "http://$E1/nope.txt"           404
    check "racine / -> 404 (pas de listing)"      "http://$E1/"                   404
    check "path traversal bloqué -> 404"          "http://$E1/../../etc/passwd"   404
    ;;
  1)
    check "fichier local -> 200"                  "http://$E1/hello.txt"          200 LOCAL local
    check "fichier absent -> image par défaut"    "http://$E1/nope.txt"           200 FALLBACK fallback
    check "racine / -> image par défaut"          "http://$E1/"                   200 FALLBACK fallback
    ;;
  2)
    base=$(origin_hits /cat.jpg)
    check "fichier local -> 200"                  "http://$E1/hello.txt"          200 LOCAL local
    check "1re demande : cache miss -> origin"    "http://$E1/cat.jpg"            200 MISS origin
    check "2e demande : cache hit"                "http://$E1/cat.jpg"            200 HIT cache
    expect_count "l'origin n'a vu cat.jpg qu'une fois" "$((base + 1))" "$(origin_hits /cat.jpg)"
    check "absent partout -> image par défaut"    "http://$E1/nope.txt"           200 FALLBACK fallback
    check "sous-dossier mis en cache"             "http://$E1/video/intro.txt"    200 MISS origin
    check "sous-dossier : hit"                    "http://$E1/video/intro.txt"    200 HIT cache
    ;;
  3)
    base=$(origin_hits /cat.jpg)
    check "edge2 : miss, personne -> origin"      "http://$E2/cat.jpg"            200 MISS origin
    check "edge1 : miss, edge2 l'a -> voisin"     "http://$E1/cat.jpg"            200 MISS peer
    check "edge1 : hit (copie locale)"            "http://$E1/cat.jpg"            200 HIT cache
    expect_count "l'origin n'a vu cat.jpg qu'une fois" "$((base + 1))" "$(origin_hits /cat.jpg)"
    check "route /video/ -> origin imposée"       "http://$E1/video/intro.txt"    200 MISS origin
    check "edge2 : edge1 a la vidéo -> voisin"    "http://$E2/video/intro.txt"    200 MISS peer
    check "absent partout -> image par défaut"    "http://$E1/nope.txt"           200 FALLBACK fallback
    check "fichier local edge2 -> 200"            "http://$E2/hello.txt"          200 LOCAL local
    ;;
  *)
    echo "Phase inconnue : $PHASE"; exit 1 ;;
esac

echo
echo "Résultat : $PASS réussi(s), $FAIL échec(s)"
[ "$FAIL" = 0 ]
