"""Core of the wyzer-cam installer: settings, SD card preparation, profile.

No window code lives here, so it can be tested without one.

SD card: copies the aiwatch script and a runonce.sh onto the card, together
with the settings entered in the window. thingino runs runonce.sh once when
the card is in the camera. For a camera that is still on Wyze's firmware the
thingino installer's files can be put on the card too: they are read out of
the community Wyze Cam v3 installer image and copied as ordinary files.
The card is never formatted and no disk is written raw.

SSH: `card_files()` is the same set of files, which camera_ssh.py sends to a
camera that already runs thingino instead.
"""

import fnmatch
import json
import os
import shutil
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fatimage import FatError, FatImage  # noqa: E402

# The thingino installer for the Wyze Cam v3, maintained by wltechblog. It is a
# disk image holding one FAT32 partition with a boot file that the stock
# bootloader loads from the card, plus one firmware per hardware variant.
THINGINO_IMAGE_URL = ("https://raw.githubusercontent.com/wltechblog/thingino-installers/"
                      "main/wyze-cam-3/wyze-cam-3-sd.zip")
THINGINO_BOOT_FILE = "factory_t31_ZMC6tiIDQN"
THINGINO_FIRMWARE_PATTERN = "thingino-wyze_cam3_*.bin"
FIRMWARE_BYTES = 16 * 1024 * 1024  # the camera's flash size
UIMAGE_MAGIC = b"\x27\x05\x19\x56"
IMAGE_CACHE_DAYS = 7
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024
FLASH_SPACE_BYTES = 64 * 1024 * 1024

REPO_ROOT = Path(__file__).resolve().parent.parent
AIWATCH_SRC = REPO_ROOT / "package" / "thingino-aiwatch" / "files" / "aiwatch"
RUNONCE_SRC = Path(__file__).resolve().parent / "runonce.sh"
# The settings page: (path in the package, path under wyzer/ on the card).
WEBUI_SRC = REPO_ROOT / "package" / "thingino-aiwatch" / "files"
WEBUI_FILES = (
    ("www/config-aiwatch.html", "www/config-aiwatch.html"),
    ("www/a/config-aiwatch.js", "www/a/config-aiwatch.js"),
    ("www/x/json-aiwatch.cgi", "www/x/json-aiwatch.cgi"),
    ("aiwatch.webui.json", "www/a/plugins/aiwatch.webui.json"),
)
RUNONCE_MARKER = b"wyzer-cam-runonce"
DEFAULT_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
THINGINO_INSTALL_URL = "https://github.com/wltechblog/thingino-installers/tree/main/wyze-cam-3"
SECRET_FILES = ("aiwatch.json", "send2.json", "wifi_ssid", "wifi_pass", "enable_motion")

RECOMMENDED_MODEL = "google/gemini-2.5-flash-lite"
MODEL_ID_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:/-")
LARGE_CARD_BYTES = 33 * 1000 ** 3  # past what Windows will format as FAT32

DEFAULTS = {
    "wifi_ssid": "",
    "wifi_pass": "",
    "enabled": True,
    "enable_motion": True,
    "endpoint": DEFAULT_ENDPOINT,
    "api_key": "",
    "model": RECOMMENDED_MODEL,
    "free_pool": False,
    "free_models": [],
    "free_daily_limit": "45",
    "max_attempts": "3",
    "camera_name": "",
    "context": "",
    "language": "English",
    "extra_instructions": "",
    "active_from": "",
    "active_to": "",
    "rules": [],
    "cooldown": "60",
    "daily_limit": "200",
    "tg_token": "",
    "tg_channel": "",
    "ntfy_host": "ntfy.sh",
    "ntfy_topic": "",
}

# What the window remembers between runs. Nothing that grants access: no API
# key, Telegram token, ntfy topic (anyone who knows it can read it), Wi-Fi
# password or SSH password.
PROFILE_KEYS = ("wifi_ssid", "enabled", "enable_motion", "endpoint", "model", "free_pool", "free_models",
                "free_daily_limit", "max_attempts", "camera_name", "context", "language",
                "extra_instructions", "active_from", "active_to", "rules", "cooldown", "daily_limit",
                "tg_channel", "ntfy_host")


