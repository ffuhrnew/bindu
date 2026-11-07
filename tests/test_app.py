"""Fenster-Start: der Check, ob der Hintergrund-Server antwortet, darf nie über einen Proxy gehen."""

import os
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

from bindu import app


class _Alive:
    def poll(self):
        return None


class _Dead:
    def poll(self):
        return 1


class _Ok(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


class WaitForTests(unittest.TestCase):
    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), _Ok)
        self.url = "http://127.0.0.1:%d/" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def test_antwortet_der_server_ist_es_fertig(self):
        self.assertTrue(app.wait_for(self.url, _Alive(), timeout=5))

    def test_proxy_umgebungsvariablen_werden_ignoriert(self):
        # Ein Proxy, der nicht erreichbar ist: ginge die Anfrage darüber, käme nie eine Antwort
        env = {"http_proxy": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9"}
        # Zwei Dinge würden den Test wirkungslos machen: eine gesetzte NO_PROXY-Liste (leitet 127.0.0.1 am Proxy vorbei)
        # und der Standard-Opener von urlopen, der die Proxy-Einstellungen vom ersten Aufruf an behält
        with mock.patch.dict(os.environ, env), mock.patch.object(urllib.request, "_opener", None):
            os.environ.pop("no_proxy", None)
            os.environ.pop("NO_PROXY", None)
            self.assertTrue(app.wait_for(self.url, _Alive(), timeout=5))

    def test_beendeter_prozess_bricht_ab(self):
        self.assertFalse(app.wait_for(self.url, _Dead(), timeout=5))


if __name__ == "__main__":
    unittest.main()
