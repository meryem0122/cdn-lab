#!/usr/bin/env python3
"""
Mini CDN - un seul programme, trois rôles : origin, edge1, edge2.

Le comportement dépend de deux choses lues À CHAQUE REQUÊTE :
  - config/phase       : numéro de phase (0 à 3) -> changer de phase sans redéployer
  - config/nodes.json  : rôle du nœud (origin/edge), voisins, origin, table de routes

Phase 0 : fichiers locaux uniquement, sinon 404
Phase 1 : fichier absent -> image par défaut (default.png)
Phase 2 : fichier absent -> cache, sinon téléchargement depuis l'origin + mise en cache
Phase 3 : fichier absent -> cache, sinon stratégie : voisins (edges) puis origin

En-têtes de réponse utiles pour les tests et l'oral :
  X-Cache  : LOCAL | HIT | MISS | FALLBACK
  X-Source : local | cache | origin | peer:<url> | fallback | none
"""
import json
import mimetypes
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NODE = os.environ.get("NODE", "edge1")
ROOT = os.path.realpath(os.environ.get("ROOT", "/app"))
CONFIG_DIR = os.environ.get("CONFIG_DIR", os.path.join(ROOT, "config"))
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "80"))
FETCH_TIMEOUT = float(os.environ.get("FETCH_TIMEOUT", "3"))

FILES_DIR = os.path.realpath(os.path.join(ROOT, "files"))  # contenu propre au nœud
CACHE_DIR = os.path.realpath(os.path.join(ROOT, "cache"))  # copies récupérées ailleurs
DEFAULT_IMAGE = "default.png"


# ---------------------------------------------------------------- configuration
def read_phase():
    try:
        with open(os.path.join(CONFIG_DIR, "phase")) as f:
            return max(0, min(3, int(f.read().strip())))
    except (OSError, ValueError):
        return 0


def read_node_config():
    try:
        with open(os.path.join(CONFIG_DIR, "nodes.json")) as f:
            return json.load(f).get(NODE, {})
    except (OSError, ValueError):
        return {}


# ------------------------------------------------------------------- chemins
def clean_rel(raw_path):
    """'/images/cat.jpg?x=1' -> 'images/cat.jpg'"""
    path = raw_path.split("?", 1)[0].split("#", 1)[0]
    return urllib.parse.unquote(path).lstrip("/")


def resolve(base, rel):
    """Chemin absolu de rel DANS base, ou None s'il en sort (path traversal)."""
    if not rel or "\x00" in rel:
        return None
    full = os.path.realpath(os.path.join(base, rel))
    return full if full.startswith(base + os.sep) else None


def existing_file(base, rel):
    full = resolve(base, rel)
    return full if full and os.path.isfile(full) else None


def guess_type(name):
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def save_cache(rel, body):
    dest = resolve(CACHE_DIR, rel)
    if not dest:
        return False
    tmp = dest + "." + uuid.uuid4().hex + ".tmp"
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(tmp, "wb") as f:
            f.write(body)
        os.replace(tmp, dest)  # écriture atomique : jamais de fichier à moitié écrit
        return True
    except OSError:
        return False


# ------------------------------------------------------------------ réseau
def http_get(base_url, rel, internal):
    """Retourne (status, body). status = None si la source est injoignable."""
    url = base_url.rstrip("/") + "/" + urllib.parse.quote(rel)
    headers = {"X-CDN-Internal": "1"} if internal else {}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, b""
    except Exception:
        return None, b""


def candidate_sources(cfg, rel, phase):
    """Phase 2 : origin seulement. Phase 3 : stratégie (table de routes, sinon voisins puis origin)."""
    origin = cfg.get("origin")
    origin_src = [("origin", origin)] if origin else []
    if phase < 3:
        return origin_src

    peer_src = [("peer:" + p, p) for p in cfg.get("peers", [])]
    routes = cfg.get("routes", {})
    path = "/" + rel
    match = max((p for p in routes if path.startswith(p)), key=len, default=None)
    rule = routes[match] if match else "peers_then_origin"
    if rule == "origin":
        return origin_src
    if rule == "peers":
        return peer_src
    return peer_src + origin_src


# ----------------------------------------------------------------- serveur
class Handler(BaseHTTPRequestHandler):
    server_version = "MiniCDN/1.0"

    def log_message(self, *args):  # on journalise nous-mêmes (voir send_bytes)
        pass

    # --- réponses
    def send_bytes(self, status, body, ctype, cache, source):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Cache", cache)
        self.send_header("X-Source", source)
        self.send_header("X-Served-By", NODE)
        self.end_headers()
        try:
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
        print(f"[{NODE}] phase={read_phase()} {self.client_address[0]} "
              f"{self.command} {self.path} -> {status} {cache} {source}", flush=True)

    def send_file(self, path, cache, source):
        with open(path, "rb") as f:
            body = f.read()
        self.send_bytes(200, body, guess_type(path), cache, source)

    def send_text(self, status, text, cache="MISS", source="none"):
        self.send_bytes(status, text.encode(), "text/plain; charset=utf-8", cache, source)

    def serve_fallback(self):
        default = existing_file(FILES_DIR, DEFAULT_IMAGE)
        if default:
            return self.send_file(default, "FALLBACK", "fallback")
        return self.send_text(404, "404 Not Found\n")

    # --- logique principale
    def do_GET(self):
        phase = read_phase()
        cfg = read_node_config()
        role = cfg.get("role", "edge")
        rel = clean_rel(self.path)
        internal = self.headers.get("X-CDN-Internal") == "1"

        # Chemin qui sort du dossier (../) : refusé tout de suite, jamais relayé à l'origin
        if rel and resolve(FILES_DIR, rel) is None:
            return self.send_text(404, "404 Not Found\n")

        # 1. contenu propre au nœud
        local = existing_file(FILES_DIR, rel)
        if local:
            return self.send_file(local, "LOCAL", "local")

        # Phase 0 et origin : rien d'autre à essayer
        if role == "origin" or phase == 0:
            return self.send_text(404, "404 Not Found\n")

        # 2. cache (phases 2 et 3)
        if phase >= 2:
            cached = existing_file(CACHE_DIR, rel)
            if cached:
                return self.send_file(cached, "HIT", "cache")

        # Un voisin qui nous interroge n'a droit qu'à nos fichiers et notre cache :
        # pas de récursion, pas de boucle entre edges, pas d'image par défaut.
        if internal:
            return self.send_text(404, "404 Not Found\n")

        # 3. phase 1 : image par défaut
        if phase == 1 or not rel:
            return self.serve_fallback()

        # 4. phases 2 et 3 : aller chercher ailleurs
        origin_failed = False
        for label, base_url in candidate_sources(cfg, rel, phase):
            status, body = http_get(base_url, rel, internal=label.startswith("peer:"))
            if status == 200:
                save_cache(rel, body)
                return self.send_bytes(200, body, guess_type(rel), "MISS", label)
            if status is None and label == "origin":
                origin_failed = True

        if origin_failed:
            return self.send_text(502, "502 Bad Gateway: origin unreachable\n")
        return self.serve_fallback()

    do_HEAD = do_GET

    def method_not_allowed(self):
        body = b"405 Method Not Allowed\n"
        self.send_response(405)
        self.send_header("Allow", "GET, HEAD")
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_POST = do_PUT = do_DELETE = do_PATCH = method_not_allowed


if __name__ == "__main__":
    print(f"[{NODE}] MiniCDN on {HOST}:{PORT} root={ROOT} phase={read_phase()}", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
