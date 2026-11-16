"""wyzer-cam installer window.

  python installer/wyzer_installer.py

Two ways to get the AI addon (aiwatch) onto a Wyze Cam v3:
  Camera (SSH)  for a camera that already runs thingino: find it on the
                network, log in, install or update, test, uninstall.
  SD card       prepares a card the camera reads when it starts, and can put
                the thingino installer on it for a camera still on Wyze firmware.

The settings tabs below (Wi-Fi, AI model, Rules and limits, Alerts) are shared.
Nothing is formatted and no disk is written raw. Needs the `paramiko` package
for SSH (pip install paramiko); everything else is the standard library.
"""

import queue
import threading
import time
import traceback
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, ttk

import camera_ssh
import discover
import openrouter
import telegram_helper
import wyzer_core as core

RECOMMENDED = core.RECOMMENDED_MODEL
THINGINO_GUIDE = core.THINGINO_INSTALL_URL
PAD = {"padx": 8, "pady": 3}
TEXT_KEYS = ("wifi_ssid", "wifi_pass", "endpoint", "api_key", "model", "camera_name", "context", "language",
             "extra_instructions", "cooldown", "daily_limit", "free_daily_limit", "max_attempts", "active_from",
             "active_to", "tg_token", "tg_channel", "ntfy_host", "ntfy_topic")
BOOL_KEYS = ("enabled", "enable_motion")


