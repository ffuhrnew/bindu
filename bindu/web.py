"""Eingebauter Webserver (nur Standardbibliothek): UI + kleine JSON-API.

Schutz: Standard-Bindung 127.0.0.1, Quell-IP-Prüfung (nur Tailnet/Loopback, siehe access.py),
Host-Header-Prüfung gegen DNS-Rebinding (auch bei anderer Bindung: nur IP-Adressen, localhost und mit --allow-host
ausdrücklich erlaubte Namen), POST nur mit Content-Type application/json (verhindert Cross-Site-Formposts),
Content-Security-Policy (Skripte nur der Seite selbst, per Hash) und frame-ancestors 'none'.

Mit --tls-cert/--tls-key spricht der Server HTTPS.
"""

import base64
import hashlib
import ipaddress
import json
import re
import ssl
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources

from . import micron
from .access import AccessPolicy
from .nomad import NomadError
from .provision import SchemaMissing

MAX_BODY = 1_000_000

_INLINE_SCRIPT = re.compile(rb"<script(?:\s[^>]*)?>(.*?)</script>", re.S)
_API_CSP = "default-src 'none'; frame-ancestors 'none'"
_page_cache = {}


def _csp_for_page(html):
    """Skripte der Seite laufen nur, wenn ihr Hash stimmt: eingeschleustes HTML (onerror=…, fremde <script>) wird blockiert.
    Inline-Stile bleiben erlaubt, weil der Micron-Renderer Stilattribute setzt."""
    hashes = " ".join("'sha256-%s'" % base64.b64encode(hashlib.sha256(m).digest()).decode()
                      for m in _INLINE_SCRIPT.findall(html))
    return ("default-src 'none'; script-src %s; style-src 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'" % (hashes or "'none'"))


def _index_page():
    """(Inhalt, Content-Security-Policy) der Oberfläche; die Datei ändert sich zur Laufzeit nicht."""
    if "index" not in _page_cache:
        html = resources.files("bindu").joinpath("static/index.html").read_bytes()
        _page_cache["index"] = (html, _csp_for_page(html))
    return _page_cache["index"]


def host_allowed(header, port, extra=()):
    """Prüft den Host-Header gegen DNS-Rebinding, wenn der Server nicht nur an Loopback lauscht.

    Erlaubt: IP-Adressen, ``localhost`` und Namen aus ``extra`` (``*`` = alles), jeweils mit dem echten Port.
    Ein Angreifer-Name, der per DNS auf diese Adresse zeigt, steht nie in dieser Liste."""
    extra = {str(x).strip().lower() for x in extra}
    if "*" in extra:
        return True
    h = (header or "").strip().lower()
    if h.startswith("["):
        end = h.find("]")
        if end < 0:
            return False
        name, rest = h[1:end], h[end + 1:]
    else:
        name, sep, tail = h.partition(":")
        rest = sep + tail
        if ":" in tail:  # IPv6 ohne Klammern ist als Host-Header ungültig
            return False
    if rest:
        if not rest.startswith(":") or not rest[1:].isdigit() or int(rest[1:]) != port:
            return False
    elif port not in (80, 443):
        return False
    if name == "localhost" or name in extra:
        return True
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


