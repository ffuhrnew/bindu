"""Tests für Abschnitt B: Cache-Status nach Namespace-Hashes (statt Gesamt-Hash) und Name aus GetState (ns 100, Feld 5)."""

import json
import os
import shutil
import tempfile
import unittest

from bindu.provision import ProvisionBackend, ProvisionError

from .fakedevice import FakeDevice
from .test_provision import FakeNomad

A = "ab" * 16


def _backend(tmp, dev):
    return ProvisionBackend(FakeNomad(tmp), wire=lambda d, i, data, t: dev.handle(data))


class _Base(unittest.TestCase):
    legacy = False

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bindu-b-")
        self.dev = FakeDevice()
        self.dev.legacy_caps = self.legacy
        self.p = _backend(self.tmp, self.dev)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _edit_cache_hash(self, nid, new_hash):
        path = os.path.join(self.p.cache_dir, A + ".json")
        with open(path) as fh:
            cache = json.load(fh)
        cache[str(nid)]["hash"] = new_hash
        with open(path, "w") as fh:
            json.dump(cache, fh)


class NamespaceHashStatusTests(_Base):
    """Neuere Firmware: je Namespace ein Hash in GetCapabilities; der Gesamt-Hash aus GetInfo (hier 1234) passt zu keinem."""

    def test_ping_after_overview_is_current_not_unknown(self):
        self.p.overview(A, "m", allow_fetch=True)
        self.dev.log.clear()
        res = self.p.ping(A, "m")
        self.assertEqual(self.dev.log, [2])  # ping bleibt bei GetInfo
        self.assertEqual(res["cache"]["status"], "current")
        self.assertEqual(res["cache"]["stale_namespaces"], 0)

    def test_ping_without_check_is_unchecked(self):
        self.p.overview(A, "m", allow_fetch=True)
        fresh = _backend(self.tmp, self.dev)  # neuer Prozess: Cache da, aber noch nichts geprüft
        self.dev.log.clear()
        res = fresh.ping(A, "m")
        self.assertEqual(self.dev.log, [2])
        self.assertEqual(res["cache"]["status"], "unchecked")
        self.assertNotIn("stale_namespaces", res["cache"])

    def test_changed_namespace_hash_is_stale_without_getschema(self):
        self.p.overview(A, "m", allow_fetch=True)
        self._edit_cache_hash(2, 5)  # im Cache steht ein anderer Hash als der Node meldet
        fresh = _backend(self.tmp, self.dev)
        self.dev.log.clear()
        res = fresh.overview(A, "m", allow_fetch=False)
        self.assertNotIn(1, self.dev.log)  # nie GetSchema von selbst
        self.assertEqual(res["cache"]["status"], "stale")
        self.assertEqual(res["cache"]["stale_namespaces"], 1)
        self.assertEqual(len(res["schema"]), 2)  # gespeichertes Schema wird weiter benutzt
        self.assertEqual(fresh.ping(A, "m")["cache"]["status"], "stale")

    def test_stale_is_logged_once_with_count(self):
        self.p.overview(A, "m", allow_fetch=True)
        self._edit_cache_hash(1, 7)
        self._edit_cache_hash(2, 8)
        fresh = _backend(self.tmp, self.dev)
        fresh.overview(A, "m", allow_fetch=False)
        lines = [e["text"] for e in fresh.events(0)["events"] if "weicht ab" in e["text"]]
        self.assertEqual(len(lines), 1)
        self.assertIn("2 von 2", lines[0])

    def test_total_hash_change_alone_is_not_stale(self):
        """Der Gesamt-Hash aus GetInfo darf den Status nicht kippen (ändert sich bei jedem Commit)."""
        self.p.overview(A, "m", allow_fetch=True)
        self.dev.needs_reboot = True  # beliebige Änderung am Node; GetInfo liefert weiter 1234
        res = self.p.overview(A, "m", allow_fetch=False)
        self.assertEqual(res["cache"]["status"], "current")

    def test_adopted_stays_adopted(self):
        self.p.overview(A, "m", allow_fetch=True)
        path = os.path.join(self.p.cache_dir, A + ".json")
        with open(path) as fh:
            cache = json.load(fh)
        for hit in cache.values():
            hit["hash"], hit["adopted"] = 0, "cd" * 16
        with open(path, "w") as fh:
            json.dump(cache, fh)
        self.assertEqual(self.p.cache_info(A, 1234)["status"], "adopted")


class LegacyStatusTests(_Base):
    """Firmware 1.86: nur der Gesamt-Hash; Vergleich mit GetInfo bleibt wie bisher."""
    legacy = True

    def test_single_total_hash(self):
        self.p.overview(A, "m", allow_fetch=True)
        self.assertEqual(self.p.cache_info(A, 1234)["status"], "current")
        stale = self.p.cache_info(A, 9999)
        self.assertEqual(stale["status"], "stale")
        self.assertNotIn("stale_namespaces", stale)  # kommt vom Gesamt-Hash, nicht aus einem Namespace-Vergleich


