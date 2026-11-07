import http.client
import json
import threading
import unittest

from bindu import micron
from bindu.nomad import NomadError
from bindu.web import make_server


class MicronTests(unittest.TestCase):
    def test_escapes_html(self):
        out = micron.render("x <script>alert(1)</script>")
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;script&gt;", out)
        # "\<" am Zeilenanfang ist ein wörtliches "<" (ohne Escape = Tiefe zurücksetzen)
        out = micron.render("\\<b>fett?</b>")
        self.assertNotIn("<b>", out)
        self.assertIn("&lt;b&gt;", out)

    def test_heading_and_depth(self):
        out = micron.render(">Titel\ntext\n>>Unter\ntext2")
        self.assertIn("mu-h1", out)
        self.assertIn("mu-h2", out)
        self.assertIn("margin-left:1.5ch", out)  # Text unter Level 1
        self.assertIn("margin-left:3.0ch", out)  # Text unter Level 2

    def test_comment_and_divider(self):
        out = micron.render("# kommentar\n-\nhallo")
        self.assertNotIn("kommentar", out)
        self.assertIn("mu-hr", out)

    def test_bold_toggle(self):
        out = micron.render("`!fett`! normal")
        self.assertIn("font-weight:bold", out)
        self.assertIn("fett</span>", out)
        self.assertTrue(out.endswith("normal</div>"))

    def test_color(self):
        out = micron.render("`Ff00rot`f weiter")
        self.assertIn("color:#f00", out)

    def test_bad_color_ignored(self):
        out = micron.render("`Fzzzx")
        self.assertNotIn("color:", out)

    def test_link_with_fields(self):
        out = micron.render("`[Senden`:/page/x.mu`name,*,a=b]")
        self.assertIn('data-dest=":/page/x.mu"', out)
        self.assertIn('data-fields="name,*,a=b"', out)
        self.assertIn(">Senden</a>", out)

    def test_link_attribute_injection(self):
        out = micron.render('`[x`"><img src=x onerror=1>]')
        self.assertNotIn("<img", out)

    def test_text_and_masked_field(self):
        out = micron.render("`<name`vorgabe>`<!|pw`>")
        self.assertIn('data-name="name"', out)
        self.assertIn('value="vorgabe"', out)
        self.assertIn('type="password"', out)

    def test_checkbox_and_radio(self):
        out = micron.render("`<?|opt|1|*`Ja>\n`<^|wahl|a`A>")
        self.assertIn('type="checkbox"', out)
        self.assertIn(" checked", out)
        self.assertIn('type="radio"', out)

    def test_literal_block(self):
        out = micron.render("`=\n`!nicht fett`!\n`=")
        self.assertNotIn("font-weight", out)
        self.assertIn("`!nicht fett`!", out)

    def test_escape(self):
        out = micron.render("\\`!kein befehl")
        self.assertIn("`!kein befehl", out)

    def test_stray_backticks_are_dropped(self):
        out = micron.render("`[• Heap`:/page/stack.mu`c=heap]`\nende`")
        self.assertNotIn("`", out)
        self.assertIn('data-fields="c=heap"', out)
        self.assertNotIn("`", micron.render("a`?b ` c"))

    def test_center(self):
        out = micron.render("`cmitte")
        self.assertIn("text-align:center", out)


class FakeBackend:
    def __init__(self):
        self.calls = []

    def state(self):
        return {"nodes": [{"hash": "a" * 32, "name": "N", "last": 1}], "identities": []}

    def create_identity(self, name):
        if name == "dup":
            raise NomadError("existiert bereits")
        return {"name": name, "hash": "b" * 32}

    def fetch(self, node, path, identity, fields, variables):
        self.calls.append((node, path, identity, fields, variables))
        if identity == "boom":
            raise RuntimeError("kaputt")
        if path == "/page/denied.mu":
            raise NomadError("Anfrage fehlgeschlagen")
        return ">Hallo\n`!ok`!"


class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.backend = FakeBackend()
        cls.server = make_server(cls.backend, "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def request(self, method, path, body=None, headers=None, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        hdrs = {"Host": host or "127.0.0.1:%d" % self.port}
        hdrs.update(headers or {})
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, data

    def post(self, path, obj, **kw):
        headers = {"Content-Type": "application/json"}
        headers.update(kw.pop("headers", {}))
        return self.request("POST", path, json.dumps(obj).encode(), headers, **kw)

    def test_index(self):
        status, data = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"bindu", data)

    def test_state(self):
        status, data = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(data)["ok"])

    def test_page_ok_and_fields_forwarded(self):
        status, data = self.post(
            "/api/page",
            {"node": "a" * 32, "path": "/page/x.mu", "identity": "me", "fields": {"k": "v"}, "vars": {"z": "1"}},
        )
        res = json.loads(data)
        self.assertEqual(status, 200)
        self.assertTrue(res["ok"])
        self.assertIn("mu-h1", res["html"])
        self.assertEqual(self.backend.calls[-1], ("a" * 32, "/page/x.mu", "me", {"k": "v"}, {"z": "1"}))

    def test_page_nomad_error_is_clear_text(self):
        _, data = self.post("/api/page", {"node": "a" * 32, "path": "/page/denied.mu"})
        res = json.loads(data)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "Anfrage fehlgeschlagen")

    def test_unexpected_error_is_500(self):
        status, data = self.post("/api/page", {"node": "a" * 32, "identity": "boom"})
        self.assertEqual(status, 500)
        self.assertFalse(json.loads(data)["ok"])

    def test_identity_create_and_duplicate(self):
        _, data = self.post("/api/identity", {"name": "neu"})
        self.assertTrue(json.loads(data)["ok"])
        _, data = self.post("/api/identity", {"name": "dup"})
        self.assertFalse(json.loads(data)["ok"])

    def test_rejects_wrong_content_type(self):
        status, _ = self.request("POST", "/api/page", b"{}", {"Content-Type": "text/plain"})
        self.assertEqual(status, 415)

    def test_rejects_foreign_host(self):
        status, _ = self.request("GET", "/api/state", host="evil.example:80")
        self.assertEqual(status, 403)

    def test_rejects_bad_json(self):
        status, _ = self.request("POST", "/api/page", b"{nope", {"Content-Type": "application/json"})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
