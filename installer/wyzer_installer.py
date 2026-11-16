"""wyzer-cam SD card installer.

Prepares an SD card that sets up aiwatch on a camera running thingino:
it copies the aiwatch script and a runonce.sh onto the card, together with
the settings entered in the window. thingino runs runonce.sh once when the
card is in the camera.

Run:  python installer/wyzer_installer.py

For a camera that is still on Wyze's firmware it can also put the thingino
installer on the card: it downloads the community Wyze Cam v3 installer image
and copies the files inside it to the card.

Standard library only. The card is written with ordinary file copies; nothing
is formatted and no disk is written raw.
"""

import fnmatch
import json
import os
import shutil
import sys
import time
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

DEFAULTS = {
    "wifi_ssid": "",
    "wifi_pass": "",
    "enabled": True,
    "enable_motion": True,
    "endpoint": DEFAULT_ENDPOINT,
    "api_key": "",
    "model": "",
    "camera_name": "",
    "context": "",
    "language": "English",
    "rules": [],
    "cooldown": "60",
    "daily_limit": "200",
    "tg_token": "",
    "tg_channel": "",
    "ntfy_host": "ntfy.sh",
    "ntfy_topic": "",
}


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
    """(file system name or None if unknown, free bytes) for the card."""
    fs_name = None
    if sys.platform == "win32":
        import ctypes

        root = os.path.splitdrive(os.path.abspath(str(path)))[0] + "\\"
        fs = ctypes.create_unicode_buffer(261)
        if ctypes.windll.kernel32.GetVolumeInformationW(root, None, 0, None, None, None, fs, 261):
            fs_name = fs.value
    return fs_name, shutil.disk_usage(str(path)).free


def image_cache_path():
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(base) / "wyzer-cam" / "wyze-cam-3-sd.zip"


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


def validate(settings, target, flash=False):
    """Returns (errors, warnings) as lists of sentences."""
    errors, warnings = [], []
    target = Path(target) if target else None
    if not target or not target.is_dir():
        errors.append("Choose the SD card (or a folder) to write to.")
    elif flash:
        fs_name, free = card_info(target)
        if fs_name is not None and fs_name.upper() != "FAT32":
            errors.append("To install thingino the card must be formatted FAT32; this one is %s. "
                          "Cards over 32 GB usually are not." % fs_name)
        if free < FLASH_SPACE_BYTES:
            errors.append("The card needs at least 64 MB free for the thingino installer.")

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
    if settings["enabled"] and not settings["model"]:
        errors.append("Enter a model id, or untick 'Turn AI alerts on'.")
    for key, name in (("cooldown", "Cooldown"), ("daily_limit", "Daily limit")):
        if not str(settings[key]).isdigit():
            errors.append("%s must be a whole number." % name)
    if len(settings["rules"]) > 10:
        errors.append("At most 10 alert rules are supported.")
    if bool(settings["tg_token"]) != bool(settings["tg_channel"]):
        errors.append("Telegram needs both the bot token and the chat ID.")

    if not errors:
        if settings["enabled"] and not settings["api_key"] and "openrouter.ai" in settings["endpoint"]:
            warnings.append("No API key is set; OpenRouter will reject the requests.")
        if settings["enabled"] and not services(settings):
            warnings.append("No Telegram or ntfy details are set, so alerts have nowhere to go "
                            "unless the camera's notifiers are already configured.")
    return errors, warnings


