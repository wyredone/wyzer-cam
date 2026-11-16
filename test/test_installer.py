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


def build_fat32_image(files, mbr=True):
    """A minimal FAT32 disk image holding `files` ({name: bytes}) in its root."""
    import struct

    bps, clusters, reserved = 512, 70000, 32
    fat_sectors = (clusters + 2) * 4 // bps + 1
    total = reserved + 2 * fat_sectors + clusters
    vol = bytearray(total * bps)
    vol[0:3] = b"\xeb\x58\x90"
    vol[3:11] = b"mkfs.fat"
    struct.pack_into("<HBHBHHBH", vol, 11, bps, 1, reserved, 2, 0, 0, 0xF8, 0)
    struct.pack_into("<II", vol, 32, total, fat_sectors)
    struct.pack_into("<I", vol, 44, 2)
    vol[510:512] = b"\x55\xaa"

    root_clusters = 4
    fat = [0x0FFFFFF8, 0x0FFFFFFF] + [0] * clusters
    for c in range(2, 2 + root_clusters):
        fat[c] = c + 1 if c < 1 + root_clusters else 0x0FFFFFFF
    next_free = 2 + root_clusters
    data_start = (reserved + 2 * fat_sectors) * bps
    entries = bytearray()

    for index, (name, content) in enumerate(files.items()):
        short = ("F%05d~1BIN" % index).encode()  # 8.3 alias: exactly 11 bytes
        checksum = 0
        for byte in short:
            checksum = ((checksum >> 1) | ((checksum & 1) << 7)) + byte & 0xFF
        units = name.encode("utf-16-le") + b"\x00\x00"
        units += b"\xff\xff" * (-(len(units) // 2) % 13)
        pieces = [units[i:i + 26] for i in range(0, len(units), 26)]
        for seq in range(len(pieces), 0, -1):
            p = pieces[seq - 1]
            flag = seq | (0x40 if seq == len(pieces) else 0)
            entries += bytes([flag]) + p[0:10] + b"\x0f\x00" + bytes([checksum]) + p[10:22] + b"\x00\x00" + p[22:26]
        first = next_free if content else 0
        count = (len(content) + bps - 1) // bps
        for i in range(count):
            fat[next_free + i] = next_free + i + 1 if i < count - 1 else 0x0FFFFFFF
        offset = data_start + (next_free - 2) * bps
        vol[offset:offset + len(content)] = content
        next_free += count
        entry = bytearray(32)
        entry[0:11] = short
        entry[11] = 0x20
        struct.pack_into("<H", entry, 20, first >> 16)
        struct.pack_into("<H", entry, 26, first & 0xFFFF)
        struct.pack_into("<I", entry, 28, len(content))
        entries += entry

    assert len(entries) <= root_clusters * bps and next_free <= clusters
    vol[data_start:data_start + len(entries)] = entries
    table = struct.pack("<%dI" % len(fat), *fat)
    for n in range(2):
        start = (reserved + n * fat_sectors) * bps
        vol[start:start + len(table)] = table
    if not mbr:
        return bytes(vol)
    head = bytearray(2048 * 512)
    struct.pack_into("<BBBBBBBBII", head, 446, 0, 0, 0, 0, 0x0C, 0, 0, 0, 2048, total)
    head[510:512] = b"\x55\xaa"
    return bytes(head) + bytes(vol)


BOOT = wi.UIMAGE_MAGIC + b"kernel" * 1000
FIRMWARE = bytes(range(256)) * (wi.FIRMWARE_BYTES // 256)
FW_NAME = "thingino-wyze_cam3_t31x_gc2053_rtl8189ftv.bin"


class FlashTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wyzer-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def make_zip(self, files, name="sd.img", mbr=True):
        import zipfile

        path = self.tmp / "installer.zip"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(name, build_fat32_image(files, mbr))
        return path

    def test_reads_installer_files_and_ignores_others(self):
        path = self.make_zip({wi.THINGINO_BOOT_FILE: BOOT, FW_NAME: FIRMWARE,
                              "autorun.sh": b"rm -rf /", "empty.txt": b""})
        files = wi.read_thingino_files(path)
        self.assertEqual(sorted(files), sorted([wi.THINGINO_BOOT_FILE, FW_NAME]))
        self.assertEqual(files[wi.THINGINO_BOOT_FILE], BOOT)
        self.assertEqual(files[FW_NAME], FIRMWARE)

    def test_reads_image_without_partition_table(self):
        files = wi.read_thingino_files(self.make_zip({wi.THINGINO_BOOT_FILE: BOOT, FW_NAME: FIRMWARE}, mbr=False))
        self.assertEqual(len(files), 2)

    def test_rejects_unexpected_images(self):
        cases = {
            "no valid": {wi.THINGINO_BOOT_FILE: b"not a kernel", FW_NAME: FIRMWARE},
            "no Wyze Cam v3 firmware": {wi.THINGINO_BOOT_FILE: BOOT},
            "not a 16 MB": {wi.THINGINO_BOOT_FILE: BOOT, FW_NAME: FIRMWARE[:-512]},
        }
        for message, files in cases.items():
            with self.assertRaises(ValueError) as caught:
                wi.read_thingino_files(self.make_zip(files))
            self.assertIn(message, str(caught.exception))
        with self.assertRaises(ValueError):
            wi.read_thingino_files(self.make_zip({wi.THINGINO_BOOT_FILE: BOOT}, name="readme.txt"))
        import zipfile
        garbage = self.tmp / "garbage.zip"
        with zipfile.ZipFile(garbage, "w") as z:
            z.writestr("sd.img", b"\x00" * 4096)
        with self.assertRaises(ValueError):
            wi.read_thingino_files(garbage)

    def test_write_and_prepare_share_a_card(self):
        card = self.tmp / "card"
        card.mkdir()
        done = wi.write_thingino_files(card, {wi.THINGINO_BOOT_FILE: BOOT, FW_NAME: FIRMWARE})
        wi.prepare_card(card, settings())
        self.assertIn("1 firmware file", done[0])
        self.assertEqual((card / wi.THINGINO_BOOT_FILE).read_bytes(), BOOT)
        self.assertEqual((card / FW_NAME).stat().st_size, wi.FIRMWARE_BYTES)
        self.assertTrue((card / "runonce.sh").exists())

    def test_download_caches_and_rejects_non_zip(self):
        source = self.make_zip({wi.THINGINO_BOOT_FILE: BOOT, FW_NAME: FIRMWARE})
        dest = self.tmp / "cache" / "image.zip"
        seen = []
        self.assertTrue(wi.download_thingino_image(dest, lambda d, t: seen.append(d), url=source.as_uri()))
        self.assertEqual(dest.read_bytes(), source.read_bytes())
        self.assertTrue(seen)
        self.assertFalse(wi.download_thingino_image(dest, url="http://127.0.0.1:9/unused"))  # cached

        bad = self.tmp / "bad.bin"
        bad.write_bytes(b"<html>not found</html>")
        with self.assertRaises(ValueError):
            wi.download_thingino_image(self.tmp / "other.zip", url=bad.as_uri())
        self.assertFalse((self.tmp / "other.zip").exists())
        self.assertFalse((self.tmp / "other.zip.part").exists())

    def test_validate_flash_checks_space(self):
        real = wi.card_info
        self.addCleanup(setattr, wi, "card_info", real)
        wi.card_info = lambda path: ("exFAT", 10 * 1024 * 1024)
        errors, _ = wi.validate(settings(tg_token="t", tg_channel="1"), self.tmp, flash=True)
        self.assertEqual(len(errors), 2)
        self.assertIn("FAT32", errors[0])
        wi.card_info = lambda path: ("FAT32", 10 ** 9)
        self.assertEqual(wi.validate(settings(tg_token="t", tg_channel="1"), self.tmp, flash=True)[0], [])
        self.assertEqual(wi.validate(settings(tg_token="t", tg_channel="1"), self.tmp)[0], [])


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

    def test_web_page_installed_and_added_to_menu_once(self):
        www = self.root / "var/www"
        (www / "a").mkdir(parents=True)
        (www / "x").mkdir()
        stock = ("// Generated\n(function(){var cfg=window.thinginoUIConfig||{};window.thinginoUIConfig=cfg;"
                 "cfg.plugins={\"prudynt\":{}};})();\n")
        (www / "a/plugins.js").write_bytes(stock.encode())
        for _ in range(2):
            wi.prepare_card(self.card, settings(enable_motion=False))
            result, log = self.run_card()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("ERROR", log)
        self.assertIn("AI Alerts page", log)
        for name in ("config-aiwatch.html", "a/config-aiwatch.js", "x/json-aiwatch.cgi",
                     "a/plugins/aiwatch.webui.json"):
            self.assertTrue((www / name).is_file(), name)
        plugins = (www / "a/plugins.js").read_text(encoding="utf-8")
        self.assertTrue(plugins.startswith(stock))
        self.assertEqual(plugins.count("c.plugins.aiwatch="), 1)
        node = shutil.which("node")
        if node:  # the appended registration must be valid JavaScript
            check = ("var window={};" + plugins +
                     ";var p=window.thinginoUIConfig.plugins;"
                     "if(!p.prudynt||p.aiwatch.nav[0].items[0].href!=='/config-aiwatch.html')process.exit(1);")
            self.assertEqual(subprocess.run([node, "-e", check]).returncode, 0)

    def test_camera_without_web_interface(self):
        wi.prepare_card(self.card, settings(enable_motion=False))
        result, log = self.run_card()
        self.assertEqual(result.returncode, 0)
        self.assertIn("no web interface found", log)
        self.assertNotIn("ERROR", log)

    def test_motion_enabled_on_older_firmware_without_prudynt_config(self):
        (self.bin / "prudynt-config").unlink()
        (self.root / "etc/prudynt.json").write_text('{"motion": {"enabled": false}}')
        wi.prepare_card(self.card, settings())
        result, log = self.run_card()
        self.assertNotIn("ERROR", log)
        self.assertIs(json.loads((self.root / "etc/prudynt.json").read_text())["motion"]["enabled"], True)

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