def make_tls_context(cert, key=None):
    """TLS-Kontext für den Server; wirft OSError/ssl.SSLError, wenn Zertifikat oder Schlüssel nicht ladbar sind."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert, key)
    return ctx


def make_handler(backend, allowed_hosts, chat=None, prov=None, meta=None, access=None):
    if access is None:
        access = AccessPolicy.default()

    class Handler(BaseHTTPRequestHandler):
        server_version = "bindu"

        def log_message(self, fmt, *args):
            pass

        def setup(self):
            # TLS: Handshake hier im Thread der Verbindung (nicht in der Annahmeschleife), mit Zeitlimit
            conn = self.request
            if isinstance(conn, ssl.SSLSocket):
                conn.settimeout(15)
                conn.do_handshake()
                conn.settimeout(None)
            super().setup()

        # ---- Helfer ----
        def _send(self, code, body, ctype, csp=None):
            try:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy", csp or _API_CSP)
                self.send_header("X-Frame-Options", "DENY")
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # Browser hat die Verbindung schon geschlossen (z. B. Seite neu geladen während einer langen Funkanfrage)

        def _json(self, code, obj):
            self._send(code, json.dumps(obj).encode(), "application/json; charset=utf-8")

        def _access_ok(self):
            if access.allows(self.client_address[0]):
                return True
            self._json(403, {"ok": False, "error": "Zugriff nur aus dem Tailnet erlaubt"})
            return False

        def _host_ok(self):
            if allowed_hosts is None:
                return True
            header = self.headers.get("Host", "")
            if allowed_hosts(header) if callable(allowed_hosts) else header in allowed_hosts:
                return True
            self._json(403, {"ok": False, "error": "Host nicht erlaubt"})
            return False

        # ---- Routen ----
        def do_GET(self):
            if not self._access_ok() or not self._host_ok():
                return
            if self.path in ("/", "/index.html"):
                page, csp = _index_page()
                self._send(200, page, "text/html; charset=utf-8", csp)
            elif self.path == "/api/state":
                self._json(200, dict(backend.state(), ok=True, app=meta or {}))
            elif self.path == "/api/chat":
                if chat is None:
                    self._json(404, {"ok": False, "error": "Chat nicht verfügbar"})
                else:
                    self._json(200, dict(chat.state(), ok=True))
            elif self.path.split("?")[0] == "/api/node/log":
                if prov is None:
                    self._json(404, {"ok": False, "error": "Node-Verwaltung nicht verfügbar"})
                else:
                    try:
                        since = int(self.path.partition("since=")[2].split("&")[0] or 0)
                    except ValueError:
                        since = 0
                    self._json(200, dict(prov.events(since), ok=True))
            elif self.path == "/api/node":
                if prov is None:
                    self._json(404, {"ok": False, "error": "Node-Verwaltung nicht verfügbar"})
                else:
                    self._json(200, dict(prov.state(), ok=True))
            else:
                self._json(404, {"ok": False, "error": "nicht gefunden"})

        def do_POST(self):
            if not self._access_ok() or not self._host_ok():
                return
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                self._json(415, {"ok": False, "error": "Content-Type application/json erforderlich"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 0 or length > MAX_BODY:
                    raise ValueError
                payload = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(payload, dict):
                    raise ValueError
            except ValueError:
                self._json(400, {"ok": False, "error": "Ungültige Anfrage"})
                return

            try:
                if self.path == "/api/page":
                    text = backend.fetch(
                        payload.get("node", ""),
                        payload.get("path", ""),
                        payload.get("identity", "") or "",
                        payload.get("fields") or {},
                        payload.get("vars") or {},
                    )
                    self._json(200, {"ok": True, "html": micron.render(text), "raw": text})
                elif self.path == "/api/identity":
                    ident = backend.create_identity(str(payload.get("name", "")))
                    self._json(200, {"ok": True, "identity": ident})
                elif self.path.startswith("/api/chat/"):
                    self._chat_post(payload)
                elif self.path.startswith("/api/node/"):
                    self._node_post(payload)
                else:
                    self._json(404, {"ok": False, "error": "nicht gefunden"})
            except SchemaMissing as exc:
                self._json(200, {"ok": False, "error": str(exc), "need_schema": True, "donors": exc.donors})
            except NomadError as exc:
                self._json(200, {"ok": False, "error": str(exc)})
            except Exception as exc:  # unerwarteter Fehler, UI soll nicht hängen
                self._json(500, {"ok": False, "error": "Interner Fehler: %s" % exc})

        def _chat_post(self, payload):
            if chat is None:
                self._json(404, {"ok": False, "error": "Chat nicht verfügbar"})
                return
            action = self.path[len("/api/chat/"):]
            if action == "open":
                self._json(200, dict(chat.open(payload.get("peer", "")), ok=True))
            elif action == "send":
                res = chat.send(payload.get("peer", ""), payload.get("content", ""), payload.get("title", ""))
                self._json(200, dict(res, ok=True))
            elif action == "announce":
                self._json(200, dict(chat.announce(), ok=True))
            elif action == "delete":
                self._json(200, dict(chat.delete(payload.get("id", "")), ok=True))
            else:
                self._json(404, {"ok": False, "error": "nicht gefunden"})

        def _node_post(self, payload):
            if prov is None:
                self._json(404, {"ok": False, "error": "Node-Verwaltung nicht verfügbar"})
                return
            action = self.path[len("/api/node/"):]
            dest, ident = payload.get("dest", ""), payload.get("identity", "") or ""
            ns = payload.get("ns") or None
            if action == "overview":
                # GetSchema über Funk nur auf ausdrücklichen Wunsch (fetch_schema), sonst SchemaMissing
                res = prov.overview(dest, ident, bool(payload.get("fetch_schema")))
            elif action == "ping":
                res = prov.ping(dest, ident)
            elif action == "adopt":
                res = prov.adopt_schema(dest, ident, payload.get("donor", ""))
            elif action == "state":
                res = prov.get_state(dest, ident, ns, True)
            elif action == "schema":
                refresh = bool(payload.get("refresh"))
                if refresh and payload.get("confirm") is not True:
                    raise NomadError("Schema neu holen muss ausdrücklich bestätigt werden (kann den Node zum Hängen bringen)")
                window = payload.get("window")  # gleichzeitig offene Anfragen; nur ausdrücklich, Standard 1
                if window is not None and (not isinstance(window, int) or isinstance(window, bool) or not 1 <= window <= 3):
                    raise NomadError("window muss eine ganze Zahl von 1 bis 3 sein")
                res = {"schema": prov.schema(dest, ident, refresh, None, allow_fetch=refresh, window=window)}
            elif action == "set":
                res = prov.set_state(dest, ident, payload.get("changes") or {})
            elif action == "commit":
                res = prov.commit(dest, ident, ns)
            elif action == "discard":
                res = prov.discard(dest, ident, ns)
            elif action == "disconnect":
                res = prov.disconnect(dest, ident)
            elif action == "reboot":
                res = prov.reboot(dest, ident)
            elif action == "factory_reset":
                if payload.get("confirm") is not True:
                    raise NomadError("Werksreset muss ausdrücklich bestätigt werden")
                res = prov.factory_reset(dest, ident)
            elif action == "rename":
                res = prov.rename(dest, payload.get("name", ""))
            elif action == "forget":
                res = prov.forget(dest)
            else:
                self._json(404, {"ok": False, "error": "nicht gefunden"})
                return
            self._json(200, dict(res, ok=True))

    return Handler


def make_server(backend, host="127.0.0.1", port=8765, chat=None, prov=None, meta=None, access=None, tls=None,
                allow_hosts=()):
    """``tls``: fertiger ssl.SSLContext (siehe make_tls_context) -> der Port spricht dann nur noch HTTPS.
    ``allow_hosts``: zusätzliche Namen im Host-Header (z. B. MagicDNS-Name oder Reverse-Proxy), ``*`` = jeder."""

    class _Server(ThreadingHTTPServer):
        daemon_threads = True

        def handle_error(self, request, client_address):
            # Fehlgeschlagene Handshakes (Portscanner, Klartext auf dem TLS-Port) und abgerissene Verbindungen sind kein Fehler
            if isinstance(sys.exc_info()[1], (ssl.SSLError, ConnectionError, TimeoutError)):
                return
            super().handle_error(request, client_address)

    server = _Server((host, port), make_handler(backend, None, chat, prov, meta, access))
    if tls is not None:
        server.socket = tls.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
    # Port 0 -> frei wählen; erlaubte Hosts erst nach dem Binden bekannt
    real_port = server.server_address[1]
    if host in ("127.0.0.1", "localhost", "::1"):
        allowed = {"127.0.0.1:%d" % real_port, "localhost:%d" % real_port, "[::1]:%d" % real_port}
        for name in allow_hosts:
            allowed.add("%s:%d" % (str(name).strip().lower(), real_port))
        if any(str(n).strip() == "*" for n in allow_hosts):
            allowed = None
    else:
        # Andere Bindung (Tailnet-Adresse, 0.0.0.0): ohne diese Prüfung könnte eine fremde Webseite per DNS-Rebinding
        # mit der Oberfläche sprechen
        allowed = lambda header: host_allowed(header, real_port, allow_hosts)  # noqa: E731
    server.RequestHandlerClass = make_handler(backend, allowed, chat, prov, meta, access)
    return server