def build_aiwatch_config(settings):
    return {
        "enabled": bool(settings["enabled"]),
        "endpoint": settings["endpoint"],
        "api_key": settings["api_key"],
        "model": settings["model"],
        "camera_name": settings["camera_name"],
        "context": settings["context"],
        "language": settings["language"] or "English",
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


def prepare_card(target, settings):
    """Writes the setup files to `target`. Returns the list of what was done."""
    target = Path(target)
    for src in (AIWATCH_SRC, RUNONCE_SRC):
        if not src.is_file():
            raise FileNotFoundError("%s is missing; run this from a full copy of the repository" % src)

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

    _write(target / "runonce.sh", _unix_script(RUNONCE_SRC))
    _write(data_dir / "aiwatch", _unix_script(AIWATCH_SRC))
    done.append("Copied runonce.sh and the aiwatch script")

    if all((WEBUI_SRC / src).is_file() for src, _ in WEBUI_FILES):
        for src, dest in WEBUI_FILES:
            (data_dir / dest).parent.mkdir(parents=True, exist_ok=True)
            _write(data_dir / dest, _unix_script(WEBUI_SRC / src))
        done.append("Copied the AI Alerts page for the camera's web interface")

    _write(data_dir / "aiwatch.json", json.dumps(build_aiwatch_config(settings), indent=2) + "\n")
    done.append("Wrote AI settings (%d rule%s)" % (len(settings["rules"]), "" if len(settings["rules"]) == 1 else "s"))

    overlay = build_send2_overlay(settings)
    if overlay:
        _write(data_dir / "send2.json", json.dumps(overlay, indent=2) + "\n")
        done.append("Wrote notifier settings: " + ", ".join(sorted(overlay)))

    if settings["enable_motion"]:
        _write(data_dir / "enable_motion", "1\n")
        done.append("Motion detection will be turned on")

    if settings["wifi_ssid"]:
        _write(data_dir / "wifi_ssid", settings["wifi_ssid"])
        _write(data_dir / "wifi_pass", settings["wifi_pass"])
        done.append("Wrote Wi-Fi settings for '%s'" % settings["wifi_ssid"])

    return done


# --- GUI ---------------------------------------------------------------------

def run_gui():
    import threading
    import tkinter as tk
    import webbrowser
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title("wyzer-cam SD card installer")
    root.minsize(560, 0)
    root.columnconfigure(0, weight=1)
    pad = {"padx": 8, "pady": 3}

    var = {key: tk.StringVar(value=value) for key, value in DEFAULTS.items() if isinstance(value, str)}
    var["enabled"] = tk.BooleanVar(value=True)
    var["enable_motion"] = tk.BooleanVar(value=True)
    target_var = tk.StringVar()
    drive_paths = {}

    def field(parent, row, label, key, show=None, hint=None):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", **pad)
        entry = ttk.Entry(parent, textvariable=var[key], show=show or "")
        entry.grid(row=row, column=1, sticky="ew", **pad)
        if hint:
            ttk.Label(parent, text=hint, foreground="#666").grid(row=row + 1, column=1, sticky="w", padx=8)
        return entry

    def secret_field(parent, row, label, key):
        entry = field(parent, row, label, key, show="\u2022")
        shown = tk.BooleanVar(value=False)

        def toggle():
            entry.configure(show="" if shown.get() else "\u2022")

        ttk.Checkbutton(parent, text="Show", variable=shown, command=toggle).grid(row=row, column=2, **pad)

    # --- card ---
    card = ttk.LabelFrame(root, text="1. SD card")
    card.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 4))
    card.columnconfigure(0, weight=1)
    drive_box = ttk.Combobox(card, textvariable=target_var)
    drive_box.grid(row=0, column=0, sticky="ew", **pad)

    def refresh_drives():
        drives = list_removable_drives()
        drive_paths.clear()
        drive_paths.update({text: path for path, text in drives})
        drive_box["values"] = [text for _, text in drives]
        if drives and not target_var.get():
            target_var.set(drives[0][1])

    def browse():
        folder = filedialog.askdirectory(title="Choose the SD card or a folder")
        if folder:
            target_var.set(folder)

    ttk.Button(card, text="Refresh", command=refresh_drives).grid(row=0, column=1, **pad)
    ttk.Button(card, text="Browse\u2026", command=browse).grid(row=0, column=2, **pad)
    ttk.Label(card, foreground="#666", wraplength=520, justify="left",
              text="Files are added to the card; nothing is formatted or erased. "
                   "Use a FAT32 card of 32 GB or less.").grid(
        row=1, column=0, columnspan=3, sticky="w", **pad)
    flash_var = tk.BooleanVar(value=False)
    ttk.Checkbutton(card, variable=flash_var,
                    text="Camera is still on Wyze firmware: also install thingino (25 MB download)").grid(
        row=2, column=0, columnspan=2, sticky="w", **pad)
    ttk.Button(card, text="About", command=lambda: webbrowser.open(THINGINO_INSTALL_URL)).grid(
        row=2, column=2, **pad)

    # --- tabs ---
    tabs = ttk.Notebook(root)
    tabs.grid(row=1, column=0, sticky="ew", padx=10, pady=4)

    wifi = ttk.Frame(tabs)
    wifi.columnconfigure(1, weight=1)
    tabs.add(wifi, text="2. Wi-Fi")
    field(wifi, 0, "Network name", "wifi_ssid", hint="Leave empty to keep the camera's current Wi-Fi. 2.4 GHz only.")
    secret_field(wifi, 2, "Password", "wifi_pass")

    ai = ttk.Frame(tabs)
    ai.columnconfigure(1, weight=1)
    tabs.add(ai, text="3. AI")
    ttk.Checkbutton(ai, text="Turn AI alerts on", variable=var["enabled"]).grid(
        row=0, column=0, columnspan=2, sticky="w", **pad)
    ttk.Checkbutton(ai, text="Turn the camera's motion detection on", variable=var["enable_motion"]).grid(
        row=1, column=0, columnspan=2, sticky="w", **pad)
    field(ai, 2, "Endpoint", "endpoint")
    secret_field(ai, 3, "API key", "api_key")
    field(ai, 4, "Model", "model", hint="A model that accepts images, as listed on openrouter.ai/models")
    field(ai, 6, "Camera name", "camera_name")
    field(ai, 7, "Scene", "context", hint="One line, such as: Front porch facing the street")
    field(ai, 9, "Language", "language")
    ttk.Label(ai, text="Alert rules").grid(row=10, column=0, sticky="nw", **pad)
    rules_box = tk.Text(ai, height=4, width=40, wrap="word")
    rules_box.grid(row=10, column=1, sticky="ew", **pad)
    ttk.Label(ai, foreground="#666", text="One per line, up to 10. Empty: alert on anything notable.").grid(
        row=11, column=1, sticky="w", padx=8)
    limits = ttk.Frame(ai)
    limits.grid(row=12, column=1, sticky="w")
    ttk.Label(limits, text="Seconds between calls").grid(row=0, column=0, **pad)
    ttk.Entry(limits, textvariable=var["cooldown"], width=6).grid(row=0, column=1, **pad)
    ttk.Label(limits, text="Calls per day").grid(row=0, column=2, **pad)
    ttk.Entry(limits, textvariable=var["daily_limit"], width=6).grid(row=0, column=3, **pad)

    alerts = ttk.Frame(tabs)
    alerts.columnconfigure(1, weight=1)
    tabs.add(alerts, text="4. Alerts")
    secret_field(alerts, 0, "Telegram bot token", "tg_token")
    field(alerts, 1, "Telegram chat ID", "tg_channel")
    ttk.Separator(alerts).grid(row=2, column=0, columnspan=3, sticky="ew", pady=6)
    field(alerts, 3, "ntfy server", "ntfy_host")
    field(alerts, 4, "ntfy topic", "ntfy_topic", hint="Leave a service empty to skip it.")

    # --- write ---
    bottom = ttk.Frame(root)
    bottom.grid(row=2, column=0, sticky="ew", padx=10, pady=(4, 10))
    bottom.columnconfigure(0, weight=1)
    log = tk.Text(bottom, height=8, width=60, wrap="word", state="disabled")
    log.grid(row=1, column=0, sticky="ew", pady=(6, 0))

    def show(lines):
        log.configure(state="normal")
        log.delete("1.0", "end")
        log.insert("end", "\n".join(lines))
        log.configure(state="disabled")

    def collect():
        settings = {key: (v.get().strip() if isinstance(v, tk.StringVar) else bool(v.get()))
                    for key, v in var.items()}
        settings["wifi_pass"] = var["wifi_pass"].get()  # spaces can be part of a password
        settings["rules"] = [line.strip() for line in rules_box.get("1.0", "end").splitlines() if line.strip()]
        return settings

    def finish(target, done, flash):
        if flash:
            steps = [
                "1. Eject the card safely. Unplug the camera, put the card in, plug it back in.",
                "2. Leave it alone for about six minutes. It backs up the Wyze firmware to the "
                "card, installs thingino, restarts, applies your settings and restarts again.",
                "3. Afterwards, copy the WYZE_BACKUP folder from the card to your computer. "
                "It is the only way back to Wyze firmware and is unique to this camera.",
                "4. wyzer/install.log on the card lists what was set up.",
            ]
        else:
            steps = [
                "1. Eject the card safely and put it in the camera.",
                "2. Power the camera on and leave it for about two minutes; it restarts once.",
                "3. Afterwards wyzer/install.log on the card lists what was done.",
            ]
        show(done + ["", "Card is ready: %s" % target] + steps +
             ["Keys and passwords are deleted from the card once the camera has read them."])

    def work(target, settings, flash):
        # Runs off the UI thread; talks to the window only through root.after.
        def status(text):
            root.after(0, lambda: show([text]))

        def progress(done_bytes, total_bytes):
            now = time.time()
            if now - progress.last > 0.3:
                progress.last = now
                total = " of %.0f MB" % (total_bytes / 1e6) if total_bytes else ""
                status("Downloading the thingino installer: %.0f MB%s" % (done_bytes / 1e6, total))

        progress.last = 0.0
        try:
            done = []
            if flash:
                status("Downloading the thingino installer…")
                cache = image_cache_path()
                fresh = download_thingino_image(cache, progress)
                done.append("Downloaded the thingino installer" if fresh
                            else "Used the thingino installer downloaded earlier")
                status("Checking the installer image…")
                files = read_thingino_files(cache)
                status("Copying thingino to the card…")
                done += write_thingino_files(target, files)
            done += prepare_card(target, settings)
        except Exception as exc:  # shown to the user; nothing else can handle it here
            message = str(exc) or exc.__class__.__name__
            root.after(0, lambda: (show(["Stopped: " + message]),
                                   messagebox.showerror("Writing failed", message),
                                   write_button.state(["!disabled"])))
            return
        root.after(0, lambda: (finish(target, done, flash), write_button.state(["!disabled"])))

    def write():
        settings = collect()
        flash = bool(flash_var.get())
        choice = target_var.get().strip()
        target = drive_paths.get(choice, choice)
        errors, warnings = validate(settings, target, flash)
        if errors:
            messagebox.showerror("Cannot write the card", "\n".join(errors))
            return
        if warnings and not messagebox.askyesno("Check before writing", "\n\n".join(warnings) + "\n\nWrite anyway?"):
            return
        if foreign_runonce(target) and not messagebox.askyesno(
                "Replace runonce.sh?",
                "This card already has a runonce.sh that was not made by this tool.\n\nReplace it?"):
            return
        if flash and not messagebox.askyesno(
                "Replace the Wyze firmware?",
                "A camera started with this card will have its Wyze firmware replaced by thingino. "
                "The Wyze app will stop working with it.\n\n"
                "The installer first saves the Wyze firmware to the card in a WYZE_BACKUP folder; "
                "that backup is the only way back.\n\n"
                "Only for the Wyze Cam v3 (square face). Continue?"):
            return
        write_button.state(["disabled"])
        threading.Thread(target=work, args=(target, settings, flash), daemon=True).start()

    write_button = ttk.Button(bottom, text="Write to SD card", command=write)
    write_button.grid(row=0, column=0, sticky="e")

    refresh_drives()
    show(["Fill in the tabs, then press Write to SD card."])
    return root


if __name__ == "__main__":
    run_gui().mainloop()
