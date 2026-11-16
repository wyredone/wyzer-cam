#!/bin/sh
# wyzer-cam-uninstall v1
#
# Removes what runonce.sh installed: the aiwatch program, its web page and menu
# entry, and the line that calls it from the motion script. The camera's own
# settings and the notifier services are left as they are.
#
#   sh uninstall.sh              also deletes /etc/aiwatch.json (it holds the API key)
#   sh uninstall.sh --keep-config   leaves /etc/aiwatch.json in place
#
# Every step is reported on its own line, beginning "ok:" or "ERROR:".

ROOT="${WYZER_ROOT:-}"
MOTION="$ROOT/usr/sbin/motion"
WWW="$ROOT/var/www"
KEEP_CONFIG=""
[ "$1" = "--keep-config" ] && KEEP_CONFIG=1

# The motion script: drop the blank line, the comment and the call we added.
if [ -f "$MOTION" ] && grep -q 'aiwatch motion' "$MOTION"; then
	awk '
		/^$/ { if (blank) print ""; blank = 1; next }
		/# Describe the event with a vision model and alert by rule/ { blank = 0; next }
		/aiwatch motion/ { next }
		{ if (blank) { print ""; blank = 0 } print }
		END { if (blank) print "" }
	' "$MOTION" >"$MOTION.new" && cat "$MOTION.new" >"$MOTION"
	rm -f "$MOTION.new"
	if grep -q 'aiwatch motion' "$MOTION"; then
		echo "ERROR: could not remove the aiwatch call from /usr/sbin/motion"
	else
		echo "ok: removed the aiwatch call from the motion script"
	fi
else
	echo "ok: the motion script does not call aiwatch"
fi

# The menu entry.
if [ -f "$WWW/a/plugins.js" ] && grep -q 'plugins.aiwatch' "$WWW/a/plugins.js"; then
	awk '
		/^\/\/ aiwatch settings page \(added by wyzer-cam\)$/ { skip = 1; next }
		skip { if (index($0, ";})();")) skip = 0; next }
		{ print }
	' "$WWW/a/plugins.js" >"$WWW/a/plugins.js.new" && cat "$WWW/a/plugins.js.new" >"$WWW/a/plugins.js"
	rm -f "$WWW/a/plugins.js.new"
	if grep -q 'plugins.aiwatch' "$WWW/a/plugins.js"; then
		echo "ERROR: could not remove the menu entry"
	else
		echo "ok: removed the menu entry"
	fi
else
	echo "ok: no menu entry to remove"
fi

rm -f "$WWW/config-aiwatch.html" "$WWW/a/config-aiwatch.js" "$WWW/x/json-aiwatch.cgi" \
	"$WWW/a/plugins/aiwatch.webui.json" "$ROOT/usr/sbin/aiwatch"
rm -rf "${AIWATCH_STATE_DIR:-/tmp/aiwatch}"
echo "ok: removed the aiwatch program and web page"

if [ -z "$KEEP_CONFIG" ]; then
	rm -f "$ROOT/etc/aiwatch.json"
	echo "ok: removed /etc/aiwatch.json"
else
	echo "ok: kept /etc/aiwatch.json"
fi
exit 0
