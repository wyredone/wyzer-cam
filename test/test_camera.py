"""Tests for installer/camera_ssh.py.

The camera is a directory of fake files with stand-ins for its commands.
LocalExecutor runs the real scripts against it; the SshExecutor tests run the
same commands through a real SSH connection to an in-process server.

Run:  python -m unittest discover -s test -v      (needs `sh`, e.g. Git Bash)
"""
import json
import logging
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "installer"))
sys.path.insert(0, str(HERE))
import camera_ssh as cam  # noqa: E402
import fake_camera  # noqa: E402
import wyzer_core as core  # noqa: E402

try:
    import paramiko
    logging.getLogger("paramiko").setLevel(logging.CRITICAL)
except ImportError:  # the SSH tests are skipped without it
    paramiko = None

SH = fake_camera.SH

MOTION = ("#!/bin/sh\nstart() {\n\ttouch \"$MOTION_ALARM\"\n\n"
          "\t# Trigger the Wyze Floodlight v1 night-time motion lamp (if installed)\n"
          "\t[ -x /usr/sbin/floodlight_ctl ] && floodlight_ctl motion &\n\n"
          "\t# Audible alert\n}\n")
PLUGINS = "// Generated\n(function(){var cfg=window.thinginoUIConfig||{};window.thinginoUIConfig=cfg;cfg.plugins={\"p\":{}};})();\n"
OS_RELEASE = 'NAME=Thingino\nID=thingino\nIMAGE_ID="wyze_cam3_t31x_gc2053_rtl8189ftv"\nBUILD_TIME="2026-08-14 03:38:36"\n'


def settings(**changes):
    s = dict(core.DEFAULTS)
    s.update(api_key="sk-test", rules=["A person is at the door"], tg_token="123456789:ABCDEFGHIJKLMNOPQRSTUV",
             tg_channel="-100")
    s.update(changes)
    return s


class FakeCameraRoot:
    """A temp directory laid out like the camera, plus stubs for its tools."""

    def __init__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="wyzer-cam-"))
        self.root = self.dir / "root"
        self.bin = self.dir / "bin"
        self.home = self.dir / "home"
        self.remote = self.dir / "wyzer-card"
        self.calls = self.dir / "calls"
        for d in (self.root / "usr/sbin", self.root / "etc", self.root / "var/www/a", self.root / "var/www/x",
                  self.bin, self.home):
            d.mkdir(parents=True)
        (self.root / "usr/sbin/motion").write_bytes(MOTION.encode())
        (self.root / "etc/os-release").write_bytes(OS_RELEASE.encode())
        (self.root / "etc/send2.json").write_text(json.dumps({"telegram": {"token": "", "caption": "%hostname"}}))
        (self.root / "etc/prudynt.json").write_text('{"motion": {"enabled": false}}')
        (self.root / "var/www/a/plugins.js").write_bytes(PLUGINS.encode())
        calls = self.calls.as_posix()
        self.stub("jct", 'exec "%s" -I "%s" "$@"' % (Path(sys.executable).as_posix(), (HERE / "mock_jct.py").as_posix()))
        self.stub("prudyntctl", "cat >/dev/null; echo prudyntctl \"$@\" >>'%s'" % calls)
        self.stub("prudynt-config", "echo prudynt-config \"$@\" >>'%s'; exit 0" % calls)
        for name in ("reboot", "sync"):
            self.stub(name, "echo %s >>'%s'" % (name, calls))
        self.stub("wlan", "{ printf wlan; for a in \"$@\"; do printf ' [%%s]' \"$a\"; done; echo; } >>'%s'" % calls)
        self.stub("aiwatch", "echo \"aiwatch $*\" >>'%s'; echo 'model: test/m'; echo 'description: A courier.'" % calls)
        self.set_free_kb(5000)

    def stub(self, name, body):
        path = self.bin / name
        path.write_bytes(("#!/bin/sh\n%s\n" % body).encode())
        path.chmod(path.stat().st_mode | stat.S_IEXEC)

    def set_free_kb(self, kb):
        self.stub("df", "echo 'Filesystem 1K-blocks Used Available Use%% Mounted'; echo 'ov 9000 1 %d 1%% /overlay'" % kb)

    def env(self):
        return {"WYZER_ROOT": self.root.as_posix(), "HOME": self.home.as_posix(),
                "AIWATCH_STATE_DIR": (self.dir / "state").as_posix(),
                "PATH": str(self.bin) + os.pathsep + os.environ["PATH"]}

    def executor(self):
        return cam.LocalExecutor(self.env(), sh=SH)

    def called(self):
        return self.calls.read_text(encoding="utf-8") if self.calls.exists() else ""

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)


