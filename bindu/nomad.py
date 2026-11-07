"""NomadNet-Backend: eigener RNS-Stack, Links zu Nodes, Identity-Auth.

Eine Instanz startet genau eine RNS.Reticulum (eigenes Config-Verzeichnis,
share_instance = No), hört auf NomadNet-Node-Announces, hält Links pro
(Node, Identity) offen und identifiziert sich auf Wunsch per link.identify().
"""

import json
import os
import re
import threading
import time

NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
HASH_RE = re.compile(r"^[0-9a-f]{32}$")
DEFAULT_PATH = "/page/index.mu"
MAX_NODES = 500  # gehörte Nodes; die ältesten fliegen raus


class NomadError(Exception):
    """Fehler mit Klartext für die Oberfläche."""


class DeferredWriter:
    """Fasst häufige Schreibwünsche zusammen: höchstens ein Schreibvorgang je `delay` Sekunden.

    Announces treffen oft im Sekundentakt ein; jedes Mal die ganze JSON-Datei neu zu schreiben
    belastet SD-Karten (Raspberry Pi). ``flush()`` schreibt sofort (beim Beenden).
    """

    def __init__(self, write, delay=30.0):
        self._write = write
        self._delay = delay
        self._lock = threading.Lock()
        self._timer = None
        self._dirty = False

    def mark(self):
        with self._lock:
            self._dirty = True
            if self._timer is None:
                self._timer = threading.Timer(self._delay, self.flush)
                self._timer.daemon = True
                self._timer.start()

    def flush(self):
        with self._lock:
            dirty, self._dirty = self._dirty, False
            timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()
        if dirty:
            try:
                self._write()
            except Exception:
                pass


def write_default_config(config_dir, tcp_targets):
    """Schreibt eine minimale Reticulum-Config (nur Client, kein Transport)."""
    os.makedirs(config_dir, exist_ok=True)
    lines = [
        "[reticulum]",
        "  enable_transport = No",
        "  share_instance = No",
        "  panic_on_interface_error = No",
        "",
        "[logging]",
        "  loglevel = 4",
        "",
        "[interfaces]",
    ]
    if not tcp_targets:
        lines += [
            "  [[Default Interface]]",
            "    type = AutoInterface",
            "    enabled = yes",
        ]
    for idx, target in enumerate(tcp_targets, 1):
        host, _, port = target.rpartition(":")
        if not host or not port.isdigit():
            raise NomadError("Ungültiges TCP-Ziel '%s' (erwartet HOST:PORT)" % target)
        lines += [
            "  [[TCP %d]]" % idx,
            "    type = TCPClientInterface",
            "    enabled = yes",
            "    target_host = %s" % host,
            "    target_port = %s" % port,
        ]
    with open(os.path.join(config_dir, "config"), "w") as fh:
        fh.write("\n".join(lines) + "\n")


