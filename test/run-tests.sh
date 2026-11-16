#!/bin/sh
# Runs aiwatch against a mock endpoint with stand-ins for the camera tools.
# Usage: test/run-tests.sh [shell]      (shell defaults to sh; try dash too)
# Needs: python, curl, base64.

SH="${1:-sh}"
HERE=$(cd "$(dirname "$0")" && pwd)
AIWATCH="$HERE/../package/thingino-aiwatch/files/aiwatch"
PORT="${AIWATCH_TEST_PORT:-18765}"
T=$(mktemp -d "${TMPDIR:-/tmp}/aiwatch-test.XXXXXX")
BIN="$T/bin"
SRV="$T/srv"
mkdir -p "$BIN" "$SRV"
PASS=0
FAIL=0

# --- stand-ins for the tools the camera provides -----------------------------
cat >"$BIN/jct" <<EOF
#!/bin/sh
exec python -I "$HERE/mock_jct.py" "\$@"
EOF
cat >"$BIN/prudyntctl" <<EOF
#!/bin/sh
cat "$T/frame.jpg"
EOF
cat >"$BIN/logger" <<EOF
#!/bin/sh
shift 2
echo "\$*" >>"$T/log"
EOF
for s in telegram ntfy mqtt webhook; do
	cat >"$BIN/send2$s" <<EOF