@unittest.skipUnless(SH, "sh not found")
class CameraTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeCameraRoot()
        self.addCleanup(self.fake.cleanup)
        self.ex = self.fake.executor()

    def install(self, **changes):
        return cam.install(self.ex, settings(**changes), remote_dir=self.fake.remote.as_posix())

    # --- inspect ---

    def test_inspect_reads_the_camera(self):
        info = cam.inspect(self.ex)
        self.assertEqual(info["thingino"], "yes")
        self.assertEqual(info["image"], "wyze_cam3_t31x_gc2053_rtl8189ftv")
        self.assertEqual((info["motion_script"], info["prudynt"], info["jct"], info["webui"]),
                         ("yes", "yes", "yes", "yes"))
        self.assertEqual(info["free_kb"], "5000")
        self.assertEqual((info["aiwatch"], info["hooked"], info["aiwatch_config"]), ("no", "no", "no"))
        self.assertEqual(cam.check_ready(info), ([], []))

    def test_check_ready_explains_problems(self):
        errors, _ = cam.check_ready({"thingino": "no"})
        self.assertIn("SD card tab", errors[0])
        errors, _ = cam.check_ready({"thingino": "yes", "motion_script": "no", "prudynt": "no", "jct": "yes",
                                     "curl": "no", "free_kb": "100", "webui": "no"})
        text = " ".join(errors)
        self.assertIn("prudynt", text)
        self.assertIn("'curl'", text)
        self.assertIn("100 KB", text)
        _, warnings = cam.check_ready({"thingino": "yes", "motion_script": "yes", "prudynt": "yes", "jct": "yes",
                                       "curl": "yes", "free_kb": "9999", "webui": "no"})
        self.assertIn("no web interface", warnings[0])

    def test_inspect_flags_a_stock_camera(self):
        (self.fake.root / "etc/os-release").write_text("NAME=Wyze\n")
        self.assertEqual(cam.inspect(self.ex)["thingino"], "no")

    # --- install ---

    def test_install_puts_everything_in_place_and_cleans_up(self):
        result = self.install(wifi_ssid='My "Net" $HOME', wifi_pass="pa ss'word;rm")
        self.assertTrue(result.ok, result.errors)
        self.assertTrue(result.restart_needed)
        self.assertTrue(any("installed /usr/sbin/aiwatch" in line for line in result.log))
        self.assertFalse(any(line[:2] == "20" for line in result.log))  # timestamps dropped

        root = self.fake.root
        self.assertTrue((root / "usr/sbin/aiwatch").is_file())
        self.assertEqual(json.loads((root / "etc/aiwatch.json").read_text())["api_key"], "sk-test")
        self.assertEqual(json.loads((root / "etc/send2.json").read_text())["telegram"]["channel"], "-100")
        self.assertEqual((root / "usr/sbin/motion").read_text().count("aiwatch motion"), 1)
        self.assertIn("c.plugins.aiwatch=", (root / "var/www/a/plugins.js").read_text())
        self.assertTrue((root / "var/www/config-aiwatch.html").is_file())
        calls = self.fake.called()
        self.assertIn("wlan [configure] [My \"Net\" $HOME] [pa ss'word;rm]", calls)
        self.assertIn("prudyntctl json -", calls)
        self.assertNotIn("reboot", calls)  # told, not restarted
        # nothing sent to the camera is left behind, least of all the secrets
        self.assertFalse(self.fake.remote.exists())

    def test_install_without_wifi_needs_no_restart(self):
        result = self.install()
        self.assertTrue(result.ok, result.errors)
        self.assertFalse(result.restart_needed)

    def test_update_keeps_the_api_key_and_does_not_double_hook(self):
        self.install(api_key="sk-first")
        result = self.install(api_key="", rules=["A car is in the driveway"], model="other/model")
        self.assertTrue(result.ok, result.errors)
        self.assertTrue(any("kept the API key" in line for line in result.log))
        config = cam.read_existing_config(self.ex)
        self.assertEqual((config["api_key"], config["rules"], config["model"]),
                         ("sk-first", ["A car is in the driveway"], "other/model"))
        root = self.fake.root
        self.assertEqual((root / "usr/sbin/motion").read_text().count("aiwatch motion"), 1)
        self.assertEqual((root / "var/www/a/plugins.js").read_text().count("c.plugins.aiwatch="), 1)

    def test_settings_from_the_camera_fill_the_form_without_the_key(self):
        self.install(api_key="sk-first", free_pool=True, free_models=["a/b:free"], free_daily_limit="30",
                     rules=["A person"], active_from="22:00", active_to="06:00")
        config = cam.read_existing_config(self.ex)
        form = cam.settings_from_config(config)
        self.assertEqual((form["api_key"], form["free_pool"], form["free_models"], form["free_daily_limit"],
                          form["rules"], form["active_from"]), ("", True, ["a/b:free"], "30", ["A person"], "22:00"))
        self.assertEqual(core.check_settings(form)[0], [])

    def test_read_existing_config_when_absent_or_broken(self):
        self.assertIsNone(cam.read_existing_config(self.ex))
        (self.fake.root / "etc/aiwatch.json").write_text("{broken")
        self.assertIsNone(cam.read_existing_config(self.ex))

    def test_install_reports_script_errors_and_still_cleans_up(self):
        (self.fake.root / "usr/sbin/motion").unlink()
        result = self.install()
        self.assertFalse(result.ok)
        self.assertIn("does not look like thingino", " ".join(result.errors))
        self.assertFalse(self.fake.remote.exists())

    def test_install_failure_to_write_is_an_error(self):
        class Broken(cam.LocalExecutor):
            def run(self, command, stdin=None, timeout=120):
                if "cat > " in command and "aiwatch.json" in command:
                    return 1, "", "disk full"
                return super().run(command, stdin, timeout)

        ex = Broken(self.fake.env(), sh=SH)
        with self.assertRaises(cam.CameraError) as caught:
            cam.install(ex, settings(), remote_dir=self.fake.remote.as_posix())
        self.assertIn("disk full", str(caught.exception))
        self.assertFalse(self.fake.remote.exists())

    # --- uninstall ---

    def test_uninstall_restores_the_original_files(self):
        self.install()
        lines = cam.uninstall(self.ex)
        self.assertEqual([line for line in lines if line.startswith("ERROR")], [])
        root = self.fake.root
        self.assertEqual((root / "usr/sbin/motion").read_bytes(), MOTION.encode())
        self.assertEqual((root / "var/www/a/plugins.js").read_bytes(), PLUGINS.encode())
        for name in ("usr/sbin/aiwatch", "etc/aiwatch.json", "var/www/config-aiwatch.html",
                     "var/www/a/config-aiwatch.js", "var/www/x/json-aiwatch.cgi",
                     "var/www/a/plugins/aiwatch.webui.json"):
            self.assertFalse((root / name).exists(), name)

    def test_uninstall_can_keep_the_config(self):
        self.install()
        cam.uninstall(self.ex, keep_config=True)
        self.assertTrue((self.fake.root / "etc/aiwatch.json").is_file())
        self.assertFalse((self.fake.root / "usr/sbin/aiwatch").exists())

    def test_uninstall_when_nothing_is_installed(self):
        lines = cam.uninstall(self.ex)
        self.assertEqual([line for line in lines if line.startswith("ERROR")], [])
        self.assertEqual((self.fake.root / "usr/sbin/motion").read_bytes(), MOTION.encode())

    def test_install_uninstall_install_again(self):
        self.install()
        cam.uninstall(self.ex)
        self.assertTrue(self.install().ok)
        self.assertEqual((self.fake.root / "usr/sbin/motion").read_text().count("aiwatch motion"), 1)

    # --- test run, key, restart ---

    def test_run_test_and_restart(self):
        self.assertEqual(cam.run_test(self.ex), (True, "model: test/m\ndescription: A courier."))
        cam.run_test(self.ex, send=True)
        cam.restart(self.ex)
        calls = self.fake.called().splitlines()
        self.assertEqual(calls[:2], ["aiwatch test", "aiwatch test -n"])
        self.assertIn("reboot", calls)

    def test_authorize_key_adds_once(self):
        line = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAbcdefghijklmnopqrstuvwxyz0123456789ABCDEFGH wyzer-cam"
        self.assertTrue(cam.authorize_key(self.ex, line))
        self.assertFalse(cam.authorize_key(self.ex, line))
        self.assertTrue(cam.authorize_key(self.ex, "ssh-ed25519 AAAAOTHERKEYOTHERKEYOTHERKEY other@pc"))
        stored = (self.fake.home / ".ssh/authorized_keys").read_text().splitlines()
        self.assertEqual(len(stored), 2)
        self.assertEqual(stored[0], line)


