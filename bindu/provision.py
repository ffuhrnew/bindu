"""Remote-Verwaltung von microReticulum-Nodes (Provisioning-Protokoll).

Dasselbe Protokoll wie die RNode-Console der Firmware, aber direkt über einen
RNS-Link zur Destination ``rnstransport.remote.management`` (Request-Pfad
``/provision``) – ohne rnsapid/MeshChatX. Payload: MsgPack ``[op, seq, body]``.

Der Node nimmt Anfragen nur von Identities in seiner Allow-Liste an; bindu
meldet sich dafür mit einer seiner Identities (Link ``identify``).

Änderung gegenüber 0.3.11: schnelle Wiederholung auf LoRa. Kleine Anfragen (GetInfo, GetCapabilities)
warten nur FAST_TIMEOUT Sekunden und werden bis zu FAST_RETRIES-mal auf demselben
Link wiederholt (vorher erneutes Identify). Der Link wird erst nach dem letzten
Fehlversuch abgebaut. Link-Aufbau: kürzere Versuche, mehr Versuche.

Änderung 0.3.14: Protokollzeilen auch in die Logdatei; Verbindungstest (nur GetInfo); GetSchema nur noch
nach Bestätigung (ein Node ohne Cache bekommt es nicht automatisch); Schema von einem gleichartigen Node
übernehmen; Anzeige des Cache-Zustands; veraltete Pfade werden vor dem Link-Aufbau aufgefrischt;
mgmt_nodes.json wird gebündelt geschrieben.
"""

import collections
import concurrent.futures
import contextlib
import itertools
import json
import logging
import os
import re
import threading
import time

from .nomad import DeferredWriter, NomadError

LOG = logging.getLogger("bindu.provision")

HASH_RE = re.compile(r"^[0-9a-f]{32}$")
NS_ASPECTS = ("rnstransport", "remote", "management")
REQUEST_PATH = "/provision"

OP = {
    "GetSchema": 1, "GetInfo": 2, "GetCapabilities": 3, "GetState": 4, "SetState": 5,
    "Commit": 6, "Discard": 7, "FactoryReset": 8, "Reboot": 9, "Ack": 100, "Error": 101,
}
OP_NAMES = {v: k for k, v in OP.items()}
FT = {"None": 0, "Bool": 1, "Int": 2, "Float": 3, "String": 4, "Bytes": 5, "Enum": 6, "BytesList": 7, "Void": 8}
FT_NAMES = {v: k.lower() for k, v in FT.items()}
FF = {"live_apply": 1, "reboot_required": 2, "read_only": 4, "secret": 8, "write_only": 16}
ERR_NAMES = {
    0: "Ok", 1: "BadRequest", 2: "UnknownOp", 3: "UnknownNs", 4: "UnknownField", 5: "InvalidValue",
    6: "Constraint", 7: "ReadOnly", 8: "StorageError", 9: "NotInitialized", 99: "Internal",
}
# Schlüssel in Anfragen/Antworten
FK = {"id": 1, "name": 2, "type": 3, "flags": 4, "min_i": 5, "max_i": 6, "min_f": 7, "max_f": 8,
      "max_len": 9, "enum_values": 10, "enum_labels": 11, "default": 12, "element_size": 13, "max_count": 14}
IK_FIRMWARE, IK_SCHEMA_VERSION, IK_NEEDS_REBOOT, IK_SCHEMA_HASH = 1, 2, 3, 4
GK_NS_FILTER, GK_DRAFT, GK_STATE, GK_PRIOR_HASH = 1, 2, 3, 4
GSK_VALUES, GSK_DRAFTS, GSK_HASH = 1, 2, 3
CAP_ID, CAP_NAME, CAP_PARENT, CAP_FIELDS, CAP_HASH = 1, 2, 4, 5, 6
SK_APPLIED, SK_DRAFT_REBOOT, SK_FIELD_ERRORS = 1, 2, 3
CK_APPLIED, CK_NEEDS_REBOOT = 1, 2
EK_CODE, EK_MESSAGE, EK_NS, EK_FIELD = 1, 2, 3, 4
WK_COMPRESSED = 101
NAME_NS, NAME_FIELD = 100, 5  # GetState: ns 100 (RNode General Config), Feld 5 = Name des Nodes
MAX_NODES = 500  # Einträge in mgmt_nodes.json; zuerst fliegen aufgeschnappte Hashes raus, benannte Einträge bleiben
IDLE_TIMEOUT = 120  # Sekunden ohne Anfrage, danach wird der Management-Link abgebaut (spart Heap und Airtime am Node)

# Kleine Anfragen: Antwort passt in ein Paket. Kommt sie nach ~4 RTT nicht, ist das Paket verloren -> neu senden.
FAST_OPS = None  # wird unten gesetzt (braucht OP)
FAST_TIMEOUT = 15      # Sekunden je Versuch
FAST_RETRIES = 3       # Wiederholungen auf demselben Link (insgesamt 4 Versuche)
LINK_TRY_TIMEOUT = 15  # Sekunden je Link-Aufbau-Versuch
LINK_ATTEMPTS = 3
PATH_WAIT = 25         # Sekunden auf eine Pfadantwort warten, bevor ein Link-Versuch gestartet wird
# SetState schreibt nur in den Entwurf und Discard verwirft ihn: beides ist wiederholbar, ohne Schaden anzurichten.
# Commit, Reboot und FactoryReset bleiben ohne automatische Wiederholung.
FAST_OPS = (OP["GetInfo"], OP["GetCapabilities"], OP["SetState"], OP["Discard"])
FAST_MAX_BYTES = 300  # größere Anfragen gehen als Resource über mehrere Pakete: dann längeres Zeitlimit
PATH_MAX_AGE = 1800     # Sekunden: ein älterer Pfad wird vor dem ersten Link-Versuch aufgefrischt
PATH_REFRESH_WAIT = 4   # Sekunden auf die Antwort einer solchen Auffrischung warten


class ProvisionError(NomadError):
    def __init__(self, message, code=None, timeout=False):
        super().__init__(message)
        self.code = code
        self.timeout = timeout  # True: der Node hat gar nicht geantwortet (Paketverlust möglich), Wiederholung sinnvoll


class SchemaMissing(ProvisionError):
    """Für den Node ist kein (vollständiges) Schema gespeichert; Holen über Funk braucht eine Bestätigung."""

    def __init__(self, message, donors=None):
        super().__init__(message)
        self.donors = donors or []


# ---------- Wert-Umwandlung (Wire <-> JSON) ----------

