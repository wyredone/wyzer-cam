#!/bin/sh
# wyzer-cam-runonce v1
#
# SD card setup for a camera running thingino. thingino's automount runs
# "sh <card>/runonce.sh" once when the card is mounted, then creates
# runonce.done next to it. Everything this script needs is in <card>/wyzer/,
# written there by installer/wyzer_installer.py.
#
# What it does: installs aiwatch, hooks it into the motion script, applies the
# notifier and Wi-Fi settings, removes the secrets from the card, and reboots
# if a setting needs it. Progress is logged to <card>/wyzer/install.log.

CARD=$(dirname "$0")
SRC="$CARD/wyzer"
LOG="$SRC/install.log"
ROOT="${WYZER_ROOT:-}"
MOTION="$ROOT/usr/sbin/motion"
need_reboot=0
# Set WYZER_NO_REBOOT=1 (the SSH installer does) to be told instead of restarted.
NO_REBOOT="${WYZER_NO_REBOOT:-}"

say() {
	echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >>"$LOG" 2>/dev/null
}

say "--- setup started"

if [ ! -f "$SRC/aiwatch" ]; then
	say "ERROR: wyzer/aiwatch is missing from the card; nothing done"
	exit 1
fi
if [ ! -f "$MOTION" ] || ! command -v jct >/dev/null 2>&1; then
	say "ERROR: this does not look like thingino with the prudynt streamer; nothing done"
	exit 1
fi

# --- aiwatch itself ----------------------------------------------------------
if cp "$SRC/aiwatch" "$ROOT/usr/sbin/aiwatch" && chmod 755 "$ROOT/usr/sbin/aiwatch"; then
	say "installed /usr/sbin/aiwatch"
else
	say "ERROR: could not install /usr/sbin/aiwatch"
fi

if [ -f "$SRC/aiwatch.json" ]; then
	# Updating: a blank API key in the new settings means "keep the one I have".
	old_key=""
	[ -f "$ROOT/etc/aiwatch.json" ] && old_key=$(jct "$ROOT/etc/aiwatch.json" get api_key 2>/dev/null)
	new_key=$(jct "$SRC/aiwatch.json" get api_key 2>/dev/null)
	if cp "$SRC/aiwatch.json" "$ROOT/etc/aiwatch.json" && chmod 600 "$ROOT/etc/aiwatch.json"; then
		say "installed /etc/aiwatch.json"
		if [ -z "$new_key" ] && [ -n "$old_key" ]; then
			if jct "$ROOT/etc/aiwatch.json" set api_key "$old_key" >/dev/null 2>&1; then
				say "kept the API key already on the camera"
			else
				say "ERROR: could not keep the API key already on the camera"
			fi
		fi
	else
		say "ERROR: could not install /etc/aiwatch.json"
	fi
fi

# --- hook into the motion script ---------------------------------------------
add_hook_after() {
	# $1 = text of the line to add the hook after (first match only).
	# Written back with cat so the script keeps its permissions.
	awk -v pat="$1" '
		{ print }
		!added && index($0, pat) {
			print ""
			print "\t# Describe the event with a vision model and alert by rule (if installed)"
			print "\t[ -x /usr/sbin/aiwatch ] && aiwatch motion >/dev/null 2>&1 &"
			added = 1
		}' "$MOTION" >"$MOTION.new" && cat "$MOTION.new" >"$MOTION"
	rm -f "$MOTION.new"
}

if grep -q 'aiwatch motion' "$MOTION"; then
	say "motion script already calls aiwatch"
elif grep -q 'floodlight_ctl motion' "$MOTION" && add_hook_after 'floodlight_ctl motion' &&
	grep -q 'aiwatch motion' "$MOTION"; then
	say "hooked aiwatch into the motion script"
elif grep -q 'touch "$MOTION_ALARM"' "$MOTION" && add_hook_after 'touch "$MOTION_ALARM"' &&
	grep -q 'aiwatch motion' "$MOTION"; then
	say "hooked aiwatch into the motion script (fallback position)"
else
	say "ERROR: could not find where to hook aiwatch in /usr/sbin/motion"
fi

