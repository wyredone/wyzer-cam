"""Drives the installer window end to end: the SSH flow against an in-process
SSH server that runs commands on a fake camera, the SD card flow into a folder,
and the helpers (Telegram, OpenRouter, camera search) with the network replaced.

Skipped when there is no display or no paramiko.
Run:  python -m unittest discover -s test -v
"""
import gc
import ipaddress
import json
import os
import shutil
import sys
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "installer"))
sys.path.insert(0, str(HERE))
import camera_ssh  # noqa: E402
import discover  # noqa: E402
import openrouter  # noqa: E402
import telegram_helper  # noqa: E402
import test_camera as tc  # noqa: E402
import test_helpers as th  # noqa: E402
import wyzer_core as core  # noqa: E402

try:
    import wyzer_installer as wi
except Exception:  # pragma: no cover
    wi = None


def tk_available():
    try:
        root = tk.Tk()
        root.destroy()
        return True
    except tk.TclError:
        return False


@unittest.skipUnless(wi and tk_available() and tc.paramiko and tc.SH, "needs a display, paramiko and sh")
class AppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wyzer-gui-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        for name, sub in (("WYZER_HOME", "home"), ("WYZER_SSH_DIR", "ssh")):
            os.environ[name] = str(self.tmp / sub)
            self.addCleanup(os.environ.pop, name, None)
        real_fetch = openrouter.fetch_catalog
        openrouter.fetch_catalog = lambda timeout=15, http=None: openrouter.parse_catalog(th.CATALOG)
        self.addCleanup(setattr, openrouter, "fetch_catalog", real_fetch)

        self.fake = tc.FakeCameraRoot()
        self.addCleanup(self.fake.cleanup)
        local = self.fake.executor()
        self.server = tc.SshServer(lambda cmd, stdin: local.run(cmd, stdin),
                                   authorized_file=self.fake.home / ".ssh" / "authorized_keys")
        self.addCleanup(self.server.close)

        self.root = tk.Tk()
        self.app = wi.InstallerApp(self.root)
        self.addCleanup(self.close_window)
        self.dialogs = []
        self.answers = {}
        app = self.app
        app.confirm = lambda title, text: (self.dialogs.append(("confirm", title, text)), self.answers.get(title, True))[1]
        app.inform = lambda title, text: self.dialogs.append(("inform", title, text))
        app.error = lambda title, text: self.dialogs.append(("error", title, text))
        self.pump(lambda: app.catalog is not None)

    # --- helpers ---

    def close_window(self):
        """Destroys the window and frees its tkinter variables on this thread.

        tkinter finalises its variables when they are garbage collected, and that
        deadlocks if the collection happens on another thread (such as the fake SSH
        server's) while this one is in the Tk loop. So collect here, now.
        """
        if self.app.ex:
            self.app.ex.close()
        self.root.destroy()
        self.app.v.clear()
        self.app = self.root = None
        gc.collect()

    def pump(self, condition, timeout=120):
        end = time.time() + timeout
        while time.time() < end:
            self.root.update()
            if condition():
                return
            time.sleep(0.01)
        import faulthandler
        faulthandler.dump_traceback(file=sys.stderr, all_threads=True)  # where is everything stuck?
        self.fail("timed out waiting (busy=%s)\nlog: %s\ndialogs: %s" % (self.app.busy, self.log_text(), self.dialogs))

    def idle(self):
        """Waits until no task is running, also across tasks that start the next one."""
        quiet = [0]

        def check():
            quiet[0] = quiet[0] + 1 if not self.app.busy else 0
            return quiet[0] >= 5

        self.pump(check)

    def log_text(self):
        return self.app.log.get("1.0", "end")

    def titles(self, kind=None):
        return [t for k, t, _ in self.dialogs if kind in (None, k)]

    def address(self):
        return "127.0.0.1:%d" % self.server.port

    def connect(self, password="pw", expect_ready=True):
        self.app.v["host"].set(self.address())
        self.app.v["ssh_password"].set(password)
        self.app.v["use_key"].set(False)
        self.app.connect()
        self.idle()
        if password == "pw" and expect_ready:  # a good login must work, and if not we want to know why
            self.assertTrue(self.app.camera_ready, "connect failed: %s | dialogs: %s" % (self.log_text(), self.dialogs))

    def good_settings(self, **changes):
        s = dict(core.DEFAULTS)
        s.update(api_key="sk-gui", rules=["A person is at the door"], tg_token="123456789:ABCDEFGHIJKLMNOPQRSTUV",
                 tg_channel="-100", ntfy_host="ntfy.sh")
        s.update(changes)
        self.app.fill(s)

    # --- the window itself ---

    def test_starts_with_the_recommended_model_and_defaults(self):
        s = self.app.collect()
        self.assertEqual(s["model"], "google/gemini-2.5-flash-lite")
        self.assertEqual((s["free_pool"], s["free_models"], s["rules"]), (False, [], []))
        for key in core.DEFAULTS:
            self.assertIn(key, s)
        self.assertEqual(self.app.v["user"].get(), "root")

    def test_catalog_fills_the_pickers(self):
        self.assertIn("google/gemini-2.5-flash-lite", self.app.model_box["values"])
        self.assertEqual(self.app.pool_ids, ["free/without-suffix", "google/gemma-4-31b-it:free", "nvidia/big-reasoning:free"])
        self.assertIn("reasoning", self.app.pool_list.get(2))
        self.app.v["model"].set("google/gemini-2.5-flash-lite")
        self.app.show_model_info()
        self.assertEqual(self.app.v["model_info"].get(), "$0.10 in / $0.40 out per million tokens")
        self.app.v["model"].set("google/gemini-2.5-flash-lit")
        self.app.check_model()
        self.assertIn("spelling", self.app.v["model_info"].get())

    def test_pool_selection_round_trip_and_unknown_models_are_kept(self):
        self.good_settings(free_pool=True, free_models=["google/gemma-4-31b-it:free", "gone/model:free"])
        s = self.app.collect()
        self.assertIs(s["free_pool"], True)
        self.assertEqual(sorted(s["free_models"]), ["gone/model:free", "google/gemma-4-31b-it:free"])
        self.assertIn("not in the current list", self.app.pool_list.get(self.app.pool_ids.index("gone/model:free")))
        self.assertEqual(core.check_settings(s)[0], [])

    def test_parse_address(self):
        self.assertEqual(wi.parse_address("192.168.1.5"), ("192.168.1.5", 22))
        self.assertEqual(wi.parse_address("192.168.1.5:2222"), ("192.168.1.5", 2222))
        self.assertEqual(wi.parse_address("192.168.1.5    thingino camera"), ("192.168.1.5", 22))
        self.assertEqual(wi.parse_address("  "), ("", 22))
        self.assertEqual(wi.parse_address("fe80::1"), ("fe80::1", 22))

    def test_find_cameras_fills_the_list_and_picks_a_thingino_camera(self):
        found = [{"host": "192.168.1.1", "banner": "SSH-2.0-OpenSSH_9", "thingino": False},
                 {"host": "192.168.1.185", "banner": "SSH-2.0-dropbear", "thingino": True}]
        real = discover.scan_local
        discover.scan_local = lambda progress=None, stop=None: (found, ipaddress.ip_network("192.168.1.0/24"))
        self.addCleanup(setattr, discover, "scan_local", real)
        self.app.find_cameras()
        self.idle()
        self.assertEqual(wi.parse_address(self.app.v["host"].get())[0], "192.168.1.185")
        self.assertEqual(len(self.app.host_box["values"]), 2)
        self.assertIn("thingino camera", self.app.host_box["values"][1])
        self.assertIn("1 look like thingino", self.log_text())

    # --- SSH flow ---

    def test_connect_install_test_update_and_uninstall(self):
        self.connect()
        self.assertTrue(self.app.camera_ready, self.log_text())
        self.assertIn("ready", self.app.v["camera_status"].get())
        self.assertEqual(self.titles("confirm"), ["First time with this camera"])
        self.assertIn("SHA256:", self.dialogs[0][2])
        self.assertEqual(self.app.known.check(self.address(), camera_ssh.fingerprint(self.server.host_key)), "known")

        self.good_settings()
        self.dialogs.clear()
        self.app.install_ssh()
        self.idle()
        self.assertEqual(self.titles("error"), [], self.dialogs)
        self.assertIn("installed /usr/sbin/aiwatch", self.log_text())
        self.assertIn("Test result:", self.log_text())
        self.assertIn("description: A courier.", self.log_text())
        root = self.fake.root
        self.assertEqual(json.loads((root / "etc/aiwatch.json").read_text())["api_key"], "sk-gui")
        self.assertEqual(json.loads((root / "etc/send2.json").read_text())["telegram"]["channel"], "-100")
        self.assertTrue((root / "var/www/config-aiwatch.html").is_file())
        profile = (self.tmp / "home" / "profile.json").read_text()
        self.assertNotIn("sk-gui", profile)
        self.assertNotIn("ABCDEFGHIJKLMNOPQRSTUV", profile)
        self.assertIn(self.address(), profile)

        # reconnect: the camera has settings, load them; a blank key keeps the one on the camera
        self.dialogs.clear()
        self.app.connect()
        self.idle()
        self.assertEqual(self.titles("confirm"), ["Load the camera's settings?"])
        self.assertEqual(self.app.collect()["rules"], ["A person is at the door"])
        self.assertEqual(self.app.v["api_key"].get(), "")
        self.app.v["model"].set("other/model")
        self.dialogs.clear()
        self.app.install_ssh()
        self.idle()
        config = json.loads((root / "etc/aiwatch.json").read_text())
        self.assertEqual((config["api_key"], config["model"]), ("sk-gui", "other/model"))
        self.assertTrue(any("No API key is set" in text for _, _, text in self.dialogs))  # warned, then went ahead
        self.assertEqual(self.titles("error"), [])

        # send a real alert only after asking
        self.dialogs.clear()
        self.answers["Send a real alert?"] = False
        self.app.test_ssh(True)
        self.idle()
        self.assertNotIn("aiwatch test -n", self.fake.called())
        self.answers["Send a real alert?"] = True
        self.app.test_ssh(True)
        self.idle()
        self.assertIn("aiwatch test -n", self.fake.called())

        # uninstall, keeping the settings
        self.answers["Keep the settings?"] = True
        self.app.uninstall_ssh()
        self.idle()
        self.assertFalse((root / "usr/sbin/aiwatch").exists())
        self.assertTrue((root / "etc/aiwatch.json").is_file())
        self.assertEqual((root / "usr/sbin/motion").read_bytes(), tc.MOTION.encode())

    def test_wifi_change_needs_a_confirmation_and_offers_a_restart(self):
        self.connect()
        self.good_settings(wifi_ssid="HomeNet", wifi_pass="password1")
        self.answers["Change the camera's Wi-Fi?"] = False
        self.app.install_ssh()
        self.idle()
        self.assertFalse((self.fake.root / "usr/sbin/aiwatch").exists())
        self.answers["Change the camera's Wi-Fi?"] = True
        self.answers["Restart the camera?"] = True
        self.app.install_ssh()
        self.idle()
        self.assertIn("wlan [configure] [HomeNet] [password1]", self.fake.called(),
                      "dialogs: %s | log: %s" % (self.dialogs, self.log_text()))
        self.assertIn("Restart the camera?", self.titles("confirm"))
        self.assertIn("reboot", self.fake.called())
        self.assertIsNone(self.app.ex)
        self.assertIn("Restarting", self.app.v["camera_status"].get())

    def test_bad_settings_are_stopped_before_touching_the_camera(self):
        self.connect()
        self.good_settings(free_pool=True, free_models=[], active_from="22:00")
        self.dialogs.clear()
        self.app.install_ssh()
        self.idle()
        self.assertEqual(self.titles("error"), ["Check the settings"])
        self.assertIn("free model", self.dialogs[-1][2])
        self.assertFalse((self.fake.root / "usr/sbin/aiwatch").exists())

    def test_wrong_password_is_reported(self):
        self.connect(password="wrong")
        self.assertFalse(self.app.camera_ready)
        self.assertEqual(self.titles("error"), ["Connect"])
        self.assertIn("rejected the login", self.dialogs[-1][2])
        self.assertEqual(self.app.v["camera_status"].get(), "Not connected.")

    def test_a_changed_camera_identity_needs_a_deliberate_yes(self):
        self.app.known.remember(self.address(), "SHA256:someoneelse")
        self.answers["This camera's identity changed"] = False
        self.connect(expect_ready=False)
        self.assertIsNone(self.app.ex)
        self.assertIn("Cancelled", self.log_text())
        self.answers["This camera's identity changed"] = True
        self.connect()
        self.assertTrue(self.app.camera_ready)
        self.assertEqual(self.app.known.check(self.address(), camera_ssh.fingerprint(self.server.host_key)), "known")

    def test_a_camera_that_is_not_thingino_is_refused(self):
        (self.fake.root / "etc/os-release").write_text("NAME=Wyze\n")
        self.connect(expect_ready=False)
        self.assertFalse(self.app.camera_ready)
        self.assertIn("cannot be used", self.app.v["camera_status"].get())
        self.assertIn("SD card tab", self.log_text())
        self.dialogs.clear()
        self.app.install_ssh()
        self.assertEqual(self.titles("inform"), ["Not connected"])

    def test_setting_up_key_login(self):
        self.connect()
        self.answers["Create an SSH key?"] = True
        self.dialogs.clear()
        self.app.setup_key()
        self.idle()
        self.assertEqual(self.titles("error"), [], self.dialogs)
        self.assertEqual(self.titles("inform"), ["Key login works"])
        self.assertTrue((self.tmp / "ssh" / "id_ed25519").exists() or (self.tmp / "ssh" / "id_ecdsa").exists())
        self.assertEqual(len((self.fake.home / ".ssh" / "authorized_keys").read_text().splitlines()), 1)
        # now connect with the key alone
        self.app.v["use_key"].set(True)
        self.app.v["ssh_password"].set("")
        self.app.connect()
        self.idle()
        self.assertTrue(self.app.camera_ready)
        self.assertEqual(self.app.ex.login, "key")

    # --- SD card flow ---

    def test_write_the_sd_card_folder(self):
        card = self.tmp / "card"
        card.mkdir()
        self.good_settings(wifi_ssid="HomeNet", wifi_pass="password1")
        self.app.v["target"].set(str(card))
        self.app.write_sd()
        self.idle()
        self.assertEqual(self.titles("error"), [], self.dialogs)
        self.assertTrue((card / "runonce.sh").is_file())
        self.assertEqual(json.loads((card / "wyzer/aiwatch.json").read_text())["api_key"], "sk-gui")
        self.assertIn("Card is ready", self.log_text())
        self.assertNotIn("WYZE_BACKUP", self.log_text())

    def test_sd_card_install_of_thingino_checks_the_card_first(self):
        card = self.tmp / "card"
        card.mkdir()
        self.good_settings()
        self.app.v["target"].set(str(card))
        self.app.v["flash"].set(True)
        real = core.card_info
        core.card_info = lambda path: ("exFAT", 10 ** 11, 128 * 10 ** 9)
        self.addCleanup(setattr, core, "card_info", real)
        self.app.write_sd()
        self.assertEqual(self.titles("error"), ["Cannot write the card"])
        self.assertIn("guiformat", self.dialogs[-1][2])
        self.assertFalse((card / "runonce.sh").exists())

    # --- helpers ---

    def test_telegram_helpers(self):
        real = (telegram_helper.get_me, telegram_helper.find_chats, telegram_helper.send_test)
        self.addCleanup(lambda: [setattr(telegram_helper, n, f) for n, f in
                                 zip(("get_me", "find_chats", "send_test"), real)])
        sent = []
        telegram_helper.get_me = lambda token: {"username": "cool_cam_bot", "name": "Cool Cam"}
        telegram_helper.find_chats = lambda token: [{"id": 42, "name": "Sam", "type": "private"},
                                                     {"id": -7, "name": "Family", "type": "group"}]
        telegram_helper.send_test = lambda token, chat: sent.append((token, chat))
        self.app.v["tg_token"].set("123456789:ABCDEFGHIJKLMNOPQRSTUV")
        self.app.check_bot()
        self.idle()
        self.assertEqual(self.app.v["bot_status"].get(), "✓ Bot @cool_cam_bot")
        self.app.choose_chat = lambda chats: chats[1]
        self.app.find_chat()
        self.idle()
        self.assertEqual(self.app.v["tg_channel"].get(), "-7")
        self.app.send_test_message()
        self.idle()
        self.assertEqual(sent, [("123456789:ABCDEFGHIJKLMNOPQRSTUV", "-7")])
        telegram_helper.find_chats = lambda token: []
        self.dialogs.clear()
        self.app.find_chat()
        self.idle()
        self.assertEqual(self.titles("inform"), ["No messages found"])

        def broken(token):
            raise telegram_helper.TelegramError("Telegram does not recognise this token.")

        telegram_helper.get_me = broken
        self.app.check_bot()
        self.idle()
        self.assertIn("does not recognise", self.app.v["bot_status"].get())

    def test_key_check_only_talks_to_openrouter(self):
        real = openrouter.check_key
        self.addCleanup(setattr, openrouter, "check_key", real)
        calls = []
        openrouter.check_key = lambda key: (calls.append(key), (True, "The key is accepted."))[1]
        self.app.v["api_key"].set("sk-or-v1-abc")
        self.app.check_key()
        self.idle()
        self.assertEqual(calls, ["sk-or-v1-abc"])
        self.assertEqual(self.app.v["key_status"].get(), "✓ The key is accepted.")
        self.app.v["endpoint"].set("http://192.168.1.50:11434/v1/chat/completions")
        self.app.check_key()
        self.idle()
        self.assertEqual(calls, ["sk-or-v1-abc"])  # a local server never gets the key
        self.assertIn("only available for OpenRouter", self.app.v["key_status"].get())


if __name__ == "__main__":
    unittest.main()
