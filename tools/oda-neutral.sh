#!/usr/bin/env bash
# Run ODA File Converter under a neutral account name.
# ODA stamps $LASTSAVEDBY with the passwd login name of the running user on every
# write (environment variables do not change it). bubblewrap substitutes a passwd
# entry for this UID so outputs carry MACHINA_ODA_ACCOUNT (default: machina) instead.
#
#   export ODA_FILE_CONVERTER=/path/to/ODAFileConverter   # or the AppImage's AppRun
#   export MACHINA_ODA_CONVERTER="$PWD/tools/oda-neutral.sh"
set -euo pipefail
: "${ODA_FILE_CONVERTER:?set ODA_FILE_CONVERTER to the ODAFileConverter executable}"
name="${MACHINA_ODA_ACCOUNT:-machina}"
passwd="$(mktemp)"
trap 'rm -f "$passwd"' EXIT
awk -F: -v uid="$(id -u)" -v name="$name" 'BEGIN { OFS = ":" } $3 == uid { $1 = name; $5 = name } { print }' /etc/passwd > "$passwd"
run=(bwrap --dev-bind / / --ro-bind "$passwd" /etc/passwd)
if [ -z "${DISPLAY:-}" ] && command -v xvfb-run >/dev/null; then
  run+=(xvfb-run -a)
fi
"${run[@]}" "$ODA_FILE_CONVERTER" "$@"
