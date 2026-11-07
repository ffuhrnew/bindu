import os
import shutil
import tempfile
import unittest

from bindu.nomad import NomadError
from bindu.provision import ProvisionBackend, ProvisionError, from_wire, to_wire

from .fakedevice import FakeDevice


class FakeNomad:
    def __init__(self, data_dir):
        import RNS

        self.RNS = RNS
        self.data_dir = data_dir


class ProvisionTests(unittest.TestCase):
    DEST = "ab" * 16

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bindu-prov-")
        self.dev = FakeDevice()
        self.calls = []

        def wire(dest, ident, data, timeout):
            self.calls.append((dest, ident))
            return self.dev.handle(data)

        self.p = ProvisionBackend(FakeNomad(self.tmp), wire=wire)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_info_and_caps(self):
        self.assertEqual(self.p.info(self.DEST, "m")["firmware"], "1.86")
        caps = self.p.capabilities(self.DEST, "m")
        self.assertEqual([c["name"] for c in caps], ["General", "Radio"])

    def test_schema_parsed_and_cached(self):
        ns = self.p.schema(self.DEST, "m")
        radio = [n for n in ns if n["name"] == "Radio"][0]
        freq = radio["fields"][0]
        self.assertEqual(freq["type"], "int")
        self.assertEqual(freq["flags"], ["reboot_required"])
        self.assertEqual(radio["fields"][1]["enum_labels"], ["full", "gateway"])
        self.assertIn("read_only", radio["fields"][2]["flags"])
        self.dev.log.clear()
        self.p.schema(self.DEST, "m")
        self.assertNotIn(1, self.dev.log)  # GetSchema kam aus dem Cache
        self.p.schema(self.DEST, "m", refresh=True)
        self.assertIn(1, self.dev.log)

    def test_state_hex_for_bytes(self):
        st = self.p.get_state(self.DEST, "m")
        self.assertEqual(st["values"][1][3], ["01" * 16])
        self.assertEqual(st["values"][1][1], "NODE-A")

    def test_set_commit_flow(self):
        self.p.schema(self.DEST, "m")
        res = self.p.set_state(self.DEST, "m", {1: {1: "NEU", 3: ["02" * 16]}, 2: {1: 868000000}})
        self.assertEqual(res["applied"], 3)
        self.assertEqual(self.dev.drafts[1][3], [b"\x02" * 16])  # Hex -> bytes
        st = self.p.get_state(self.DEST, "m", drafts=True)
        self.assertEqual(st["drafts"][1][1], "NEU")
        self.assertEqual(self.p.commit(self.DEST, "m")["applied"], 2)
        self.assertEqual(self.dev.working[1][1], "NEU")
        self.assertEqual(self.p.get_state(self.DEST, "m")["drafts"], {})

    def test_readonly_and_unknown_field_rejected_client_side(self):
        with self.assertRaises(ProvisionError):
            self.p.set_state(self.DEST, "m", {2: {3: 5}})
        with self.assertRaises(ProvisionError):
            self.p.set_state(self.DEST, "m", {9: {9: 5}})
        with self.assertRaises(ProvisionError):
            self.p.set_state(self.DEST, "m", {})

    def test_discard_reboot_factory(self):
        self.p.schema(self.DEST, "m")
        self.p.set_state(self.DEST, "m", {1: {2: False}})
        self.assertEqual(self.p.discard(self.DEST, "m")["applied"], 1)
        self.assertEqual(self.dev.drafts, {})
        self.p.reboot(self.DEST, "m")
        self.assertTrue(self.dev.rebooted)
        self.assertTrue(self.p.factory_reset(self.DEST, "m")["needs_reboot"])

    def test_device_error_is_clear(self):
        with self.assertRaises(ProvisionError) as cm:
            self.p.call(self.DEST, "m", 77)
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("UnknownOp", str(cm.exception))

    def test_bad_hash_and_nodes_list(self):
        with self.assertRaises(NomadError):
            self.p.info("xyz", "m")
        self.p.info(self.DEST, "m")
        self.assertEqual(self.p.state()["nodes"], [])  # ohne Namen nicht in der Liste
        self.p.rename(self.DEST, "Dach")
        self.assertEqual(self.p.state()["nodes"][0]["hash"], self.DEST)
        self.assertEqual(self.p.state()["nodes"][0]["name"], "Dach")
        self.p.forget(self.DEST)
        self.assertEqual(self.p.state()["nodes"], [])

    def test_value_conversion(self):
        self.assertEqual(to_wire(5, "0aff"), b"\x0a\xff")
        self.assertEqual(to_wire(7, ["01", "02"]), [b"\x01", b"\x02"])
        self.assertEqual(from_wire([b"\x01"]), ["01"])
        with self.assertRaises(ProvisionError):
            to_wire(5, "zz")