class InstallerApp:
    def __init__(self, root):
        self.root = root
        root.title("wyzer-cam installer")
        root.minsize(640, 0)
        root.columnconfigure(0, weight=1)
        self.inbox = queue.Queue()
        self.known = camera_ssh.KnownHosts()
        self.ex = None
        self.host_key = None
        self.camera_ready = False
        self.busy = False
        self.catalog = None
        self.pool_ids = []
        self.drive_paths = {}
        self.buttons = []
        self.v = {key: tk.StringVar(value=core.DEFAULTS[key]) for key in TEXT_KEYS}
        self.v.update({key: tk.BooleanVar(value=core.DEFAULTS[key]) for key in BOOL_KEYS})
        self.v.update(host=tk.StringVar(), user=tk.StringVar(value="root"), ssh_password=tk.StringVar(),
                      use_key=tk.BooleanVar(value=bool(camera_ssh.find_key_files())),
                      mode=tk.StringVar(value="single"), flash=tk.BooleanVar(value=False),
                      target=tk.StringVar(), key_status=tk.StringVar(), model_info=tk.StringVar(),
                      bot_status=tk.StringVar(), camera_status=tk.StringVar(value="Not connected."))
        self.build()
        self.fill(core.apply_profile(dict(core.DEFAULTS), core.load_profile()))
        profile = core.load_profile()
        self.v["host"].set(profile.get("camera_host", ""))
        self.v["user"].set(profile.get("camera_user", "root") or "root")
        self.refresh_drives()
        self.say("Pick how to install, fill in the tabs, then press the button. "
                 "Find cameras looks for thingino cameras on your network.")
        self.poll()
        root.bind("<Destroy>", lambda event: event.widget is root and root.after_cancel(self.poll_id))
        self.load_catalog()

    # --- dialogs (replaced in tests) ---

    def confirm(self, title, text):
        return messagebox.askyesno(title, text, parent=self.root)

    def inform(self, title, text):
        messagebox.showinfo(title, text, parent=self.root)

    def error(self, title, text):
        messagebox.showerror(title, text, parent=self.root)

    def choose_chat(self, chats):
        """Shows the chats found for the bot; returns the chosen one or None."""
        dialog = tk.Toplevel(self.root)
        dialog.title("Choose the chat for alerts")
        dialog.transient(self.root)
        dialog.grab_set()
        ttk.Label(dialog, text="These chats messaged your bot. Pick the one that should get alerts.").grid(row=0, column=0, **PAD)
        box = tk.Listbox(dialog, height=min(8, len(chats)), width=60, exportselection=False)
        for chat in chats:
            box.insert("end", "%s    (%s, id %s)" % (chat["name"], chat["type"], chat["id"]))
        box.selection_set(0)
        box.grid(row=1, column=0, **PAD)
        chosen = []

        def ok():
            if box.curselection():
                chosen.append(chats[box.curselection()[0]])
            dialog.destroy()

        ttk.Button(dialog, text="Use this chat", command=ok).grid(row=2, column=0, sticky="e", **PAD)
        dialog.wait_window()
        return chosen[0] if chosen else None

    # --- layout ---

    def build(self):
        root = self.root
        self.where = ttk.Notebook(root)
        self.where.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 4))
        self.build_ssh_tab()
        self.build_sd_tab()

        self.tabs = ttk.Notebook(root)
        self.tabs.grid(row=1, column=0, sticky="ew", padx=10, pady=4)
        self.build_wifi_tab()
        self.build_model_tab()
        self.build_rules_tab()
        self.build_alerts_tab()

        log_frame = ttk.Frame(root)
        log_frame.grid(row=2, column=0, sticky="ew", padx=10, pady=(4, 10))
        log_frame.columnconfigure(0, weight=1)
        self.log = tk.Text(log_frame, height=7, width=70, wrap="word", state="disabled")
        self.log.grid(row=0, column=0, sticky="ew")
        scroll = ttk.Scrollbar(log_frame, command=self.log.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=scroll.set)

    def button(self, parent, text, command, **grid):
        b = ttk.Button(parent, text=text, command=command)
        b.grid(**grid)
        self.buttons.append(b)
        return b

    def field(self, parent, row, label, key, show=None, width=None, hint=None):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", **PAD)
        entry = ttk.Entry(parent, textvariable=self.v[key], show=show or "", width=width or 40)
        entry.grid(row=row, column=1, sticky="ew", **PAD)
        if hint:
            ttk.Label(parent, text=hint, foreground="#666").grid(row=row + 1, column=1, columnspan=2, sticky="w", padx=8)
        return entry

    def secret(self, parent, row, label, key, hint=None):
        entry = self.field(parent, row, label, key, show="•", hint=hint)
        shown = tk.BooleanVar(value=False)
        ttk.Checkbutton(parent, text="Show", variable=shown,
                        command=lambda: entry.configure(show="" if shown.get() else "•")).grid(row=row, column=2, **PAD)
        return entry

    def build_ssh_tab(self):
        tab = ttk.Frame(self.where)
        tab.columnconfigure(1, weight=1)
        self.where.add(tab, text="Camera (SSH)")
        ttk.Label(tab, text="Camera address").grid(row=0, column=0, sticky="w", **PAD)
        self.host_box = ttk.Combobox(tab, textvariable=self.v["host"])
        self.host_box.grid(row=0, column=1, sticky="ew", **PAD)
        self.button(tab, "Find cameras", self.find_cameras, row=0, column=2, **PAD)
        ttk.Label(tab, text="User").grid(row=1, column=0, sticky="w", **PAD)
        ttk.Entry(tab, textvariable=self.v["user"], width=14).grid(row=1, column=1, sticky="w", **PAD)
        self.secret(tab, 2, "Password", "ssh_password")
        ttk.Checkbutton(tab, text="Use my SSH key when I have one (then the password is optional)",
                        variable=self.v["use_key"]).grid(row=3, column=1, columnspan=2, sticky="w", **PAD)
        self.button(tab, "Connect and check", self.connect, row=4, column=0, **PAD)
        ttk.Label(tab, textvariable=self.v["camera_status"], wraplength=440, justify="left").grid(
            row=4, column=1, columnspan=2, sticky="w", **PAD)
        actions = ttk.Frame(tab)
        actions.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(4, 6))
        self.install_button = self.button(actions, "Install / update", self.install_ssh, row=0, column=0, **PAD)
        self.button(actions, "Test AI", lambda: self.test_ssh(False), row=0, column=1, **PAD)
        self.button(actions, "Test and send alert", lambda: self.test_ssh(True), row=0, column=2, **PAD)
        self.button(actions, "Uninstall", self.uninstall_ssh, row=0, column=3, **PAD)
        self.button(actions, "Set up key login", self.setup_key, row=1, column=0, **PAD)
        self.button(actions, "Restart camera", self.restart_ssh, row=1, column=1, **PAD)

    def build_sd_tab(self):
        tab = ttk.Frame(self.where)
        tab.columnconfigure(0, weight=1)
        self.where.add(tab, text="SD card")
        self.drive_box = ttk.Combobox(tab, textvariable=self.v["target"])
        self.drive_box.grid(row=0, column=0, sticky="ew", **PAD)
        self.button(tab, "Refresh", self.refresh_drives, row=0, column=1, **PAD)
        self.button(tab, "Browse…", self.browse, row=0, column=2, **PAD)
        ttk.Label(tab, foreground="#666", wraplength=560, justify="left",
                  text="Files are added to the card; nothing is formatted or erased. The card must be FAT32. "
                       "A large FAT32 card (for example 128 GB) works for the files, but the Wyze bootloader "
                       "is only known to read cards of 32 GB or less when installing thingino.").grid(
            row=1, column=0, columnspan=3, sticky="w", **PAD)
        ttk.Checkbutton(tab, variable=self.v["flash"],
                        text="Camera is still on Wyze firmware: also install thingino (25 MB download)").grid(
            row=2, column=0, columnspan=2, sticky="w", **PAD)
        ttk.Button(tab, text="About", command=lambda: webbrowser.open(THINGINO_GUIDE)).grid(row=2, column=2, **PAD)
        self.write_button = self.button(tab, "Write to SD card", self.write_sd, row=3, column=2, sticky="e", **PAD)

    def build_wifi_tab(self):
        tab = ttk.Frame(self.tabs)
        tab.columnconfigure(1, weight=1)
        self.tabs.add(tab, text="Wi-Fi")
        self.field(tab, 0, "Network name", "wifi_ssid",
                   hint="Leave empty to keep the camera's current Wi-Fi. A wrong password cuts the camera off until it is reset.")
        self.secret(tab, 2, "Password", "wifi_pass")

    def build_model_tab(self):
        tab = ttk.Frame(self.tabs)
        tab.columnconfigure(1, weight=1)
        self.tabs.add(tab, text="AI model")
        ttk.Checkbutton(tab, text="Turn AI alerts on", variable=self.v["enabled"]).grid(
            row=0, column=0, columnspan=3, sticky="w", **PAD)
        ttk.Checkbutton(tab, text="Turn the camera's motion detection on", variable=self.v["enable_motion"]).grid(
            row=1, column=0, columnspan=3, sticky="w", **PAD)
        self.field(tab, 2, "Endpoint", "endpoint")
        self.secret(tab, 3, "API key", "api_key", hint="Blank on an update keeps the key already on the camera.")
        self.button(tab, "Check key", self.check_key, row=4, column=2, **PAD)
        ttk.Label(tab, textvariable=self.v["key_status"], wraplength=420, justify="left", foreground="#444").grid(
            row=5, column=1, columnspan=2, sticky="w", padx=8)

        modes = ttk.Frame(tab)
        modes.grid(row=6, column=0, columnspan=3, sticky="w", **PAD)
        ttk.Radiobutton(modes, text="One model", value="single", variable=self.v["mode"], command=self.show_mode).grid(row=0, column=0, padx=(0, 16))
        ttk.Radiobutton(modes, text="Rotate between free models", value="pool", variable=self.v["mode"], command=self.show_mode).grid(row=0, column=1)

        self.single_frame = ttk.Frame(tab)
        self.single_frame.grid(row=7, column=0, columnspan=3, sticky="ew")
        self.single_frame.columnconfigure(1, weight=1)
        ttk.Label(self.single_frame, text="Model").grid(row=0, column=0, sticky="w", **PAD)
        self.model_box = ttk.Combobox(self.single_frame, textvariable=self.v["model"])
        self.model_box.grid(row=0, column=1, sticky="ew", **PAD)
        self.model_box.bind("<<ComboboxSelected>>", lambda e: self.show_model_info())
        self.model_box.bind("<FocusOut>", lambda e: self.show_model_info())
        self.button(self.single_frame, "Use recommended", lambda: (self.v["model"].set(RECOMMENDED), self.show_model_info()),
                    row=0, column=2, **PAD)
        ttk.Label(self.single_frame, foreground="#666",
                  text="Recommended: %s (cheap, fast). The list is image-capable OpenRouter models, cheapest first." % RECOMMENDED,
                  wraplength=560, justify="left").grid(row=1, column=1, columnspan=2, sticky="w", padx=8)
        self.button(self.single_frame, "Check model", self.check_model, row=2, column=2, **PAD)
        ttk.Label(self.single_frame, textvariable=self.v["model_info"], foreground="#444", wraplength=420,
                  justify="left").grid(row=2, column=1, sticky="w", padx=8)

        self.pool_frame = ttk.Frame(tab)
        self.pool_frame.grid(row=7, column=0, columnspan=3, sticky="ew")
        self.pool_frame.columnconfigure(0, weight=1)
        ttk.Label(self.pool_frame, text="Free models to rotate between (select one or more)").grid(row=0, column=0, sticky="w", **PAD)
        holder = ttk.Frame(self.pool_frame)
        holder.grid(row=1, column=0, sticky="ew", **PAD)
        holder.columnconfigure(0, weight=1)
        self.pool_list = tk.Listbox(holder, selectmode="multiple", exportselection=False, height=6)
        self.pool_list.grid(row=0, column=0, sticky="ew")
        bar = ttk.Scrollbar(holder, command=self.pool_list.yview)
        bar.grid(row=0, column=1, sticky="ns")
        self.pool_list.configure(yscrollcommand=bar.set)
        limits = ttk.Frame(self.pool_frame)
        limits.grid(row=2, column=0, sticky="w", **PAD)
        ttk.Label(limits, text="Free calls per day").grid(row=0, column=0, padx=(0, 4))
        ttk.Entry(limits, textvariable=self.v["free_daily_limit"], width=6).grid(row=0, column=1, padx=(0, 16))
        ttk.Label(limits, text="Models to try per event").grid(row=0, column=2, padx=(0, 4))
        ttk.Entry(limits, textvariable=self.v["max_attempts"], width=6).grid(row=0, column=3, padx=(0, 16))
        self.button(limits, "Reload list", self.load_catalog, row=0, column=4)
        ttk.Label(self.pool_frame, foreground="#666", wraplength=580, justify="left",
                  text="Each event starts with the model after the one that last answered; a model that fails is "
                       "skipped for a few minutes. OpenRouter applies its free daily allowance to your whole account "
                       "across all free models, so rotating helps with rate limits and outages but adds no calls. "
                       "Free models can be run by providers that keep or train on what they receive: check "
                       "OpenRouter's privacy settings first.").grid(row=3, column=0, sticky="w", padx=8)
        self.show_mode()

    def build_rules_tab(self):
        tab = ttk.Frame(self.tabs)
        tab.columnconfigure(1, weight=1)
        self.tabs.add(tab, text="Rules and limits")
        self.field(tab, 0, "Camera name", "camera_name", hint="Uses the camera's hostname when empty.")
        self.field(tab, 2, "What the camera sees", "context", hint="One line, such as: Front porch facing the street")
        self.field(tab, 4, "Language", "language")
        self.field(tab, 5, "Extra instructions", "extra_instructions", hint="For example: the grey cat lives here; do not report it")
        ttk.Label(tab, text="Alert rules").grid(row=7, column=0, sticky="nw", **PAD)
        self.rules_box = tk.Text(tab, height=4, width=40, wrap="word")
        self.rules_box.grid(row=7, column=1, sticky="ew", **PAD)
        ttk.Label(tab, foreground="#666", text="One per line, up to 10. Empty: alert on anything notable.").grid(
            row=8, column=1, sticky="w", padx=8)
        limits = ttk.Frame(tab)
        limits.grid(row=9, column=0, columnspan=3, sticky="w", **PAD)
        for column, (label, key, width) in enumerate((("Seconds between calls", "cooldown", 6), ("Events per day", "daily_limit", 6),
                                                      ("Active from", "active_from", 7), ("until", "active_to", 7))):
            ttk.Label(limits, text=label).grid(row=0, column=2 * column, padx=(8, 4))
            ttk.Entry(limits, textvariable=self.v[key], width=width).grid(row=0, column=2 * column + 1)
        ttk.Label(tab, foreground="#666", text="Active times like 22:00 to 06:00; leave empty for all day.").grid(
            row=10, column=1, sticky="w", padx=8)

    def build_alerts_tab(self):
        tab = ttk.Frame(self.tabs)
        tab.columnconfigure(1, weight=1)
        self.tabs.add(tab, text="Alerts")
        self.secret(tab, 0, "Telegram bot token", "tg_token")
        self.button(tab, "Check bot", self.check_bot, row=1, column=2, **PAD)
        ttk.Label(tab, textvariable=self.v["bot_status"], foreground="#444").grid(row=1, column=1, sticky="w", padx=8)
        self.field(tab, 2, "Telegram chat ID", "tg_channel")
        self.button(tab, "Find my chat", self.find_chat, row=2, column=2, **PAD)
        self.button(tab, "Send test message", self.send_test_message, row=3, column=2, **PAD)
        ttk.Label(tab, foreground="#666", text="To find your chat: open Telegram, send any message to your bot, then press Find my chat.").grid(
            row=3, column=0, columnspan=2, sticky="w", padx=8)
        ttk.Separator(tab).grid(row=4, column=0, columnspan=3, sticky="ew", pady=6)
        self.field(tab, 5, "ntfy server", "ntfy_host")
        self.field(tab, 6, "ntfy topic", "ntfy_topic", hint="Leave a service empty to skip it. Alerts also need the camera's own Send to services settings for ntfy.")

    # --- settings <-> window ---

    def collect(self):
        s = {key: self.v[key].get().strip() for key in TEXT_KEYS}
        s["wifi_pass"] = self.v["wifi_pass"].get()  # spaces can be part of a password
        s.update({key: bool(self.v[key].get()) for key in BOOL_KEYS})
        s["free_pool"] = self.v["mode"].get() == "pool"
        s["free_models"] = self.selected_pool()
        s["rules"] = [line.strip() for line in self.rules_box.get("1.0", "end").splitlines() if line.strip()]
        s["extra_instructions"] = " ".join(s["extra_instructions"].split())
        return s

    def fill(self, s):
        for key in TEXT_KEYS:
            if key in s:
                self.v[key].set(str(s[key]))
        for key in BOOL_KEYS:
            if key in s:
                self.v[key].set(bool(s[key]))
        self.v["mode"].set("pool" if s.get("free_pool") else "single")
        self.rules_box.delete("1.0", "end")
        self.rules_box.insert("end", "\n".join(s.get("rules", [])))
        self.set_pool_choices(s.get("free_models", []))
        self.show_mode()
        self.show_model_info()

    def show_mode(self):
        pool = self.v["mode"].get() == "pool"
        (self.pool_frame if pool else self.single_frame).grid()
        (self.single_frame if pool else self.pool_frame).grid_remove()

    # --- log and tasks ---

    def say(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def show(self, lines):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        for line in lines:
            self.say(line)

    def say_later(self, text):
        """say() for worker threads."""
        self.post(lambda: self.say(text))

    def post(self, function):
        """Runs `function` on the window's thread. Safe to call from any thread."""
        self.inbox.put(function)

    def poll(self):
        try:
            while True:
                function = self.inbox.get_nowait()
                try:
                    function()
                except Exception:  # one failing callback must not stop the window
                    traceback.print_exc()
        except queue.Empty:
            pass
        self.poll_id = self.root.after(30, self.poll)

    def set_busy(self, busy):
        self.busy = busy
        for b in self.buttons:
            b.state(["disabled"] if busy else ["!disabled"])

    def run_task(self, work, done, label=None):
        """Runs `work()` off the window's thread, then `done(result, error)` on it."""
        if self.busy:
            return
        self.set_busy(True)
        if label:
            self.say(label)

        def target():
            try:
                result, error = work(), None
            except Exception as exc:  # shown to the user by done()
                result, error = None, exc
            self.post(lambda: self.finish_task(done, result, error))

        threading.Thread(target=target, daemon=True).start()

    def finish_task(self, done, result, error):
        self.set_busy(False)
        done(result, error)

    def fail(self, title, error):
        text = str(error) or error.__class__.__name__
        self.say("Stopped: " + text)
        self.error(title, text)

    # --- model list, key check ---

    def load_catalog(self):
        """Fetches OpenRouter's model list in the background; the window stays usable meanwhile."""
        if not core.is_openrouter(self.v["endpoint"].get()):
            return

        def work():
            try:
                result, error = openrouter.fetch_catalog(), None
            except Exception as exc:  # a missing list is not fatal: the model id can be typed
                result, error = None, exc
            self.post(lambda: done(result, error))

        def done(result, error):
            if error:
                self.say("Could not load OpenRouter's model list (%s). You can still type a model id." % error)
                return
            self.catalog = result
            self.model_box["values"] = [m["id"] for m in result["paid"][:80]]
            self.set_pool_choices(self.selected_pool())
            self.show_model_info()
            self.say("Loaded %d paid and %d free image-capable models from OpenRouter." % (len(result["paid"]), len(result["free"])))

        threading.Thread(target=work, daemon=True).start()

    def set_pool_choices(self, selected):
        selected = list(selected)
        known = [m for m in (self.catalog["free"] if self.catalog else [])]
        ids, labels = [], []
        for m in known:
            ids.append(m["id"])
            labels.append(m["id"] + ("   (reasoning model, may run out of answer space)" if m["reasoning"] else ""))
        for model_id in selected:
            if model_id not in ids:
                ids.append(model_id)
                labels.append(model_id + ("   (not in the current list)" if self.catalog else ""))
        self.pool_ids = ids
        self.pool_list.delete(0, "end")
        for index, label in enumerate(labels):
            self.pool_list.insert("end", label)
            if ids[index] in selected:
                self.pool_list.selection_set(index)

    def selected_pool(self):
        return [self.pool_ids[i] for i in self.pool_list.curselection()]

    def show_model_info(self):
        model = self.v["model"].get().strip()
        for entry in (self.catalog["paid"] + self.catalog["free"]) if self.catalog else []:
            if entry["id"] == model:
                self.v["model_info"].set("$%.2f in / $%.2f out per million tokens" % (entry["input"], entry["output"])
                                         if entry["input"] == entry["input"] else "price not listed")
                return
        self.v["model_info"].set("")

    def check_model(self):
        model = self.v["model"].get().strip()
        if not model:
            self.v["model_info"].set("Enter a model id first.")
            return
        if not self.catalog:
            self.v["model_info"].set("The model list is not loaded (OpenRouter only). Press Reload list on the free models option, or check your connection.")
            return
        ok, message = openrouter.check_model(self.catalog, model)
        self.v["model_info"].set(message)

    def check_key(self):
        key = self.v["api_key"].get().strip()
        if not core.is_openrouter(self.v["endpoint"].get()):
            self.v["key_status"].set("Key checking is only available for OpenRouter endpoints.")
            return
        self.v["key_status"].set("Checking…")
        self.run_task(lambda: openrouter.check_key(key), lambda r, e: self.v["key_status"].set(
            str(e) if e else ("✓ " if r[0] else "✗ " if r[0] is False else "? ") + r[1]))

    # --- Telegram ---

    def check_bot(self):
        token = self.v["tg_token"].get().strip()
        self.v["bot_status"].set("Checking…")

        def done(result, error):
            if error:
                self.v["bot_status"].set("✗ " + str(error))
            else:
                self.v["bot_status"].set("✓ Bot @%s" % result["username"])

        self.run_task(lambda: telegram_helper.get_me(token), done)

    def find_chat(self):
        token = self.v["tg_token"].get().strip()

        def done(chats, error):
            if error:
                return self.fail("Telegram", error)
            if not chats:
                return self.inform("No messages found", "Open Telegram, find your bot, press Start or send it any "
                                   "message, then press Find my chat again. (Telegram keeps a bot's messages for a day.)")
            chat = self.choose_chat(chats)
            if chat:
                self.v["tg_channel"].set(str(chat["id"]))
                self.say("Chat set to %s (%s)." % (chat["name"], chat["id"]))

        self.run_task(lambda: telegram_helper.find_chats(token), done, "Looking for messages to your bot…")

    def send_test_message(self):
        token, chat = self.v["tg_token"].get().strip(), self.v["tg_channel"].get().strip()
        if not token or not chat:
            return self.inform("Telegram", "Enter the bot token and the chat ID first.")

        def done(_, error):
            if error:
                return self.fail("Telegram", error)
            self.inform("Telegram", "Sent. Check Telegram for the test message.")

        self.run_task(lambda: telegram_helper.send_test(token, chat), done, "Sending a test message…")

    # --- camera (SSH) ---

    def current_host(self):
        host, port = parse_address(self.v["host"].get())
        return host if port == 22 else "%s:%d" % (host, port)

    def find_cameras(self):
        def work():
            return discover.scan_local(progress=lambda done, total: (
                self.say_later("Scanning your network… %d of %d addresses" % (done, total)) if done % 64 == 0 else None))

        def done(result, error):
            if error:
                return self.fail("Find cameras", error)
            found, network = result
            if network is None:
                return self.say("Could not work out which network this computer is on. Type the camera's address instead.")
            labels = ["%s    %s" % (f["host"], "thingino camera" if f["thingino"] else f["banner"] or "SSH device") for f in found]
            self.host_box["values"] = labels
            cameras = [i for i, f in enumerate(found) if f["thingino"]]
            if cameras:
                self.v["host"].set(labels[cameras[0]])
            self.say("Scanned %s: %d device%s with SSH, %d look like thingino cameras. %s" % (
                network, len(found), "" if len(found) == 1 else "s", len(cameras),
                "Pick one in the Camera address list." if len(found) > 1 else ""))

        self.run_task(work, done, "Scanning your network for cameras…")

    def connect(self):
        host, port = parse_address(self.v["host"].get())
        if not host:
            return self.inform("Camera address", "Enter the camera's address, or press Find cameras.")
        self.run_task(lambda: camera_ssh.fetch_host_key(host, port), lambda r, e: self.after_host_key(host, port, r, e),
                      "Contacting %s…" % host)

    def after_host_key(self, host, port, result, error):
        if error:
            return self.fail("Connect", error)
        key, fp = result
        state = self.known.check(self.current_host(), fp)
        if state == "new" and not self.confirm(
                "First time with this camera",
                "You have not connected to %s from this program before.\n\nIts identity is\n%s\n\n"
                "Continue and remember it?" % (host, fp)):
            return self.say("Cancelled.")
        if state == "changed" and not self.confirm(
                "This camera's identity changed",
                "%s presents a different identity than last time.\n\nThat is normal after the camera was reset or "
                "its firmware was reinstalled. It could also mean another device is answering at this address.\n\n"
                "New identity: %s\n\nOnly continue if you reset or reinstalled this camera. Continue?" % (host, fp)):
            return self.say("Cancelled.")
        self.known.remember(self.current_host(), fp)
        user = self.v["user"].get().strip() or "root"
        password = self.v["ssh_password"].get()
        keys = camera_ssh.find_key_files() if self.v["use_key"].get() else []

        def work():
            ex = camera_ssh.SshExecutor(host, port, user=user)
            ex.connect(key, password=password or None, key_files=keys)
            try:
                info = camera_ssh.inspect(ex)
                existing = camera_ssh.read_existing_config(ex)
            except Exception:
                ex.close()
                raise
            return ex, key, info, existing

        self.run_task(work, lambda r, e: self.after_login(host, r, e), "Logging in…")

    def after_login(self, host, result, error):
        if self.ex:
            self.ex.close()
        self.ex, self.camera_ready = None, False
        if error:
            self.v["camera_status"].set("Not connected.")
            return self.fail("Connect", error)
        self.ex, self.host_key, info, existing = result
        errors, warnings = camera_ssh.check_ready(info)
        self.camera_ready = not errors
        lines = ["Connected to %s (%s, logged in with your %s)." % (
            info.get("hostname") or host, info.get("image") or "unknown model", self.ex.login)]
        if info.get("build_time"):
            lines.append("Firmware built %s." % info["build_time"])
        lines.append("aiwatch is %s on this camera." % (
            "installed%s" % (" and hooked to motion events" if info.get("hooked") == "yes" else ", but not hooked to motion events")
            if info.get("aiwatch") == "yes" else "not installed"))
        lines += ["Problem: " + e for e in errors] + ["Note: " + w for w in warnings]
        self.v["camera_status"].set("%s — %s" % (info.get("hostname") or host, "ready" if self.camera_ready else "cannot be used"))
        self.show(lines)
        if existing and self.camera_ready and self.confirm(
                "Load the camera's settings?",
                "This camera already has aiwatch settings. Load them into the form so you can change them? "
                "(The API key stays on the camera; leave the key box empty to keep it.)"):
            self.fill(camera_ssh.settings_from_config(existing, base=self.collect()))
            self.say("Loaded the camera's current settings.")
        self.remember()

    def need_camera(self):
        if not self.ex or not self.camera_ready:
            self.inform("Not connected", "Press Connect and check first. The camera must be running thingino.")
            return False
        return True

    def remember(self):
        try:
            core.save_profile(self.collect(), self.current_host(), self.v["user"].get().strip() or "root")
        except OSError:
            pass

    def install_ssh(self):
        if not self.need_camera():
            return
        s = self.collect()
        errors, warnings = core.check_settings(s)
        if errors:
            return self.error("Check the settings", "\n".join(errors))
        if s["wifi_ssid"] and not self.confirm(
                "Change the camera's Wi-Fi?",
                "The camera is working on its current network. If this network name or password is wrong, or the "
                "network is 5 GHz only, the camera goes offline until it is reset.\n\nChange the Wi-Fi anyway?"):
            return
        if warnings and not self.confirm("Check before installing", "\n\n".join(warnings) + "\n\nInstall anyway?"):
            return
        self.remember()
        self.run_task(lambda: camera_ssh.install(self.ex, s, progress=self.say_later),
                      lambda r, e: self.after_install(s, r, e), "Installing…")

    def after_install(self, settings, result, error):
        if error:
            return self.fail("Install", error)
        self.say("")
        for line in result.log:
            self.say("  " + line)
        if not result.ok:
            return self.error("The install had problems", "\n".join(result.errors))
        self.say("Installed. Running a test on the camera…")
        if result.restart_needed and self.confirm("Restart the camera?", "The Wi-Fi settings apply after a restart. "
                                                   "Restart the camera now? (It is back in about a minute.)"):
            return self.restart_ssh(ask=False)
        self.test_ssh(False)

    def test_ssh(self, send):
        if not self.need_camera():
            return
        if send and not self.confirm("Send a real alert?", "This takes a snapshot, asks the model, and sends the alert "
                                      "to your Telegram or ntfy. Continue?"):
            return

        def done(result, error):
            if error:
                return self.fail("Test", error)
            ok, output = result
            self.say(("Test result:\n" if ok else "The test failed:\n") + output)

        self.run_task(lambda: camera_ssh.run_test(self.ex, send), done, "Testing on the camera (up to half a minute)…")

    def uninstall_ssh(self):
        if not self.need_camera():
            return
        if not self.confirm("Uninstall aiwatch?", "This removes aiwatch, its web page and the hook in the motion script "
                             "from the camera. thingino itself stays."):
            return
        keep = self.confirm("Keep the settings?", "Keep the settings file (it holds your API key) on the camera, "
                            "so a later install can reuse it?")

        def done(lines, error):
            if error:
                return self.fail("Uninstall", error)
            for line in lines:
                self.say("  " + line)
            self.say("Done." if not any(l.startswith("ERROR") for l in lines) else "Finished with problems.")

        self.run_task(lambda: camera_ssh.uninstall(self.ex, keep_config=keep), done, "Removing…")

    def restart_ssh(self, ask=True):
        if not self.need_camera():
            return
        if ask and not self.confirm("Restart the camera?", "It is offline for about a minute."):
            return

        def done(_, error):
            self.ex.close()
            self.ex, self.camera_ready = None, False
            self.v["camera_status"].set("Restarting… press Connect again in a minute.")
            self.say("The camera is restarting. Press Connect and check in about a minute.")

        self.run_task(lambda: camera_ssh.restart(self.ex), done)

    def setup_key(self):
        if not self.need_camera():
            return
        keys = camera_ssh.find_key_files()
        if not keys:
            if not self.confirm("Create an SSH key?", "You have no SSH key yet. Create one in your .ssh folder? "
                                 "It stays on this computer; only its public half goes to the camera."):
                return
            try:
                keys = [camera_ssh.create_key()]
            except Exception as exc:  # no ssh-keygen, no write access, ...
                return self.fail("SSH key", exc)
            self.say("Created %s." % keys[0])
        key = camera_ssh.load_key(keys[0])
        if key is None:
            return self.inform("SSH key", "Your key %s needs a passphrase, which this tool cannot use. Remove the "
                               "passphrase or use another key." % keys[0].name)
        (host, port), user, host_key = parse_address(self.v["host"].get()), self.v["user"].get().strip() or "root", self.host_key

        def work():
            added = camera_ssh.authorize_key(self.ex, camera_ssh.public_line(key))
            check = camera_ssh.SshExecutor(host, port, user=user)
            try:
                check.connect(host_key, password=None, key_files=[keys[0]])  # the key alone, no password
                check.run("true")
            finally:
                check.close()
            return added

        def done(added, error):
            if error:
                return self.fail("SSH key", "The key was sent but logging in with it did not work: %s" % error)
            self.v["use_key"].set(True)
            self.inform("Key login works", "%s Next time you can leave the password empty." %
                        ("The key was added to the camera." if added else "The camera already had this key."))

        self.run_task(work, done, "Setting up key login…")

    # --- SD card ---

    def refresh_drives(self):
        drives = core.list_removable_drives()
        self.drive_paths = {text: path for path, text in drives}
        self.drive_box["values"] = [text for _, text in drives]
        if drives and not self.v["target"].get():
            self.v["target"].set(drives[0][1])

    def browse(self):
        folder = filedialog.askdirectory(title="Choose the SD card or a folder")
        if folder:
            self.v["target"].set(folder)

    def write_sd(self):
        s = self.collect()
        flash = bool(self.v["flash"].get())
        choice = self.v["target"].get().strip()
        target = self.drive_paths.get(choice, choice)
        errors, warnings = core.validate(s, target, flash)
        if errors:
            return self.error("Cannot write the card", "\n".join(errors))
        if warnings and not self.confirm("Check before writing", "\n\n".join(warnings) + "\n\nWrite anyway?"):
            return
        if core.foreign_runonce(target) and not self.confirm(
                "Replace runonce.sh?", "This card already has a runonce.sh that was not made by this tool.\n\nReplace it?"):
            return
        if flash and not self.confirm(
                "Replace the Wyze firmware?",
                "A camera started with this card will have its Wyze firmware replaced by thingino. The Wyze app will "
                "stop working with it.\n\nThe installer first saves the Wyze firmware to the card in a WYZE_BACKUP "
                "folder; that backup is the only way back.\n\nOnly for the Wyze Cam v3 (square face). Continue?"):
            return
        self.remember()

        def work():
            done = []
            last = [0.0]

            def progress(got, total):
                if time.time() - last[0] > 1.0:
                    last[0] = time.time()
                    self.say_later("Downloading: %.0f MB%s" % (got / 1e6, " of %.0f MB" % (total / 1e6) if total else ""))

            if flash:
                self.say_later("Downloading the thingino installer…")
                cache = core.image_cache_path()
                fresh = core.download_thingino_image(cache, progress)
                done.append("Downloaded the thingino installer" if fresh else "Used the thingino installer downloaded earlier")
                files = core.read_thingino_files(cache)
                self.say_later("Copying thingino to the card…")
                done += core.write_thingino_files(target, files)
            return done + core.prepare_card(target, s)

        def finish(done, error):
            if error:
                return self.fail("Writing failed", error)
            steps = ([
                "1. Eject the card safely. Unplug the camera, put the card in, plug it back in.",
                "2. Leave it alone for about six minutes. It backs up the Wyze firmware to the card, installs "
                "thingino, restarts, applies your settings and restarts again.",
                "3. Afterwards copy the WYZE_BACKUP folder from the card to your computer. It is the only way back "
                "to Wyze firmware and is unique to this camera.",
                "4. wyzer/install.log on the card lists what was set up."] if flash else [
                "1. Eject the card safely and put it in the camera.",
                "2. Power the camera on and leave it for about two minutes; it restarts once.",
                "3. Afterwards wyzer/install.log on the card lists what was done."])
            self.show(done + ["", "Card is ready: %s" % target] + steps +
                      ["Keys and passwords are deleted from the card once the camera has read them."])

        self.run_task(work, finish, "Writing the card…")


def parse_address(text):
    """"192.168.1.5", "192.168.1.5:2222" or the combobox's "192.168.1.5    thingino camera" -> (host, port)."""
    parts = text.split()
    if not parts:
        return "", 22
    host, _, port = parts[0].rpartition(":")
    if host and port.isdigit() and ":" not in host:
        return host, int(port)
    return parts[0], 22


def run_gui():
    root = tk.Tk()
    app = InstallerApp(root)
    root.app = app
    return root


if __name__ == "__main__":
    run_gui().mainloop()
