#!/bin/sh
# Backend for the AI Alerts page.
#
#   GET                      the settings file
#   POST (JSON body)         replace the settings file
#   GET ?action=status       install state, today's call count, recent events
#   GET ?action=test         analyze a snapshot now, send nothing
#   GET ?action=test_notify  analyze a snapshot now and send the alert
#   GET ?action=motion_on    turn the camera's motion detection on

WWW_X="${AIWATCH_WWW_X:-/var/www/x}"
. "$WWW_X/auth.sh"
require_auth

CONFIG="${AIWATCH_CONFIG:-/etc/aiwatch.json}"
STATE_DIR="${AIWATCH_STATE_DIR:-/tmp/aiwatch}"
MOTION="${AIWATCH_MOTION:-/usr/sbin/motion}"
PRUDYNT_JSON="${AIWATCH_PRUDYNT_JSON:-/etc/prudynt.json}"

header() {
	# $1 = status line, such as "200 OK"
	printf 'Status: %s\r\n' "$1"
	printf 'Content-Type: application/json\r\n'
	printf 'Cache-Control: no-store\r\n'
	printf 'Connection: close\r\n'
	printf '\r\n'
}

# Any text as the inside of a JSON string, line breaks kept as \n.
jesc() {
	printf '%s' "$1" | tr '\t\r' '  ' | tr -d '\000-\011\013-\037' |
		sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' |
		awk 'NR > 1 { printf "\\n" } { printf "%s", $0 }'
}

fail() {
	header "$1"
	printf '{"ok":false,"error":"%s"}\n' "$(jesc "$2")"
	exit 0
}

motion_enabled() {
	if command -v prudynt-config >/dev/null 2>&1; then
		prudynt-config get motion.enabled 2>/dev/null
	else
		jct "$PRUDYNT_JSON" get motion.enabled 2>/dev/null
	fi
}

action=""
case "$QUERY_STRING" in
	*action=test_notify*) action=test_notify ;;
	*action=test*) action=test ;;
	*action=status*) action=status ;;
	*action=motion_on*) action=motion_on ;;
esac

case "$action" in
	status)
		installed=false
		command -v aiwatch >/dev/null 2>&1 && installed=true
		hooked=false
		grep -q 'aiwatch motion' "$MOTION" 2>/dev/null && hooked=true
		motion=$(motion_enabled)
		[ "$motion" = "true" ] || motion=false
		count=0
		if [ -f "$STATE_DIR/count" ]; then
			read -r count_day count_n <"$STATE_DIR/count"
			case "$count_n" in '' | *[!0-9]*) count_n=0 ;; esac
			[ "$count_day" = "$(date +%Y%m%d)" ] && count=$count_n
		fi
		events=""
		if [ -f "$STATE_DIR/events.jsonl" ]; then
			events=$(tail -n 20 "$STATE_DIR/events.jsonl" | grep '^{.*}$' | awk 'NR > 1 { printf "," } { printf "%s", $0 }')
		fi
		# One entry per model used today. Same file format as `aiwatch usage json`:
		# DAY MODEL CALLS FAILURES HOLD_UNTIL.
		usage=""
		if [ -f "$STATE_DIR/usage" ]; then
			usage=$(awk -v day="$(date +%Y%m%d)" -v now="$(date +%s)" '
				$1 == day && NF == 5 && $2 ~ /^[A-Za-z0-9._:\/-]+$/ {
					printf "%s{\"model\":\"%s\",\"calls\":%d,\"failures\":%d,\"wait\":%d}", (n++ ? "," : ""), $2, $3, $4, ($5 > now ? $5 - now : 0)
				}' "$STATE_DIR/usage")
		fi
		header "200 OK"
		printf '{"ok":true,"installed":%s,"hooked":%s,"motion_enabled":%s,"calls_today":%s,"usage":[%s],"events":[%s]}\n' \
			"$installed" "$hooked" "$motion" "$count" "$usage" "$events"
		;;

	test | test_notify)
		flag=""
		[ "$action" = "test_notify" ] && flag="-n"
		ok=true
		output=$(aiwatch test $flag 2>&1) || ok=false
		header "200 OK"
		printf '{"ok":%s,"output":"%s"}\n' "$ok" "$(jesc "$output")"
		;;

	motion_on)
		if command -v prudynt-config >/dev/null 2>&1; then
			prudynt-config set motion.enabled true >/dev/null 2>&1 || fail "500 Internal Server Error" "could not save the setting"
		else
			jct "$PRUDYNT_JSON" set motion.enabled true >/dev/null 2>&1 || fail "500 Internal Server Error" "could not save the setting"
		fi
		# Apply it to the running streamer too; a restart is not needed.
		echo '{"motion":{"enabled":true}}' | prudyntctl json - >/dev/null 2>&1
		header "200 OK"
		printf '{"ok":true}\n'
		;;

	*)
		if [ "$REQUEST_METHOD" = "POST" ]; then
			length=${CONTENT_LENGTH:-0}
			case "$length" in '' | *[!0-9]*) length=0 ;; esac
			if [ "$length" -lt 2 ] || [ "$length" -gt 16384 ]; then
				fail "400 Bad Request" "settings are missing or too large"
			fi
			new="$CONFIG.new"
			(umask 077 && head -c "$length" >"$new")
			# Must parse as JSON and look like aiwatch settings before it replaces them.
			if ! jct "$new" get endpoint >/dev/null 2>&1 || ! jct "$new" get enabled >/dev/null 2>&1; then
				rm -f "$new"
				fail "400 Bad Request" "settings are not valid"
			fi
			mv "$new" "$CONFIG" && chmod 600 "$CONFIG" || fail "500 Internal Server Error" "could not save the settings"
		fi
		[ -f "$CONFIG" ] || fail "404 Not Found" "$CONFIG does not exist"
		header "200 OK"
		cat "$CONFIG"
		;;
esac
exit 0