class KnownHostsTests(unittest.TestCase):
    def test_new_known_changed_forget(self):
        with tempfile.TemporaryDirectory() as tmp:
            hosts = cam.KnownHosts(Path(tmp) / "known.json")
            self.assertEqual(hosts.check("1.2.3.4", "SHA256:aaa"), "new")
            hosts.remember("1.2.3.4", "SHA256:aaa")
            self.assertEqual(hosts.check("1.2.3.4", "SHA256:aaa"), "known")
            self.assertEqual(hosts.check("1.2.3.4", "SHA256:bbb"), "changed")
            self.assertEqual(hosts.check("5.6.7.8", "SHA256:aaa"), "new")
            hosts.forget("1.2.3.4")
            self.assertEqual(hosts.check("1.2.3.4", "SHA256:bbb"), "new")
            (Path(tmp) / "known.json").write_text("garbage")
            self.assertEqual(hosts.check("1.2.3.4", "x"), "new")


@unittest.skipUnless(paramiko, "paramiko not installed")
class KeyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wyzer-keys-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        os.environ["WYZER_SSH_DIR"] = str(self.tmp / "ssh")
        self.addCleanup(os.environ.pop, "WYZER_SSH_DIR", None)

    def test_create_find_load_and_fingerprint(self):
        self.assertEqual(cam.find_key_files(), [])
        path = cam.create_key()
        self.assertEqual(cam.find_key_files(), [path])
        key = cam.load_key(path)
        self.assertIsNotNone(key)
        line = cam.public_line(key)
        self.assertTrue(line.startswith(("ssh-ed25519 ", "ecdsa-sha2-nistp256 ")))
        fp = cam.fingerprint(key)
        self.assertRegex(fp, r"^SHA256:[A-Za-z0-9+/]{43}$")
        keygen = shutil.which("ssh-keygen")
        if keygen and path.name == "id_ed25519":
            shown = subprocess.run([keygen, "-lf", str(path) + ".pub"], capture_output=True, text=True).stdout
            self.assertIn(fp, shown)  # same fingerprint as OpenSSH reports
        with self.assertRaises(cam.CameraError):
            cam.create_key()

    def test_unreadable_key_is_skipped(self):
        (self.tmp / "ssh").mkdir()
        (self.tmp / "ssh" / "id_rsa").write_text("not a key")
        self.assertIsNone(cam.load_key(self.tmp / "ssh" / "id_rsa"))