# --- core logic (no GUI) -----------------------------------------------------

def list_removable_drives():
    """Removable drives as (path, description). Windows only; [] elsewhere."""
    if sys.platform != "win32":
        return []
    import ctypes

    kernel32 = ctypes.windll.kernel32
    drives = []
    mask = kernel32.GetLogicalDrives()
    for i in range(26):
        if not mask & (1 << i):
            continue
        root = "%s:\\" % chr(ord("A") + i)
        if kernel32.GetDriveTypeW(root) != 2:  # DRIVE_REMOVABLE
            continue
        label = ctypes.create_unicode_buffer(261)
        fs = ctypes.create_unicode_buffer(261)
        if not kernel32.GetVolumeInformationW(root, label, 261, None, None, None, fs, 261):
            continue  # reader with no card in it
        try:
            size_gb = shutil.disk_usage(root).total / 1e9
        except OSError:
            continue
        text = "%s  %s  %s  %.1f GB" % (root, label.value or "(no label)", fs.value, size_gb)
        drives.append((root, text))
    return drives


def services(settings):
    result = []
    if settings["tg_token"] and settings["tg_channel"]:
        result.append("telegram")
    if settings["ntfy_topic"]:
        result.append("ntfy")
    return result


def card_info(path):
    """(file system name or None if unknown, free bytes, total bytes) for the card."""
    fs_name = None
    if sys.platform == "win32":
        import ctypes

        root = os.path.splitdrive(os.path.abspath(str(path)))[0] + "\\"
        fs = ctypes.create_unicode_buffer(261)
        if ctypes.windll.kernel32.GetVolumeInformationW(root, None, 0, None, None, None, fs, 261):
            fs_name = fs.value
    usage = shutil.disk_usage(str(path))
    return fs_name, usage.free, usage.total


def app_dir():
    """Where the window keeps its small files (profile, known cameras, image cache)."""
    override = os.environ.get("WYZER_HOME")
    if override:
        return Path(override)
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "wyzer-cam"


def image_cache_path():
    base = os.environ.get("WYZER_HOME") or os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".cache")
    path = Path(base)
    return (path if os.environ.get("WYZER_HOME") else path / "wyzer-cam") / "wyze-cam-3-sd.zip"


