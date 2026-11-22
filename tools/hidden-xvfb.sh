#!/usr/bin/env bash
# Start a hidden X server for ODA File Converter without root.
#
# Unpack Ubuntu packages once (apt-get download needs no root):
#   mkdir -p ~/.local/opt/xvfb/debs && cd ~/.local/opt/xvfb/debs
#   apt-get download xvfb libxfont2 libfontenc1 x11-xkb-utils libxkbfile1
#   for d in *.deb; do dpkg-deb -x "$d" ~/.local/opt/xvfb/root; done
# Then: tools/hidden-xvfb.sh :97 && export MACHINA_ODA_DISPLAY=:97
#
# Xvfb runs xkbcomp from the fixed path /usr/bin/xkbcomp, so a user namespace
# overlays the unpacked binary onto /usr/bin for the server process only.
set -euo pipefail
display="${1:-:97}"
root="${XVFB_ROOT:-$HOME/.local/opt/xvfb/root}"
xkb="$(mktemp -d)"
cp "$root/usr/bin/xkbcomp" "$xkb/"
unshare --user --map-root-user --mount sh -c '
  mount -t overlay overlay -o "lowerdir=$1:/usr/bin" /usr/bin &&
  LD_LIBRARY_PATH="$2/usr/lib/x86_64-linux-gnu" exec "$2/usr/bin/Xvfb" "$3" -screen 0 1024x768x24 -nolisten tcp
' sh "$xkb" "$root" "$display" >/dev/null 2>&1 &
for _ in $(seq 50); do
  [ -S "/tmp/.X11-unix/X${display#:}" ] && exit 0
  sleep 0.1
done
echo "hidden-xvfb: display $display did not start" >&2
exit 1
