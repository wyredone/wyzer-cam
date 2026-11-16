"""Tests for the SD card installer and the runonce.sh it puts on the card.

Run:  python -m unittest discover -s test -v
The runonce tests need `sh` on PATH (Git Bash on Windows) and are skipped without it.
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "installer"))
import wyzer_installer as wi  # noqa: E402

SH = shutil.which("sh")

MOTION_FIXTURE = """#!/bin/sh
start() {
\ttouch "$MOTION_ALARM"

\t# Notify Home Assistant immediately (if installed)
\t[ -x /usr/sbin/ha-event ] && ha-event motion ON &

\t# Trigger the Wyze Floodlight v1 night-time motion lamp (if installed)
\t[ -x /usr/sbin/floodlight_ctl ] && floodlight_ctl motion &

\t# Audible alert
}
"""


def settings(**changes):
    s = dict(wi.DEFAULTS)
    s.update(model="test/vision", api_key="sk-test", rules=["A person is at the door"])
    s.update(changes)
    return s


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wyzer-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_validate_accepts_good_settings(self):
        errors, warnings = wi.validate(settings(tg_token="t", tg_channel="1"), self.tmp)
        self.assertEqual((errors, warnings), ([], []))

    def test_validate_errors(self):
        bad = settings(wifi_ssid="net", wifi_pass="short", model="", cooldown="x", tg_token="t",
                       endpoint="openrouter.ai")
        errors, _ = wi.validate(bad, self.tmp / "missing")
        text = " ".join(errors)
        for expected in ("Choose the SD card", "8 to 64", "model id", "Cooldown", "Telegram needs", "http://"):
            self.assertIn(expected, text)

    def test_validate_warns_without_key_or_service(self):
        errors, warnings = wi.validate(settings(api_key=""), self.tmp)
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 2)

    def test_prepare_card_writes_expected_files(self):
        s = settings(wifi_ssid='My "Net" $HOME', wifi_pass="pa ss'word;rm", tg_token="123:abc",
                     tg_channel="-100", ntfy_topic="cam")
        (self.tmp / "runonce.done").write_text("x")
        wi.prepare_card(self.tmp, s)

        self.assertFalse((self.tmp / "runonce.done").exists())
        for name in ("runonce.sh", "wyzer/aiwatch"):
            data = (self.tmp / name).read_bytes()
            self.assertTrue(data.startswith(b"#!/bin/sh\n"), name)
            self.assertNotIn(b"\r", data, name)
        cfg = json.loads((self.tmp / "wyzer/aiwatch.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["model"], "test/vision")
        self.assertEqual(cfg["services"], ["telegram", "ntfy"])
        self.assertEqual(cfg["rules"], ["A person is at the door"])
        self.assertIs(cfg["enabled"], True)
        self.assertEqual(cfg["cooldown"], 60)
        overlay = json.loads((self.tmp / "wyzer/send2.json").read_text(encoding="utf-8"))
        self.assertEqual(overlay["telegram"], {"token": "123:abc", "channel": "-100"})
        self.assertEqual(overlay["ntfy"]["topic"], "cam")
        self.assertEqual((self.tmp / "wyzer/wifi_ssid").read_bytes(), b'My "Net" $HOME')
        self.assertEqual((self.tmp / "wyzer/wifi_pass").read_bytes(), b"pa ss'word;rm")
        self.assertTrue((self.tmp / "wyzer/enable_motion").exists())

    def test_prepare_card_clears_stale_secrets(self):
        wi.prepare_card(self.tmp, settings(wifi_ssid="net", wifi_pass="password1", tg_token="t", tg_channel="1"))
        wi.prepare_card(self.tmp, settings(enable_motion=False))
        for name in ("wifi_ssid", "wifi_pass", "send2.json", "enable_motion"):
            self.assertFalse((self.tmp / "wyzer" / name).exists(), name)

    def test_foreign_runonce_detected(self):
        self.assertFalse(wi.foreign_runonce(self.tmp))
        (self.tmp / "runonce.sh").write_text("wlan configure a b\n")
        self.assertTrue(wi.foreign_runonce(self.tmp))
        wi.prepare_card(self.tmp, settings())
        self.assertFalse(wi.foreign_runonce(self.tmp))


@unittest.skipUnless(SH, "sh not found")
class RunonceTests(unittest.TestCase):
    """Runs the card's runonce.sh against a fake camera root."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wyzer-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.card = self.tmp / "card"
        self.root = self.tmp / "root"
        self.bin = self.tmp / "bin"
        self.calls = self.tmp / "calls"
        for d in (self.card, self.root / "usr/sbin", self.root / "etc", self.bin):
            d.mkdir(parents=True)
        (self.root / "usr/sbin/motion").write_bytes(MOTION_FIXTURE.encode())
        (self.root / "etc/send2.json").write_text(
            json.dumps({"telegram": {"token": "", "channel": "", "caption": "%hostname"}, "ntfy": {"topic": ""}}))
        calls = self.calls.as_posix()
        self.stub("jct", 'exec python -I "%s" "$@"' % (HERE / "mock_jct.py").as_posix())
        for name in ("wlan", "prudynt-config", "reboot", "sync"):
            self.stub(name, "{ printf '%s'; for a in \"$@\"; do printf ' [%%s]' \"$a\"; done; echo; } >>'%s'"
                      % (name, calls))

    def stub(self, name, body):
        path = self.bin / name
        path.write_bytes(("#!/bin/sh\n%s\n" % body).encode())
        path.chmod(path.stat().st_mode | stat.S_IEXEC)

    def run_card(self):
        env = dict(os.environ, WYZER_ROOT=self.root.as_posix())
        env["PATH"] = str(self.bin) + os.pathsep + env["PATH"]
        result = subprocess.run([SH, (self.card / "runonce.sh").as_posix()], env=env,
                                capture_output=True, text=True, timeout=120)
        log = self.card / "wyzer/install.log"
        return result, (log.read_text(encoding="utf-8") if log.exists() else "")

    def called(self):
        return self.calls.read_text(encoding="utf-8") if self.calls.exists() else ""

    def test_full_setup(self):
        wi.prepare_card(self.card, settings(wifi_ssid='My "Net" $HOME', wifi_pass="pa ss'word;rm",
                                            tg_token="123:abc", tg_channel="-100"))
        result, log = self.run_card()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("ERROR", log)

        self.assertEqual((self.root / "usr/sbin/aiwatch").read_bytes(), (self.card / "wyzer/aiwatch").read_bytes())
        cfg = json.loads((self.root / "etc/aiwatch.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["api_key"], "sk-test")

        motion = (self.root / "usr/sbin/motion").read_text(encoding="utf-8")
        self.assertEqual(motion.count("aiwatch motion"), 1)
        self.assertIn("floodlight_ctl motion &\n\n\t# Describe the event with a vision model and alert by rule "
                      "(if installed)\n\t[ -x /usr/sbin/aiwatch ] && aiwatch motion >/dev/null 2>&1 &\n\n\t# Audible",
                      motion)
        self.assertEqual(subprocess.run([SH, "-n", (self.root / "usr/sbin/motion").as_posix()]).returncode, 0)

        send2 = json.loads((self.root / "etc/send2.json").read_text(encoding="utf-8"))
        self.assertEqual(send2["telegram"], {"token": "123:abc", "channel": "-100", "caption": "%hostname"})

        called = self.called()
        self.assertIn("wlan [configure] [My \"Net\" $HOME] [pa ss'word;rm]", called)
        self.assertIn("prudynt-config [set] [motion.enabled] [true]", called)
        self.assertIn("reboot", called)
        self.assertTrue((self.card / "runonce.done").exists())
        for name in wi.SECRET_FILES:
            self.assertFalse((self.card / "wyzer" / name).exists(), name)
        self.assertNotIn("sk-test", log)
        self.assertNotIn("pa ss", log)

    def test_no_wifi_no_motion_means_no_reboot(self):
        wi.prepare_card(self.card, settings(enable_motion=False))
        result, log = self.run_card()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("ERROR", log)
        self.assertEqual(self.called(), "")

    def test_second_run_does_not_hook_twice(self):
        wi.prepare_card(self.card, settings(enable_motion=False))
        self.run_card()
        wi.prepare_card(self.card, settings(enable_motion=False))
        _, log = self.run_card()
        motion = (self.root / "usr/sbin/motion").read_text(encoding="utf-8")
        self.assertEqual(motion.count("aiwatch motion"), 1)
        self.assertIn("already calls aiwatch", log)

    def test_fallback_hook_position(self):
        (self.root / "usr/sbin/motion").write_bytes(b'#!/bin/sh\nstart() {\n\ttouch "$MOTION_ALARM"\n}\n')
        wi.prepare_card(self.card, settings(enable_motion=False))
        _, log = self.run_card()
        self.assertIn("fallback position", log)
        self.assertIn("aiwatch motion", (self.root / "usr/sbin/motion").read_text(encoding="utf-8"))

    def test_unhookable_motion_script_is_reported(self):
        (self.root / "usr/sbin/motion").write_bytes(b"#!/bin/sh\necho hi\n")
        wi.prepare_card(self.card, settings(enable_motion=False))
        result, log = self.run_card()
        self.assertEqual(result.returncode, 0)
        self.assertIn("ERROR: could not find where to hook", log)
        self.assertEqual((self.root / "usr/sbin/motion").read_bytes(), b"#!/bin/sh\necho hi\n")

    def test_not_thingino(self):
        (self.root / "usr/sbin/motion").unlink()
        wi.prepare_card(self.card, settings())
        result, log = self.run_card()
        self.assertEqual(result.returncode, 1)
        self.assertIn("does not look like thingino", log)
        self.assertTrue((self.card / "wyzer/aiwatch.json").exists())  # nothing consumed


if __name__ == "__main__":
    unittest.main()
