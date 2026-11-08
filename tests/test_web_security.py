"""Host-Header-Prüfung bei jeder Bindung, Content-Security-Policy und frame-ancestors."""

import base64
import hashlib
import http.client
import re
import threading
import unittest

from bindu.web import _csp_for_page, host_allowed, make_server

from .test_bindu import FakeBackend


class HostAllowedTests(unittest.TestCase):
    PORT = 8765

    def ok(self, header, extra=()):
        return host_allowed(header, self.PORT, extra)

    def test_ip_addresses_and_localhost(self):
        for h in ("127.0.0.1:8765", "100.99.204.44:8765", "[::1]:8765", "[fd7a:115c:a1e0::1]:8765", "localhost:8765", "LOCALHOST:8765"):
            self.assertTrue(self.ok(h), h)

    def test_foreign_names_are_rejected(self):
        # So sieht DNS-Rebinding aus: der Name gehört dem Angreifer, die Adresse dem Opfer
        for h in ("evil.example:8765", "evil.example", "kali.tailnet.ts.net:8765", "127.0.0.1.evil.example:8765", ""):
            self.assertFalse(self.ok(h), h)

    def test_wrong_or_missing_port(self):
        for h in ("127.0.0.1:80", "127.0.0.1:99999", "127.0.0.1:abc", "127.0.0.1", "127.0.0.1:", "[::1]8765"):
            self.assertFalse(self.ok(h), h)

    def test_ipv6_without_brackets_is_invalid(self):
        self.assertFalse(self.ok("::1:8765"))

    def test_explicitly_allowed_names(self):
        self.assertTrue(self.ok("kali.tailnet.ts.net:8765", ["kali.tailnet.ts.net"]))
        self.assertTrue(self.ok("KALI.tailnet.ts.net:8765", ["kali.TAILNET.ts.net"]))
        self.assertFalse(self.ok("other.tailnet.ts.net:8765", ["kali.tailnet.ts.net"]))

    def test_allowed_names_work_without_or_with_any_port(self):
        # Ein Reverse-Proxy schickt meist nur den Namen (Port 443 steht nicht im Header)
        extra = ["bindu.example"]
        for h in ("bindu.example", "bindu.example:443", "bindu.example:8443", "BINDU.example"):
            self.assertTrue(self.ok(h, extra), h)

    def test_allowed_name_needs_a_valid_port_syntax(self):
        extra = ["bindu.example"]
        for h in ("bindu.example:", "bindu.example:abc", "bindu.example:80:80", "bindu.example 80", "x.bindu.example", "bindu.example.evil"):
            self.assertFalse(self.ok(h, extra), h)

    def test_ip_still_needs_the_real_port(self):
        self.assertFalse(self.ok("127.0.0.1:1", ["bindu.example"]))
        self.assertFalse(self.ok("127.0.0.1", ["bindu.example"]))

    def test_wildcard(self):
        self.assertTrue(self.ok("anything.example:1", ["*"]))

    def test_default_ports(self):
        self.assertTrue(host_allowed("127.0.0.1", 80))
        self.assertTrue(host_allowed("127.0.0.1", 443))
        self.assertFalse(host_allowed("127.0.0.1", 8765))


class _ServerTest(unittest.TestCase):
    HOST = "127.0.0.1"
    ALLOW = ()

    @classmethod
    def setUpClass(cls):
        cls.server = make_server(FakeBackend(), cls.HOST, 0, allow_hosts=cls.ALLOW)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def get(self, path, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("GET", path, headers={"Host": host if host is not None else "127.0.0.1:%d" % self.port})
        resp = conn.getresponse()
        data = resp.read()
        headers = {k.lower(): v for k, v in resp.getheaders()}
        conn.close()
        return resp.status, headers, data


class NonLoopbackBindingTests(_ServerTest):
    """Server an allen Adressen: der Schutz gegen DNS-Rebinding darf hier nicht fehlen."""

    HOST = "0.0.0.0"
    ALLOW = ("kali.tailnet.ts.net",)

    def test_ip_host_is_served(self):
        self.assertEqual(self.get("/api/state")[0], 200)
        self.assertEqual(self.get("/api/state", host="100.64.1.2:%d" % self.port)[0], 200)

    def test_rebinding_host_is_rejected(self):
        self.assertEqual(self.get("/api/state", host="evil.example:%d" % self.port)[0], 403)
        self.assertEqual(self.get("/", host="evil.example:%d" % self.port)[0], 403)

    def test_allowed_name_is_served(self):
        self.assertEqual(self.get("/api/state", host="kali.tailnet.ts.net:%d" % self.port)[0], 200)

    def test_allowed_name_without_port_is_served(self):
        self.assertEqual(self.get("/api/state", host="kali.tailnet.ts.net")[0], 200)


class LoopbackBindingTests(_ServerTest):
    ALLOW = ("proxy.example",)

    def test_loopback_hosts(self):
        self.assertEqual(self.get("/api/state")[0], 200)
        self.assertEqual(self.get("/api/state", host="localhost:%d" % self.port)[0], 200)

    def test_other_ip_is_rejected_on_loopback(self):
        self.assertEqual(self.get("/api/state", host="100.64.1.2:%d" % self.port)[0], 403)

    def test_allow_host_extends_loopback(self):
        self.assertEqual(self.get("/api/state", host="proxy.example:%d" % self.port)[0], 200)
        self.assertEqual(self.get("/api/state", host="evil.example:%d" % self.port)[0], 403)

    def test_allow_host_without_port_on_loopback(self):
        self.assertEqual(self.get("/api/state", host="proxy.example")[0], 200)
        self.assertEqual(self.get("/api/state", host="proxy.example:443")[0], 200)
        self.assertEqual(self.get("/api/state", host="100.64.1.2")[0], 403)


class WildcardTests(_ServerTest):
    ALLOW = ("*",)

    def test_any_host(self):
        self.assertEqual(self.get("/api/state", host="anything.example:1")[0], 200)


class HeaderTests(_ServerTest):
    def test_index_csp_pins_the_page_script(self):
        status, headers, body = self.get("/")
        self.assertEqual(status, 200)
        csp = headers["content-security-policy"]
        scripts = re.findall(rb"<script(?:\s[^>]*)?>(.*?)</script>", body, re.S)
        self.assertEqual(len(scripts), 1)
        digest = base64.b64encode(hashlib.sha256(scripts[0]).digest()).decode()
        self.assertIn("script-src 'sha256-%s'" % digest, csp)
        self.assertNotIn("script-src 'unsafe-inline'", csp)
        self.assertNotIn("'unsafe-eval'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("connect-src 'self'", csp)
        self.assertIn("default-src 'none'", csp)
        self.assertEqual(headers["x-frame-options"], "DENY")

    def test_api_responses_have_a_closed_policy(self):
        _, headers, _ = self.get("/api/state")
        self.assertIn("default-src 'none'", headers["content-security-policy"])
        self.assertIn("frame-ancestors 'none'", headers["content-security-policy"])
        self.assertEqual(headers["x-frame-options"], "DENY")

    def test_error_responses_have_the_headers_too(self):
        _, headers, _ = self.get("/api/state", host="evil.example:80")
        self.assertEqual(headers["x-frame-options"], "DENY")

    def test_csp_without_script_blocks_scripts(self):
        self.assertIn("script-src 'none'", _csp_for_page(b"<html><body>kein Skript</body></html>"))

    def test_csp_hashes_every_inline_script(self):
        csp = _csp_for_page(b"<script>a()</script><script type='module'>b()</script>")
        self.assertEqual(csp.count("'sha256-"), 2)


if __name__ == "__main__":
    unittest.main()
