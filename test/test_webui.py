"""Tests for the AI Alerts page backend (json-aiwatch.cgi) and its files.

Run:  python -m unittest discover -s test -v
Needs `sh` on PATH (Git Bash on Windows).
"""
import json
import re
import sys
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fake_camera  # noqa: E402

FILES = fake_camera.FILES


@unittest.skipUnless(fake_camera.SH, "sh not found")
class BackendTests(unittest.TestCase):
    def setUp(self):
        self.cam = fake_camera.FakeCamera()
        self.addCleanup(self.cam.cleanup)

    def settings(self, **changes):
        config = json.loads((FILES / "aiwatch.json").read_text(encoding="utf-8"))
        config.update(changes)
        return config

    def test_get_returns_settings(self):
        status, data, _ = self.cam.request()
        self.assertEqual(status, 200)
        self.assertEqual(data["endpoint"], "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(data["active_from"], "")

    def test_post_saves_and_returns_settings(self):
        new = self.settings(enabled=True, model="test/vision", api_key='k"ey\\1',
                            rules=["A person is at the door", "Señor's car “quoted”"],
                            extra_instructions="Ignore the cat", active_from="22:00", active_to="06:00")
        status, data, _ = self.cam.request(body=json.dumps(new, indent=2).encode("utf-8"))
        self.assertEqual(status, 200)
        self.assertEqual(data, new)
        self.assertEqual(json.loads(self.cam.config.read_text(encoding="utf-8")), new)
        self.assertFalse(Path(str(self.cam.config) + ".new").exists())

    def test_post_rejects_bad_settings(self):
        before = self.cam.config.read_bytes()
        for body in (b"not json at all", b'{"hello": 1}', b"[1, 2, 3]", b"{", b"x" * 20000):
            status, data, _ = self.cam.request(body=body)
            self.assertEqual(status, 400, body[:20])
            self.assertIs(data["ok"], False)
            self.assertEqual(self.cam.config.read_bytes(), before)
            self.assertFalse(Path(str(self.cam.config) + ".new").exists())

    def test_requires_auth(self):
        (self.cam.dir / "deny").write_text("1")
        before = self.cam.config.read_bytes()
        for query, body in (("", None), ("action=test_notify", None), ("", b'{"endpoint":"x","enabled":true}')):
            status, data, text = self.cam.request(query, body)
            self.assertEqual(status, 401)
            self.assertEqual(text, "")
        self.assertEqual(self.cam.config.read_bytes(), before)
        self.assertEqual(self.cam.called(), "")

    def test_status(self):
        status, data, _ = self.cam.request("action=status")
        self.assertEqual(status, 200)
        self.assertEqual(data, {"ok": True, "installed": True, "hooked": True, "motion_enabled": False,
                                "calls_today": 0, "events": []})

        (self.cam.state / "count").write_bytes((time.strftime("%Y%m%d") + " 7\n").encode())
        events = [{"time": "2026-11-15T20:30:00-0600", "status": "ok", "notify": True,
                   "description": "A dog “runs” past", "rule": ""},
                  {"time": "2026-11-15T20:31:00-0600", "status": "error", "notify": False,
                   "description": "", "rule": ""}]
        lines = [json.dumps(e, ensure_ascii=False) for e in events] + ["half a line {"]
        (self.cam.state / "events.jsonl").write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
        self.cam.motion.write_text("#!/bin/sh\necho no hook here\n")
        _, data, _ = self.cam.request("action=status")
        self.assertEqual(data["calls_today"], 7)
        self.assertEqual(data["events"], events)
        self.assertIs(data["hooked"], False)

    def test_status_ignores_yesterdays_count(self):
        (self.cam.state / "count").write_bytes(b"20200101 99\n")
        self.assertEqual(self.cam.request("action=status")[1]["calls_today"], 0)

    def test_test_actions(self):
        _, data, _ = self.cam.request("action=test")
        self.assertIs(data["ok"], True)
        self.assertEqual(data["output"].splitlines()[0], "description: A courier leaves a parcel at the door.")
        self.assertEqual(len(data["output"].splitlines()), 3)
        _, data, _ = self.cam.request("action=test_notify")
        self.assertIs(data["ok"], True)
        self.assertEqual(self.cam.called().splitlines(), ["aiwatch test", "aiwatch test -n"])

    def test_failed_test_reports_output(self):
        (self.cam.dir / "aiwatch_fails").write_text("1")
        status, data, _ = self.cam.request("action=test")
        self.assertEqual(status, 200)
        self.assertIs(data["ok"], False)
        self.assertEqual(data["output"], 'aiwatch: request failed: HTTP 401 "bad" key')

    def test_motion_on(self):
        _, data, _ = self.cam.request("action=motion_on")
        self.assertIs(data["ok"], True)
        self.assertIn("prudyntctl json -", self.cam.called())
        self.assertIs(self.cam.request("action=status")[1]["motion_enabled"], True)


class FileTests(unittest.TestCase):
    def test_manifest(self):
        manifest = json.loads((FILES / "aiwatch.webui.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], "aiwatch")
        self.assertEqual(manifest["pages"], ["/config-aiwatch.html"])
        self.assertEqual(manifest["nav"][0]["items"][0]["href"], "/config-aiwatch.html")

    def test_page_and_script_agree_on_fields(self):
        html = (FILES / "www/config-aiwatch.html").read_text(encoding="utf-8")
        script = (FILES / "www/a/config-aiwatch.js").read_text(encoding="utf-8")
        ids = set(re.findall(r'\bid="([^"]+)"', html))
        used = set(re.findall(r'\bel\("([^"]+)"\)', script))
        for group in re.findall(r"const (?:TEXT_FIELDS|SWITCHES) = \[(.*?)\]", script, re.S):
            used |= set(re.findall(r'"([^"]+)"', group))
        used |= set(re.findall(r"(\w+):", re.search(r"const NUMBERS = \{(.*?)\}", script).group(1)))
        self.assertEqual(used - ids, set())
        # Every setting aiwatch reads can be edited on the page.
        defaults = json.loads((FILES / "aiwatch.json").read_text(encoding="utf-8"))
        editable = ids | {"services"}
        self.assertEqual(set(defaults) - editable, set())

    def test_shell_files_have_unix_line_endings(self):
        for name in ("www/x/json-aiwatch.cgi", "aiwatch"):
            self.assertNotIn(b"\r", (FILES / name).read_bytes(), name)


if __name__ == "__main__":
    unittest.main()
