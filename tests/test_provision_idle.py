"""Link-Abbau (Trennen, Leerlauf) und GetState-Formate der Provisionierung."""
import tempfile
import threading
import time
import types
import unittest

from bindu import provision as P

DEST = "a" * 32


class _Link:
    def __init__(self):
        self.down = False

    def teardown(self):
        self.down = True


def _backend():
    rns = types.SimpleNamespace(log=lambda *a: None, LOG_ERROR=1, LOG_VERBOSE=2)
    nomad = types.SimpleNamespace(RNS=rns, data_dir=tempfile.mkdtemp())
    return P.ProvisionBackend(nomad, wire=lambda *a: None)


def _one_round(backend):
    rounds = [False, True]
    backend._stop = types.SimpleNamespace(wait=lambda _t: rounds.pop(0), set=lambda: None)
    backend._reap_loop()


class DisconnectTest(unittest.TestCase):
    def test_disconnect_closes_only_matching_identity(self):
        b = _backend()
        mine, other = _Link(), _Link()
        b._links[(DEST, "bindu")] = mine
        b._links[(DEST, "other")] = other
        self.assertEqual(b.disconnect(DEST, "bindu"), {"closed": 1})
        self.assertTrue(mine.down)
        self.assertFalse(other.down)
        self.assertEqual(b.disconnect(DEST, "bindu"), {"closed": 0})

    def test_disconnect_without_identity_closes_all(self):
        b = _backend()
        links = [_Link(), _Link()]
        b._links[(DEST, "a")], b._links[(DEST, "b")] = links
        self.assertEqual(b.disconnect(DEST), {"closed": 2})
        self.assertTrue(all(l.down for l in links))

    def test_disconnect_rejects_bad_hash(self):
        with self.assertRaises(P.ProvisionError):
            _backend().disconnect("xyz")


class IdleReaperTest(unittest.TestCase):
    def setUp(self):
        self._old = P.IDLE_TIMEOUT
        P.IDLE_TIMEOUT = 0.05

    def tearDown(self):
        P.IDLE_TIMEOUT = self._old

    def test_idle_link_is_closed(self):
        b = _backend()
        link = _Link()
        b._links[(DEST, "m")] = link
        b._touch((DEST, "m"))
        time.sleep(0.15)
        _one_round(b)
        self.assertTrue(link.down)
        self.assertEqual(b._links, {})

    def test_fresh_link_is_kept(self):
        b = _backend()
        P.IDLE_TIMEOUT = 60
        link = _Link()
        b._links[(DEST, "m")] = link
        b._touch((DEST, "m"))
        _one_round(b)
        self.assertFalse(link.down)

    def test_link_with_running_request_is_kept(self):
        b = _backend()
        link = _Link()
        b._links[(DEST, "m")] = link
        b._last_used[(DEST, "m")] = 0
        lock = threading.Lock()
        lock.acquire()
        b._call_locks[DEST] = lock
        _one_round(b)
        self.assertFalse(link.down)


class GetStateFormatTest(unittest.TestCase):
    def _state(self, body):
        b = _backend()
        b.call = lambda *a, **k: body
        return b.get_state(DEST, "m")

    def test_wrapped_format(self):
        st = self._state({P.GSK_VALUES: {100: {1: True}}, P.GSK_DRAFTS: {100: {5: "x"}}, P.GSK_HASH: 7})
        self.assertEqual(st["values"], {100: {1: True}})
        self.assertEqual(st["drafts"], {100: {5: "x"}})
        self.assertEqual(st["hash"], 7)

    def test_flat_format(self):
        st = self._state({100: {1: True, 5: "microX1"}, 103: {}})
        self.assertEqual(st["values"], {100: {1: True, 5: "microX1"}, 103: {}})
        self.assertEqual(st["drafts"], {})
        self.assertIsNone(st["hash"])


if __name__ == "__main__":
    unittest.main()