def to_wire(ftype, value):
    t = FT_NAMES.get(ftype, "none")
    try:
        if t == "bool":
            return bool(value)
        if t in ("int", "enum"):
            return int(value)
        if t == "float":
            return float(value)
        if t == "string":
            return "" if value is None else str(value)
        if t == "bytes":
            return bytes.fromhex(value or "")
        if t == "byteslist":
            return [bytes.fromhex(v) for v in (value or [])]
    except (ValueError, TypeError):
        raise ProvisionError("Ungültiger Wert für Feldtyp %s" % t)
    return None  # void / none


def from_wire(value):
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    if isinstance(value, list):
        return [from_wire(v) for v in value]
    return value


def _ns_map(m):
    out = {}
    if isinstance(m, dict):
        for ns, fields in m.items():
            if isinstance(fields, dict):
                out[int(ns)] = {int(f): from_wire(v) for f, v in fields.items()}
    return out


class ProvisionBackend:
    def __init__(self, nomad, wire=None):
        """``wire(dest_hex, identity_name, data, timeout)`` -> bytes; nur für Tests ersetzbar."""
        self.nomad = nomad
        self.RNS = nomad.RNS
        self._wire = wire or self._wire_link
        self._seq = itertools.count(1)
        self._lock = threading.Lock()
        self._links = {}
        self._link_locks = {}
        self._call_locks = {}
        self._last_used = {}
        self._flat = {}  # dest -> True: der Node nutzt das flache Format {ns: {feld: wert}} (ältere Firmware)
        self._stop = threading.Event()
        self._events = collections.deque(maxlen=500)  # Protokoll für die Web-Oberfläche
        self._ev_id = 0
        self.cache_dir = os.path.join(nomad.data_dir, "node_cache")
        self.nodes_file = os.path.join(nomad.data_dir, "mgmt_nodes.json")
        os.makedirs(self.cache_dir, mode=0o700, exist_ok=True)
        self._nodes = self._load_json(self.nodes_file, {})
        self._trim_nodes()
        self._nodes_writer = DeferredWriter(self._write_nodes, 30.0)
        self._schema_state = {}  # dest -> {"stale": Anzahl, "at": Zeit}: Ergebnis des letzten schema()-Laufs
        if wire is None:
            self.RNS.Transport.register_announce_handler(_MgmtAnnounces(self))
            threading.Thread(target=self._reap_loop, name="bindu-mgmt-reaper", daemon=True).start()

    # ---------- Hilfen ----------

    @staticmethod
    def _load_json(path, default):
        try:
            with open(path) as fh:
                data = json.load(fh)
            return data if isinstance(data, type(default)) else default
        except (OSError, ValueError):
            return default

    @staticmethod
    def _check(dest):
        dest = (dest or "").strip().lower()
        if not HASH_RE.match(dest):
            raise ProvisionError("Ziel-Hash muss aus 32 Hex-Zeichen bestehen")
        return dest

    def _trim_nodes(self):
        """Begrenzt die Liste auf MAX_NODES. Reihenfolge des Entfernens: nur aufgeschnappte Hashes (nie verbunden, ohne
        Namen), dann Nodes mit selbst gemeldetem Namen, jeweils die ältesten zuerst. Eigene Namen (rename) bleiben immer."""
        with self._lock:
            over = len(self._nodes) - MAX_NODES
            if over <= 0:
                return

            def rank(item):
                v = item[1]
                return (2 if v.get("name") else 1 if v.get("device_name") else 0, v.get("last", 0))

            for h, v in sorted(self._nodes.items(), key=rank)[:over]:
                if v.get("name"):
                    break  # alles Weitere sind eigene Namen
                del self._nodes[h]

    def _write_nodes(self):
        with self._lock:
            data = json.loads(json.dumps(self._nodes))
        tmp = self.nodes_file + ".tmp"
        try:
            with open(tmp, "w") as fh:
                json.dump(data, fh)
            os.replace(tmp, self.nodes_file)
        except OSError:
            pass

    def _note(self, dest):
        with self._lock:
            self._nodes.setdefault(dest, {})["last"] = int(time.time())
        self._trim_nodes()
        self._nodes_writer.mark()

    def state(self):
        with self._lock:
            nodes = [{"hash": h, "name": v.get("name", ""), "last": v.get("last", 0), "_dev": v.get("device_name", "")}
                     for h, v in self._nodes.items()]
        derived = self._derived({n["hash"] for n in nodes})
        # Anzeigename: eigener Name > Name, den der Node selbst meldet (GetState ns 100, Feld 5)
        # > Name des NomadNet-Nodes mit derselben Identity
        auto = {d["mgmt"]: d["name"] for d in derived if d["name"]}
        for n in nodes:
            dev = n.pop("_dev")
            if n["name"]:
                n["src"] = "alias"
            elif dev:
                n["name"], n["auto"], n["src"] = dev, True, "node"
            elif auto.get(n["hash"]):
                n["name"], n["auto"], n["src"] = auto[n["hash"]], True, "nomad"
        # Einträge ohne Namen sind nur aufgeschnappte Hashes, nie verbunden: nicht in die Liste
        nodes = [n for n in nodes if n.get("name")]
        nodes.sort(key=lambda n: n["last"], reverse=True)
        return {"nodes": nodes, "derived": derived}

    def _derived(self, announced):
        """Management-Adressen, die sich aus bekannten NomadNet-Nodes (gleiche Node-Identity) ableiten lassen."""
        RNS = self.RNS
        out = []
        lock = getattr(self.nomad, "_lock", None) or threading.Lock()
        with lock:
            known = dict(getattr(self.nomad, "_nodes", {}) or {})
        for h, v in known.items():
            try:
                ident = RNS.Identity.recall(bytes.fromhex(h))
                if ident is None:
                    continue
                d = RNS.Destination(ident, RNS.Destination.OUT, RNS.Destination.SINGLE, *NS_ASPECTS)
            except Exception:
                continue
            out.append({"name": v.get("name", ""), "nomad": h, "mgmt": d.hash.hex(), "announced": d.hash.hex() in announced})
        out.sort(key=lambda x: x["name"].lower())
        return out

    def forget(self, dest):
        dest = self._check(dest)
        with self._lock:
            self._nodes.pop(dest, None)
        self._write_nodes()
        return {}

    def rename(self, dest, name):
        dest = self._check(dest)
        with self._lock:
            self._nodes.setdefault(dest, {})["name"] = str(name or "")[:60]
        self._trim_nodes()
        self._write_nodes()
        return {}

    def _learn_name(self, dest, values):
        """Merkt sich den Namen, den der Node selbst meldet (GetState ns 100, Feld 5), als Anzeigenamen."""
        name = (values.get(NAME_NS) or {}).get(NAME_FIELD)
        if not isinstance(name, str) or not name.strip():
            return
        name = name.strip()[:60]
        dest = self._check(dest)
        with self._lock:
            entry = self._nodes.setdefault(dest, {})
            if entry.get("device_name") == name:
                return
            entry["device_name"] = name
        self._trim_nodes()
        self._nodes_writer.mark()

    # ---------- Link ----------

    def _resolve_destination(self, dest, timeout):
        RNS = self.RNS
        h = bytes.fromhex(dest)
        ident = RNS.Identity.recall(h)
        if ident is None:
            if not RNS.Transport.has_path(h):
                RNS.Transport.request_path(h)
                end = time.time() + timeout
                while time.time() < end and not RNS.Transport.has_path(h):
                    time.sleep(0.1)
            ident = RNS.Identity.recall(h)
        if ident is not None:
            # Entweder ist h die Management-Destination selbst, oder irgendeine andere Destination
            # derselben Node-Identity (z. B. die NomadNet-Adresse) – dann wird die Management-Adresse abgeleitet.
            return RNS.Destination(ident, RNS.Destination.OUT, RNS.Destination.SINGLE, *NS_ASPECTS)
        # Vielleicht wurde der Identity-Hash statt des Destination-Hashs angegeben
        ident = RNS.Identity.recall(h, from_identity_hash=True)
        if ident is not None:
            return RNS.Destination(ident, RNS.Destination.OUT, RNS.Destination.SINGLE, *NS_ASPECTS)
        raise ProvisionError(
            "Node unbekannt – kein Announce und keine Pfadantwort für diesen Hash "
            "(Destination-Hash von rnstransport.remote.management prüfen, Netz-Anbindung?)"
        )

    def _path_age(self, h):
        """Alter des Pfadeintrags in Sekunden, None wenn unbekannt."""
        try:
            entry = self.RNS.Transport.path_table.get(h)
            if entry:
                return time.time() - entry[0]
        except Exception:
            pass
        return None

    def _refresh_path(self, h, why):
        """Fragt den Pfad erneut an, auch wenn Reticulum noch einen Eintrag hat (der kann veraltet sein)."""
        before = self._path_age(h)
        try:
            self.RNS.Transport.request_path(h)
        except Exception:
            return
        self._log("Pfad zu %s aufgefrischt (%s%s)" % (
            h.hex()[:8], why, ", alt %.0f s" % before if before is not None else ""))
        end = time.time() + PATH_REFRESH_WAIT
        while time.time() < end:
            age = self._path_age(h)
            if before is not None and age is not None and age < before - 0.5:
                break  # neue Pfadantwort ist eingetroffen
            time.sleep(0.1)

    def _get_link(self, dest, identity_name, timeout):
        RNS = self.RNS
        key = (dest, identity_name or "")
        with self._lock:
            lock = self._link_locks.setdefault(key, threading.Lock())
        with lock:
            link = self._links.get(key)
            if link is not None and link.status == RNS.Link.ACTIVE:
                self._touch(key)
                return link
            identity = self.nomad._load_identity(identity_name)
            destination = self._resolve_destination(dest, timeout)
            link = None
            attempts = LINK_ATTEMPTS
            for attempt in range(attempts):
                # Ein Link, der nie aktiv wurde, lässt Reticulum den Pfad zum Ziel verwerfen (Transport.expire_path).
                # Darum nie vor Ablauf der eigenen Aufbau-Frist abbrechen und vor jedem Versuch den Pfad sichern.
                if not RNS.Transport.has_path(destination.hash):
                    self._log("Pfad zu %s fehlt – Pfadanfrage (bis %d s)" % (destination.hash.hex()[:8], min(timeout, PATH_WAIT)))
                    RNS.Transport.request_path(destination.hash)
                    end = time.time() + min(timeout, PATH_WAIT)
                    while time.time() < end and not RNS.Transport.has_path(destination.hash):
                        time.sleep(0.1)
                    if not RNS.Transport.has_path(destination.hash):
                        self._log("kein Pfad zu %s (Versuch %d/%d)" % (destination.hash.hex(), attempt + 1, attempts), True)
                        continue
                elif attempt > 0:
                    self._refresh_path(destination.hash, "vor Wiederholung")
                else:
                    age = self._path_age(destination.hash)
                    if age is not None and age > PATH_MAX_AGE:
                        self._refresh_path(destination.hash, "Pfad ist alt")
                established, closed = threading.Event(), threading.Event()
                self._log("Link-Aufbau zu %s, Versuch %d/%d" % (destination.hash.hex()[:8], attempt + 1, attempts))
                t_start = time.time()
                link = RNS.Link(
                    destination,
                    established_callback=lambda _l, e=established: e.set(),
                    closed_callback=lambda _l, c=closed: c.set(),
                )
                limit = (getattr(link, "establishment_timeout", None) or LINK_TRY_TIMEOUT) + 3
                end = time.time() + min(timeout, limit)
                while time.time() < end and not established.is_set() and not closed.is_set():
                    time.sleep(0.05)
                if established.is_set():
                    self._log("Link steht nach %.1f s (RTT %.1f s)" % (time.time() - t_start, link.rtt or 0))
                    break
                self._log("Link-Versuch %d/%d ohne Erfolg nach %.0f s" % (attempt + 1, attempts, time.time() - t_start), True)
                try:
                    link.teardown()
                except Exception:
                    pass
                link = None
            if link is None:
                hops = RNS.Transport.hops_to(destination.hash)
                raise ProvisionError(
                    "Link zu %s kam in %d Versuchen nicht zustande (%s, %s Hops) – Node antwortet nicht auf den "
                    "Management-Link: Funkstrecke zu schlecht, Management am Node aus, oder Hash falsch"
                    % (destination.hash.hex(), attempts,
                       "Pfad vorhanden" if RNS.Transport.has_path(destination.hash) else "kein Pfad",
                       hops if hops != RNS.Transport.PATHFINDER_M else "?"))
            if identity is not None:
                link.identify(identity)
                # Identify und Request sind getrennte Funkpakete; der Node soll die Identity vor der Anfrage kennen
                time.sleep(self._identify_pause(link))
            self._links[key] = link
            self._touch(key)
            return link

    @staticmethod
    def _identify_pause(link):
        return min(6.0, max(1.5, (link.rtt or 1.0) * 1.5))

    def _reidentify(self, dest, identity_name):
        """Sendet das Identify auf dem bestehenden Link erneut (einzelnes Paket, kann verloren gegangen sein)."""
        key = (dest, identity_name or "")
        with self._lock:
            link = self._links.get(key)
        if link is None or link.status != self.RNS.Link.ACTIVE:
            return  # kein Link mehr: der nächste Versuch baut einen neuen auf und identifiziert sich dabei
        try:
            identity = self.nomad._load_identity(identity_name)
            if identity is not None:
                link.identify(identity)
                time.sleep(min(4.0, max(1.0, link.rtt or 1.0)))
        except Exception as exc:
            self._log("Identify-Wiederholung fehlgeschlagen: %s" % exc, True)

    def _drop_link(self, dest, identity_name):
        with self._lock:
            link = self._links.pop((dest, identity_name or ""), None)
            self._last_used.pop((dest, identity_name or ""), None)
        if link is not None:
            try:
                link.teardown()
            except Exception:
                pass

    def _wire_link(self, dest, identity_name, data, timeout):
        link = self._get_link(dest, identity_name, 60)
        done = threading.Event()
        box = {}

        def ok(receipt):
            box["response"] = receipt.response
            done.set()

        def failed(_receipt):
            box["failed"] = True
            done.set()

        link.request(REQUEST_PATH, data=data, response_callback=ok, failed_callback=failed, timeout=timeout)
        # Der Link bleibt bei Zeitüberschreitung bestehen: call() wiederholt auf ihm und baut ihn erst nach dem letzten Fehlschlag ab.
        if not done.wait(timeout + 5):
            raise ProvisionError("Zeitüberschreitung – keine Antwort vom Node", timeout=True)
        if box.get("failed"):
            raise ProvisionError(
                "Anfrage fehlgeschlagen – steht die Identity '%s' in der Allow-Liste des Nodes?"
                % (identity_name or "(keine)"),
                timeout=True,
            )
        response = box.get("response")
        if isinstance(response, (bytes, bytearray)):
            return bytes(response)
        if isinstance(response, (list, tuple)):  # Firmware liefert die Antwort schon entpackt
            return list(response)
        raise ProvisionError("Unerwartete Antwort vom Node (Typ %s: %.80r)" % (type(response).__name__, response))

    def _log(self, text, error=False):
        with self._lock:
            self._ev_id += 1
            self._events.append({"id": self._ev_id, "t": time.time(), "error": bool(error),
                                 "text": text if len(text) <= 400 else text[:400] + " …"})
        LOG.log(logging.ERROR if error else logging.INFO, text)
        try:
            self.RNS.log("bindu/provision: " + text, self.RNS.LOG_ERROR if error else self.RNS.LOG_VERBOSE)
        except Exception:
            pass

    def events(self, since=0):
        """Protokollzeilen mit id > since (für die Oberfläche)."""
        with self._lock:
            ev = [e for e in self._events if e["id"] > since]
            last = self._ev_id
        return {"events": ev, "last": last}

    # ---------- Protokoll ----------

    def call(self, dest, identity_name, op, payload=None, timeout=60, parallel=False):
        """``parallel=True``: nur für die Schema-Abfrage mit Fenster. Der Aufrufer hält dann selbst die Sperre des Nodes,
        und ein Zeitlimit baut den Link nicht ab (andere Anfragen laufen noch auf ihm)."""
        from RNS.vendor import umsgpack

        dest = self._check(dest)
        if not identity_name:
            raise ProvisionError("Identity wählen – der Node lehnt Anfragen ohne Identity aus seiner Allow-Liste ab")
        seq = next(self._seq)
        env = [op, seq] if payload is None else [op, seq, payload]
        # Die Firmware erwartet im Request die MsgPack-Struktur selbst (nicht deren Bytes als bin) und
        # antwortet ebenso als Struktur – RNS packt die Struktur beim Senden selbst ein.
        data = env
        size = len(umsgpack.packb(env))
        with self._lock:
            call_lock = self._call_locks.setdefault(dest, threading.Lock())
        opname = OP_NAMES.get(op, op)
        self._log("Anfrage %s (op=%s seq=%s, %d Bytes) an %s" % (opname, op, seq, size, dest[:8]))
        if op in FAST_OPS:
            # Kleine Anfrage/Antwort = Einzelpakete ohne Reticulum-Wiederholung: kurz warten, schnell neu senden
            timeout = min(timeout, FAST_TIMEOUT if size <= FAST_MAX_BYTES else 4 * FAST_TIMEOUT)
            retries = FAST_RETRIES
        elif op == OP["GetState"]:
            retries = 1  # Antwort kann ein Resource-Transfer sein (lange Wartezeit sinnvoll), eine Wiederholung genügt
        else:
            retries = 0  # schreibende Operationen nie automatisch wiederholen
        with (contextlib.nullcontext() if parallel else call_lock):  # pro Node eine Anfrage zur Zeit (LoRa!)
            attempt = 0
            try:
                while True:
                    try:
                        raw = self._wire(dest, identity_name, data, timeout)
                        break
                    except ProvisionError as exc:
                        if exc.timeout and attempt < retries:
                            attempt += 1
                            self._log("%s seq=%s: keine Antwort nach %s s – Versuch %d von %d (gleicher Link)"
                                      % (opname, seq, timeout, attempt + 1, retries + 1), True)
                            self._reidentify(dest, identity_name)
                            continue
                        if op == OP["Reboot"] and exc.timeout:
                            self._log("Reboot seq=%s: keine Antwort – beim Neustart normal, der Node startet neu" % seq)
                        else:
                            self._log("%s seq=%s fehlgeschlagen: %s" % (opname, seq, exc), True)
                        if exc.timeout and not parallel:
                            # erst nach dem letzten Fehlversuch: Link verwerfen, der nächste Aufruf baut ihn neu auf
                            self._drop_link(dest, identity_name)
                        raise
                    except Exception as exc:
                        self._log("%s seq=%s fehlgeschlagen: %s" % (opname, seq, exc), True)
                        raise
            finally:
                self._touch((dest, identity_name or ""))
        self._log("Antwort %s (op=%s seq=%s): %s %.2000r" % (opname, op, seq, type(raw).__name__, raw))
        self._note(dest)
        if isinstance(raw, (bytes, bytearray)):
            try:
                resp = umsgpack.unpackb(bytes(raw))
            except Exception:
                raise ProvisionError("Antwort des Nodes nicht lesbar")
        else:
            resp = raw
        if not isinstance(resp, list) or len(resp) < 2:
            raise ProvisionError("Antwort des Nodes hat ein unbekanntes Format")
        rop, rseq = int(resp[0]), int(resp[1])
        body = resp[2] if len(resp) > 2 else None
        if rseq != seq:
            # RNS ordnet Antworten über die Request-ID zu und pro Node läuft nur eine Anfrage – nur protokollieren
            self._log("Sequenznummer weicht ab: gesendet %s, Antwort %s (op %s)" % (seq, rseq, rop))
        if isinstance(body, dict) and set(body) == {WK_COMPRESSED}:
            raise ProvisionError("Komprimierte Antwort – wird nicht unterstützt")
        if rop == OP["Error"]:
            code = body.get(EK_CODE, 99) if isinstance(body, dict) else 99
            msg = body.get(EK_MESSAGE, "Fehler") if isinstance(body, dict) else "Fehler"
            ns = body.get(EK_NS) if isinstance(body, dict) else None
            field = body.get(EK_FIELD) if isinstance(body, dict) else None
            text = "%s (%s)" % (msg, ERR_NAMES.get(code, code))
            if ns is not None:
                text += " ns=%s" % ns + (" field=%s" % field if field is not None else "")
            raise ProvisionError(text, code)
        return body

    # ---------- Operationen ----------

    def info(self, dest, identity):
        try:
            body = self.call(dest, identity, OP["GetInfo"]) or {}
        except ProvisionError as exc:
            if exc.timeout:
                raise ProvisionError(
                    "Der Link steht, aber der Node beantwortet GetInfo nicht (%d Versuche). Er hängt vermutlich: "
                    "einige Minuten warten, hilft das nicht, am Node Strom trennen. Ein Neustart über bindu geht "
                    "nur, solange er antwortet." % (FAST_RETRIES + 1),
                    timeout=True,
                ) from exc
            raise
        return {
            "firmware": body.get(IK_FIRMWARE),
            "schema_version": body.get(IK_SCHEMA_VERSION),
            "needs_reboot": bool(body.get(IK_NEEDS_REBOOT)),
            "schema_hash": body.get(IK_SCHEMA_HASH),
        }

    def capabilities(self, dest, identity):
        """Namespaces des Nodes. Neuere Firmware liefert Karten (id, name, Hash), ältere nur eine Liste von IDs."""
        body = self.call(dest, identity, OP["GetCapabilities"]) or []
        out = []
        for e in body:
            if isinstance(e, dict):
                out.append({
                    "id": int(e.get(CAP_ID)),
                    "name": str(e.get(CAP_NAME) or ""),
                    "parent": int(e.get(CAP_PARENT) or 0),
                    "fields": int(e.get(CAP_FIELDS) or 0),
                    "hash": int(e.get(CAP_HASH) or 0),
                })
            elif isinstance(e, int) and not isinstance(e, bool):
                out.append({"id": e, "name": "", "parent": 0, "fields": 0, "hash": 0})
        return out

    def _parse_schema(self, body):
        out = []
        for entry in body or []:
            if not isinstance(entry, list) or len(entry) < 3:
                continue
            v2 = len(entry) >= 4
            parent = int(entry[2] or 0) if v2 else 0
            raw = entry[3] if v2 else entry[2]
            fields = []
            for f in raw or []:
                if not isinstance(f, dict):
                    continue
                ftype = int(f.get(FK["type"], 0))
                flags = int(f.get(FK["flags"], 0) or 0)
                field = {
                    "id": int(f.get(FK["id"])),
                    "name": f.get(FK["name"]),
                    "type": FT_NAMES.get(ftype, "none"),
                    "type_id": ftype,
                    "flags": [k for k, bit in FF.items() if flags & bit],
                }
                for key in ("min_i", "max_i", "min_f", "max_f", "max_len", "element_size", "max_count"):
                    if f.get(FK[key]) is not None:
                        field[key] = f.get(FK[key])
                if f.get(FK["enum_values"]) is not None:
                    field["enum_values"] = list(f.get(FK["enum_values"]))
                    field["enum_labels"] = list(f.get(FK["enum_labels"]) or [])
                if f.get(FK["default"]) is not None:
                    field["default"] = from_wire(f.get(FK["default"]))
                fields.append(field)
            out.append({"id": int(entry[0]), "name": str(entry[1]), "parent": parent, "fields": fields})
        return out

    def _cache_path(self, dest):
        return os.path.join(self.cache_dir, dest + ".json")

    SCHEMA_CHUNK = 1    # Namespaces je GetSchema-Anfrage: kleine Antworten gehen auf LoRa eher unverloren durch
    SCHEMA_TRIES = 3    # Versuche je Portion, bevor sie in diesem Durchgang als fehlend gilt (nur ohne Fenster)
    SCHEMA_ROUNDS = 3   # Durchgänge: ab dem zweiten wird nur noch geholt, was noch fehlt
    SCHEMA_ROUND_PAUSE = 20.0  # Sekunden Pause zwischen zwei Durchgängen (der Kanal soll sich erholen)
    SCHEMA_PAUSE = 3.0  # Sekunden Pause zwischen zwei Portionen
    SCHEMA_WAIT = 30    # Sekunden Wartezeit je Einzelanfrage: eine einzelne Namespace-Antwort ist klein
    SCHEMA_DEADLINE = 600  # Sekunden Obergrenze je Node: ein nicht erreichbarer Node blockiert nicht länger
    SCHEMA_WINDOW = 1   # gleichzeitig offene Anfragen; 1 = nacheinander (Standard), mehr nur ausdrücklich angefordert

    def _write_json(self, path, obj):
        tmp = path + ".tmp"
        try:
            with open(tmp, "w") as fh:
                json.dump(obj, fh)
            os.replace(tmp, path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def schema(self, dest, identity, refresh=False, schema_hash=None, allow_fetch=True, window=None):
        """Schema je Namespace; per Hash zwischengespeichert (spart LoRa-Airtime).

        Fehlende Namespaces werden in Portionen geholt und nach jeder Portion gespeichert. Der Cache wird nie
        geleert: ``refresh`` holt alles neu, überschreibt aber nur, was tatsächlich ankommt.
        ``allow_fetch=False``: fehlt etwas, wird nicht über Funk gefragt, sondern SchemaMissing ausgelöst
        (GetSchema hat schon Nodes zum Hängen gebracht).
        """
        dest = self._check(dest)
        caps = self.capabilities(dest, identity)
        if schema_hash is None and any(not c["hash"] for c in caps):
            schema_hash = self.info(dest, identity).get("schema_hash")
        for cap in caps:
            if not cap["hash"] and schema_hash:  # ältere Firmware: nur der Gesamt-Schema-Hash aus GetInfo
                cap["hash"] = int(schema_hash)
        by_id = {c["id"]: c for c in caps}
        cache = self._load_json(self._cache_path(dest), {})
        found, missing, stale = {}, [], set()
        for cap in caps:
            hit = None if refresh else cache.get(str(cap["id"]))
            if hit and cap["hash"] and hit.get("hash") == cap["hash"]:
                found[cap["id"]] = hit["ns"]
            elif hit and not refresh:
                # Schema bekannt, Hash weicht ab: gespeichertes Schema sofort verwenden, nicht erst den Node fragen
                found[cap["id"]] = hit["ns"]
                stale.add(cap["id"])
            else:
                missing.append(cap["id"])

        if stale:
            self._log("Hash weicht ab bei %d von %d Namespaces (%s) – verwende gespeichertes Schema ('Schema neu' holt es vom Node)"
                      % (len(stale), len(caps), ", ".join(str(n) for n in sorted(stale))))
        if missing and not allow_fetch:
            raise SchemaMissing(
                "Für diesen Node ist kein vollständiges Schema gespeichert (%d von %d Namespaces fehlen)."
                % (len(missing), len(caps)),
                self._schema_donors(dest, {c["id"] for c in caps}),
            )

        def store():
            out = dict(cache)
            for nid, ns in found.items():
                if nid in stale:  # veraltetes Schema nie als aktuell zwischenspeichern
                    continue
                h = by_id.get(nid, {}).get("hash")
                if h:  # ohne Hash nicht zwischenspeichern (sonst nie aktuell)
                    out[str(nid)] = {"hash": h, "ns": ns}
            self._write_json(self._cache_path(dest), out)

        # Durchgänge: Was ankommt, wird sofort gespeichert. Jeder weitere Durchgang holt nur noch, was fehlt.
        window = max(1, int(window or self.SCHEMA_WINDOW))
        tries = self.SCHEMA_TRIES if window == 1 else 1  # mit Fenster wiederholen die weiteren Durchgänge
        deadline = time.time() + self.SCHEMA_DEADLINE
        pending = [missing[i:i + self.SCHEMA_CHUNK] for i in range(0, len(missing), self.SCHEMA_CHUNK)]
        still = []

        def fetch(part):
            for attempt in range(tries):
                if time.time() > deadline:
                    return None
                try:
                    return self.call(dest, identity, OP["GetSchema"], {GK_NS_FILTER: part},
                                     timeout=self.SCHEMA_WAIT, parallel=window > 1)
                except ProvisionError as exc:
                    if not exc.timeout:
                        raise
                    self._log("GetSchema %s: keine Antwort – Versuch %d von %d" % (part, attempt + 1, tries), True)
            return None

        def take(part, body):
            if body is None:
                still.append(part)
                return
            got = set()
            for ns in self._parse_schema(body):
                found[ns["id"]] = ns
                got.add(ns["id"])
            store()
            rest = [n for n in part if n not in got]  # Antwort ohne den erfragten Namespace gilt als fehlend
            if rest:
                still.append(rest)

        with self._lock:
            call_lock = self._call_locks.setdefault(dest, threading.Lock())
        # Mit Fenster hält diese Funktion die Sperre des Nodes selbst; die Einzelanfragen laufen dann ohne eigene Sperre.
        with (call_lock if window > 1 else contextlib.nullcontext()):
            for rnd in range(self.SCHEMA_ROUNDS):
                if not pending or time.time() > deadline:
                    break
                if rnd:
                    self._log("Schema: %d Namespaces fehlen noch – Durchgang %d von %d, nur die fehlenden"
                              % (sum(len(p) for p in pending), rnd + 1, self.SCHEMA_ROUNDS), True)
                    time.sleep(self.SCHEMA_ROUND_PAUSE)
                still = []
                if window > 1:
                    with concurrent.futures.ThreadPoolExecutor(max_workers=window) as pool:
                        futs = {pool.submit(fetch, part): part for part in pending}
                        for fut in concurrent.futures.as_completed(futs):
                            take(futs[fut], fut.result())
                else:
                    for i, part in enumerate(pending):
                        if i:
                            time.sleep(self.SCHEMA_PAUSE)
                        take(part, fetch(part))
                pending = still
            if window > 1 and pending:
                self._drop_link(dest, identity)  # der nächste Aufruf baut den Link neu auf
        failed = []
        for part in pending:
            if all(str(n) in cache for n in part):
                # Node antwortet nicht: gespeichertes (evtl. veraltetes) Schema für genau diese Namespaces
                self._log("GetSchema %s ohne Antwort – verwende zwischengespeichertes Schema (Hash weicht ab)" % part, True)
                for n in part:
                    found[n] = cache[str(n)]["ns"]
                    stale.add(n)
            else:
                failed.extend(part)
        self._schema_state[dest] = {"stale": len(stale), "at": time.time(), "missing": failed}
        if failed:
            # Der Fortschritt ist gespeichert: der nächste Versuch holt nur die fehlenden Namespaces.
            raise ProvisionError(
                "Schema unvollständig: %d von %d Namespaces fehlen (%s). Was angekommen ist, ist gespeichert; "
                "ein neuer Versuch holt nur die fehlenden." % (len(failed), len(caps), ", ".join(str(n) for n in failed)),
                timeout=True)
        return [found[c["id"]] for c in caps if c["id"] in found]

    def _schema_donors(self, dest, ids):
        """Gespeicherte Schemas anderer Nodes mit genau denselben Namespace-IDs (übernehmbar ohne Funkverkehr)."""
        out = []
        try:
            names = sorted(os.listdir(self.cache_dir))
        except OSError:
            return out
        for fname in names:
            other = fname[:-5]
            if not fname.endswith(".json") or other == dest or not HASH_RE.match(other):
                continue
            cache = self._load_json(os.path.join(self.cache_dir, fname), {})
            try:
                if not cache or {int(k) for k in cache} != set(ids) or any(h.get("adopted") for h in cache.values()):
                    continue
                fields = sum(len(h["ns"].get("fields", [])) for h in cache.values())
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
            with self._lock:
                name = self._nodes.get(other, {}).get("name", "")
            out.append({"dest": other, "name": name or other[:8], "namespaces": len(cache), "fields": fields})
        return out

    def adopt_schema(self, dest, identity, donor):
        """Übernimmt das gespeicherte Schema eines gleichartigen Nodes (nur fehlende Namespaces, ohne GetSchema).

        Die Einträge werden mit Hash 0 markiert: sie gelten als „nicht vom Node bestätigt“ und werden nie
        automatisch über Funk ersetzt. Unterschiede gibt es höchstens bei Standardwerten.
        """
        dest = self._check(dest)
        donor = self._check(donor)
        if donor == dest:
            raise ProvisionError("Ein Node kann kein Schema von sich selbst übernehmen")
        caps = self.capabilities(dest, identity)
        match = {d["dest"] for d in self._schema_donors(dest, {c["id"] for c in caps})}
        if donor not in match:
            raise ProvisionError("Das Schema von %s passt nicht zu den Namespaces dieses Nodes" % donor[:8])
        src = self._load_json(self._cache_path(donor), {})
        cache = self._load_json(self._cache_path(dest), {})
        added = 0
        for nid, hit in src.items():
            if nid not in cache:
                cache[nid] = {"hash": 0, "ns": hit["ns"], "adopted": donor}
                added += 1
        self._write_json(self._cache_path(dest), cache)
        self._log("Schema von %s übernommen (%d Namespaces), nicht vom Node bestätigt" % (donor[:8], added))
        return {"namespaces": added, "donor": donor}

    def cache_info(self, dest, node_hash=None):
        """Zustand des Schema-Caches eines Nodes für die Anzeige."""
        dest = self._check(dest)
        path = self._cache_path(dest)
        cache = self._load_json(path, {})
        if not cache:
            return {"present": False, "status": "none"}
        try:
            saved = os.path.getmtime(path)
        except OSError:
            saved = 0
        fields, adopted, hashes = 0, set(), set()
        for hit in cache.values():
            try:
                fields += len(hit["ns"].get("fields", []))
                if hit.get("adopted"):
                    adopted.add(hit["adopted"])
                if hit.get("hash"):
                    hashes.add(int(hit["hash"]))
            except (KeyError, TypeError, AttributeError, ValueError):
                pass
        state = self._schema_state.get(dest)
        from_state = False
        if adopted:
            status = "adopted"
        elif node_hash is not None and len(hashes) == 1:
            # Firmware 1.86: ein Gesamt-Hash für alle Namespaces, direkt mit dem Hash aus GetInfo vergleichbar
            status = "current" if hashes == {int(node_hash)} else "stale"
        elif state is not None:
            # Neuere Firmware: je Namespace ein Hash (GetCapabilities). Der Gesamt-Hash aus GetInfo ändert sich bei
            # jedem Commit und je Node und taugt nicht zum Vergleich; maßgeblich ist der letzte Lauf von schema().
            status = "stale" if state["stale"] else "current"
            from_state = True
        elif node_hash is not None:
            status = "unchecked"  # Namespace-Hashes werden erst beim Öffnen des Nodes geprüft (kostet eine Anfrage mehr)
        else:
            status = "unknown"
        out = {"present": True, "status": status, "namespaces": len(cache), "fields": fields, "saved": saved,
               "adopted": sorted(adopted)}
        if from_state:
            out["stale_namespaces"] = state["stale"]
            out["checked"] = state["at"]
        return out

    def _types(self, dest, identity):
        cache = self._load_json(self._cache_path(dest), {})
        if not cache:
            self.schema(dest, identity, allow_fetch=False)  # ohne Cache: SchemaMissing statt GetSchema über Funk
            cache = self._load_json(self._cache_path(dest), {})
        types = {}
        for nid, hit in cache.items():
            for f in hit["ns"]["fields"]:
                types[(int(nid), f["id"])] = f
        return types

    def overview(self, dest, identity, allow_fetch=True):
        """Alles für die Anzeige: Info, Schema, Werte, Entwürfe und Cache-Zustand."""
        info = self.info(dest, identity)
        if allow_fetch:
            # Werte vor dem (großen) Schema holen: direkt nach einer langen Schema-Übertragung blieben Antworten aus
            st = self.get_state(dest, identity, None, True)
            schema = self.schema(dest, identity, False, info.get("schema_hash"))
        else:
            # Kein GetSchema über Funk: fehlt etwas, scheitert das sofort (SchemaMissing), vor dem teuren GetState
            schema = self.schema(dest, identity, False, info.get("schema_hash"), allow_fetch=False)
            st = self.get_state(dest, identity, None, True)
        return {"info": info, "schema": schema, "values": st["values"], "drafts": st["drafts"],
                "cache": self.cache_info(dest)}

    def ping(self, dest, identity):
        """Verbindungstest: Link und GetInfo, nichts weiter (kein Schema, kein GetState)."""
        dest = self._check(dest)
        t0 = time.time()
        info = self.info(dest, identity)
        seconds = time.time() - t0
        link = self._links.get((dest, identity or ""))
        hops = None
        try:
            target = getattr(getattr(link, "destination", None), "hash", None) or bytes.fromhex(dest)
            hops = self.RNS.Transport.hops_to(target)
            if hops == self.RNS.Transport.PATHFINDER_M:
                hops = None
        except Exception:
            pass
        rtt = getattr(link, "rtt", None) if link is not None else None
        return {"info": info, "seconds": round(seconds, 1), "rtt": round(rtt, 2) if rtt else None, "hops": hops,
                "cache": self.cache_info(dest, info.get("schema_hash"))}

    def get_state(self, dest, identity, ns=None, drafts=True):
        payload = {}
        if ns:
            payload[GK_NS_FILTER] = [int(n) for n in ns]
        if drafts:
            payload[GK_DRAFT] = True
        body = self.call(dest, identity, OP["GetState"], payload or None, timeout=90) or {}
        if not isinstance(body, dict):
            body = {}
        vals = body.get(GSK_VALUES)
        wrapped = all(k in (GSK_VALUES, GSK_DRAFTS, GSK_HASH) for k in body) and (
            vals is None or (isinstance(vals, dict) and all(isinstance(v, dict) for v in vals.values())))
        if wrapped:
            self._flat.pop((dest or "").strip().lower(), None)
            res = {
                "values": _ns_map(vals),
                "drafts": _ns_map(body.get(GSK_DRAFTS)),
                "hash": body.get(GSK_HASH),
            }
        else:
            # Flaches Format (ältere Firmware): {ns: {feld: wert}}
            self._flat[(dest or "").strip().lower()] = True
            res = {"values": _ns_map(body), "drafts": {}, "hash": None}
        self._learn_name(dest, res["values"])
        return res

    @staticmethod
    def _validate(f, value):
        """Prüft einen Wert gegen die Grenzen aus dem Schema, bevor er über Funk geschickt wird."""
        t, name = f["type"], f["name"]
        if t in ("int", "float"):
            try:
                v = int(value) if t == "int" else float(value)
            except (TypeError, ValueError):
                raise ProvisionError("'%s': Zahl erwartet" % name)
            lo, hi = (f.get("min_i"), f.get("max_i")) if t == "int" else (f.get("min_f"), f.get("max_f"))
            if (lo is not None and v < lo) or (hi is not None and v > hi):
                raise ProvisionError("'%s': %s liegt außerhalb des Bereichs %s … %s" % (name, v, lo, hi))
        elif t == "enum":
            try:
                v = int(value)
            except (TypeError, ValueError):
                raise ProvisionError("'%s': ungültige Auswahl" % name)
            if f.get("enum_values") is not None and v not in f["enum_values"]:
                raise ProvisionError("'%s': Auswahl %s ist nicht erlaubt" % (name, v))
        elif t == "string":
            if f.get("max_len") and len(str(value or "").encode("utf-8")) > f["max_len"]:
                raise ProvisionError("'%s': Text länger als %d Byte" % (name, f["max_len"]))
        elif t == "byteslist":
            items = [str(x) for x in (value or [])]
            if f.get("max_count") is not None and len(items) > f["max_count"]:
                raise ProvisionError("'%s': höchstens %d Einträge erlaubt, übergeben %d" % (name, f["max_count"], len(items)))
            size = f.get("element_size")
            for it in items:
                if not re.fullmatch(r"[0-9a-fA-F]+", it) or len(it) % 2:
                    raise ProvisionError("'%s': '%s' ist kein Hex-Wert" % (name, it))
                if size and len(it) != int(size) * 2:
                    raise ProvisionError("'%s': '%s' hat %d statt %d Hex-Zeichen" % (name, it, len(it), int(size) * 2))
            if len({i.lower() for i in items}) != len(items):
                raise ProvisionError("'%s': doppelte Einträge" % name)

    def set_state(self, dest, identity, changes):
        """changes: {nsId: {fieldId: wert}} – Typen kommen aus dem Schema des Nodes."""
        dest = self._check(dest)
        if not isinstance(changes, dict) or not changes:
            raise ProvisionError("Keine Änderungen übergeben")
        types = self._types(dest, identity)
        wire = {}
        for ns, fields in changes.items():
            if not isinstance(fields, dict):
                raise ProvisionError("Ungültige Änderungen")
            wf = {}
            for fid, value in fields.items():
                f = types.get((int(ns), int(fid)))
                if f is None:
                    raise ProvisionError("Feld %s/%s ist im Schema unbekannt" % (ns, fid))
                if "read_only" in f["flags"]:
                    raise ProvisionError("Feld '%s' ist schreibgeschützt" % f["name"])
                self._validate(f, value)
                wf[int(fid)] = to_wire(f["type_id"], value)
            wire[int(ns)] = wf
        def send(flat):
            payload = wire if flat else {GK_STATE: wire}
            body = self.call(dest, identity, OP["SetState"], payload, timeout=90) or {}
            errors = [
                {"ns": int(e[0]), "field": int(e[1]), "code": int(e[2]), "error": ERR_NAMES.get(int(e[2]), "Unknown")}
                for e in (body.get(SK_FIELD_ERRORS) or [])
            ]
            return body, errors

        known = self._flat.get(dest)
        body, errors = send(bool(known))
        if known is None and not int(body.get(SK_APPLIED, 0) or 0) and errors and all(
                e["ns"] == GK_STATE and e["code"] == 3 for e in errors):
            # Der Node hat den Umschlag {3: {...}} als Namespace 3 gelesen: er erwartet das flache Format
            self._log("SetState: Node %s erwartet das flache Format, sende erneut" % dest)
            self._flat[dest] = True
            body, errors = send(True)
        return {"applied": int(body.get(SK_APPLIED, 0) or 0), "draft_reboot": bool(body.get(SK_DRAFT_REBOOT)), "field_errors": errors}

    def commit(self, dest, identity, ns=None):
        payload = {GK_NS_FILTER: [int(n) for n in ns]} if ns else None
        body = self.call(dest, identity, OP["Commit"], payload, timeout=90) or {}
        return {"applied": int(body.get(CK_APPLIED, 0) or 0), "needs_reboot": bool(body.get(CK_NEEDS_REBOOT))}

    def discard(self, dest, identity, ns=None):
        payload = [int(n) for n in ns] if ns else None
        body = self.call(dest, identity, OP["Discard"], payload, timeout=60) or {}
        return {"applied": int(body.get(CK_APPLIED, 0) or 0)}

    def factory_reset(self, dest, identity):
        body = self.call(dest, identity, OP["FactoryReset"], timeout=60) or {}
        return {"needs_reboot": bool(body.get(CK_NEEDS_REBOOT))}

    def reboot(self, dest, identity):
        acked = True
        try:
            self.call(dest, identity, OP["Reboot"], timeout=10)
        except ProvisionError as exc:
            if not exc.timeout:
                # Kein Link, Node unbekannt, keine Identity oder Fehlerantwort des Nodes (z. B. UnknownOp):
                # der Neustart wurde nicht ausgelöst, also nicht als gesendet melden
                raise
            # Zeitüberschreitung nach gesendeter Anfrage: der Node startet vermutlich schon neu
            acked = False
        self._drop_link(self._check(dest), identity)
        return {"acked": acked}

    def _touch(self, key):
        with self._lock:
            if key in self._links:
                self._last_used[key] = time.time()

    def _reap_loop(self):
        """Baut Management-Links ab, die länger als IDLE_TIMEOUT unbenutzt sind (nie während einer laufenden Anfrage)."""
        while not self._stop.wait(15):
            now = time.time()
            with self._lock:
                idle = [k for k in list(self._links) if now - self._last_used.get(k, 0) > IDLE_TIMEOUT]
                locks = {k: self._call_locks.get(k[0]) for k in idle}
            for key in idle:
                cl = locks.get(key)
                if cl is not None and cl.locked():
                    continue
                self._log("Link zu %s nach %s s Leerlauf abgebaut" % (key[0], IDLE_TIMEOUT))
                self._drop_link(*key)

    def disconnect(self, dest, identity=None):
        """Baut den Management-Link zum Node ab (ohne Identity: alle Links zu diesem Node)."""
        dest = self._check(dest)
        with self._lock:
            keys = [k for k in self._links if k[0] == dest and (not identity or k[1] == identity)]
        for key in keys:
            self._log("Link zu %s auf Wunsch getrennt (Identity %s)" % (key[0], key[1] or "-"))
            self._drop_link(*key)
        if not keys:
            self._log("Trennen: kein offener Link zu %s" % dest)
        return {"closed": len(keys)}

    def flush(self):
        """Gebündelte Schreibvorgänge sofort ausführen."""
        self._nodes_writer.flush()

    def shutdown(self):
        self._stop.set()
        self.flush()
        for key in list(self._links):
            self._drop_link(*key)


class _MgmtAnnounces:
    aspect_filter = "rnstransport.remote.management"

    def __init__(self, backend):
        self.backend = backend

    def received_announce(self, destination_hash, announced_identity, app_data):
        self.backend._note(destination_hash.hex())
