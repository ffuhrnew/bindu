import http.client
import json
import os
import shutil
import tempfile
import threading
import unittest

from bindu.chat import ChatError
from bindu.web import make_server

try:
    import LXMF
    import RNS
except ImportError:  # Chat-Tests brauchen die echten Bibliotheken
    LXMF = RNS = None


class FakeNomad:
    def state(self):
        return {"nodes": [], "identities": []}


class FakeChat:
    def __init__(self):
        self.calls = []

    def state(self):
        return {"address": "a" * 32, "name": "t", "conversations": [], "contacts": [], "unread": 0}

    def open(self, peer):
        self.calls.append(("open", peer))
        return {"peer": peer, "name": "", "messages": []}

    def send(self, peer, content, title=""):
        self.calls.append(("send", peer, content, title))
        if content == "boom":
            raise ChatError("Nachricht ist leer")
        return {"id": "c" * 64}

    def announce(self):
        self.calls.append(("announce",))
        return {"address": "a" * 32}

    def delete(self, message_id):
        self.calls.append(("delete", message_id))
        return {}


class ChatWebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.chat = FakeChat()
        cls.server = make_server(FakeNomad(), "127.0.0.1", 0, cls.chat)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.bare = make_server(FakeNomad(), "127.0.0.1", 0)
        cls.bare_port = cls.bare.server_address[1]
        threading.Thread(target=cls.bare.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        for srv in (cls.server, cls.bare):
            srv.shutdown()
            srv.server_close()

    def call(self, port, method, path, obj=None):
        conn = http.client.HTTPConnection("127.0.0.1", port)
        body = json.dumps(obj).encode() if obj is not None else None
        headers = {"Host": "127.0.0.1:%d" % port}
        if body is not None:
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        return resp.status, data

    def test_state(self):
        status, data = self.call(self.port, "GET", "/api/chat")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["address"], "a" * 32)

    def test_send_open_announce_delete(self):
        _, data = self.call(self.port, "POST", "/api/chat/send", {"peer": "b" * 32, "content": "hi", "title": "t"})
        self.assertTrue(data["ok"])
        self.assertEqual(self.chat.calls[-1], ("send", "b" * 32, "hi", "t"))
        _, data = self.call(self.port, "POST", "/api/chat/open", {"peer": "b" * 32})
        self.assertEqual(data["messages"], [])
        _, data = self.call(self.port, "POST", "/api/chat/announce", {})
        self.assertTrue(data["ok"])
        _, data = self.call(self.port, "POST", "/api/chat/delete", {"id": "c" * 64})
        self.assertTrue(data["ok"])

    def test_chat_error_is_clear_text(self):
        status, data = self.call(self.port, "POST", "/api/chat/send", {"peer": "b" * 32, "content": "boom"})
        self.assertEqual(status, 200)
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "Nachricht ist leer")

    def test_unknown_action(self):
        status, _ = self.call(self.port, "POST", "/api/chat/nope", {})
        self.assertEqual(status, 404)

    def test_without_chat_backend(self):
        status, data = self.call(self.bare_port, "GET", "/api/chat")
        self.assertEqual(status, 404)
        self.assertFalse(data["ok"])
        status, _ = self.call(self.bare_port, "POST", "/api/chat/send", {})
        self.assertEqual(status, 404)


