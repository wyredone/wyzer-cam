"""Tests for installer/discover.py, openrouter.py and telegram_helper.py,
against local fake servers.

Run:  python -m unittest discover -s test -v
"""
import json
import socket
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "installer"))
import discover  # noqa: E402
import openrouter  # noqa: E402
import telegram_helper as tg  # noqa: E402


def serve(handler_class):
    server = HTTPServer(("127.0.0.1", 0), handler_class)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def banner_server(banner=b"SSH-2.0-dropbear_2026.94\r\n"):
    """A TCP server that greets like an SSH server and hangs up."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(5)

    def accept():
        while True:
            try:
                conn, _ = sock.accept()
            except OSError:
                return
            conn.sendall(banner)
            conn.close()

    threading.Thread(target=accept, daemon=True).start()
    return sock


def page_handler(title):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            body = ("<html><head><title>%s</title></head></html>" % title).encode()
            self.send_response(200 if self.path == "/login.html" else 404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


class DiscoverTests(unittest.TestCase):
    def tearDown(self):
        for item in getattr(self, "closers", []):
            item()

    def start(self, title):
        ssh = banner_server()
        web = serve(page_handler(title))
        self.closers = [ssh.close, web.shutdown, web.server_close]
        return ssh.getsockname()[1], web.server_address[1]

    def test_recognises_a_thingino_camera(self):
        ssh_port, web_port = self.start("thingino · Login")
        found = discover.probe("127.0.0.1", ssh_port, web_port)
        self.assertEqual(found, {"host": "127.0.0.1", "banner": "SSH-2.0-dropbear_2026.94", "thingino": True})

    def test_other_ssh_hosts_are_listed_but_not_marked(self):
        ssh_port, web_port = self.start("My Router")
        self.assertIs(discover.probe("127.0.0.1", ssh_port, web_port)["thingino"], False)

    def test_host_without_a_web_page_is_still_found(self):
        ssh = banner_server()
        self.closers = [ssh.close]
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            dead = s.getsockname()[1]
        found = discover.probe("127.0.0.1", ssh.getsockname()[1], dead, timeout=0.5)
        self.assertIs(found["thingino"], False)

    def test_closed_port_means_nothing_there(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self.assertIsNone(discover.probe("127.0.0.1", port, 80, timeout=0.5))

    def test_scan_reports_progress_and_only_hosts_with_ssh(self):
        ssh_port, web_port = self.start("thingino · Login")
        seen = []
        found = discover.scan(["127.0.0.1", "127.0.0.2", "127.0.0.3"], ssh_port, web_port,
                              progress=lambda done, total: seen.append((done, total)))
        self.assertEqual([f["host"] for f in found], ["127.0.0.1"])
        self.assertEqual(seen[-1], (3, 3))

    def test_scan_can_be_cancelled(self):
        stop = threading.Event()
        stop.set()
        self.assertEqual(discover.scan(["127.0.0.1"] * 5, 9, 9, stop=stop), [])

    def test_local_network_is_a_slash_24_or_none(self):
        network = discover.local_network()
        if network is not None:
            self.assertEqual(network.prefixlen, 24)
            self.assertFalse(str(network.network_address).startswith("127."))


CATALOG = {"data": [
    {"id": "google/gemini-2.5-flash-lite", "name": "Gemini 2.5 Flash Lite",
     "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]},
     "pricing": {"prompt": "0.0000001", "completion": "0.0000004"}},
    {"id": "cheap/vision", "architecture": {"input_modalities": ["image"], "output_modalities": ["text"]},
     "pricing": {"prompt": "0.00000005", "completion": "0.0000001"}},
    {"id": "google/gemma-4-31b-it:free", "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]},
     "pricing": {"prompt": "0", "completion": "0"}},
    {"id": "nvidia/big-reasoning:free", "name": "Big (reasoning)",
     "architecture": {"input_modalities": ["image"], "output_modalities": ["text"]}, "pricing": {"prompt": "0", "completion": "0"}},
    {"id": "free/without-suffix", "architecture": {"input_modalities": ["image"], "output_modalities": ["text"]},
     "pricing": {"prompt": "0", "completion": "0"}},
    {"id": "openrouter/free", "architecture": {"input_modalities": ["image"], "output_modalities": ["text"]},
     "pricing": {"prompt": "0", "completion": "0"}},
    {"id": "google/lyria-3", "architecture": {"input_modalities": ["image"], "output_modalities": ["text", "audio"]},
     "pricing": {"prompt": "0", "completion": "0"}},
    {"id": "openai/batchy:batch", "architecture": {"input_modalities": ["image"], "output_modalities": ["text"]},
     "pricing": {"prompt": "0.0000001", "completion": "0.0000001"}},
    {"id": "text/only", "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
     "pricing": {"prompt": "0.0000001", "completion": "0.0000001"}},
    {"id": "evil/id;rm -rf", "architecture": {"input_modalities": ["image"], "output_modalities": ["text"]},
     "pricing": {"prompt": "0.0000001", "completion": "0.0000001"}},
    {"id": "no/price", "architecture": {"input_modalities": ["image"], "output_modalities": ["text"]}, "pricing": {}},
    {"id": "router/variable", "architecture": {"input_modalities": ["image"], "output_modalities": ["text"]},
     "pricing": {"prompt": "-1", "completion": "-1"}},
]}


class OpenRouterTests(unittest.TestCase):
    def test_catalog_keeps_only_usable_models(self):
        catalog = openrouter.parse_catalog(CATALOG)
        self.assertEqual([m["id"] for m in catalog["paid"]],
                         ["cheap/vision", "google/gemini-2.5-flash-lite", "no/price", "router/variable"])
        variable = catalog["paid"][-1]
        self.assertNotEqual(variable["input"], variable["input"])  # a "varies" price is unknown, not negative
        self.assertEqual([m["id"] for m in catalog["free"]],
                         ["free/without-suffix", "google/gemma-4-31b-it:free", "nvidia/big-reasoning:free"])
        self.assertAlmostEqual(catalog["paid"][1]["input"], 0.1)
        free = {m["id"]: m for m in catalog["free"]}
        self.assertTrue(free["nvidia/big-reasoning:free"]["reasoning"])
        self.assertFalse(free["google/gemma-4-31b-it:free"]["reasoning"])

    def test_catalog_from_nothing(self):
        self.assertEqual(openrouter.parse_catalog({}), {"paid": [], "free": []})
        self.assertEqual(openrouter.parse_catalog(None), {"paid": [], "free": []})

    def test_fetch_catalog(self):
        got = openrouter.fetch_catalog(http=lambda url, headers, timeout: (200, json.dumps(CATALOG).encode()))
        self.assertEqual(len(got["free"]), 3)
        with self.assertRaises(OSError):
            openrouter.fetch_catalog(http=lambda url, headers, timeout: (503, b""))

    def test_check_model(self):
        catalog = openrouter.parse_catalog(CATALOG)
        self.assertTrue(openrouter.check_model(catalog, "google/gemini-2.5-flash-lite")[0])
        self.assertTrue(openrouter.check_model(catalog, "google/gemma-4-31b-it:free")[0])
        ok, message = openrouter.check_model(catalog, "google/gemini-2.5-flash-lit")
        self.assertFalse(ok)
        self.assertIn("spelling", message)

    def test_check_key(self):
        sent = {}

        def reply(status, body=b"{}"):
            def http(url, headers, timeout):
                sent.update(url=url, headers=headers)
                return status, body
            return http

        info = {"data": {"label": "sk-or-v1-abc...xyz", "limit": 5, "limit_remaining": 3.5, "is_free_tier": True}}
        ok, message = openrouter.check_key("sk-or-v1-key", http=reply(200, json.dumps(info).encode()))
        self.assertTrue(ok)
        self.assertEqual(sent["headers"]["Authorization"], "Bearer sk-or-v1-key")
        self.assertTrue(sent["url"].startswith("https://openrouter.ai/"))
        for fragment in ("sk-or-v1-abc...xyz", "$3.50 of its $5.00", "free tier"):
            self.assertIn(fragment, message)
        self.assertEqual(openrouter.check_key("k", http=reply(200, b"not json"))[0], True)
        self.assertEqual(openrouter.check_key("k", http=reply(401)), (False, "OpenRouter does not recognise this key."))
        self.assertIsNone(openrouter.check_key("k", http=reply(500))[0])
        self.assertEqual(openrouter.check_key("", http=reply(200))[0], False)

        def offline(url, headers, timeout):
            raise OSError("no route")

        ok, message = openrouter.check_key("k", http=offline)
        self.assertIsNone(ok)
        self.assertIn("no route", message)


TOKEN = "123456789:AAH-abcdefghijklmnopqrstuvwxyz_0123"


class FakeTelegram(BaseHTTPRequestHandler):
    seen = []
    mode = "ok"

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        method = self.path.rsplit("/", 1)[-1]
        FakeTelegram.seen.append((self.path, method, body))
        if "/bot" + TOKEN + "/" not in self.path:
            return self.reply(401, {"ok": False, "error_code": 401, "description": "Unauthorized"})
        if FakeTelegram.mode == "webhook" and method == "getUpdates":
            return self.reply(409, {"ok": False, "error_code": 409,
                                    "description": "Conflict: can't use getUpdates method while webhook is active"})
        if method == "getMe":
            return self.reply(200, {"ok": True, "result": {"username": "cool_cam_bot", "first_name": "Cool Cam"}})
        if method == "getUpdates":
            updates = [
                {"update_id": 1, "message": {"chat": {"id": 42, "type": "private", "first_name": "Sam", "last_name": "Lee", "username": "sam"}}},
                {"update_id": 2, "message": {"chat": {"id": -1001, "type": "group", "title": "Family"}}},
                {"update_id": 3, "message": {"chat": {"id": 42, "type": "private", "first_name": "Sam"}}},
                {"update_id": 4, "channel_post": {"chat": {"id": -1002, "type": "channel", "title": "Cam channel"}}},
                {"update_id": 5, "edited_message": {"chat": {"id": 7, "type": "private", "username": "only_username"}}},
                {"update_id": 6, "something_else": {}},
            ]
            return self.reply(200, {"ok": True, "result": updates})
        if method == "sendMessage":
            return self.reply(200, {"ok": True, "result": {"message_id": 1}})
        return self.reply(404, {"ok": False, "error_code": 404, "description": "Not Found"})

    def reply(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class TelegramTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = serve(FakeTelegram)
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        FakeTelegram.seen = []
        FakeTelegram.mode = "ok"

    def test_rejects_things_that_are_not_tokens_without_any_request(self):
        for bad in ("", "hello", "123:short", "abc:AAH-abcdefghijklmnopqrstuvwxyz_0123"):
            with self.assertRaises(tg.TelegramError) as caught:
                tg.get_me(bad, base=self.base)
            self.assertIn("bot token", str(caught.exception))
        self.assertEqual(FakeTelegram.seen, [])

    def test_get_me(self):
        self.assertEqual(tg.get_me(TOKEN, base=self.base), {"username": "cool_cam_bot", "name": "Cool Cam"})

    def test_unknown_token_gets_a_plain_message(self):
        with self.assertRaises(tg.TelegramError) as caught:
            tg.get_me("123456789:ZZZ-abcdefghijklmnopqrstuvwxyz_0123", base=self.base)
        self.assertEqual(str(caught.exception), "Telegram does not recognise this token.")

    def test_find_chats_lists_each_chat_once_newest_first(self):
        chats = tg.find_chats(TOKEN, base=self.base)
        self.assertEqual([(c["id"], c["name"], c["type"]) for c in chats],
                         [(7, "@only_username", "private"), (-1002, "Cam channel", "channel"),
                          (42, "Sam", "private"), (-1001, "Family", "group")])

    def test_find_chats_explains_a_webhook(self):
        FakeTelegram.mode = "webhook"
        with self.assertRaises(tg.TelegramError) as caught:
            tg.find_chats(TOKEN, base=self.base)
        self.assertIn("webhook", str(caught.exception))

    def test_send_test_posts_to_the_chat(self):
        tg.send_test(TOKEN, -1001, "hello", base=self.base)
        path, method, body = FakeTelegram.seen[-1]
        self.assertEqual((method, body), ("sendMessage", {"chat_id": -1001, "text": "hello"}))

    def test_errors_never_contain_the_token(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        with self.assertRaises(tg.TelegramError) as caught:
            tg.get_me(TOKEN, base="http://127.0.0.1:%d" % port, timeout=2)
        self.assertNotIn(TOKEN, str(caught.exception))
        self.assertIn("Could not reach Telegram", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
