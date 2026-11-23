#!/bin/bash
# Installs TideView on a Raspberry Pi, or moves a TideView that was copied
# onto the SD card over to automatic updates. On the Pi, run:
#
#     curl -fsSL https://raw.githubusercontent.com/CactiGames/tideview-pi/main/get.sh | sudo bash
#
# It downloads the newest TideView, checks it against its SHA-256 and runs
# its install.sh. Settings and keys are kept.
main() {
  set -euo pipefail
  local base="${TIDEVIEW_BASE:-https://raw.githubusercontent.com/CactiGames/tideview-pi/main}"
  if [ "$(id -u)" -ne 0 ]; then
    echo "Please run it with sudo:  curl -fsSL $base/get.sh | sudo bash" >&2
    exit 1
  fi
  local work
  work="$(mktemp -d /tmp/tideview-get.XXXXXX)"
  trap "rm -rf '$work'" EXIT
  echo "Getting the newest TideView..."
  curl -fsSL --retry 3 --max-time 120 "$base/tvupdate.py" -o "$work/tvupdate.py"
  python3 "$work/tvupdate.py" fetch --url "$base/latest.json" --to "$work/tideview"
  TIDEVIEW_APP_DIR="$work/tideview" bash "$work/tideview/install.sh" "$@" < /dev/null
}
main "$@"