# --- settings page in the camera's web interface -----------------------------
WWW="$ROOT/var/www"
if [ -d "$SRC/www" ] && [ -d "$WWW/a" ] && [ -d "$WWW/x" ]; then
	mkdir -p "$WWW/a/plugins"
	if cp "$SRC/www/config-aiwatch.html" "$WWW/config-aiwatch.html" &&
		cp "$SRC/www/a/config-aiwatch.js" "$WWW/a/config-aiwatch.js" &&
		cp "$SRC/www/x/json-aiwatch.cgi" "$WWW/x/json-aiwatch.cgi" &&
		chmod 755 "$WWW/x/json-aiwatch.cgi" &&
		cp "$SRC/www/a/plugins/aiwatch.webui.json" "$WWW/a/plugins/aiwatch.webui.json"; then
		# The menu is built from plugins.js. Add this page to it without
		# touching the entries the firmware put there. An earlier copy of our
		# block is removed first, so an update refreshes it instead of doubling it.
		if [ -f "$WWW/a/plugins.js" ]; then
			awk '
				/^\/\/ aiwatch settings page \(added by wyzer-cam\)$/ { skip = 1; next }
				skip { if (index($0, ";})();")) skip = 0; next }
				{ print }' "$WWW/a/plugins.js" >"$WWW/a/plugins.js.new" && cat "$WWW/a/plugins.js.new" >"$WWW/a/plugins.js"
			rm -f "$WWW/a/plugins.js.new"
		fi
		{
			printf '// aiwatch settings page (added by wyzer-cam)\n'
			printf '(function(){var c=window.thinginoUIConfig=window.thinginoUIConfig||{};'
			printf 'c.plugins=c.plugins||{};c.plugins.aiwatch='
			cat "$SRC/www/a/plugins/aiwatch.webui.json"
			printf ';})();\n'
		} >>"$WWW/a/plugins.js"
		say "installed the AI Alerts page in the web interface (Services menu)"
	else
		say "ERROR: could not install the web interface page"
	fi
elif [ -d "$SRC/www" ]; then
	say "no web interface found on this camera; skipped the settings page"
fi

# --- notifier settings (Telegram, ntfy) --------------------------------------
if [ -f "$SRC/send2.json" ]; then
	if jct "$ROOT/etc/send2.json" import "$SRC/send2.json" >/dev/null 2>&1; then
		say "applied notifier settings"
	else
		say "ERROR: could not apply notifier settings to /etc/send2.json"
	fi
fi

# --- motion detection --------------------------------------------------------
if [ -f "$SRC/enable_motion" ]; then
	# Newer thingino has prudynt-config; older builds keep the setting in
	# /etc/prudynt.json directly.
	if command -v prudynt-config >/dev/null 2>&1; then
		prudynt-config set motion.enabled true >/dev/null 2>&1
	else
		jct "$ROOT/etc/prudynt.json" set motion.enabled true >/dev/null 2>&1
	fi
	if [ $? -eq 0 ]; then
		say "turned motion detection on"
		# Apply it to the running streamer too; if that is not possible the
		# setting takes effect after a restart.
		echo '{"motion":{"enabled":true}}' | prudyntctl json - >/dev/null 2>&1 || need_reboot=1
	else
		say "ERROR: could not turn motion detection on"
	fi
fi

# --- Wi-Fi -------------------------------------------------------------------
if [ -s "$SRC/wifi_ssid" ]; then
	ssid=$(cat "$SRC/wifi_ssid")
	pass=$(cat "$SRC/wifi_pass" 2>/dev/null)
	if wlan configure "$ssid" "$pass" >/dev/null 2>&1; then
		say "saved Wi-Fi settings for '$ssid'"
		need_reboot=1
	else
		say "ERROR: could not save Wi-Fi settings"
	fi
fi

# --- remove secrets from the card --------------------------------------------
rm -f "$SRC/aiwatch.json" "$SRC/send2.json" "$SRC/wifi_ssid" "$SRC/wifi_pass" "$SRC/enable_motion"
say "removed keys and passwords from the card"
say "setup finished"

if [ "$need_reboot" = 1 ]; then
	if [ -n "$NO_REBOOT" ]; then
		say "restart needed to apply the Wi-Fi settings"
	elif touch "$CARD/runonce.done" 2>/dev/null; then
		# Only if the done-flag can be written: on a write-protected card the
		# script would otherwise run again on every boot and reboot forever.
		say "rebooting to apply settings"
		sync
		reboot
	fi
fi
exit 0