# --- a real SSH connection to an in-process server -----------------------------------

if paramiko:
    class _Server(paramiko.ServerInterface):
        def __init__(self, password, authorized, runner, authorized_file=None):
            self.password, self.authorized, self.runner = password, authorized, runner
            self.authorized_file = authorized_file

        def get_allowed_auths(self, username):
            return "password,publickey"

        def check_channel_request(self, kind, chanid):
            return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

        def check_auth_password(self, username, password):
            ok = username == "root" and password == self.password
            return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED

        def check_auth_publickey(self, username, key):
            allowed = set(self.authorized)
            if self.authorized_file:  # keys the camera itself has stored
                try:
                    allowed |= {part for line in Path(self.authorized_file).read_text().splitlines()
                                for part in line.split()[1:2]}
                except OSError:
                    pass
            ok = username == "root" and key.get_base64() in allowed
            return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED

        def check_channel_exec_request(self, channel, command):
            threading.Thread(target=self._serve, args=(channel, command.decode()), daemon=True).start()
            return True

        def _serve(self, channel, command):
            data = b""
            while True:
                chunk = channel.recv(65536)
                if not chunk:
                    break
                data += chunk
            code, out, err = self.runner(command, data)
            channel.sendall(out.encode())
            channel.sendall_stderr(err.encode())
            channel.send_exit_status(code)
            channel.close()


class SshServer:
    def __init__(self, runner, password="pw", authorized=(), authorized_file=None):
        self.host_key = paramiko.ECDSAKey.generate(bits=256)
        self.password, self.authorized, self.runner = password, set(authorized), runner
        self.authorized_file = authorized_file
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.transports = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            transport = paramiko.Transport(conn)
            transport.add_server_key(self.host_key)
            self.transports.append(transport)
            try:
                transport.start_server(server=_Server(self.password, self.authorized, self.runner,
                                                      self.authorized_file))
            except Exception:
                pass

    def close(self):
        self.sock.close()
        for transport in self.transports:
            transport.close()


