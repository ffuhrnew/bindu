"""Reverse-Proxy: Besucheradresse aus X-Forwarded-For, aber nur von ausdrücklich vertrauten Proxys."""

import http.client
import threading
import unittest

from bindu.access import AccessPolicy, client_ip, parse_networks
from bindu.web import make_server

from .test_bindu import FakeBackend


def nets(*items):
    return parse_networks(items)


class ClientIpTests(unittest.TestCase):
    def test_without_trusted_proxy_the_header_is_ignored(self):
        self.assertEqual(client_ip("10.0.0.5", ["100.64.1.2"], []), "10.0.0.5")

    def test_untrusted_peer_cannot_set_the_header(self):
        # Ein Besucher direkt am Server schreibt sich selbst eine Tailnet-Adresse in den Header
        self.assertEqual(client_ip("8.8.8.8", ["100.64.1.2"], nets("10.0.0.5")), "8.8.8.8")

    def test_trusted_proxy_passes_the_visitor(self):
        self.assertEqual(client_ip("10.0.0.5", ["100.64.1.2"], nets("10.0.0.5")), "100.64.1.2")

    def test_rightmost_untrusted_address_counts(self):
        # Der Besucher fälscht links eine Tailnet-Adresse, der Proxy hängt rechts die echte an
        self.assertEqual(client_ip("10.0.0.5", ["100.64.1.2, 8.8.8.8"], nets("10.0.0.5")), "8.8.8.8")

    def test_chain_of_trusted_proxies(self):
        self.assertEqual(client_ip("10.0.0.5", ["100.64.1.2, 10.0.0.6"], nets("10.0.0.0/24")), "100.64.1.2")

    def test_multiple_header_lines(self):
        self.assertEqual(client_ip("10.0.0.5", ["1.2.3.4", "100.64.1.2"], nets("10.0.0.5")), "100.64.1.2")

    def test_missing_header_keeps_the_peer(self):
        self.assertEqual(client_ip("10.0.0.5", None, nets("10.0.0.5")), "10.0.0.5")
        self.assertEqual(client_ip("10.0.0.5", [], nets("10.0.0.5")), "10.0.0.5")

    def test_garbage_is_returned_and_then_denied(self):
        got = client_ip("10.0.0.5", ["nicht-ip"], nets("10.0.0.5"))
        self.assertEqual(got, "nicht-ip")
        self.assertFalse(AccessPolicy.default().allows(got))

    def test_ipv6_and_mapped(self):
        self.assertEqual(client_ip("::ffff:10.0.0.5", ["fd7a:115c:a1e0::1"], nets("10.0.0.5")), "fd7a:115c:a1e0::1")

    def test_parse_networks_rejects_nonsense(self):
        with self.assertRaises(ValueError):
            parse_networks(["kein-netz"])


class ProxyServerTests(unittest.TestCase):
    """Der Proxy ist hier 127.0.0.1 (der Testclient) und wird ausdrücklich vertraut."""

    @classmethod
    def setUpClass(cls):
        cls.server = make_server(FakeBackend(), "127.0.0.1", 0, access=AccessPolicy.default(),
                                 allow_hosts=("bindu.example",), trusted_proxies=nets("127.0.0.1"))
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def status(self, forwarded=None, host="bindu.example"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        headers = {"Host": host}
        if forwarded is not None:
            headers["X-Forwarded-For"] = forwarded
        conn.request("GET", "/api/state", headers=headers)
        resp = conn.getresponse()
        resp.read()
        conn.close()
        return resp.status

    def test_tailnet_visitor_through_proxy_is_served(self):
        self.assertEqual(self.status("100.64.1.2"), 200)

    def test_outside_visitor_through_proxy_is_denied(self):
        # Ohne --trusted-proxy würde bindu hier nur die Loopback-Adresse des Proxys sehen und alle durchlassen
        self.assertEqual(self.status("8.8.8.8"), 403)
        self.assertEqual(self.status("192.168.1.9"), 403)

    def test_spoofed_left_entry_does_not_help(self):
        self.assertEqual(self.status("100.64.1.2, 8.8.8.8"), 403)

    def test_foreign_host_is_still_rejected(self):
        self.assertEqual(self.status("100.64.1.2", host="evil.example"), 403)


class NoTrustServerTests(unittest.TestCase):
    """Ohne --trusted-proxy bleibt es wie bisher: der Header zählt nicht."""

    @classmethod
    def setUpClass(cls):
        cls.server = make_server(FakeBackend(), "127.0.0.1", 0, access=AccessPolicy.default())
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_header_is_ignored(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("GET", "/api/state", headers={"Host": "127.0.0.1:%d" % self.port, "X-Forwarded-For": "8.8.8.8"})
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 200)  # Verbindung von Loopback, Header ohne Wirkung


if __name__ == "__main__":
    unittest.main()
