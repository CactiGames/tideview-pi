# TideView for Raspberry Pi: updates

TideView is an always-on surf forecast for a picture frame: a Raspberry Pi
Zero W and a 7" HDMI touch screen. This repo is where those frames get their
updates.

Every hour each Pi reads `latest.json`, downloads the zip it names, checks
the zip's size and SHA-256, and test-runs it without the screen. It then
switches over at a quiet time: between 2 and 5 AM, in the first 15 minutes
after power-on, or straight away when someone taps **Settings > Check for
updates now**. If a new version doesn't start properly, the Pi goes back to
the previous one by itself and never tries that version again.

## Install on a Pi

1. Write **Raspberry Pi OS Lite (32-bit)** to the SD card with Raspberry Pi
   Imager. In its settings, set the hostname (for example `tideview`), a
   username and password, a **2.4 GHz** Wi-Fi network, and turn on SSH.
2. Optional: to use NIWA tides and MetService swell, make a folder called
   `tideview` on the card's **bootfs** drive and put your `.env.txt` in it.
   Without it, TideView uses free forecasts.
3. Put the card in the Pi and switch it on. After a few minutes, connect
   with `ssh <username>@tideview.local` and run:

   ```
   curl -fsSL https://raw.githubusercontent.com/CactiGames/tideview-pi/main/get.sh | sudo bash
   ```

The same command moves a Pi with an older, card-copied TideView over to
automatic updates. Settings and keys are kept.

## Useful commands on the Pi

| Command | What it does |
| --- | --- |
| `sudo python3 /opt/tideview/tvupdate.py status` | Versions, and the last update check |
| `sudo python3 /opt/tideview/tvupdate.py check --now` | Check now and install straight away |
| `journalctl -u tideview-update -n 30` | What the updater did |
| `journalctl -u tideview -n 50` | What TideView did |

## What's here

| File | What it is |
| --- | --- |
| `latest.json` | The newest version, its zip, size and SHA-256. The Pis read this. |
| `releases/` | TideView itself, one zip per version. |
| `tvupdate.py` | The updater (each zip has its own copy too). |
| `get.sh` | The installer that the command above runs. |
| `CHANGELOG.md` | What changed in each version. |

There are no API keys in this repo. Each Pi keeps its own copy in
`/var/lib/tideview/keys.env`, readable only by root.