@unittest.skipUnless(paramiko and SH, "paramiko or sh not available")
class SshTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeCameraRoot()
        self.addCleanup(self.fake.cleanup)
        local = self.fake.executor()
        self.server = SshServer(lambda cmd, stdin: local.run(cmd, stdin))
        self.addCleanup(self.server.close)

    def connect(self, password="pw", key_files=(), host_key=None, **kw):
        key, _ = cam.fetch_host_key("127.0.0.1", self.server.port)
        ex = cam.SshExecutor("127.0.0.1", self.server.port)
        ex.connect(host_key or key, password=password, key_files=key_files, **kw)
        self.addCleanup(ex.close)
        return ex

    def test_fetch_host_key_matches_the_server(self):
        key, fp = cam.fetch_host_key("127.0.0.1", self.server.port)
        self.assertEqual(fp, cam.fingerprint(self.server.host_key))
        self.assertEqual(key.get_base64(), self.server.host_key.get_base64())

    def test_fetch_host_key_from_a_closed_port(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        with self.assertRaises(cam.CameraError) as caught:
            cam.fetch_host_key("127.0.0.1", port, timeout=2)
        self.assertIn("Could not reach", str(caught.exception))

    def test_run_commands_stdin_stderr_exit_code_and_large_output(self):
        ex = self.connect()
        self.assertEqual(ex.run("echo hello"), (0, "hello\n", ""))
        self.assertEqual(ex.run("cat", stdin=b"from stdin")[1], "from stdin")
        code, out, err = ex.run("echo to-err >&2; exit 3")
        self.assertEqual((code, out.strip(), err.strip()), (3, "", "to-err"))
        code, out, _ = ex.run("awk 'BEGIN { for (i = 0; i < 40000; i++) print \"line\", i }'")
        self.assertEqual((code, len(out.splitlines())), (0, 40000))
        payload = bytes(range(256)) * 2000
        self.assertEqual(len(ex.run("cat | wc -c", stdin=payload)[1].strip()), len(str(len(payload))))

    def test_wrong_password_is_refused_politely(self):
        with self.assertRaises(cam.CameraError) as caught:
            self.connect(password="wrong")
        self.assertIn("rejected the login", str(caught.exception))

    def test_a_different_host_key_is_refused(self):
        other = paramiko.ECDSAKey.generate(bits=256)
        with self.assertRaises(cam.CameraError) as caught:
            self.connect(host_key=other)
        self.assertIn("identity changed", str(caught.exception))

    def test_key_login_without_a_password(self):
        tmp = Path(tempfile.mkdtemp(prefix="wyzer-keys-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        key = paramiko.ECDSAKey.generate(bits=256)
        key.write_private_key_file(str(tmp / "id_ecdsa"))
        self.server.authorized.add(key.get_base64())
        ex = self.connect(password=None, key_files=[tmp / "id_ecdsa"])
        self.assertEqual(ex.login, "key")
        self.assertEqual(ex.run("echo ok")[1], "ok\n")
        with self.assertRaises(cam.CameraError):  # a key the server does not know, and no password
            other = paramiko.ECDSAKey.generate(bits=256)
            other.write_private_key_file(str(tmp / "id_other"))
            self.connect(password=None, key_files=[tmp / "id_other"])

    def test_no_credentials_at_all(self):
        with self.assertRaises(cam.CameraError):
            self.connect(password=None, key_files=[])

    def test_full_install_inspect_test_and_uninstall_over_ssh(self):
        ex = self.connect()
        info = cam.inspect(ex)
        self.assertEqual(cam.check_ready(info), ([], []))
        result = cam.install(ex, settings(wifi_ssid="HomeNet", wifi_pass="password1"),
                             remote_dir=self.fake.remote.as_posix())
        self.assertTrue(result.ok, result.errors)
        self.assertTrue(result.restart_needed)
        self.assertEqual(cam.inspect(ex)["hooked"], "yes")
        self.assertFalse(self.fake.remote.exists())
        self.assertEqual(cam.read_existing_config(ex)["model"], "google/gemini-2.5-flash-lite")
        ok, output = cam.run_test(ex)
        self.assertTrue(ok)
        self.assertIn("description: A courier.", output)
        key = paramiko.ECDSAKey.generate(bits=256)
        self.assertTrue(cam.authorize_key(ex, cam.public_line(key)))
        self.assertEqual([line for line in cam.uninstall(ex) if line.startswith("ERROR")], [])
        self.assertEqual((self.fake.root / "usr/sbin/motion").read_bytes(), MOTION.encode())
        self.assertEqual(cam.inspect(ex)["aiwatch"], "no")


if __name__ == "__main__":
    unittest.main()
