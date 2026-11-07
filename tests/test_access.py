import http.client
import json
import threading
import unittest

from bindu.access import AccessPolicy
from bindu.web import make_server


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.p = AccessPolicy.default()

    def test_tailnet_ipv4_allowed(self):
        for ip in ("100.64.0.1", "100.100.0.5", "100.127.255.254"):
            self.assertTrue(self.p.allows(ip), ip)

    def test_outside_cgnat_range_denied(self):
        for ip in ("100.63.255.255", "100.128.0.1", "10.0.0.4", "192.168.1.5", "8.8.8.8"):
            self.assertFalse(self.p.allows(ip), ip)

    def test_tailnet_ipv6(self):
        self.assertTrue(self.p.allows("fd7a:115c:a1e0::1"))
        self.assertTrue(self.p.allows("fd7a:115c:a1e0:ab12:4843:cd96:6200:1234"))
        self.assertFalse(self.p.allows("fd7a:115c:a1e1::1"))
        self.assertFalse(self.p.allows("2001:db8::1"))
        self.assertFalse(self.p.allows("fe80::1"))

    def test_loopback_always_allowed(self):
        for ip in ("127.0.0.1", "127.5.5.5", "::1"):
            self.assertTrue(self.p.allows(ip), ip)

    def test_ipv4_mapped_ipv6(self):
        self.assertTrue(self.p.allows("::ffff:100.100.0.5"))
        self.assertTrue(self.p.allows("::ffff:127.0.0.1"))
        self.assertFalse(self.p.allows("::ffff:192.168.1.5"))

    def test_zone_suffix_and_garbage(self):
        self.assertFalse(self.p.allows("fe80::1%en0"))
        for bad in ("", "nicht-ip", "100.64.0", None):
            self.assertFalse(self.p.allows(bad), bad)

    def test_extra_cidr(self):
        p = AccessPolicy.default(["192.168.1.0/24", "2001:db8::/32"])
        self.assertTrue(p.allows("192.168.1.77"))
        self.assertFalse(p.allows("192.168.2.77"))
        self.assertTrue(p.allows("2001:db8::5"))
        self.assertTrue(p.allows("100.64.0.9"))

    def test_bad_cidr_raises(self):
        with self.assertRaises(ValueError):
            AccessPolicy.default(["kein-netz"])

    def test_disabled_allows_everything(self):
        p = AccessPolicy.disabled()
        self.assertTrue(p.allows("8.8.8.8"))
        self.assertTrue(p.allows("nicht-ip"))
        self.assertIn("AUS", p.describe())


class _FakeNomad:
    def state(self):
        return {"nodes": []}

    def shutdown(self):
        pass


class _Deny(AccessPolicy):
    def allows(self, client_ip):
        return False


class HttpTests(unittest.TestCase):
    def _serve(self, access):
        server = make_server(_FakeNomad(), "127.0.0.1", 0, access=access)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close()))
        return server.server_address[1]

    def _req(self, port, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        headers = {"Content-Type": "application/json"} if body is not None else {}
        c.request(method, path, json.dumps(body) if body is not None else None, headers)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, data

    def test_default_allows_loopback(self):
        port = self._serve(None)
        status, _ = self._req(port, "GET", "/api/state")
        self.assertEqual(status, 200)

    def test_denied_get_and_post_are_403(self):
        port = self._serve(_Deny())
        for method, path, body in (("GET", "/", None), ("GET", "/api/state", None), ("POST", "/api/page", {})):
            status, data = self._req(port, method, path, body)
            self.assertEqual(status, 403, path)
            self.assertFalse(json.loads(data)["ok"])

    def test_denied_before_content_type_check(self):
        port = self._serve(_Deny())
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("POST", "/api/node/factory_reset", "x=1", {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(c.getresponse().status, 403)

    def test_disabled_check_allows(self):
        port = self._serve(AccessPolicy.disabled())
        status, _ = self._req(port, "GET", "/api/state")
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
