"""Stand-in for thingino's jct, covering only `jct FILE get KEY`.

Mirrors jct v1.2.1: dot-separated keys, numeric segments index arrays,
strings print raw, a missing key exits 1.
"""
import json
import sys

path, command, key = sys.argv[1], sys.argv[2], sys.argv[3]


def merge(dst, src):
    for name, value in src.items():
        if isinstance(value, dict) and isinstance(dst.get(name), dict):
            merge(dst[name], value)
        else:
            dst[name] = value


if command == "import":  # `jct FILE import SOURCE`: deep-merge SOURCE into FILE
    with open(path, encoding="utf-8") as f:
        base = json.load(f)
    with open(key, encoding="utf-8") as f:
        merge(base, json.load(f))
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(base, f, indent=2)
    sys.exit(0)
if command != "get":
    sys.exit(2)
try:
    with open(path, encoding="utf-8") as f:
        value = json.load(f)
    for part in [p for p in key.split(".") if p]:
        value = value[int(part)] if isinstance(value, list) else value[part]
except (OSError, ValueError, KeyError, IndexError, TypeError):
    sys.stderr.write("Error: Key '%s' not found in config file.\n" % key)
    sys.exit(1)

if isinstance(value, bool):
    out = "true" if value else "false"
elif isinstance(value, str):
    out = value
elif value is None:
    out = "null"
elif isinstance(value, (dict, list)):
    out = json.dumps(value)
else:
    out = str(value)
sys.stdout.buffer.write((out + "\n").encode("utf-8"))
