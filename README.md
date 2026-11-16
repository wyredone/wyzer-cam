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
| `test/` | Test suite with a mock endpoint; runs on any machine with `sh`, `curl`, `python` |

The patches were made against thingino `ciao` at commit `c193194585b5`.
Only the prudynt streamer (the stable branch) is supported; raptor is not.

## Try it on a camera already running thingino

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

## License

MIT
