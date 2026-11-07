"""LXMF-Chat: eigene Adresse, Senden und Empfangen, native LXMF-Speicherung.

Speicherung (LXMF-konform): Jede Nachricht liegt als LXMF-Container-Datei in
``<data>/messages/<64-stelliger Hex-Hash>`` (``LXMessage.write_to_directory``,
msgpack mit state, lxmf_bytes, transport_encrypted, transport_encryption,
method). Beim Start wird daraus ein Index im Speicher aufgebaut. Alles, was
LXMF selbst nicht kennt (gelesen/ungelesen, gehörte Adressen), liegt in
``chat_state.json`` und ``contacts.json``.

Die Richtung einer Nachricht ergibt sich aus der Quelladresse: ist sie die
eigene, ist die Nachricht ausgehend. (``LXMessage.unpack_from_file`` setzt
``incoming`` immer auf True, darauf kann man sich nicht verlassen.)
"""

import json
import os
import re
import threading
import time

from .nomad import HASH_RE, DeferredWriter, NomadError

MSG_FILE_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_CONTENT = 20000
PREVIEW_LEN = 80
MAX_CONTACTS = 200


class ChatError(NomadError):
    """Fehler mit Klartext für die Oberfläche."""


def _atomic_json(path, obj):
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as fh:
            json.dump(obj, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _load_json(path, default):
    try:
        with open(path) as fh:
            data = json.load(fh)
        return data if isinstance(data, type(default)) else default
    except (OSError, ValueError):
        return default


class ChatBackend:
    WRITE_DELAY = 30.0  # Sekunden: gehörte Adressen werden gebündelt gespeichert

    def __init__(self, RNS, data_dir, display_name="bindu"):
        import LXMF  # erst hier, damit Renderer/Tests ohne LXMF laufen

        self.RNS = RNS
        self.LXMF = LXMF
        self.M = LXMF.LXMessage
        self.display_name = display_name

        self.msg_dir = os.path.join(data_dir, "messages")
        self.state_file = os.path.join(data_dir, "chat_state.json")
        self.contacts_file = os.path.join(data_dir, "contacts.json")
        self.identity_file = os.path.join(data_dir, "lxmf.id")
        os.makedirs(self.msg_dir, mode=0o700, exist_ok=True)

        self._lock = threading.RLock()
        self._messages = {}  # id -> Datensatz
        self._unread = {}  # peer -> Menge von Nachrichten-IDs
        self._contacts = _load_json(self.contacts_file, {})
        self._contacts_writer = DeferredWriter(self._save_contacts, self.WRITE_DELAY)
        self._load_unread()

        identity = self._load_or_create_identity()
        self.router = LXMF.LXMRouter(identity=identity, storagepath=data_dir)
        self.destination = self.router.register_delivery_identity(identity, display_name=display_name)
        if self.destination is None:
            raise ChatError("LXMF-Adresse konnte nicht angelegt werden")
        self.address = self.destination.hash.hex()
        self.router.register_delivery_callback(self._on_inbound)
        RNS.Transport.register_announce_handler(_LxmfAnnounces(self))

        self._load_index()

    # ---------- Identität und Adresse ----------

    def _load_or_create_identity(self):
        RNS = self.RNS
        if os.path.exists(self.identity_file):
            identity = RNS.Identity.from_file(self.identity_file)
            if identity is None:
                raise ChatError("LXMF-Identity %s nicht lesbar" % self.identity_file)
            return identity
        identity = RNS.Identity()
        identity.to_file(self.identity_file)
        os.chmod(self.identity_file, 0o600)
        return identity

    def announce(self):
        self.router.announce(self.destination.hash)
        return {"address": self.address}

    # ---------- Persistenz ----------

    def _load_unread(self):
        data = _load_json(self.state_file, {})
        raw = data.get("unread", {}) if isinstance(data.get("unread", {}), dict) else {}
        self._unread = {p: set(ids) for p, ids in raw.items() if isinstance(ids, list)}

    def _save_unread(self):
        _atomic_json(self.state_file, {"unread": {p: sorted(ids) for p, ids in self._unread.items() if ids}})

    def _save_contacts(self):
        with self._lock:
            data = dict(self._contacts)
        _atomic_json(self.contacts_file, data)

    def _load_index(self):
        """Baut den Index aus den nativen LXMF-Dateien auf."""
        M = self.M
        messages = {}
        try:
            names = os.listdir(self.msg_dir)
        except OSError:
            names = []
        for name in names:
            if not MSG_FILE_RE.match(name):
                continue
            try:
                with open(os.path.join(self.msg_dir, name), "rb") as fh:
                    lxm = M.unpack_from_file(fh)
            except OSError:
                continue
            if lxm is None or lxm.hash is None:
                continue
            # Eine Nachricht, deren Zustellung beim Beenden noch lief, ist verloren.
            if self._is_outgoing(lxm) and lxm.state in (M.GENERATING, M.OUTBOUND, M.SENDING):
                lxm.state = M.FAILED
                lxm.write_to_directory(self.msg_dir)
            rec = self._record(lxm)
            messages[rec["id"]] = rec
        with self._lock:
            self._messages = messages
            # Ungelesen-Markierungen verwaisen lassen wir nicht
            for peer in list(self._unread):
                self._unread[peer] &= set(messages)

    # ---------- Datensätze ----------

    def _is_outgoing(self, lxm):
        return lxm.source_hash == self.destination.hash

    def _state_name(self, lxm):
        M = self.M
        if not self._is_outgoing(lxm):
            return "received"
        if lxm.state == M.DELIVERED:
            return "delivered"
        if lxm.state == M.SENT:
            return "sent"
        if lxm.state in (M.REJECTED, M.CANCELLED, M.FAILED):
            return "failed"
        return "pending"

    def _record(self, lxm):
        out = self._is_outgoing(lxm)
        try:
            title = lxm.title_as_string()
        except Exception:
            title = ""
        content = lxm.content_as_string()
        if content is None:
            content = "(Inhalt nicht als Text lesbar)"
        return {
            "id": lxm.hash.hex(),
            "peer": (lxm.destination_hash if out else lxm.source_hash).hex(),
            "out": out,
            "ts": float(lxm.timestamp or 0),
            "title": title or "",
            "content": content,
            "state": self._state_name(lxm),
            "verified": bool(getattr(lxm, "signature_validated", False)),
        }

    def _store(self, lxm, inbound=False):
        """Schreibt die Nachricht als natives LXMF-File und aktualisiert den Index."""
        if lxm.hash is None:
            return
        if inbound:
            lxm.state = self.M.DELIVERED
        lxm.write_to_directory(self.msg_dir)
        rec = self._record(lxm)
        with self._lock:
            fresh = rec["id"] not in self._messages
            self._messages[rec["id"]] = rec
            if inbound and fresh:
                self._unread.setdefault(rec["peer"], set()).add(rec["id"])
                self._save_unread()
        if inbound and rec["peer"] not in self._contacts:
            self._note_contact(rec["peer"], "")

    def _on_inbound(self, message):
        try:
            self._store(message, inbound=True)
        except Exception as exc:  # RNS-Thread darf nicht sterben
            self.RNS.log("bindu: eingehende Nachricht nicht gespeichert: %s" % exc, self.RNS.LOG_ERROR)

    def _on_outbound_update(self, message):
        try:
            self._store(message)
        except Exception as exc:
            self.RNS.log("bindu: Zustellstatus nicht gespeichert: %s" % exc, self.RNS.LOG_ERROR)

    # ---------- Adressbuch (gehörte Announces) ----------

    def _note_contact(self, peer, name):
        if peer == self.address:
            return
        with self._lock:
            old = self._contacts.get(peer, {})
            self._contacts[peer] = {"name": name or old.get("name", ""), "last": int(time.time())}
            if len(self._contacts) > 4 * MAX_CONTACTS:
                keep = sorted(self._contacts.items(), key=lambda kv: kv[1].get("last", 0), reverse=True)
                self._contacts = dict(keep[: 2 * MAX_CONTACTS])
        self._contacts_writer.mark()

    def _name_of(self, peer):
        return self._contacts.get(peer, {}).get("name", "")

    # ---------- Abfragen für die Oberfläche ----------

    def conversations(self):
        with self._lock:
            by_peer = {}
            for rec in self._messages.values():
                by_peer.setdefault(rec["peer"], []).append(rec)
            result = []
            for peer, recs in by_peer.items():
                last = max(recs, key=lambda r: r["ts"])
                preview = (last["content"] or "").replace("\n", " ")
                result.append(
                    {
                        "peer": peer,
                        "name": self._name_of(peer),
                        "last_ts": last["ts"],
                        "preview": preview[:PREVIEW_LEN],
                        "last_out": last["out"],
                        "last_state": last["state"],
                        "unread": len(self._unread.get(peer, ())),
                    }
                )
        result.sort(key=lambda c: c["last_ts"], reverse=True)
        return result

    def contacts(self):
        with self._lock:
            items = [
                {"hash": h, "name": v.get("name", ""), "last": v.get("last", 0)} for h, v in self._contacts.items()
            ]
        items.sort(key=lambda c: c["last"], reverse=True)
        return items[:MAX_CONTACTS]

    def state(self):
        convs = self.conversations()
        return {
            "address": self.address,
            "name": self.display_name,
            "conversations": convs,
            "contacts": self.contacts(),
            "unread": sum(c["unread"] for c in convs),
        }

    def _check_peer(self, peer):
        peer = (peer or "").strip().lower()
        if not HASH_RE.match(peer):
            raise ChatError("LXMF-Adresse muss aus 32 Hex-Zeichen bestehen")
        return peer

    def open(self, peer):
        """Nachrichten einer Konversation (älteste zuerst); markiert sie als gelesen."""
        peer = self._check_peer(peer)
        with self._lock:
            msgs = sorted((dict(r) for r in self._messages.values() if r["peer"] == peer), key=lambda r: r["ts"])
            if self._unread.pop(peer, None):
                self._save_unread()
        return {"peer": peer, "name": self._name_of(peer), "messages": msgs}

    # ---------- Senden ----------

    def _recall_identity(self, dest_hash, timeout):
        RNS = self.RNS
        identity = RNS.Identity.recall(dest_hash)
        if identity is not None:
            return identity
        RNS.Transport.request_path(dest_hash)
        deadline = time.time() + timeout
        while identity is None and time.time() < deadline:
            time.sleep(0.2)
            identity = RNS.Identity.recall(dest_hash)
        return identity

    def send(self, peer, content, title="", timeout=20):
        peer = self._check_peer(peer)
        if peer == self.address:
            raise ChatError("Das ist deine eigene Adresse")
        content = (content or "").strip("\r\n")
        if not content.strip():
            raise ChatError("Nachricht ist leer")
        if len(content) > MAX_CONTENT:
            raise ChatError("Nachricht zu lang (höchstens %d Zeichen)" % MAX_CONTENT)

        RNS = self.RNS
        dest_hash = bytes.fromhex(peer)
        identity = self._recall_identity(dest_hash, timeout)
        if identity is None:
            raise ChatError(
                "Empfänger unbekannt: keine Antwort auf die Pfadanfrage. Er muss seine Adresse "
                "per Announce bekanntgeben, oder das Netz ist gerade nicht erreichbar."
            )
        destination = RNS.Destination(identity, RNS.Destination.OUT, RNS.Destination.SINGLE, "lxmf", "delivery")
        lxm = self.M(
            destination,
            self.destination,
            content,
            title=str(title or ""),
            desired_method=self.M.DIRECT,
        )
        lxm.register_delivery_callback(self._on_outbound_update)
        lxm.register_failed_callback(self._on_outbound_update)
        try:
            self.router.handle_outbound(lxm)
        except Exception as exc:
            raise ChatError("Senden nicht möglich: %s" % exc)
        self._store(lxm)
        return {"id": lxm.hash.hex()}

    def delete(self, message_id):
        message_id = (message_id or "").strip().lower()
        if not MSG_FILE_RE.match(message_id):
            raise ChatError("Ungültige Nachrichten-ID")
        with self._lock:
            self._messages.pop(message_id, None)
            for ids in self._unread.values():
                ids.discard(message_id)
            self._save_unread()
        try:
            os.unlink(os.path.join(self.msg_dir, message_id))
        except OSError:
            pass
        return {}

    def flush(self):
        """Gebündelte Schreibvorgänge sofort ausführen."""
        self._contacts_writer.flush()

    def shutdown(self):
        self.flush()
        try:
            self.router.exit_handler()
        except Exception:
            pass


class _LxmfAnnounces:
    aspect_filter = "lxmf.delivery"

    def __init__(self, chat):
        self.chat = chat

    def received_announce(self, destination_hash, announced_identity, app_data):
        try:
            name = self.chat.LXMF.display_name_from_app_data(app_data) or ""
        except Exception:
            name = ""
        self.chat._note_contact(destination_hash.hex(), name)
