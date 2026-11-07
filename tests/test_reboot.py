"""Neustart aus der Ferne: Erfolg wird nur gemeldet, wenn die Anfrage den Node erreicht haben kann."""

import shutil
import tempfile
import unittest

from bindu.nomad import NomadError
from bindu.provision import ProvisionBackend, ProvisionError

from .fakedevice import FakeDevice
from .test_provision import FakeNomad


class RebootTests(unittest.TestCase):
    DEST = "ab" * 16

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bindu-reboot-")
        self.dev = FakeDevice()
        self.behaviour = None  # None = FakeDevice antwortet, sonst Ausnahme oder Antwortbytes

        def wire(dest, ident, data, timeout):
            if isinstance(self.behaviour, Exception):
                raise self.behaviour
            if self.behaviour is not None:
                return self.behaviour
            return self.dev.handle(data)

        self.p = ProvisionBackend(FakeNomad(self.tmp), wire=wire)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_confirmed(self):
        self.assertEqual(self.p.reboot(self.DEST, "m"), {"acked": True})
        self.assertTrue(self.dev.rebooted)

    def test_no_answer_after_sending_is_expected(self):
        # Der Node startet neu und antwortet nicht mehr: gesendet, aber nicht bestätigt
        self.behaviour = ProvisionError("Zeitüberschreitung – keine Antwort vom Node", timeout=True)
        self.assertEqual(self.p.reboot(self.DEST, "m"), {"acked": False})

    def test_link_not_established_is_an_error(self):
        # Es wurde nichts gesendet: darf nicht als „Neustart gesendet“ erscheinen
        self.behaviour = ProvisionError("Link zu abab… kam in 3 Versuchen nicht zustande")
        with self.assertRaises(ProvisionError):
            self.p.reboot(self.DEST, "m")

    def test_unknown_node_is_an_error(self):
        self.behaviour = ProvisionError("Node unbekannt – kein Announce und keine Pfadantwort für diesen Hash")
        with self.assertRaises(ProvisionError):
            self.p.reboot(self.DEST, "m")

    def test_error_answer_from_node_is_an_error(self):
        from RNS.vendor import umsgpack

        self.behaviour = umsgpack.packb([101, 1, {1: 9, 2: "boom"}])  # Internal
        with self.assertRaises(ProvisionError) as cm:
            self.p.reboot(self.DEST, "m")
        self.assertEqual(cm.exception.code, 9)

    def test_unknown_op_is_an_error(self):
        from RNS.vendor import umsgpack

        self.behaviour = umsgpack.packb([101, 1, {1: 2, 2: "UnknownOp"}])
        with self.assertRaises(ProvisionError) as cm:
            self.p.reboot(self.DEST, "m")
        self.assertEqual(cm.exception.code, 2)

    def test_missing_identity_is_an_error(self):
        with self.assertRaises(ProvisionError):
            self.p.reboot(self.DEST, "")  # ohne Identity wird nichts gesendet

    def test_other_errors_pass_through(self):
        self.behaviour = NomadError("Identity 'x' nicht gefunden")
        with self.assertRaises(NomadError):
            self.p.reboot(self.DEST, "m")


if __name__ == "__main__":
    unittest.main()
