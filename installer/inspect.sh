#!/bin/sh
# wyzer-cam-inspect v1
#
# Prints what the installer needs to know about the camera, one key=value per
# line. Reads only; changes nothing.

ROOT="${WYZER_ROOT:-}"
OSREL="$ROOT/etc/os-release"

val() {
	# $1 = KEY from os-release, without quotes
	[ -f "$OSREL" ] && awk -F= -v k="$1" '$1 == k { gsub(/"/, "", $2); print $2; exit }' "$OSREL"
}

echo "hostname=$(hostname 2>/dev/null)"
echo "image=$(val IMAGE_ID)"
echo "build_time=$(val BUILD_TIME)"
if [ -f "$OSREL" ] && grep -qi thingino "$OSREL"; then
	echo "thingino=yes"
else
	echo "thingino=no"
fi
[ -f "$ROOT/usr/sbin/motion" ] && echo "motion_script=yes" || echo "motion_script=no"
command -v prudyntctl >/dev/null 2>&1 && echo "prudynt=yes" || echo "prudynt=no"
command -v jct >/dev/null 2>&1 && echo "jct=yes" || echo "jct=no"
command -v curl >/dev/null 2>&1 && echo "curl=yes" || echo "curl=no"
[ -d "$ROOT/var/www/a" ] && [ -d "$ROOT/var/www/x" ] && echo "webui=yes" || echo "webui=no"
echo "free_kb=$(df -k "$ROOT/overlay" 2>/dev/null | awk 'NR == 2 { print $4 }')"
[ -f "$ROOT/usr/sbin/aiwatch" ] && echo "aiwatch=yes" || echo "aiwatch=no"
grep -qs 'aiwatch motion' "$ROOT/usr/sbin/motion" && echo "hooked=yes" || echo "hooked=no"
[ -f "$ROOT/var/www/config-aiwatch.html" ] && echo "aiwatch_page=yes" || echo "aiwatch_page=no"
[ -f "$ROOT/etc/aiwatch.json" ] && echo "aiwatch_config=yes" || echo "aiwatch_config=no"
exit 0
