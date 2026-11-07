"""Tests für 0.3.14: Logdatei, gebündeltes Schreiben, GetSchema-Schutz, Schema übernehmen, Verbindungstest, Pfad auffrischen."""

import json
import logging
import os
import shutil
import tempfile
import threading
import time
import types
import unittest

from bindu.cli import setup_logging
from bindu.nomad import DeferredWriter, NomadBackend
from bindu.provision import ProvisionBackend, ProvisionError, SchemaMissing

from .fakedevice import FakeDevice
from .test_provision import FakeNomad

A = "ab" * 16
B = "cd" * 16


def _load(path):
    with open(path) as fh:
        return json.load(fh)


def _save(path, obj):
    with open(path, "w") as fh:
        json.dump(obj, fh)


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bindu-v14-")
        self.dev = FakeDevice()
        self.dev.legacy_caps = True  # wie die echten Nodes (Firmware 1.86): nur IDs, ein Gesamt-Hash
        self.p = ProvisionBackend(FakeNomad(self.tmp), wire=lambda d, i, data, t: self.dev.handle(data))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class DeferredWriterTests(unittest.TestCase):
    def test_coalesces_and_flushes(self):
        writes = []
        w = DeferredWriter(lambda: writes.append(1), delay=0.15)
        for _ in range(20):
            w.mark()
        self.assertEqual(writes, [])  # nichts sofort
        time.sleep(0.4)
        self.assertEqual(len(writes), 1)  # 20 Wünsche, ein Schreibvorgang
        w.mark()
        w.flush()
        self.assertEqual(len(writes), 2)  # flush schreibt sofort
        w.flush()
        self.assertEqual(len(writes), 2)  # nichts Neues

    def test_nodes_pruned(self):
        import bindu.nomad as N

        b = NomadBackend.__new__(NomadBackend)
        b._lock = threading.Lock()
        b._nodes = {}
        b._nodes_writer = types.SimpleNamespace(mark=lambda: None)
        old = N.MAX_NODES
        N.MAX_NODES = 5
        try:
            for i in range(8):
                b._nodes["%032x" % i] = {"name": str(i), "last": i}
            b._note_node("f" * 32, "neu")
        finally:
            N.MAX_NODES = old
        self.assertEqual(len(b._nodes), 5)
        self.assertIn("f" * 32, b._nodes)
        self.assertNotIn("%032x" % 0, b._nodes)  # die ältesten fliegen raus


class LogFileTests(_Base):
    def test_events_reach_logfile(self):
        path = setup_logging(os.path.join(self.tmp, "sub", "bindu.log"))
        self.assertTrue(path and os.path.exists(path))
        try:
            self.p._log("Testzeile eins")
            self.p._log("Fehlerzeile", True)
            for h in logging.getLogger("bindu").handlers:
                h.flush()
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        finally:
            for h in list(logging.getLogger("bindu").handlers):
                logging.getLogger("bindu").removeHandler(h)
                h.close()
        self.assertIn("INFO bindu.provision: Testzeile eins", text)
        self.assertIn("ERROR bindu.provision: Fehlerzeile", text)

    def test_unwritable_path_is_not_fatal(self):
        blocker = os.path.join(self.tmp, "datei")
        open(blocker, "w").close()
        self.assertIsNone(setup_logging(os.path.join(blocker, "x", "bindu.log")))


