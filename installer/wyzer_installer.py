"""wyzer-cam SD card installer.

Prepares an SD card that sets up aiwatch on a camera running thingino:
it copies the aiwatch script and a runonce.sh onto the card, together with
the settings entered in the window. thingino runs runonce.sh once when the
card is in the camera.

Run:  python installer/wyzer_installer.py

Standard library only. The card is written with ordinary file copies; nothing
is formatted and no disk is written raw.
"""

import json
import os
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AIWATCH_SRC = REPO_ROOT / "package" / "thingino-aiwatch" / "files" / "aiwatch"
RUNONCE_SRC = Path(__file__).resolve().parent / "runonce.sh"
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


def validate(settings, target):
    """Returns (errors, warnings) as lists of sentences."""
    errors, warnings = [], []
    target = Path(target) if target else None
    if not target or not target.is_dir():
        errors.append("Choose the SD card (or a folder) to write to.")

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
              text="The camera must run thingino. Files are added to the card; nothing is "
                   "formatted or erased. Use a FAT32 card of 32 GB or less.").grid(
        row=1, column=0, columnspan=3, sticky="w", **pad)
    ttk.Button(card, text="Camera still on Wyze firmware? Open the thingino install guide",
               command=lambda: webbrowser.open(THINGINO_INSTALL_URL)).grid(
        row=2, column=0, columnspan=3, sticky="w", **pad)

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

    def write():
        settings = collect()
        choice = target_var.get().strip()
        target = drive_paths.get(choice, choice)
        errors, warnings = validate(settings, target)
        if errors:
            messagebox.showerror("Cannot write the card", "\n".join(errors))
            return
        if warnings and not messagebox.askyesno("Check before writing", "\n\n".join(warnings) + "\n\nWrite anyway?"):
            return
        if foreign_runonce(target) and not messagebox.askyesno(
                "Replace runonce.sh?",
                "This card already has a runonce.sh that was not made by this tool.\n\nReplace it?"):
            return
        try:
            done = prepare_card(target, settings)
        except OSError as exc:
            messagebox.showerror("Writing failed", str(exc))
            return
        show(done + [
            "",
            "Card is ready: %s" % target,
            "1. Eject the card safely and put it in the camera.",
            "2. Power the camera on and leave it for about two minutes; it restarts once.",
            "3. Afterwards wyzer/install.log on the card lists what was done.",
            "Keys and passwords are deleted from the card once the camera has read them.",
        ])

    ttk.Button(bottom, text="Write to SD card", command=write).grid(row=0, column=0, sticky="e")

    refresh_drives()
    show(["Fill in the tabs, then press Write to SD card."])
    return root


if __name__ == "__main__":
    run_gui().mainloop()
