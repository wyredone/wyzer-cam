# wyzer-cam

Custom firmware work for the Wyze Cam v3, built on top of
[thingino](https://github.com/themactep/thingino-firmware) (stable `ciao` branch).

The first piece is **aiwatch**: AI descriptions and plain-language alert rules
for motion events, using any OpenAI-compatible endpoint (OpenRouter by default).

## What aiwatch does

When the camera detects motion it takes one snapshot, sends it to a vision
model, and gets back a one-line description plus a yes/no on whether to alert.
If the answer is yes, the description goes out through thingino's existing
notifiers (Telegram, ntfy, MQTT, webhook) with the photo attached.

- **Descriptions:** "A courier leaves a parcel at the door." instead of "Motion detected".
- **Rules in plain language:** list things like "A person is at the door". Only matching events alert.
- **No rules set:** it acts as a false-alert filter and stays quiet for empty scenes, rain, insects and lighting changes.
- **One model or a pool of free ones:** the default is `google/gemini-2.5-flash-lite` (cheap and fast). Or rotate between OpenRouter's free models, with per-model usage tracking. See [Models](#models).
- **Cost guards:** a cooldown between calls, a daily cap, and active hours.
- **Fail soft:** if the endpoint is down or replies with nonsense, a plain motion alert is sent instead.

It is off by default, because it sends frames from your camera to the endpoint
you configure.

## Layout

| Path | What it is |
|---|---|
| `package/thingino-aiwatch/` | Buildroot package: the `aiwatch` script, default config, web page, `Config.in`, `.mk` |
| `patches/` | Make thingino's motion script call `aiwatch motion`, and add the package to its menu |
| `installer/wyzer_installer.py` | The installer window (SSH and SD card) |
| `installer/camera_ssh.py`, `discover.py`, `openrouter.py`, `telegram_helper.py` | What the window uses: talking to the camera, finding cameras, model list and key check, Telegram setup |
| `installer/wyzer_core.py` | Settings checks, the files that get installed, the SD card writer, remembered settings |
| `installer/runonce.sh`, `uninstall.sh`, `inspect.sh` | Scripts that run on the camera |
| `installer/fatimage.py` | Reads files out of a FAT disk image, used for the thingino installer |
| `test/` | Test suites with a mock endpoint, a fake camera, and an in-process SSH server |

The patches were made against thingino `ciao` at commit `c193194585b5`.
Only the prudynt streamer (the stable branch) is supported; raptor is not.

## The installer window

```bash
pip install paramiko
python installer/wyzer_installer.py
```

Python 3.8 or newer. `paramiko` is only needed for the SSH tab; the SD card tab
uses the standard library alone. Nothing is ever formatted and no disk is
written raw.

Both tabs share the settings below them: **Wi-Fi**, **AI model**, **Rules and
limits** and **Alerts**.

### Camera (SSH): for a camera that already runs thingino

1. **Find cameras** scans your network for thingino cameras and lists them (a `/24` network, using the camera's web page title to recognise thingino). Or type the address.
2. Enter the camera's password and press **Connect and check**. The first time, the window shows the camera's identity (an SSH fingerprint) and asks you to accept it. It is remembered, and a camera that later presents a different identity is refused unless you deliberately accept the change. That is normal after a reset or reinstall, and it is also what a different device answering at that address looks like.
3. The window reads the camera's model, firmware date, free space and whether aiwatch is already installed, and refuses cameras that are not running thingino with the prudynt streamer. If the camera already has aiwatch settings it offers to load them into the form.
4. Fill in the tabs and press **Install / update**. The files are sent over the SSH connection straight into a private folder on the camera, installed, and deleted again, secrets included. Nothing with your API key is written to this computer.
5. The window then runs `aiwatch test` on the camera and shows what the model said. **Test and send alert** also sends a real alert, after asking.

Other buttons:

- **Uninstall** removes aiwatch, its web page and the hook in the motion script, restoring the files exactly as they were. It can keep the settings file.
- **Restart camera** for the Wi-Fi settings to apply. Installing never restarts the camera by itself; it asks.
- **Set up key login** installs your SSH key on the camera (creating one in your `.ssh` folder if you have none, after asking). It then checks that the key alone works, so later you can leave the password empty.
- **Update:** pressing Install again updates in place. A blank API key means "keep the key already on the camera".

A full thingino firmware update wipes aiwatch; press Install again afterwards.

### SD card: for a camera that is still on Wyze firmware, or without SSH

Pick the card, fill in the tabs and press **Write to SD card**. Put the card in
the camera and power it on. thingino runs the `runonce.sh` on the card once; it
installs everything and restarts the camera if needed. `wyzer/install.log` on the
card lists what it did. Keys and passwords are deleted from the card after the
camera has read them.

Tick **Camera is still on Wyze firmware: also install thingino** to put the
community [thingino installer for the Wyze Cam v3](https://github.com/wltechblog/thingino-installers/tree/main/wyze-cam-3)
on the card first. The tool downloads it (about 25 MB, kept for a week in your
local app data folder) and copies its files to the card: a boot file the stock
bootloader loads, and one firmware per hardware variant. The camera picks the
right one itself. Power on and leave it about six minutes; it backs up the Wyze
firmware to the card, installs thingino, restarts, then applies your settings.

- **Keep the backup.** Copy the `WYZE_BACKUP_...` folder from the card to your computer afterwards. It is the only way back to Wyze firmware and is unique to that camera.
- **Wyze Cam v3 only** (square face). A v4 in a v3 base ignores the card and boots stock.
- **The card must be FAT32.** Any size is accepted by the tool. A large card (for example 128 GB) works for the files, but Windows will not format cards over 32 GB as FAT32 (use a tool such as guiformat, or make a 32 GB partition), and the Wyze bootloader that reads the installer is only known to work with smaller cards. The window warns about this and the install may need a card of 32 GB or less.
- **How the files get on the card:** the upstream installer ships as a disk image. It holds a single FAT32 partition and nothing outside it, so this tool reads the files out of the image and copies them. That needs no admin rights and cannot overwrite the wrong disk. Whether the stock bootloader accepts a card prepared this way has not been confirmed on a camera. If it does not start the install, write the upstream image with an imaging tool, then use this tool on that card without the tick.
- **One card for both steps is also unconfirmed:** it relies on thingino finding `runonce.sh` on the card on its first boot after the install.

### Helpers in the window

- **Check key** asks OpenRouter whether the API key is valid (only for OpenRouter endpoints; a local server never receives it).
- **Model picker** lists OpenRouter's image-capable models with prices, loaded live. **Check model** confirms an id exists.
- **Telegram:** **Check bot** validates the token, **Find my chat** lists the chats that messaged your bot (message the bot first) so you can pick the chat ID, **Send test message** confirms alerts arrive.

### What the window remembers

Camera address, user and non-secret settings (model, rules, scene, limits,
Wi-Fi name, Telegram chat ID) in `%APPDATA%\wyzer-cam\profile.json`, and the
identities of cameras you accepted in `known_hosts.json` beside it. It never
stores an API key, Telegram token, ntfy topic, Wi-Fi password or SSH password.

## Models

**One model** (default) is `google/gemini-2.5-flash-lite`: cheap and fast, and it
does not spend its reply on hidden "thinking". Any other image-capable model id works.

**Rotate between free models** (OpenRouter's `:free` models):

- Each event starts with the model after the one that last answered, and tries up to *Models to try per event* (default 3).
- A model that fails is skipped for a while: 10 minutes after a rate limit or refusal, 5 after other errors, an hour if the model is gone. A rejected key or missing credit stops the attempts at once.
- Calls and failures are counted per model per day (shown in the web page and by `aiwatch usage`), and a combined daily cap for the free pool (default 45) stops calls once reached.
- OpenRouter applies its free daily allowance to your whole account across all free models, so rotating helps with per-model rate limits and outages but does **not** add calls. Set the cap to match your account.
- Free models can be served by providers that keep or train on what they receive, and your camera snapshots would be sent to them. Check OpenRouter's privacy settings and each model's page first. The window and the web page both say so.
- "Reasoning" models are flagged: they may spend the small reply budget thinking and return nothing, which counts as a failure and moves on.

## Settings page in the camera's web interface

Once installed, open the camera's web interface and go to
**Services &rarr; AI Alerts**. Every aiwatch setting can be changed there:

- **Model:** on/off, endpoint, API key, one model or the free pool (picked from the live list).
- **Scene:** camera name, what the camera sees, language, extra instructions for the model.
- **Alert rules:** one per line, plus the quiet-when-empty filter.
- **Send alerts to:** Telegram, ntfy, MQTT, webhook; attach the snapshot; plain alert when the model is unreachable.
- **Limits:** seconds between calls, calls per day, active hours (may cross midnight), snapshot stream, largest snapshot, timeout.

The top of the page shows whether aiwatch is installed and hooked to motion
events, whether motion detection is on (with a button to turn it on), and how
many events were analyzed today. **Test now** analyzes a fresh snapshot and
prints the result; **Test and send** also sends the alert. Below the settings:
**Model usage today** (calls, failures and wait time per model) and the last 20
events with the model that answered each.

If the menu entry does not show after installing, reload the page with the
browser cache bypassed (Ctrl+F5). To look at the page without a camera:

```bash
python test/preview_server.py
```

## Install by hand on a camera already running thingino

The installer window does all of this for you. By hand, replacing `CAM` with the
camera's address:

```bash
scp -O package/thingino-aiwatch/files/aiwatch root@CAM:/usr/sbin/aiwatch
```

```bash
scp -O package/thingino-aiwatch/files/aiwatch.json root@CAM:/etc/aiwatch.json
```

Then on the camera:

```sh
chmod 755 /usr/sbin/aiwatch
chmod 600 /etc/aiwatch.json
jct /etc/aiwatch.json set api_key "YOUR_KEY"
aiwatch test          # takes a snapshot, prints the description, sends nothing
aiwatch test -n       # same, and sends the notifications
aiwatch usage         # per-model calls and failures today
```

To hook it to motion events, add this line to `/usr/sbin/motion` inside
`start()`, after the `floodlight_ctl` line (this is what patch 0001 does):

```sh
[ -x /usr/sbin/aiwatch ] && aiwatch motion >/dev/null 2>&1 &
```

Then turn it on:

```sh
jct /etc/aiwatch.json set enabled true
```

## Build it into the firmware

```bash
git clone --recurse-submodules -b ciao https://github.com/themactep/thingino-firmware
```

Copy `package/thingino-aiwatch` into the clone's `package/` directory, apply
both patches with `git apply`, and add this line to the camera's defconfig,
for example `configs/cameras/wyze_cam3_t31x_gc2053_rtl8189ftv/`:

```
BR2_PACKAGE_THINGINO_AIWATCH=y
```

Then build as usual with `make`. The Wyze Cam v3 has three hardware variants in
thingino; pick the config that matches your unit's SoC and Wi-Fi chip.

## Configuration

`/etc/aiwatch.json`:

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Master switch for motion events. `aiwatch test` works regardless. |
| `endpoint` | OpenRouter | Any OpenAI-compatible chat-completions URL, including one on your LAN. |
| `api_key` | empty | Sent as a bearer token. Leave empty for local servers that need none. |
| `model` | `google/gemini-2.5-flash-lite` | A model that accepts image input. Used when `free_pool` is off. |
| `free_pool` | `false` | Rotate between `free_models` instead of using `model`. |
| `free_models` | `[]` | Up to 10 model ids (OpenRouter `:free` models) to rotate between. An empty list falls back to `model`. |
| `free_daily_limit` | `45` | Combined calls per day across the free pool. |
| `max_attempts` | `3` | Models tried per event when rotating. |
| `camera_name` | hostname | Given to the model and included in MQTT/webhook payloads. |
| `context` | empty | One line about the scene, such as "Front porch facing the street". |
| `language` | `English` | Language of the description. |
| `extra_instructions` | empty | One line of extra guidance for the model, such as "The grey cat lives here; do not report it". |
| `active_from`, `active_to` | empty | Only analyze events between these times (`HH:MM`). Empty means all day. May cross midnight. |
| `rules` | `[]` | Up to 10 plain-language alert rules. |
| `filter_empty` | `true` | With no rules: stay quiet when nothing notable is visible. `false` alerts on every event. |
| `services` | `["telegram"]` | Any of `telegram`, `ntfy`, `mqtt`, `webhook`. Each must be set up in `/etc/send2.json`. |
| `send_photo` | `true` | Attach the analyzed snapshot. |
| `fallback_on_error` | `true` | Send a plain alert when the AI call fails. |
| `cooldown` | `60` | Minimum seconds between events analyzed. |
| `daily_limit` | `200` | Maximum events analyzed per day. |
| `snapshot_channel` | `1` | Stream to snapshot. 1 is the low-resolution stream; falls back to 0. |
| `max_image_kb` | `600` | Larger snapshots are not sent. |
| `timeout` | `30` | Seconds to wait for the endpoint, per model tried. |

Example rules:

```sh
jct /etc/aiwatch.json set rules.0 "A person is at the door"
jct /etc/aiwatch.json set rules.1 "A vehicle is parked in the driveway"
```

MQTT and webhook receive a JSON payload:

```json
{"event":"motion","camera":"porch","description":"...","rule":"...","time":"2026-11-15T20:30:00-0600","model":"google/gemini-2.5-flash-lite"}
```

## Things to know

- **Older thingino builds:** aiwatch was written against the current stable branch. On builds from mid-2026 the settings page, Telegram, MQTT and webhook alerts are expected to work, but ntfy needs the server set as a full `https://` address under Send to services. Updating thingino first avoids this.
- **Model text is made inert:** quotes in descriptions become typographic quotes and `` ` ``, `$` and `\` are removed before the text reaches a notifier, because older notifier scripts pass it through a shell.
- **Duplicate alerts:** if thingino's own motion notifications are also on for the same service, you get two messages per event. Turn the plain one off.
- **Key storage:** the camera's flash is not encrypted. Use a dedicated API key with a spending limit.
- **Counters reset on reboot:** the daily counts, per-model usage and event log live in `/tmp/aiwatch`, which is RAM.
- **Wi-Fi changes are risky on a working camera:** a wrong password cuts it off until it is reset, so the window asks you to confirm.
- **Not tested on hardware yet:** the logic is covered by the test suites against a mock endpoint, a fake camera filesystem, and an in-process SSH server, not a real camera.

## Tests

```bash
sh test/run-tests.sh                      # aiwatch against a mock endpoint (needs sh, curl, python)
```

```bash
python -m unittest discover -s test       # installer, web page backend, SSH, window
```

The SSH and window tests need `pip install paramiko`, `sh` (Git Bash on
Windows) and, for the window tests, a display; they skip themselves otherwise.

## License

MIT