class SchemaProtectionTests(_Base):
    def test_no_getschema_without_cache(self):
        with self.assertRaises(SchemaMissing) as cm:
            self.p.overview(A, "m", allow_fetch=False)
        self.assertEqual(cm.exception.donors, [])
        self.assertNotIn(1, self.dev.log)  # kein GetSchema
        self.assertNotIn(4, self.dev.log)  # und auch kein GetState: schnell scheitern

    def test_set_state_without_cache_does_not_fetch(self):
        with self.assertRaises(SchemaMissing):
            self.p.set_state(A, "m", {1: {1: "X"}})
        self.assertNotIn(1, self.dev.log)

    def test_fetch_on_request_then_cached(self):
        res = self.p.overview(A, "m", allow_fetch=True)
        self.assertIn(1, self.dev.log)
        self.assertEqual(res["cache"]["status"], "current")
        self.dev.log.clear()
        res = self.p.overview(A, "m", allow_fetch=False)  # jetzt aus dem Cache
        self.assertNotIn(1, self.dev.log)
        self.assertEqual(res["cache"]["namespaces"], 2)

    def test_donor_adoption(self):
        self.p.overview(A, "m", allow_fetch=True)
        with self.assertRaises(SchemaMissing) as cm:
            self.p.overview(B, "m", allow_fetch=False)
        self.assertEqual([d["dest"] for d in cm.exception.donors], [A])
        self.dev.log.clear()
        self.assertEqual(self.p.adopt_schema(B, "m", A)["namespaces"], 2)
        self.assertNotIn(1, self.dev.log)  # übernommen, ohne GetSchema
        res = self.p.overview(B, "m", allow_fetch=False)
        self.assertEqual(res["cache"]["status"], "adopted")
        self.assertEqual(res["cache"]["adopted"], [A])
        self.assertNotIn(1, self.dev.log)
        # ein übernommenes Schema ist kein Spender für weitere Nodes
        c = "ef" * 16
        with self.assertRaises(SchemaMissing) as cm:
            self.p.overview(c, "m", allow_fetch=False)
        self.assertEqual([d["dest"] for d in cm.exception.donors], [A])

    def test_adopt_refuses_other_namespaces(self):
        self.p.overview(A, "m", allow_fetch=True)
        path = os.path.join(self.p.cache_dir, A + ".json")
        cache = _load(path)
        cache.pop("2")  # A kennt nur noch Namespace 1: passt nicht mehr zu B (1 und 2)
        _save(path, cache)
        with self.assertRaises(ProvisionError):
            self.p.adopt_schema(B, "m", A)
        with self.assertRaises(ProvisionError):
            self.p.adopt_schema(B, "m", B)  # nicht von sich selbst

    def test_adopt_keeps_real_entries(self):
        self.p.overview(A, "m", allow_fetch=True)
        self.p.overview(B, "m", allow_fetch=True)
        before = _load(os.path.join(self.p.cache_dir, B + ".json"))
        self.p.adopt_schema(B, "m", A)
        self.assertEqual(_load(os.path.join(self.p.cache_dir, B + ".json")), before)

    def test_cache_info_states(self):
        self.assertEqual(self.p.cache_info(A)["status"], "none")
        self.p.overview(A, "m", allow_fetch=True)
        self.assertEqual(self.p.cache_info(A, 1234)["status"], "current")
        self.assertEqual(self.p.cache_info(A, 9999)["status"], "stale")  # Node meldet anderen Hash
        self.assertTrue(self.p.cache_info(A)["fields"] >= 6)


class PingTests(_Base):
    def test_ping_only_getinfo(self):
        self.p.overview(A, "m", allow_fetch=True)
        self.dev.log.clear()
        res = self.p.ping(A, "m")
        self.assertEqual(self.dev.log, [2])  # nur GetInfo
        self.assertEqual(res["info"]["firmware"], "1.86")
        self.assertEqual(res["cache"]["status"], "current")
        self.assertIn("seconds", res)

    def test_hanging_node_hint(self):
        def silent(dest, ident, data, timeout):
            raise ProvisionError("Zeitüberschreitung", timeout=True)

        p = ProvisionBackend(FakeNomad(self.tmp), wire=silent)
        with self.assertRaises(ProvisionError) as cm:
            p.info(A, "m")
        self.assertIn("Der Link steht", str(cm.exception))
        self.assertTrue(cm.exception.timeout)