class NomadBackend:
    WRITE_DELAY = 30.0  # Sekunden: gehörte Nodes werden gebündelt gespeichert

    def __init__(self, data_dir, rns_config=None, tcp_targets=(), loglevel=None):
        import RNS  # erst hier, damit Renderer/Tests ohne RNS laufen

        self.RNS = RNS
        self.data_dir = data_dir
        self.id_dir = os.path.join(data_dir, "identities")
        self.nodes_file = os.path.join(data_dir, "nodes.json")
        os.makedirs(self.id_dir, mode=0o700, exist_ok=True)

        config_dir = rns_config or os.path.join(data_dir, "reticulum")
        cfg_file = os.path.join(config_dir, "config")
        if not os.path.exists(cfg_file):
            write_default_config(config_dir, tcp_targets)
        elif tcp_targets and not rns_config:
            # --tcp gilt bei jedem Start: bindus eigene Config wird danach neu geschrieben (Sicherung: config.bak)
            old = open(cfg_file).read()
            write_default_config(config_dir, tcp_targets)
            if open(cfg_file).read() != old:
                with open(cfg_file + ".bak", "w") as fh:
                    fh.write(old)
        self.config_dir = config_dir

        self._lock = threading.Lock()
        self._link_locks = {}
        self._links = {}
        self._nodes = self._load_nodes()
        self._nodes_writer = DeferredWriter(self._write_nodes, self.WRITE_DELAY)

        self.reticulum = RNS.Reticulum(configdir=config_dir, loglevel=RNS.LOG_WARNING if loglevel is None else loglevel)
        RNS.Transport.register_announce_handler(_NodeAnnounces(self))

    # ---------- Nodes (Announces) ----------

    def _load_nodes(self):
        try:
            with open(self.nodes_file) as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_nodes(self):
        with self._lock:
            data = dict(self._nodes)
        tmp = self.nodes_file + ".tmp"
        try:
            with open(tmp, "w") as fh:
                json.dump(data, fh)
            os.replace(tmp, self.nodes_file)
        except OSError:
            pass

    def _note_node(self, node_hash, name):
        with self._lock:
            self._nodes[node_hash] = {"name": name, "last": int(time.time())}
            if len(self._nodes) > MAX_NODES:
                keep = sorted(self._nodes.items(), key=lambda kv: kv[1].get("last", 0), reverse=True)
                self._nodes = dict(keep[:MAX_NODES])
        self._nodes_writer.mark()

    # ---------- Identities ----------

    def _identity_path(self, name):
        if not NAME_RE.match(name):
            raise NomadError("Ungültiger Identity-Name (erlaubt: Buchstaben, Ziffern, _ . -)")
        return os.path.join(self.id_dir, name + ".id")

    def create_identity(self, name):
        path = self._identity_path(name)
        if os.path.exists(path):
            raise NomadError("Identity '%s' existiert bereits" % name)
        identity = self.RNS.Identity()
        identity.to_file(path)
        os.chmod(path, 0o600)
        return {"name": name, "hash": identity.hash.hex()}

    def _load_identity(self, name):
        if not name:
            return None
        path = self._identity_path(name)
        if not os.path.exists(path):
            raise NomadError("Identity '%s' nicht gefunden" % name)
        identity = self.RNS.Identity.from_file(path)
        if identity is None:
            raise NomadError("Identity '%s' nicht lesbar" % name)
        return identity

    def identities(self):
        result = []
        for fname in sorted(os.listdir(self.id_dir)):
            if not fname.endswith(".id"):
                continue
            name = fname[:-3]
            try:
                identity = self.RNS.Identity.from_file(os.path.join(self.id_dir, fname))
                result.append({"name": name, "hash": identity.hash.hex()})
            except Exception:
                result.append({"name": name, "hash": "?"})
        return result

    # ---------- Zustand ----------

    def state(self):
        with self._lock:
            nodes = [
                {"hash": h, "name": v.get("name", ""), "last": v.get("last", 0)}
                for h, v in self._nodes.items()
            ]
        nodes.sort(key=lambda n: n["last"], reverse=True)
        return {"nodes": nodes, "identities": self.identities()}

    # ---------- Link und Seitenabruf ----------

    def _get_link(self, node, identity_name, identity, timeout):
        RNS = self.RNS
        key = (node, identity_name or "")
        with self._lock:
            lock = self._link_locks.setdefault(key, threading.Lock())
        with lock:
            link = self._links.get(key)
            if link is not None and link.status == RNS.Link.ACTIVE:
                return link

            dest_hash = bytes.fromhex(node)
            if not RNS.Transport.has_path(dest_hash):
                RNS.Transport.request_path(dest_hash)
                deadline = time.time() + timeout
                while not RNS.Transport.has_path(dest_hash) and time.time() < deadline:
                    time.sleep(0.1)
                if not RNS.Transport.has_path(dest_hash):
                    raise NomadError(
                        "Kein Pfad zum Node – er hat nicht auf die Pfadanfrage geantwortet "
                        "(Announce abwarten oder Netz-Anbindung prüfen)"
                    )
            server_identity = RNS.Identity.recall(dest_hash)
            if server_identity is None:
                raise NomadError("Identity des Nodes unbekannt (noch kein Announce empfangen)")

            destination = RNS.Destination(
                server_identity,
                RNS.Destination.OUT,
                RNS.Destination.SINGLE,
                "nomadnetwork",
                "node",
            )
            established = threading.Event()
            closed = threading.Event()
            link = RNS.Link(
                destination,
                established_callback=lambda _l: established.set(),
                closed_callback=lambda _l: closed.set(),
            )
            end = time.time() + timeout
            while time.time() < end and not established.is_set() and not closed.is_set():
                time.sleep(0.05)
            if not established.is_set():
                try:
                    link.teardown()
                except Exception:
                    pass
                raise NomadError("Link zum Node kam nicht zustande")
            if identity is not None:
                link.identify(identity)
            self._links[key] = link
            return link

    def fetch(self, node, path, identity_name, fields=None, variables=None, timeout=45):
        node = (node or "").strip().lower()
        if not HASH_RE.match(node):
            raise NomadError("Node-Hash muss aus 32 Hex-Zeichen bestehen")
        path = (path or DEFAULT_PATH).strip() or DEFAULT_PATH
        if not path.startswith("/"):
            path = "/" + path
        identity = self._load_identity(identity_name)

        data = {}
        for key, value in (fields or {}).items():
            data["field_" + str(key)] = str(value)
        for key, value in (variables or {}).items():
            data["var_" + str(key)] = str(value)

        link = self._get_link(node, identity_name, identity, timeout)

        done = threading.Event()
        box = {}

        def on_response(receipt):
            box["response"] = receipt.response
            done.set()

        def on_failed(_receipt):
            box["failed"] = True
            done.set()

        link.request(
            path,
            data=data or None,
            response_callback=on_response,
            failed_callback=on_failed,
            timeout=timeout,
        )
        if not done.wait(timeout + 10):
            raise NomadError("Zeitüberschreitung beim Seitenabruf")
        if box.get("failed"):
            hint = " – Identity in der Allow-Liste des Nodes?" if identity_name else \
                " – Seite evtl. nur mit Identity abrufbar (Allow-Liste)"
            raise NomadError("Anfrage fehlgeschlagen" + hint)

        response = box.get("response")
        if isinstance(response, (bytes, bytearray)):
            return bytes(response).decode("utf-8", "replace")
        if isinstance(response, str):
            return response
        if response is None:
            raise NomadError("Leere Antwort vom Node")
        raise NomadError("Antworttyp wird noch nicht unterstützt (Datei-Download?)")

    def interface_names(self):
        try:
            return [str(i) for i in self.RNS.Transport.interfaces]
        except Exception:
            return []

    def flush(self):
        """Gebündelte Schreibvorgänge sofort ausführen."""
        self._nodes_writer.flush()

    def shutdown(self):
        self.flush()
        for link in list(self._links.values()):
            try:
                link.teardown()
            except Exception:
                pass


class _NodeAnnounces:
    aspect_filter = "nomadnetwork.node"

    def __init__(self, backend):
        self.backend = backend

    def received_announce(self, destination_hash, announced_identity, app_data):
        name = ""
        if app_data:
            try:
                name = app_data.decode("utf-8", "replace")
            except Exception:
                name = ""
        self.backend._note_node(destination_hash.hex(), name)