class DeviceNameTests(_Base):
    def _names(self, p=None):
        return {n["hash"]: n for n in (p or self.p).state()["nodes"]}

    def test_name_from_getstate_becomes_display_name(self):
        self.dev.working[100] = {5: "microReticulum Node [90706910BED1]"}
        self.p.get_state(A, "m", None, False)
        n = self._names()[A]
        self.assertEqual(n["name"], "microReticulum Node [90706910BED1]")
        self.assertEqual(n["src"], "node")
        self.assertTrue(n["auto"])

    def test_own_alias_wins_and_survives(self):
        self.dev.working[100] = {5: "vom Node"}
        self.p.get_state(A, "m", None, False)
        self.p.rename(A, "Schreibtisch")
        n = self._names()[A]
        self.assertEqual((n["name"], n["src"]), ("Schreibtisch", "alias"))
        self.assertNotIn("auto", n)
        self.dev.working[100] = {5: "neu am Node"}
        self.p.get_state(A, "m", None, False)
        self.assertEqual(self._names()[A]["name"], "Schreibtisch")  # Alias bleibt
        self.p.rename(A, "")
        self.assertEqual(self._names()[A]["name"], "neu am Node")  # ohne Alias gilt der Node-Name

    def test_name_updates_when_node_is_renamed(self):
        self.dev.working[100] = {5: "alt"}
        self.p.get_state(A, "m", None, False)
        self.dev.working[100] = {5: "neu"}
        self.p.get_state(A, "m", None, False)
        self.assertEqual(self._names()[A]["name"], "neu")

    def test_ignores_missing_empty_or_non_string(self):
        for working in ({}, {100: {5: ""}}, {100: {5: "   "}}, {100: {5: 42}}, {100: {4: "falsches Feld"}}):
            self.dev.working.pop(100, None)
            self.dev.working.update(working)
            self.p.get_state(A, "m", None, False)
        self.assertNotIn(A, self._names())  # ohne gültigen Namen steht der Node nicht in der Liste

    def test_name_is_persisted(self):
        self.dev.working[100] = {5: "bleibt"}
        self.p.get_state(A, "m", None, False)
        self.p.flush()
        fresh = _backend(self.tmp, self.dev)
        self.assertEqual(self._names(fresh)[A]["name"], "bleibt")

    def test_long_name_is_cut(self):
        self.dev.working[100] = {5: "x" * 200}
        self.p.get_state(A, "m", None, False)
        self.assertEqual(len(self._names()[A]["name"]), 60)

    def test_forget_removes_entry_and_name(self):
        self.dev.working[100] = {5: "weg damit"}
        self.p.get_state(A, "m", None, False)
        self.p.forget(A)
        self.assertNotIn(A, self._names())


if __name__ == "__main__":
    unittest.main()


class WindowAndLossTests(_Base):
    """Schema-Abruf: gleichzeitige Anfragen (Fenster), verlorene Pakete und Gesamtgrenze."""

    def setUp(self):
        super().setUp()
        self.p.SCHEMA_PAUSE = 0
        self.p.SCHEMA_ROUND_PAUSE = 0

    def _flaky(self):
        """Backend, bei dem jede GetSchema-Anfrage beim ersten Mal verloren geht."""
        seen = set()

        def wire(dest, ident, data, timeout):
            if data[0] == 1:  # GetSchema
                key = tuple(data[2][1])
                if key not in seen:
                    seen.add(key)
                    raise ProvisionError("Zeitüberschreitung", timeout=True)
            return self.dev.handle(data)

        p = ProvisionBackend(FakeNomad(self.tmp), wire=wire)
        p.SCHEMA_PAUSE = 0
        p.SCHEMA_ROUND_PAUSE = 0
        return p

    def test_window_gives_same_schema_as_sequential(self):
        seq = self.p.schema(A, "m", True, None, allow_fetch=True, window=1)
        par = self.p.schema(A, "m", True, None, allow_fetch=True, window=3)
        self.assertEqual(par, seq)
        self.assertGreater(len(par), 1)

    def test_lost_packets_are_retried_without_window(self):
        ns = self._flaky().schema(A, "m", True, None, allow_fetch=True, window=1)
        self.assertEqual(len(ns), 2)

    def test_lost_packets_are_fetched_in_later_round_with_window(self):
        p = self._flaky()
        ns = p.schema(A, "m", True, None, allow_fetch=True, window=2)  # ein Versuch je Durchgang: erst Durchgang 2 schafft es
        self.assertEqual(len(ns), 2)
        self.assertEqual(p._schema_state[A]["missing"], [])

    def test_progress_is_kept_when_node_stays_silent(self):
        first = self.p.capabilities(A, "m")[0]["id"]

        def wire(dest, ident, data, timeout):
            if data[0] == 1 and data[2][1] != [first]:  # nur der erste Namespace kommt an
                raise ProvisionError("Zeitüberschreitung", timeout=True)
            return self.dev.handle(data)

        p2 = ProvisionBackend(FakeNomad(self.tmp), wire=wire)
        p2.SCHEMA_PAUSE = 0
        p2.SCHEMA_ROUND_PAUSE = 0
        with self.assertRaises(ProvisionError) as cm:
            p2.schema(A, "m", True, None, allow_fetch=True, window=1)
        self.assertIn("unvollständig", str(cm.exception))
        with open(os.path.join(p2.cache_dir, A + ".json")) as fh:
            cache = json.load(fh)
        self.assertIn(str(first), cache)  # was angekommen ist, bleibt gespeichert
        ns = self.p.schema(A, "m", False, None, allow_fetch=True)  # zweiter Anlauf holt den Rest
        self.assertEqual(len(ns), 2)

    def test_deadline_stops_waiting(self):
        self.p.SCHEMA_DEADLINE = -1
        with self.assertRaises(ProvisionError) as cm:
            self.p.schema(A, "m", True, None, allow_fetch=True)
        self.assertIn("unvollständig", str(cm.exception))