#!/bin/sh
{
	printf 'send2$s'
	for a in "\$@"; do printf ' [%s]' "\$a"; done
	[ -s "\$MOTION_PHOTO_FILE" ] && printf ' photo=yes'
	printf '\n'
} >>"$T/sent"
EOF
done
chmod +x "$BIN"/*
printf 'not-really-a-jpeg-but-bytes\377\330\377' >"$T/frame.jpg"

python -I "$HERE/mock_server.py" "$PORT" "$SRV" &
SERVER=$!
trap 'kill $SERVER 2>/dev/null; rm -rf "$T"' EXIT
sleep 1

# --- helpers -----------------------------------------------------------------
config() {
	# $1 = extra JSON members (start with a comma) overriding nothing; keep simple
	cat >"$T/aiwatch.json" <<EOF
{
  "enabled": ${ENABLED:-true},
  "endpoint": "http://127.0.0.1:$PORT/v1/chat/completions",
  "api_key": "sk-test-123",
  "model": "${MODEL-test/vision}",
  "camera_name": "porch",
  "context": "Front porch and \"driveway\"",
  "rules": ${RULES:-[]},
  "services": ${SERVICES:-[\"telegram\"]},
  "send_photo": true,
  "fallback_on_error": ${FALLBACK:-true},
  "free_pool": ${POOL_ON:-false},
  "free_models": ${POOL:-[]},
  "free_daily_limit": ${FREE_LIMIT:-45},
  "max_attempts": ${ATTEMPTS:-3},
  "cooldown": ${COOLDOWN:-0},
  "daily_limit": ${LIMIT:-200}
}
EOF
}

reset() {
	rm -rf "$T/state" "$T/sent" "$T/log" "$SRV/hits" "$SRV/hits.log" "$SRV/request.json" "$SRV/auth"
	echo "${1:-notify}" >"$SRV/mode"
}

run() {
	PATH="$BIN:$PATH" AIWATCH_CONFIG="$T/aiwatch.json" AIWATCH_STATE_DIR="$T/state" \
		"$SH" "$AIWATCH" "$@"
}

hits() { [ -f "$SRV/hits" ] && wc -c <"$SRV/hits" | tr -d ' ' || echo 0; }
sent() { cat "$T/sent" 2>/dev/null; }

check() {
	# $1 name, $2 actual, $3 expected substring ("" means actual must be empty)
	if [ -z "$3" ] && [ -z "$2" ]; then
		PASS=$((PASS + 1))
	elif [ -n "$3" ] && printf '%s' "$2" | grep -qF -- "$3"; then
		PASS=$((PASS + 1))
	else
		FAIL=$((FAIL + 1))
		echo "FAIL: $1"
		echo "  expected: ${3:-(empty)}"
		echo "  actual:   $2"
	fi
}

check_eq() {
	# $1 name, $2 actual, $3 expected (exactly)
	if [ "$2" = "$3" ]; then
		PASS=$((PASS + 1))
	else
		FAIL=$((FAIL + 1))
		echo "FAIL: $1"
		echo "  expected: $3"
		echo "  actual:   $2"
	fi
}

# --- tests -------------------------------------------------------------------
ENABLED=false config; reset
run motion
check "disabled: no API call" "$(hits)" "0"
check "disabled: nothing sent" "$(sent)" ""

RULES='["A person is at the door","A car is in the driveway"]' config; reset notify
run motion
check "notify: one API call" "$(hits)" "1"
check "notify: telegram gets description" "$(sent)" "send2telegram [-S] [-m] [A courier leaves a parcel at the door.] photo=yes"
check "notify: bearer key sent" "$(cat "$SRV/auth")" "Bearer sk-test-123"
check "notify: event logged" "$(cat "$T/state/events.jsonl")" '"status":"ok","notify":true,"description":"A courier leaves a parcel at the door.","rule":"A person is at the door","model":"test/vision"'
check "usage: single model counted" "$(run usage json)" '[{"model":"test/vision","calls":1,"failures":0,"wait":0}]'
REQ=$(python -I -c "
import json,base64,sys
d=json.load(open(sys.argv[1],encoding='utf-8'))
u=d['messages'][1]['content']
img=base64.b64decode(u[1]['image_url']['url'].split(',',1)[1])
ok=img==open(sys.argv[2],'rb').read()
print('model='+d['model'], 'image_ok='+str(ok))
print(u[0]['text'])
" "$SRV/request.json" "$T/frame.jpg" 2>&1)
check "request: valid JSON with model" "$REQ" "model=test/vision image_ok=True"
check "request: rules listed" "$REQ" "2. A car is in the driveway"
check "request: quoted context survives" "$REQ" 'Scene: Front porch and "driveway"'

config; reset quiet
run motion
check "quiet: API called" "$(hits)" "1"
check "quiet: nothing sent" "$(sent)" ""
check "quiet: event logged" "$(cat "$T/state/events.jsonl")" '"notify":false'

config; reset fenced
run motion
check "fenced reply parsed" "$(sent)" "[A dog runs across the lawn.]"

SERVICES='["ntfy","mqtt","webhook","bogus"]' config; reset tricky
run motion
OUT=$(sent)
check "tricky: ntfy percent escaped, shell characters neutralised" "$OUT" 'send2ntfy [-S] [-m] [Sign says ”50%% off”  sale it’s id (id)]'
check "tricky: webhook payload" "$OUT" 'send2webhook [-S] [-m] [{"event":"motion","camera":"porch","description":"Sign says ”50% off”  sale it’s id (id)"'
check "tricky: mqtt payload" "$OUT" 'send2mqtt [-m] [{"event":"motion","camera":"porch","description":"Sign says ”50%% off”  sale it’s id (id)"'
case "$OUT" in
	*"'"* | *'`'* | *'$('*)
		FAIL=$((FAIL + 1))
		echo "FAIL: tricky: shell metacharacters reached a notifier"
		;;
	*) PASS=$((PASS + 1)) ;;
esac
check "tricky: unsupported service logged" "$(cat "$T/log")" "service 'bogus' is not supported"
python -I -c "
import json,sys
for line in open(sys.argv[1],encoding='utf-8'): json.loads(line)
" "$T/state/events.jsonl" 2>/dev/null
check "tricky: event log is valid JSON" "$?" "0"

config; reset error
run motion
check "error: fallback alert sent" "$(sent)" "[Motion detected (AI description unavailable)]"
check "error: reason logged" "$(cat "$T/log")" "HTTP 500 upstream exploded"

FALLBACK=false config; reset error
run motion
check "error, fallback off: nothing sent" "$(sent)" ""

config; reset prose
run motion
check "prose reply: treated as failure" "$(sent)" "AI description unavailable"

COOLDOWN=600 config; reset notify
run motion
run motion
check "cooldown: second event skipped" "$(hits)" "1"

config; reset notify
python -I -c "
import json, sys
c = json.load(open(sys.argv[1], encoding='utf-8'))
c.update(active_from='22:00', active_to='06:00', extra_instructions='Ignore the cat next door')
json.dump(c, open(sys.argv[1], 'w', encoding='utf-8'))
" "$T/aiwatch.json"
AIWATCH_NOW=12:30 run motion
check "active hours: midday skipped" "$(hits)" "0"
check "active hours: logged" "$(cat "$T/log")" "outside active hours (22:00-06:00)"
AIWATCH_NOW=23:15 run motion
check "active hours: late evening runs" "$(hits)" "1"
check "extra instructions sent" "$(python -I -c "import json,sys; print(json.load(open(sys.argv[1],encoding='utf-8'))['messages'][1]['content'][0]['text'])" "$SRV/request.json")" "Owner's instructions: Ignore the cat next door"
AIWATCH_NOW=05:59 run motion
check "active hours: early morning runs" "$(hits)" "2"

LIMIT=2 config; reset notify
run motion; run motion; run motion
check "daily limit: third event skipped" "$(hits)" "2"
check "daily limit: logged" "$(cat "$T/log")" "daily limit of 2 calls reached"

config; reset notify
mkdir -p "$T/state/lock"
run motion
check "lock held: skipped" "$(hits)" "0"
rmdir "$T/state/lock"

MODEL="" config; reset notify
OUT=$(run motion 2>&1)
check "no model: clear error" "$OUT" "no model set"
check "no model: no API call" "$(hits)" "0"

ENABLED=false config; reset notify
OUT=$(run test "$T/frame.jpg" 2>&1)
check "test command: prints description" "$OUT" "description: A courier leaves a parcel at the door."
check "test command: sends nothing" "$(sent)" ""
run test -n >/dev/null 2>&1
check "test -n: sends" "$(sent)" "send2telegram"

OUT=$(run status 2>&1)
check "status: shows model" "$OUT" "model: test/vision"

models_tried() { tr '\n' ',' <"$SRV/hits.log" | sed 's/,$//'; }

# --- rotating free models --------------------------------------------------
POOL_ON=true POOL='["free/a:free","free/b:free","free/c:free"]' config
reset '{"*":"notify","free/a:free":"429"}'
run motion
check_eq "pool: a rate limited, b answers" "$(models_tried)" "free/a:free,free/b:free"
check "pool: event names the model" "$(cat "$T/state/events.jsonl")" '"model":"free/b:free"'
check "pool: failure counted and held" "$(run usage json)" '{"model":"free/a:free","calls":1,"failures":1,"wait":'
run motion
check_eq "pool: next event starts after b and skips held a" "$(models_tried)" "free/a:free,free/b:free,free/c:free"
run motion
check_eq "pool: then wraps round, a still held" "$(models_tried)" "free/a:free,free/b:free,free/c:free,free/b:free"
OUT=$(run usage)
check "usage table: mode line" "$OUT" "mode: rotating free models (4 of 45 calls used today)"
check "usage table: held model" "$OUT" "after an error"
python -I -c "import json,sys; json.loads(sys.argv[1])" "$(run usage json)"
check "usage json parses" "$?" "0"

ATTEMPTS=2 POOL_ON=true POOL='["free/a:free","free/b:free","free/c:free"]' config
reset '{"*":"error"}'
run motion
check_eq "pool: gives up after max attempts" "$(models_tried)" "free/a:free,free/b:free"
check "pool: all failed, plain alert sent" "$(sent)" "AI description unavailable"

POOL_ON=true POOL='["free/a:free","free/b:free"]' config
reset '{"*":"401"}'
run motion
check_eq "pool: rejected key stops at once" "$(models_tried)" "free/a:free"

POOL_ON=true POOL='["free/a:free","free/b:free"]' reset notify
POOL_ON=true POOL='["free/a:free","free/b:free"]' FREE_LIMIT=2 config
reset notify
run motion; run motion; run motion
check_eq "pool: free daily cap stops calls" "$(models_tried)" "free/a:free,free/b:free"
check "pool: free cap logged" "$(cat "$T/log")" "free model limit of 2 calls a day reached"

POOL_ON=true POOL='[]' config; reset notify
run motion
check_eq "pool: empty pool falls back to the model" "$(models_tried)" "test/vision"

POOL_ON=true POOL='["bad id;rm","free/x:free","free/x:free"]' config; reset notify
run motion
check_eq "pool: invalid and duplicate ids ignored" "$(models_tried)" "free/x:free"

POOL_ON=false POOL='["free/a:free"]' config; reset notify
run motion
check_eq "pool off: single model used" "$(models_tried)" "test/vision"

POOL_ON=true POOL='["free/a:free","free/b:free"]' config; reset '{"*":"notify","free/a:free":"empty"}'
run motion
check_eq "pool: empty answer moves on" "$(models_tried)" "free/a:free,free/b:free"

check "no work dirs left behind" "$(ls -d "${TMPDIR:-/tmp}"/aiwatch.?????? 2>/dev/null)" ""

echo "$SH: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
