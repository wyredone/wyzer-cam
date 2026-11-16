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
- **Cost guards:** a cooldown between calls and a daily call cap.
- **Fail soft:** if the endpoint is down or replies with nonsense, a plain motion alert is sent instead.

It is off by default, because it sends frames from your camera to the endpoint
you configure.

## Layout

| Path | What it is |
|---|---|
| `package/thingino-aiwatch/` | Buildroot package: the `aiwatch` script, default config, `Config.in`, `.mk` |
| `patches/0001-motion-call-aiwatch.patch` | Makes thingino's motion script call `aiwatch motion` |
| `patches/0002-config-add-aiwatch.patch` | Adds the package to thingino's menu |
| `installer/wyzer_installer.py` | Window that prepares an SD card with aiwatch and your settings |
| `installer/runonce.sh` | Script the camera runs from the card to install everything |
| `installer/fatimage.py` | Reads files out of a FAT disk image, used for the thingino installer |
| `test/` | Test suites with a mock endpoint and a fake camera; need `sh`, `curl`, `python` |

The patches were made against thingino `ciao` at commit `c193194585b5`.
Only the prudynt streamer (the stable branch) is supported; raptor is not.

## Install with the SD card installer

A small window that puts aiwatch and your settings on an SD card. It needs
Python 3.8 or newer and nothing else.

```bash
python installer/wyzer_installer.py
```

1. Pick the SD card, fill in Wi-Fi, the API key and model, your alert rules, and Telegram or ntfy details.
2. Press **Write to SD card**, eject the card, put it in the camera and power it on.
3. The camera reads the card, installs everything, and restarts once. `wyzer/install.log` on the card lists what it did.

How it works: thingino runs a `runonce.sh` found on the SD card once. The
installer writes that script plus a `wyzer/` folder holding the aiwatch script
and your settings. Keys and passwords are deleted from the card after the
camera has read them.

- **Nothing is formatted.** Files are copied onto the card as it is. Use a FAT32 card of 32 GB or less.
- **Not tested on hardware yet.** The card contents and the on-camera script are covered by tests against a fake camera filesystem.

### Cameras still on Wyze firmware

Tick **Camera is still on Wyze firmware: also install thingino**. The tool then
downloads the community
[thingino installer for the Wyze Cam v3](https://github.com/wltechblog/thingino-installers/tree/main/wyze-cam-3)
(about 25 MB, kept for a week in your local app data folder) and copies its
files to the card: a boot file the stock bootloader loads from the card, and
one firmware per hardware variant. The camera picks the right one itself.

With that card in the camera, power on and leave it for about six minutes.
It backs up the Wyze firmware to the card, installs thingino, restarts, then
runs `runonce.sh` to apply your settings.

- **Keep the backup.** Copy the `WYZE_BACKUP_...` folder from the card to your computer afterwards. It is the only way back to Wyze firmware and is unique to that camera.
- **Wyze Cam v3 only** (square face). A v4 in a v3 base ignores the card and boots stock.
- **The card must be FAT32.** The tool refuses other file systems for this step.
- **How the files get on the card:** the upstream installer ships as a disk image meant to be written with an imaging tool. The image holds a single FAT32 partition and nothing outside it, so this tool reads the files out of the image and copies them, which needs no admin rights and cannot overwrite the wrong disk. Whether the stock bootloader accepts a card prepared this way has not been confirmed on a camera. If it does not start the install, write the upstream image with an imaging tool, then run this tool on that card without the tick.
- **One card for both steps is also unconfirmed:** it relies on thingino finding `runonce.sh` on the card on its first boot after the install.

## Install by hand on a camera already running thingino

No rebuild needed. Replace `CAM` with the camera's address.

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
jct /etc/aiwatch.json set model "A_VISION_MODEL_ID"
aiwatch test          # takes a snapshot, prints the description, sends nothing
aiwatch test -n       # same, and sends the notifications
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
| `model` | empty | Required. A model that accepts image input. |
| `camera_name` | hostname | Given to the model and included in MQTT/webhook payloads. |
| `context` | empty | One line about the scene, such as "Front porch facing the street". |
| `language` | `English` | Language of the description. |
| `rules` | `[]` | Up to 10 plain-language alert rules. |
| `filter_empty` | `true` | With no rules: stay quiet when nothing notable is visible. `false` alerts on every event. |
| `services` | `["telegram"]` | Any of `telegram`, `ntfy`, `mqtt`, `webhook`. Each must be set up in `/etc/send2.json`. |
| `send_photo` | `true` | Attach the analyzed snapshot. |
| `fallback_on_error` | `true` | Send a plain alert when the AI call fails. |
| `cooldown` | `60` | Minimum seconds between API calls. |
| `daily_limit` | `200` | Maximum API calls per day. |
| `snapshot_channel` | `1` | Stream to snapshot. 1 is the low-resolution stream; falls back to 0. |
| `max_image_kb` | `600` | Larger snapshots are not sent. |
| `timeout` | `30` | Seconds to wait for the endpoint. |

Example rules:

```sh
jct /etc/aiwatch.json set rules.0 "A person is at the door"
jct /etc/aiwatch.json set rules.1 "A vehicle is parked in the driveway"
```

MQTT and webhook receive a JSON payload:

```json
{"event":"motion","camera":"porch","description":"...","rule":"...","time":"2026-11-15T20:30:00-0600"}
```

## Things to know

- **Duplicate alerts:** if thingino's own motion notifications are also on for the same service, you get two messages per event. Turn the plain one off.
- **Key storage:** the camera's flash is not encrypted. Use a dedicated API key with a spending limit.
- **Counters reset on reboot:** the daily count and event log live in `/tmp/aiwatch`, which is RAM.
- **Event log:** the last 200 results are in `/tmp/aiwatch/events.jsonl`; `aiwatch status` shows the latest.
- **Not tested on hardware yet:** the logic is covered by the test suite against a mock endpoint, with stand-ins for the camera's tools.

## Tests

```bash
sh test/run-tests.sh
```

```bash
python -m unittest discover -s test
```

## License

MIT