def load_profile():
    """The remembered non-secret settings, or {} if there are none or they are unreadable."""
    try:
        data = json.loads((app_dir() / "profile.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if k in PROFILE_KEYS or k in ("camera_host", "camera_user")}


def save_profile(settings, camera_host="", camera_user="root"):
    """Remembers the non-secret settings. Never writes a key, token or password."""
    data = {k: settings[k] for k in PROFILE_KEYS if k in settings}
    data["camera_host"] = camera_host
    data["camera_user"] = camera_user
    folder = app_dir()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "profile.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


def apply_profile(settings, profile):
    """`settings` with the remembered values laid over the defaults, type-checked."""
    merged = dict(settings)
    for key in PROFILE_KEYS:
        if key in profile and isinstance(profile[key], type(DEFAULTS[key])):
            merged[key] = profile[key]
    return merged


def download_thingino_image(dest, progress=None, url=THINGINO_IMAGE_URL):
    """Downloads the installer image to `dest`, reusing a recent copy.

    `progress(done_bytes, total_bytes)` is called while downloading.
    Returns True if a download happened, False if the cached copy was used.
    """
    dest = Path(dest)
    if (dest.is_file() and zipfile.is_zipfile(dest)
            and time.time() - dest.stat().st_mtime < IMAGE_CACHE_DAYS * 86400):
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    with urllib.request.urlopen(url, timeout=30) as response, open(part, "wb") as out:
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = response.read(256 * 1024)
            if not chunk:
                break
            done += len(chunk)
            if done > MAX_DOWNLOAD_BYTES:
                raise ValueError("the download is larger than expected; stopped")
            out.write(chunk)
            if progress:
                progress(done, total)
    if not zipfile.is_zipfile(part):
        part.unlink()
        raise ValueError("the downloaded file is not a zip archive")
    os.replace(part, dest)
    return True


def read_thingino_files(zip_path):
    """The installer's files from the downloaded image, as {name: bytes}.

    Only the boot file and the Wyze Cam v3 firmware files are taken, and each
    is checked, so an unexpected image is rejected instead of copied.
    """
    with zipfile.ZipFile(zip_path) as archive:
        images = [i for i in archive.infolist() if i.filename.lower().endswith(".img")]
        if len(images) != 1:
            raise ValueError("expected one disk image in the download, found %d" % len(images))
        if images[0].file_size > 1024 ** 3:
            raise ValueError("the disk image in the download is larger than expected")
        data = archive.read(images[0])  # also verifies the zip checksum

    try:
        found = {path: content for path, content in FatImage(data).files()
                 if path == THINGINO_BOOT_FILE or fnmatch.fnmatchcase(path, THINGINO_FIRMWARE_PATTERN)}
    except FatError as exc:
        raise ValueError("could not read the installer image: %s" % exc)

    boot = found.get(THINGINO_BOOT_FILE)
    firmware = [name for name in found if name != THINGINO_BOOT_FILE]
    if boot is None or not boot.startswith(UIMAGE_MAGIC):
        raise ValueError("the installer image has no valid %s" % THINGINO_BOOT_FILE)
    if not firmware:
        raise ValueError("the installer image has no Wyze Cam v3 firmware")
    for name in firmware:
        if len(found[name]) != FIRMWARE_BYTES:
            raise ValueError("%s is not a 16 MB firmware image" % name)
    return found


def write_thingino_files(target, files):
    target = Path(target)
    for name, content in files.items():
        _write(target / name, content)
    count = len(files) - 1
    return ["Copied the thingino installer and %d firmware file%s" % (count, "" if count == 1 else "s")]


def is_openrouter(endpoint):
    host = urllib.parse.urlsplit(endpoint or "").hostname or ""
    return host == "openrouter.ai" or host.endswith(".openrouter.ai")


def valid_model_id(model):
    return bool(model) and all(c in MODEL_ID_CHARS for c in model)


def validate(settings, target, flash=False):
    """Checks for writing an SD card. Returns (errors, warnings) as lists of sentences."""
    errors, warnings = check_settings(settings)
    target = Path(target) if target else None
    if not target or not target.is_dir():
        errors.append("Choose the SD card (or a folder) to write to.")
    elif flash:
        fs_name, free, total = card_info(target)
        if fs_name is not None and fs_name.upper() != "FAT32":
            errors.append("To install thingino the card must be formatted FAT32; this one is %s. "
                          "Windows will only format cards of 32 GB or less as FAT32; a larger card "
                          "needs a tool such as guiformat or a 32 GB partition." % fs_name)
        if free < FLASH_SPACE_BYTES:
            errors.append("The card needs at least 64 MB free for the thingino installer.")
        if total > LARGE_CARD_BYTES and not errors:
            warnings.append("This card is %.0f GB. Large FAT32 cards work for the files, but the Wyze "
                            "bootloader that reads the installer is only known to work with smaller "
                            "cards. If the camera does not start the install, try a card of 32 GB or less."
                            % (total / 1e9))
    return errors, warnings


def check_settings(settings):
    """Checks of the settings alone, for both SD and SSH. Returns (errors, warnings)."""
    errors, warnings = [], []
    ssid, password = settings["wifi_ssid"], settings["wifi_pass"]
    if ssid:
        if len(ssid.encode("utf-8")) > 32:
            errors.append("The Wi-Fi name is longer than 32 bytes.")
        if not 8 <= len(password) <= 64:
            errors.append("The Wi-Fi password must be 8 to 64 characters.")
        if any(c in ssid + password for c in "\r\n"):
            errors.append("The Wi-Fi name and password cannot contain line breaks.")
    elif password:
        errors.append("A Wi-Fi password is set but the Wi-Fi name is empty.")

    if not settings["endpoint"].startswith(("http://", "https://")):
        errors.append("The endpoint must start with http:// or https://.")
    if settings["free_pool"]:
        pool = settings["free_models"]
        if settings["enabled"] and not pool:
            errors.append("Pick at least one free model, or choose a single model.")
        if len(pool) > 10:
            errors.append("At most 10 free models can be rotated.")
        if not all(valid_model_id(m) for m in pool):
            errors.append("A free model id contains characters a model id does not use.")
    else:
        if settings["enabled"] and not settings["model"]:
            errors.append("Enter a model id, or untick 'Turn AI alerts on'.")
        if settings["model"] and not valid_model_id(settings["model"]):
            errors.append("The model id contains characters a model id does not use.")
    for key, name in (("cooldown", "Cooldown"), ("daily_limit", "Daily limit"),
                      ("free_daily_limit", "Free calls per day"), ("max_attempts", "Models per event")):
        if not str(settings[key]).isdigit():
            errors.append("%s must be a whole number." % name)
    if len(settings["rules"]) > 10:
        errors.append("At most 10 alert rules are supported.")
    if bool(settings["active_from"]) != bool(settings["active_to"]):
        errors.append("Set both active times, or leave both empty.")
    for key in ("active_from", "active_to"):
        value = settings[key]
        if value and not (len(value) == 5 and value[2] == ":" and value[:2].isdigit() and value[3:].isdigit()
                          and int(value[:2]) < 24 and int(value[3:]) < 60):
            errors.append("Active times must look like 22:00.")
            break
    if bool(settings["tg_token"]) != bool(settings["tg_channel"]):
        errors.append("Telegram needs both the bot token and the chat ID.")

    if not errors:
        if settings["enabled"] and not settings["api_key"] and is_openrouter(settings["endpoint"]):
            warnings.append("No API key is set; OpenRouter will reject the requests. "
                            "(On an update the key already on the camera is kept.)")
        if settings["enabled"] and not services(settings):
            warnings.append("No Telegram or ntfy details are set, so alerts have nowhere to go "
                            "unless the camera's notifiers are already configured.")
        if settings["enabled"] and settings["free_pool"]:
            warnings.append("Free models can be run by providers that keep or train on what they receive, "
                            "and your camera snapshots would be sent to them. Check OpenRouter's privacy "
                            "settings and each model's page first.")
    return errors, warnings


def build_aiwatch_config(settings):
    return {
        "enabled": bool(settings["enabled"]),
        "endpoint": settings["endpoint"],
        "api_key": settings["api_key"],
        "model": settings["model"] or RECOMMENDED_MODEL,
        "free_pool": bool(settings["free_pool"]),
        "free_models": list(settings["free_models"]),
        "free_daily_limit": int(settings["free_daily_limit"]),
        "max_attempts": int(settings["max_attempts"]),
        "camera_name": settings["camera_name"],
        "context": settings["context"],
        "language": settings["language"] or "English",
        "extra_instructions": " ".join(settings["extra_instructions"].split()),
        "active_from": settings["active_from"],
        "active_to": settings["active_to"],
        "rules": list(settings["rules"]),
        "filter_empty": True,
        "services": services(settings) or ["telegram"],
        "send_photo": True,
        "fallback_on_error": True,
        "cooldown": int(settings["cooldown"]),
        "daily_limit": int(settings["daily_limit"]),
        "snapshot_channel": 1,
        "max_image_kb": 600,
        "timeout": 30,
    }


def build_send2_overlay(settings):
    """Only the notifier keys the user filled in; merged on the camera."""
    overlay = {}
    if settings["tg_token"] and settings["tg_channel"]:
        overlay["telegram"] = {"token": settings["tg_token"], "channel": settings["tg_channel"]}
    if settings["ntfy_topic"]:
        overlay["ntfy"] = {
            "host": settings["ntfy_host"] or "ntfy.sh",
            "port": 443,
            "use_ssl": True,
            "topic": settings["ntfy_topic"],
        }
    return overlay


def foreign_runonce(target):
    """True if the card has a runonce.sh that this tool did not write."""
    path = Path(target) / "runonce.sh"
    try:
        return path.is_file() and RUNONCE_MARKER not in path.read_bytes()[:400]
    except OSError:
        return False


def _unix_script(path):
    """A shell script's bytes with Unix line endings, whatever the checkout did."""
    return Path(path).read_bytes().replace(b"\r\n", b"\n")


def _write(path, data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    with open(path, "wb") as f:
        f.write(data)


def card_files(settings):
    """Every file of the setup, as {path relative to the card root: bytes}.

    The SD card writer puts these on the card; the SSH installer sends the same
    files to the camera. Everything under wyzer/ that holds a key, token or
    password is listed in SECRET_FILES, and runonce.sh deletes those after use.
    """
    for src in (AIWATCH_SRC, RUNONCE_SRC):
        if not src.is_file():
            raise FileNotFoundError("%s is missing; run this from a full copy of the repository" % src)

    files = {
        "runonce.sh": _unix_script(RUNONCE_SRC),
        "wyzer/aiwatch": _unix_script(AIWATCH_SRC),
    }
    if all((WEBUI_SRC / src).is_file() for src, _ in WEBUI_FILES):
        for src, dest in WEBUI_FILES:
            files["wyzer/" + dest] = _unix_script(WEBUI_SRC / src)
    files["wyzer/aiwatch.json"] = (json.dumps(build_aiwatch_config(settings), indent=2) + "\n").encode("utf-8")
    overlay = build_send2_overlay(settings)
    if overlay:
        files["wyzer/send2.json"] = (json.dumps(overlay, indent=2) + "\n").encode("utf-8")
    if settings["enable_motion"]:
        files["wyzer/enable_motion"] = b"1\n"
    if settings["wifi_ssid"]:
        files["wyzer/wifi_ssid"] = settings["wifi_ssid"].encode("utf-8")
        files["wyzer/wifi_pass"] = settings["wifi_pass"].encode("utf-8")
    return files


def describe_files(files, settings):
    """What `files` will do, as sentences for the window."""
    done = ["Copied runonce.sh and the aiwatch script"]
    if any(name.startswith("wyzer/www/") for name in files):
        done.append("Copied the AI Alerts page for the camera's web interface")
    rules = len(settings["rules"])
    if settings["free_pool"]:
        done.append("Wrote AI settings: rotating %d free model%s, %d rule%s"
                    % (len(settings["free_models"]), "" if len(settings["free_models"]) == 1 else "s",
                       rules, "" if rules == 1 else "s"))
    else:
        done.append("Wrote AI settings: %s, %d rule%s" % (settings["model"] or RECOMMENDED_MODEL, rules,
                                                          "" if rules == 1 else "s"))
    if "wyzer/send2.json" in files:
        done.append("Wrote notifier settings: " + ", ".join(sorted(json.loads(files["wyzer/send2.json"]))))
    if "wyzer/enable_motion" in files:
        done.append("Motion detection will be turned on")
    if "wyzer/wifi_ssid" in files:
        done.append("Wrote Wi-Fi settings for '%s'" % settings["wifi_ssid"])
    return done


def prepare_card(target, settings):
    """Writes the setup files to `target`. Returns the list of what was done."""
    target = Path(target)
    files = card_files(settings)
    done = []
    data_dir = target / "wyzer"
    data_dir.mkdir(exist_ok=True)

    # Leftovers from an earlier run must not leak into this one.
    for name in SECRET_FILES:
        (data_dir / name).unlink(missing_ok=True)
    flag = target / "runonce.done"
    if flag.exists():
        flag.unlink()
        done.append("Removed runonce.done from an earlier setup")

    for name, content in files.items():
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        _write(path, content)
    return done + describe_files(files, settings)