@unittest.skipIf(LXMF is None, "lxmf/rns nicht installiert")
class ChatBackendTests(unittest.TestCase):
    """Echte LXMF-Objekte; eine Reticulum-Instanz ohne Interfaces (nur ein Start je Prozess möglich)."""

    @classmethod
    def setUpClass(cls):
        from bindu.chat import ChatBackend

        cls.tmp = tempfile.mkdtemp(prefix="bindu-test-")
        cfg = os.path.join(cls.tmp, "rns")
        os.makedirs(cfg)
        with open(os.path.join(cfg, "config"), "w") as fh:
            fh.write("[reticulum]\n  enable_transport = No\n  share_instance = No\n\n[interfaces]\n")
        cls.reticulum = RNS.Reticulum(configdir=cfg, loglevel=RNS.LOG_CRITICAL)
        cls.data = os.path.join(cls.tmp, "data")
        os.makedirs(cls.data)
        cls.chat = ChatBackend(RNS, cls.data, "test")

        # Ein "Gegenüber", dessen Identity das Netz schon kennt
        cls.peer_identity = RNS.Identity()
        cls.peer_dest = RNS.Destination(cls.peer_identity, RNS.Destination.IN, RNS.Destination.SINGLE, "lxmf", "delivery")
        RNS.Identity.remember(None, cls.peer_dest.hash, cls.peer_identity.get_public_key())
        cls.peer = cls.peer_dest.hash.hex()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def incoming(self, text, title=""):
        mine = RNS.Destination(
            RNS.Identity.recall(self.chat.destination.hash),
            RNS.Destination.OUT,
            RNS.Destination.SINGLE,
            "lxmf",
            "delivery",
        )
        lxm = LXMF.LXMessage(mine, self.peer_dest, text, title=title)
        lxm.pack()
        self.assertTrue(self.chat.router.lxmf_delivery(lxm.packed, mine.type))
        return lxm

    def test_address_and_identity_file(self):
        self.assertRegex(self.chat.address, r"^[0-9a-f]{32}$")
        path = os.path.join(self.data, "lxmf.id")
        self.assertTrue(os.path.exists(path))
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_inbound_is_native_file_unread_and_open_marks_read(self):
        lxm = self.incoming("Hallo <b>Welt</b>", "Betreff")
        fname = os.path.join(self.chat.msg_dir, lxm.hash.hex())
        self.assertTrue(os.path.exists(fname))
        # natives LXMF-Format: mit LXMF selbst wieder lesbar
        with open(fname, "rb") as fh:
            back = LXMF.LXMessage.unpack_from_file(fh)
        self.assertEqual(back.content_as_string(), "Hallo <b>Welt</b>")
        self.assertEqual(back.hash, lxm.hash)

        convs = {c["peer"]: c for c in self.chat.conversations()}
        self.assertEqual(convs[self.peer]["unread"], 1)
        self.assertEqual(self.chat.state()["unread"], 1)
        res = self.chat.open(self.peer)
        rec = [m for m in res["messages"] if m["id"] == lxm.hash.hex()][0]
        self.assertFalse(rec["out"])
        self.assertEqual(rec["title"], "Betreff")
        self.assertEqual(rec["state"], "received")
        self.assertTrue(rec["verified"])
        self.assertEqual({c["peer"]: c for c in self.chat.conversations()}[self.peer]["unread"], 0)

    def test_duplicate_delivery_not_counted_twice(self):
        lxm = self.incoming("einmal")
        before = len([m for m in self.chat.open(self.peer)["messages"] if m["content"] == "einmal"])
        self.chat._on_inbound(LXMF.LXMessage.unpack_from_bytes(lxm.packed))
        after = len([m for m in self.chat.open(self.peer)["messages"] if m["content"] == "einmal"])
        self.assertEqual(before, after)
        self.assertEqual({c["peer"]: c for c in self.chat.conversations()}[self.peer]["unread"], 0)

    def test_send_status_and_reload_from_files(self):
        res = self.chat.send(self.peer, "ausgehend")
        mid = res["id"]
        rec = [m for m in self.chat.open(self.peer)["messages"] if m["id"] == mid][0]
        self.assertTrue(rec["out"])
        self.assertEqual(rec["state"], "pending")
        self.assertTrue(os.path.exists(os.path.join(self.chat.msg_dir, mid)))

        # Zustellbestätigung simulieren: derselbe Weg wie der LXMF-Callback
        pending = [m for m in self.chat.router.pending_outbound if m.hash.hex() == mid][0]
        pending.state = LXMF.LXMessage.DELIVERED
        self.chat._on_outbound_update(pending)
        rec = [m for m in self.chat.open(self.peer)["messages"] if m["id"] == mid][0]
        self.assertEqual(rec["state"], "delivered")

        # Index nur aus den Dateien neu aufbauen
        self.chat._messages.clear()
        self.chat._load_index()
        again = [m for m in self.chat.open(self.peer)["messages"] if m["id"] == mid][0]
        self.assertTrue(again["out"])
        self.assertEqual(again["state"], "delivered")
        self.assertEqual(again["content"], "ausgehend")

    def test_stale_pending_becomes_failed_on_reload(self):
        res = self.chat.send(self.peer, "unterbrochen")
        mid = res["id"]
        pending = [m for m in self.chat.router.pending_outbound if m.hash.hex() == mid][0]
        self.assertEqual(self.chat._state_name(pending), "pending")
        self.chat._messages.clear()
        self.chat._load_index()
        rec = [m for m in self.chat.open(self.peer)["messages"] if m["id"] == mid][0]
        self.assertEqual(rec["state"], "failed")

    def test_delete_removes_file_and_index(self):
        lxm = self.incoming("weg damit")
        mid = lxm.hash.hex()
        self.chat.delete(mid)
        self.assertFalse(os.path.exists(os.path.join(self.chat.msg_dir, mid)))
        self.assertNotIn(mid, [m["id"] for m in self.chat.open(self.peer)["messages"]])

    def test_validation(self):
        with self.assertRaises(ChatError):
            self.chat.send("xyz", "hi")
        with self.assertRaises(ChatError):
            self.chat.send(self.peer, "   \n")
        with self.assertRaises(ChatError):
            self.chat.send(self.chat.address, "an mich selbst")
        with self.assertRaises(ChatError):
            self.chat.send(self.peer, "x" * 20001)
        with self.assertRaises(ChatError):
            self.chat.delete("../../etc/passwd")

    def test_unknown_recipient_gives_clear_error(self):
        with self.assertRaises(ChatError) as cm:
            self.chat.send("d" * 32, "hi", timeout=0.3)
        self.assertIn("Empfänger unbekannt", str(cm.exception))

    def test_announce_handler_records_contact(self):
        handler = [h for h in RNS.Transport.announce_handlers if getattr(h, "aspect_filter", "") == "lxmf.delivery"]
        handler = [h for h in handler if hasattr(h, "chat")][0]
        app_data = LXMF.LXMRouter.get_announce_app_data(self.chat.router, self.chat.destination.hash)
        other = bytes.fromhex("e" * 32)
        handler.received_announce(other, None, app_data)
        names = {c["hash"]: c["name"] for c in self.chat.contacts()}
        self.assertEqual(names["e" * 32], "test")
        self.assertFalse(os.path.exists(os.path.join(self.data, "contacts.json")))  # gebündelt, nicht sofort
        self.chat._contacts_writer.flush()
        with open(os.path.join(self.data, "contacts.json")) as fh:
            self.assertIn("e" * 32, json.load(fh))


if __name__ == "__main__":
    unittest.main()