class PathRefreshTests(unittest.TestCase):
    def _backend(self, age, events):
        import bindu.provision as P

        class Link:
            ACTIVE = 1
            establishment_timeout = 0.05
            rtt = 0.01

            def __init__(self, dest, established_callback=None, closed_callback=None):
                events.append("link")
                established_callback(self)

            def teardown(self):
                pass

            def identify(self, i):
                pass

        table = {bytes.fromhex(A): [time.time() - age]}
        transport = types.SimpleNamespace(
            has_path=lambda h: True,
            request_path=lambda h: events.append("path?"),
            hops_to=lambda h: 1,
            PATHFINDER_M=128,
            path_table=table,
        )
        rns = types.SimpleNamespace(log=lambda *a: None, LOG_ERROR=1, LOG_VERBOSE=2, Link=Link, Transport=transport)
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        nomad = types.SimpleNamespace(RNS=rns, data_dir=tmp, _load_identity=lambda n: None)
        b = ProvisionBackend(nomad, wire=lambda *a: None)
        b._resolve_destination = lambda d, t: types.SimpleNamespace(hash=bytes.fromhex(d))
        self.addCleanup(setattr, P, "PATH_REFRESH_WAIT", P.PATH_REFRESH_WAIT)
        P.PATH_REFRESH_WAIT = 0.2
        return b

    def test_old_path_is_refreshed_once(self):
        events = []
        b = self._backend(4000, events)
        b._get_link(A, "m", 60)
        self.assertEqual(events, ["path?", "link"])  # erst auffrischen, dann Link

    def test_fresh_path_is_left_alone(self):
        events = []
        b = self._backend(60, events)
        b._get_link(A, "m", 60)
        self.assertEqual(events, ["link"])


class WebTests(_Base):
    def setUp(self):
        super().setUp()
        import http.client

        from bindu.web import make_server

        self.http = http.client

        class Nomad:
            def state(self):
                return {"nodes": [], "identities": []}

        self.server = make_server(Nomad(), "127.0.0.1", 0, None, self.p, {"version": "9.9.9", "file": "/x"})
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def call(self, method, path, obj=None):
        c = self.http.HTTPConnection("127.0.0.1", self.port)
        body = json.dumps(obj) if obj is not None else None
        c.request(method, path, body, {"Content-Type": "application/json", "Host": "127.0.0.1:%d" % self.port})
        return json.loads(c.getresponse().read())

    def test_state_carries_app_info(self):
        res = self.call("GET", "/api/state")
        self.assertEqual(res["app"]["version"], "9.9.9")

    def test_overview_needs_confirmation_for_schema(self):
        res = self.call("POST", "/api/node/overview", {"dest": A, "identity": "m"})
        self.assertFalse(res["ok"])
        self.assertTrue(res["need_schema"])
        self.assertNotIn(1, self.dev.log)
        res = self.call("POST", "/api/node/overview", {"dest": A, "identity": "m", "fetch_schema": True})
        self.assertTrue(res["ok"])
        self.assertEqual(res["cache"]["namespaces"], 2)

    def test_donor_offered_and_adopted_via_web(self):
        self.call("POST", "/api/node/overview", {"dest": A, "identity": "m", "fetch_schema": True})
        res = self.call("POST", "/api/node/overview", {"dest": B, "identity": "m"})
        self.assertEqual([d["dest"] for d in res["donors"]], [A])
        self.assertTrue(self.call("POST", "/api/node/adopt", {"dest": B, "identity": "m", "donor": A})["ok"])
        self.assertTrue(self.call("POST", "/api/node/overview", {"dest": B, "identity": "m"})["ok"])

    def test_schema_refresh_needs_confirm(self):
        res = self.call("POST", "/api/node/schema", {"dest": A, "identity": "m", "refresh": True})
        self.assertFalse(res["ok"])
        self.assertNotIn(1, self.dev.log)
        res = self.call("POST", "/api/node/schema", {"dest": A, "identity": "m", "refresh": True, "confirm": True})
        self.assertTrue(res["ok"])
        self.assertIn(1, self.dev.log)

    def test_ping_route(self):
        res = self.call("POST", "/api/node/ping", {"dest": A, "identity": "m"})
        self.assertTrue(res["ok"])
        self.assertEqual(self.dev.log, [2])


if __name__ == "__main__":
    unittest.main()
