#!/bin/sh
# Copy aiwatch into the thingino submodule, apply patches/0001 and 0002,
# and enable the package on every Wyze Cam v3 defconfig.
#
# The submodule is pinned to thingino ciao c193194585b5, the commit these
# patches were made against. The community installer image
# (wltechblog/thingino-installers wyze-cam-3/wyze-cam-3-sd.zip) is an older
# ciao build, firmware-2026-08-14 at 2c3eca50a774. git apply --check fails
# on that tree, so the pin stays on the commit the patches apply to.
#
# Usage: scripts/prepare-thingino.sh

set -eu

ROOT=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
THINGINO=$ROOT/thingino
EXPECTED=c193194585b5bcd064d535787b144ee4054e3f04
LINE='BR2_PACKAGE_THINGINO_AIWATCH=y'

# The three images shipped in the Wyze Cam v3 installer zip.
CAMERAS="
wyze_cam3_t31al_gc2053_atbm6031
wyze_cam3_t31x_gc2053_atbm6031
wyze_cam3_t31x_gc2053_rtl8189ftv
"

cd "$ROOT"

if [ ! -e "$THINGINO/.git" ]; then
	echo "Checking out the thingino submodule..."
	git submodule update --init thingino
fi

actual=$(git -C "$THINGINO" rev-parse HEAD)
echo "thingino $actual"
if [ "$actual" != "$EXPECTED" ]; then
	echo "thingino is at $actual; prepare expects $EXPECTED" >&2
	exit 1
fi

rm -rf "$THINGINO/package/thingino-aiwatch"
cp -a "$ROOT/package/thingino-aiwatch" "$THINGINO/package/thingino-aiwatch"
echo "copied package/thingino-aiwatch"

apply_one() {
	patch=$1
	name=$(basename "$patch")
	if git -C "$THINGINO" apply --check --reverse "$patch" >/dev/null 2>&1; then
		echo "already applied: $name"
		return 0
	fi
	git -C "$THINGINO" apply --check "$patch"
	git -C "$THINGINO" apply "$patch"
	echo "applied: $name"
}

apply_one "$ROOT/patches/0001-motion-call-aiwatch.patch"
apply_one "$ROOT/patches/0002-config-add-aiwatch.patch"

for cam in $CAMERAS; do
	file="$THINGINO/configs/cameras/$cam/${cam}_defconfig"
	if [ ! -f "$file" ]; then
		echo "missing defconfig: $file" >&2
		exit 1
	fi
	if grep -qxF "$LINE" "$file"; then
		echo "already enabled: $cam"
		continue
	fi
	# Keep the new symbol on its own line when the file has no trailing newline.
	if [ -n "$(tail -c 1 "$file")" ]; then
		printf '\n' >>"$file"
	fi
	printf '%s\n' "$LINE" >>"$file"
	echo "enabled: $cam"
done

if [ ! -f "$THINGINO/buildroot/Makefile" ]; then
	echo "buildroot is not checked out yet; for a firmware build run: git submodule update --init --recursive"
fi
