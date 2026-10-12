# Mini CDN sur Containerlab 

Un CDN rapproche les contenus des utilisateurs : des serveurs **edge** gardent des copies en **cache**, et l'**origin** (qui possède les fichiers originaux) n'est contacté que si nécessaire. Containerlab fournit seulement le réseau et les conteneurs. **Toute l'intelligence du CDN est dans `app/server.py`.**

## Architecture

```
            10.0.1.0/24                10.0.11.0/24
  client ───────────────── edge1 ───────────────────┐
     │                       │ 10.0.100.0/24         origin
     │                       │                       │
     └───────────────────── edge2 ───────────────────┘
            10.0.2.0/24                10.0.12.0/24
```

| Nœud | IP(s) | Rôle |
|---|---|---|
| `client` | 10.0.1.1, 10.0.2.1 | envoie les requêtes (`wget`) |
| `edge1` | 10.0.1.2 (clients), 10.0.11.2 (origin), 10.0.100.1 (edge2) | serveur CDN |
| `edge2` | 10.0.2.2 (clients), 10.0.12.2 (origin), 10.0.100.2 (edge1) | serveur CDN |
| `origin` | 10.0.11.1, 10.0.12.1 | tous les fichiers originaux |

Le client n'a **aucun lien** vers l'origin : il passe toujours par un edge. Chaque edge a déjà une interface côté clients et des interfaces internes séparées, ce qui prépare la phase 4.

## Pourquoi cette organisation

- **Un seul `server.py`** pour les trois serveurs : le rôle vient de `config/nodes.json`. Chaque phase ajoute une fonction au même programme, comme dans un vrai edge server.
- **La phase est dans `config/phase`**, relue à chaque requête. Pour passer de la phase 1 à la phase 2, on change un chiffre, sans redéployer.
- **`edgeX/files/`** = contenu propre au nœud (dont `default.png`). **`edgeX/cache/`** = copies récupérées ailleurs. Séparer les deux permet de vider le cache sans toucher au contenu.
- **En-têtes `X-Cache` et `X-Source`** : chaque réponse dit d'où elle vient (`LOCAL`, `HIT`, `MISS`, `FALLBACK` / `local`, `cache`, `origin`, `peer:...`). Ça rend le comportement visible en démo.

## Lancer

```bash
cd cdn-lab
chmod +x phase.sh test.sh

# 1. déployer
sudo containerlab deploy -t topo.clab.yml

# 2. choisir une phase (0, 1, 2 ou 3) et tester
./phase.sh 0 && ./test.sh
./phase.sh 1 && ./test.sh
./phase.sh 2 && ./test.sh
./phase.sh 3 && ./test.sh

# 3. voir ce que fait chaque serveur
docker logs clab-cdn-edge1
docker logs clab-cdn-edge2
docker logs clab-cdn-origin

# 4. détruire
sudo containerlab destroy -t topo.clab.yml --cleanup
```

Pour une démo à la main (les en-têtes s'affichent avec `-S`) :

```bash
docker exec clab-cdn-client wget -S -O /dev/null http://10.0.1.2/cat.jpg
docker exec clab-cdn-client wget -S -O /dev/null http://10.0.2.2/cat.jpg
```

## Les phases

### Phase 0 : serveur HTTP local
`GET /hello.txt` : le serveur cherche `files/hello.txt`. Trouvé : `200 OK` + contenu + `Content-Type` déduit de l'extension. Absent : `404`. Le serveur vérifie aussi que le chemin résolu reste dans `files/` (sinon `GET /../../etc/passwd` sortirait du dossier) et refuse les listings de dossier.
*Concepts : HTTP GET, 200/404, lecture de fichiers, path traversal.*

### Phase 1 : image par défaut
Fichier absent : le serveur renvoie `files/default.png` (`X-Cache: FALLBACK`). Le client reçoit toujours une image. Ce n'est pas encore un CDN, seulement une gestion d'erreur plus agréable.

### Phase 2 : cache
Ordre : fichiers locaux, puis `cache/`, puis origin.
- **Cache miss** : absent du cache, l'edge le télécharge depuis l'origin, l'écrit dans `cache/` (écriture atomique : fichier temporaire puis `rename`), puis répond.
- **Cache hit** : présent, l'edge répond sans contacter l'origin. `test.sh` le prouve en comptant les requêtes reçues par l'origin.
- Absent aussi de l'origin : image par défaut. Origin injoignable : `502 Bad Gateway`.

### Phase 3 : plusieurs edges
Ordre : fichiers locaux, cache, puis **stratégie** pour choisir la source.
- **Stratégie par défaut** : demander aux voisins (edges), puis à l'origin. Un voisin qui a le fichier évite un aller-retour jusqu'à l'origin.
- **Table de routes** dans `config/nodes.json` : sur `edge1`, `"/video/": "origin"` impose l'origin pour les vidéos (ex. : gros fichiers qu'on ne veut pas dupliquer entre edges). Valeurs possibles : `origin`, `peers`, ou rien pour le défaut.
- **Pas de boucle** : quand un edge interroge son voisin, il envoie `X-CDN-Internal: 1`. Le voisin ne répond alors qu'avec ses fichiers et son cache, sans interroger personne d'autre et sans image par défaut. Sans cette règle, `edge1` et `edge2` pourraient se renvoyer la même demande indéfiniment.
- Le voisin est interrogé même s'il n'a pas le fichier : c'est un aller-retour de plus quand le fichier est nulle part (compromis simple, à citer à l'oral).

## Réponse type à l'oral

« Quand un client demande `/cat.jpg` à edge1, l'edge cherche d'abord dans ses fichiers locaux, puis dans son cache. En cas de miss, il applique sa stratégie : il demande à edge2 (requête interne, sans récursion), puis à l'origin. Il enregistre la réponse dans son cache avant de répondre. La requête suivante est un hit et ne génère aucun trafic interne. »

## Dépannage

| Symptôme | Cause probable | Action |
|---|---|---|
| `Connection refused` | le serveur n'a pas démarré ou a planté | `docker logs clab-cdn-edge1` |
| `No such container` | lab non déployé ou autre nom de lab | `docker ps`, le préfixe doit être `clab-cdn-` |
| `Failed to lookup link "br-..."` | Docker Desktop au lieu de Docker Engine dans WSL | installer Docker Engine dans Ubuntu |
| Les fichiers de `edgeX/cache/` appartiennent à root | normal (écrits depuis le conteneur) | `./phase.sh N` les vide |

Note : tous les nœuds ont aussi une interface de management Docker (`eth0`, 172.20.20.0/24) créée par Containerlab. Elle sert à Docker, pas au CDN : les tests utilisent uniquement les IP du plan ci-dessus.

## Phase 4 (suite)

Chaque edge a déjà une interface côté clients (`eth1`) et des interfaces internes (`eth2`, `eth3`). Pour la phase 4, il restera à lier l'écoute publique à l'IP de `eth1` seulement, à réserver les interfaces internes aux échanges CDN, puis à travailler l'IP publique commune (Anycast).
