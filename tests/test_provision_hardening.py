"""0.3.11: portionierter Schema-Abruf, Cache-Schutz, Retry, Wertprüfung."""
import shutil
import tempfile
import unittest

from bindu.provision import ProvisionBackend, ProvisionError

from .fakedevice import FakeDevice
from .test_provision import FakeNomad

DEST = "ab" * 16


class HardeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bindu-hard-")
        self.dev = FakeDevice()
        self.fail = 0

        def wire(d, i, data, t):
            if self.fail:
                self.fail -= 1
                raise ProvisionError("Zeitüberschreitung – keine Antwort vom Node", timeout=True)
            return self.dev.handle(data)

        ProvisionBackend.SCHEMA_PAUSE = 0
        self.p = ProvisionBackend(FakeNomad(self.tmp), wire=wire)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_schema_in_chunks_and_cached(self):
        self.p.SCHEMA_CHUNK = 1
        ns = self.p.schema(DEST, "m")
        self.assertEqual(len(ns), 2)
        self.assertEqual(self.dev.log.count(1), 2)  # je Namespace ein GetSchema
        self.dev.log.clear()
        self.p.schema(DEST, "m")
        self.assertNotIn(1, self.dev.log)

    def test_read_is_retried(self):
        self.fail = 1
        self.assertEqual(self.p.info(DEST, "m")["firmware"], "1.86")

    def test_read_gives_up_after_all_retries(self):
        self.fail = 4  # 1 Versuch + 3 Wiederholungen
        with self.assertRaises(ProvisionError):
            self.p.info(DEST, "m")

    def test_set_state_is_retried(self):
        self.p.schema(DEST, "m")
        self.fail = 2
        res = self.p.set_state(DEST, "m", {2: {1: 868000000}})
        self.assertEqual(res["applied"], 1)
        self.assertEqual(self.dev.drafts[2][1], 868000000)

    def test_discard_is_retried(self):
        self.p.schema(DEST, "m")
        self.p.set_state(DEST, "m", {2: {1: 868000000}})
        self.fail = 1
        self.assertEqual(self.p.discard(DEST, "m")["applied"], 1)

    def test_commit_is_not_retried(self):
        self.p.schema(DEST, "m")
        self.p.set_state(DEST, "m", {2: {1: 868000000}})
        self.fail = 1
        with self.assertRaises(ProvisionError):
            self.p.commit(DEST, "m")
        self.assertEqual(self.dev.drafts[2][1], 868000000)  # Entwurf unverändert, nichts committet

    def test_validation(self):
        self.p.schema(DEST, "m")
        with self.assertRaises(ProvisionError):
            self.p.set_state(DEST, "m", {2: {1: 1}})            # unter min
        with self.assertRaises(ProvisionError):
            self.p.set_state(DEST, "m", {2: {2: 7}})            # kein Enum-Wert
        with self.assertRaises(ProvisionError):
            self.p.set_state(DEST, "m", {1: {1: "x" * 40}})     # zu lang
        with self.assertRaises(ProvisionError):
            self.p.set_state(DEST, "m", {1: {3: ["01" * 8]}})   # falsche Elementlänge
        with self.assertRaises(ProvisionError):
            self.p.set_state(DEST, "m", {1: {3: ["01" * 16, "01" * 16]}})  # doppelt


if __name__ == "__main__":
    unittest.main()


class LinkSetupTests(unittest.TestCase):
    """Nie vor Ablauf der Reticulum-Frist abbrechen; fehlender Pfad wird vor jedem Versuch neu angefragt."""

    def test_no_early_teardown_and_path_requested(self):
        import types

        import bindu.provision as P

        events = []

        class Link:
            ACTIVE = 1
            establishment_timeout = 0.05

            def __init__(self, dest, established_callback=None, closed_callback=None):
                events.append("link")
                self.cb = established_callback
                self.n = events.count("link")
                if self.n == 2:
                    self.cb(self)

            def teardown(self):
                events.append("teardown")

            def identify(self, i):
                pass

            rtt = 0.01

        paths = {"have": False}
        transport = types.SimpleNamespace(
            has_path=lambda h: paths["have"],
            request_path=lambda h: (events.append("path?"), paths.update(have=True)),
            hops_to=lambda h: 1,
            PATHFINDER_M=128,
        )
        rns = types.SimpleNamespace(log=lambda *a: None, LOG_ERROR=1, LOG_VERBOSE=2, Link=Link, Transport=transport)
        tmp = tempfile.mkdtemp()
        nomad = types.SimpleNamespace(RNS=rns, data_dir=tmp, _load_identity=lambda n: None)
        b = ProvisionBackend(nomad, wire=lambda *a: None)
        b._resolve_destination = lambda d, t: types.SimpleNamespace(hash=bytes.fromhex(d))
        # nach dem ersten (erfolglosen) Versuch verwirft Reticulum den Pfad
        orig_teardown = Link.teardown
        Link.teardown = lambda self: (orig_teardown(self), paths.update(have=False))
        try:
            link = b._get_link(DEST, "m", 60)
        finally:
            Link.teardown = orig_teardown
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(link.n, 2)
        self.assertEqual(events.count("path?"), 2)  # vor Versuch 1 und nach dem Pfadverlust vor Versuch 2
