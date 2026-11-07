"""Obergrenze für mgmt_nodes.json: aufgeschnappte Hashes verdrängen keine benannten Nodes."""

import json
import os
import shutil
import tempfile
import unittest

from bindu import provision
from bindu.provision import ProvisionBackend

from .test_provision import FakeNomad


def h(i):
    return "%032x" % i


class MgmtNodesCapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bindu-cap-")
        self.old_max = provision.MAX_NODES
        provision.MAX_NODES = 5
        self.p = ProvisionBackend(FakeNomad(self.tmp), wire=lambda *a: b"")

    def tearDown(self):
        provision.MAX_NODES = self.old_max
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_announces_are_capped_oldest_first(self):
        for i in range(1, 9):
            self.p._nodes[h(i)] = {"last": 1000 + i}
            self.p._note(h(i))  # setzt "last" auf jetzt, begrenzt danach
            self.p._nodes[h(i)]["last"] = 1000 + i  # feste Reihenfolge für den Test
        self.assertEqual(len(self.p._nodes), 5)
        self.assertEqual(sorted(self.p._nodes), [h(i) for i in range(4, 9)])  # die neuesten fünf

    def test_named_nodes_are_never_dropped(self):
        self.p.rename(h(1), "mein Node")
        self.p._nodes[h(1)]["last"] = 1  # ältester Eintrag überhaupt
        for i in range(10, 20):
            self.p._note(h(i))
        self.assertIn(h(1), self.p._nodes)
        self.assertEqual(self.p._nodes[h(1)]["name"], "mein Node")
        self.assertEqual(len(self.p._nodes), 5)

    def test_nodes_with_reported_name_outlast_plain_announces(self):
        self.p._nodes[h(1)] = {"last": 1, "device_name": "vom Node gemeldet"}
        for i in range(10, 20):
            self.p._note(h(i))
        self.assertIn(h(1), self.p._nodes)
        self.assertEqual(len(self.p._nodes), 5)

    def test_only_named_nodes_can_exceed_the_cap(self):
        for i in range(1, 9):
            self.p.rename(h(i), "n%d" % i)
        self.assertEqual(len(self.p._nodes), 8)  # eigene Namen werden nie gelöscht
        self.p._note(h(99))
        self.assertNotIn(h(99), self.p._nodes)  # ein bloßer Announce passt nicht mehr hinein
        self.assertEqual(len(self.p._nodes), 8)

    def test_large_file_is_trimmed_on_load(self):
        path = os.path.join(self.tmp, "mgmt_nodes.json")
        with open(path, "w") as fh:
            json.dump({h(i): {"last": i} for i in range(1, 51)}, fh)
        p = ProvisionBackend(FakeNomad(self.tmp), wire=lambda *a: b"")
        self.assertEqual(len(p._nodes), 5)
        self.assertEqual(sorted(p._nodes), [h(i) for i in range(46, 51)])

    def test_default_cap(self):
        self.assertEqual(self.old_max, 500)


if __name__ == "__main__":
    unittest.main()