if __name__ == "__main__":
    unittest.main()


class ProvisionWebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import http.client, json, threading
        from bindu.web import make_server

        cls.http, cls.json = http.client, json
        cls.tmp = tempfile.mkdtemp(prefix="bindu-provweb-")
        cls.dev = FakeDevice()
        prov = ProvisionBackend(FakeNomad(cls.tmp), wire=lambda d, i, data, t: cls.dev.handle(data))

        class Nomad:
            def state(self):
                return {"nodes": [], "identities": []}

        cls.server = make_server(Nomad(), "127.0.0.1", 0, None, prov)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def post(self, path, obj):
        c = self.http.HTTPConnection("127.0.0.1", self.port)
        c.request("POST", path, self.json.dumps(obj), {"Content-Type": "application/json", "Host": "127.0.0.1:%d" % self.port})
        r = c.getresponse()
        return r.status, self.json.loads(r.read())

    def test_overview_set_commit(self):
        d = "cd" * 16
        st, res = self.post("/api/node/overview", {"dest": d, "identity": "m", "fetch_schema": True})
        self.assertTrue(res["ok"])
        self.assertEqual(res["info"]["firmware"], "1.86")
        self.assertEqual(res["values"]["1"]["1"], "NODE-A")
        _, res = self.post("/api/node/set", {"dest": d, "identity": "m", "changes": {"1": {"1": "WEB"}}})
        self.assertEqual(res["applied"], 1)
        _, res = self.post("/api/node/commit", {"dest": d, "identity": "m", "ns": [1]})
        self.assertEqual(res["applied"], 1)

    def test_factory_reset_needs_confirm(self):
        _, res = self.post("/api/node/factory_reset", {"dest": "cd" * 16})
        self.assertFalse(res["ok"])
        self.assertFalse(self.dev.needs_reboot)

    def test_errors_are_clear_text(self):
        _, res = self.post("/api/node/overview", {"dest": "kaputt"})
        self.assertFalse(res["ok"])
        self.assertIn("32 Hex", res["error"])


class TcpOptionTests(unittest.TestCase):
    def test_tcp_option_rewrites_existing_config(self):
        from bindu.nomad import write_default_config

        tmp = tempfile.mkdtemp(prefix="bindu-cfg-")
        try:
            write_default_config(tmp, ["1.2.3.4:4242"])
            write_default_config(tmp, ["192.0.2.4:4243"])
            text = open(os.path.join(tmp, "config")).read()
            self.assertIn("192.0.2.4", text)
            self.assertNotIn("1.2.3.4", text)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class PreDecodedResponseTests(unittest.TestCase):
    def test_list_response_is_accepted(self):
        from RNS.vendor import umsgpack

        dev = FakeDevice()
        tmp = tempfile.mkdtemp(prefix="bindu-pre-")
        try:
            p = ProvisionBackend(FakeNomad(tmp), wire=lambda d, i, data, t: umsgpack.unpackb(dev.handle(data)))
            self.assertEqual(p.info("ab" * 16, "m")["firmware"], "1.86")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class SeqMismatchTests(unittest.TestCase):
    def test_differing_seq_is_tolerated(self):
        from RNS.vendor import umsgpack

        dev = FakeDevice()
        tmp = tempfile.mkdtemp(prefix="bindu-seq-")
        try:
            def wire(d, i, data, t):
                resp = umsgpack.unpackb(dev.handle(data))
                resp[1] = 99
                return umsgpack.packb(resp)

            p = ProvisionBackend(FakeNomad(tmp), wire=wire)
            self.assertEqual(p.info("ab" * 16, "m")["firmware"], "1.86")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class LegacyCapabilitiesTests(unittest.TestCase):
    def test_id_list_capabilities_then_schema(self):
        tmp = tempfile.mkdtemp(prefix="bindu-legacy-")
        try:
            dev = FakeDevice()
            dev.legacy_caps = True
            p = ProvisionBackend(FakeNomad(tmp), wire=lambda d, i, data, t: dev.handle(data))
            res = p.overview("ab" * 16, "m")
            self.assertEqual([n["name"] for n in res["schema"]], ["General", "Radio"])
            dev.log.clear()
            p.overview("ab" * 16, "m")  # zweiter Lauf: Schema aus dem Cache (Hash aus GetInfo)
            self.assertNotIn(1, dev.log)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
