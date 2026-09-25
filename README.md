# FlightWall: Debian 13 first version

Displays aircraft received by an existing PiAware/dump1090-fa receiver on your LAN. The T630 runs a Python backend and serves the display to Chromium. It needs no SDR. Python uses only the standard library.

## First run on any computer

1. Copy `config.example.json` to `config.json`. Edit `receiver_url` to the working aircraft JSON URL on your PiAware. Try `http://PIAWARE-IP/skyaware/data/aircraft.json` in a browser or with `curl` first; depending on your PiAware web setup, the URL may differ. Use the PiAware IP if `.local` does not resolve.
2. From this directory, run `python3 app.py`.
3. Visit `http://127.0.0.1:8765`.

The UI reports receiver connection state. Without a known receiver location, it lists the freshest aircraft first and leaves distance blank. If `receiver_lat` and `receiver_lon` are set to numeric values, it sorts by distance. Latitude must be between -90 and 90; longitude must be between -180 and 180, and both values must be supplied together. To restrict the display to a radius, also set `max_distance_miles`, for example `60`. A plane without a fresh position cannot pass a radius filter. All positions must come from your receiver; no wider-area feed is merged in.

After three fresh position samples spanning at least six seconds, the distance column indicates radial motion relative to the receiver: `APPROACHING`, `MOVING AWAY`, or `CROSSING`. A rolling 30-second trend and hysteresis reduce label flicker. These labels describe movement relative to the receiver, not an airport arrival or departure.

`use_adsbdb` controls lookup requests. When true, ADSBDB supplies aircraft details and routes where known. The backend keeps results in memory for 12 hours when a route exists, one hour when no route exists, six hours for an unknown aircraft, and five minutes for a temporary error. Restarting clears the lookup cache. Route data is not written into a database. Callsign route matches can be missing or inaccurate; the UI shows `-` when absent. Internet outages do not interrupt the receiver display.

Aircraft descriptions prefer the manufacturer and model returned by ADSBDB. When enrichment is incomplete, the backend consults SkyAware's local, static aircraft database under the receiver's `/skyaware/db/` path, then the free HexDB aircraft API, the rate-limited adsb.fi live aircraft API, and finally the local FAA aircraft index for U.S. registrations. The ICAO aircraft type in PiAware's live `t` field (for example, `B738`) remains available when enrichment has no make/model. A dash is used only when none of these sources provides a type. SkyAware database shards and HexDB results are cached for 24 hours. The SkyAware lookup stays on the LAN and remains active when `use_adsbdb` is false. `use_hexdb`, `use_adsb_fi`, and `use_faa_registry` independently control their respective fallbacks and default to true.

The deployment installs `faa-registry-update.timer`. It downloads the FAA releasable aircraft archive immediately when no current index exists and checks again every night at 1:00 AM local time. Before downloading, it compares the index timestamp with the FAA's latest expected 11:30 PM Central refresh and skips the transfer when already current, so repeated deployments and reboots do not redownload the archive. Only Mode-S hex, registration, manufacturer, and model are retained in `/var/cache/flightwall/faa-aircraft.json`; owner, address, and aircraft-year fields are never written to the index. `/var/cache/flightwall/faa-aircraft.meta.json` records the last successful parse time, record count, and source URL. Updates use atomic replacement, and the backend notices the new index without a restart. Inspect the schedule with `systemctl list-timers faa-registry-update.timer`, metadata with `cat /var/cache/flightwall/faa-aircraft.meta.json`, and logs with `journalctl -u faa-registry-update.service`.

`auto_download_logos` controls the automatic logo cache and defaults to true. When ADSBDB identifies an airline without a bundled asset, the backend searches Wikimedia Commons and prefers a standalone symbol. A matching corporate logo or wordmark is accepted when no symbol is available. Downloads are limited to 1 MB and validated as PNG before being saved. A failed or ambiguous search retains the ICAO-code badge and is not retried for six hours. Development downloads go into `logo-cache`; the systemd service stores them persistently in `/var/cache/flightwall/logos`. `sources.json` records the source page and asset type.

## Install on Debian 13 T630

From the extracted project directory on the T630, with a working PiAware URL in `config.json`:

### Automated deployment

Log in with an administrator account, open a terminal in the project folder, and run:

```sh
sudo bash deploy.sh
```

The script creates a dedicated, non-administrator account named `kiosk` and configures it for automatic graphical login. To use a different account name, use:

```sh
sudo bash deploy.sh --kiosk-user YOUR_DESKTOP_USERNAME
```

The script installs the required Debian packages, validates `config.json`, runs the unit tests, creates the kiosk account and required device-group memberships, installs the application and systemd services with normalized read permissions, downloads and parses the initial FAA aircraft index, enables its nightly 1:00 AM timer, selects LightDM as Debian's default display manager, configures Openbox and LightDM autologin, starts the backend, and verifies both the display page and receiver API. Chromium uses its basic local password store so the kiosk login does not produce a keyring prompt. The backend restarts after any exit, and the kiosk session relaunches Chromium if the browser closes or crashes. It is safe to run again for application updates. Use `--skip-packages` on later deployments to skip `apt update` and package installation. Reboot after the first deployment to verify unattended kiosk startup.

### SSH maintenance access

The first deployment asks you to set a password for the kiosk account, installs and enables OpenSSH, disables direct root login, and restricts SSH login to the kiosk account. The account owns a checkout at `~/flight-wall-clone` and receives no general-purpose sudo permission. It can invoke only two named passwordless wrappers: `flightwall-deploy` and `flightwall-reboot`.

From another computer on the same network:

```sh
ssh kiosk@DEVICE_IP
cd ~/flight-wall-clone
git pull
deploy
sudo /usr/local/sbin/flightwall-reboot
```

The `deploy` alias skips package installation for routine updates. Use `deploy-initial` when packages must be installed or repaired; it runs the same deployment without `--skip-packages`. Both aliases are maintained in the kiosk account's `.bashrc`.

Because `kiosk` can modify the repository and invoke its deployment script as root, control of the kiosk SSH account is effectively administrative access to this appliance. Use a strong unique password and do not expose TCP port 22 directly to the internet. The installer does not alter firewall rules.

If the login screen still appears, collect the effective configuration and current-boot log with:

```sh
cat /etc/X11/default-display-manager
readlink -f /etc/systemd/system/display-manager.service
sudo lightdm --show-config
sudo journalctl -u lightdm -b --no-pager
```

### Manual deployment

```sh
sudo apt update
sudo apt install python3 chromium lightdm openbox x11-xserver-utils openssh-server git sudo
sudo useradd --system --user-group --home-dir /opt/flightwall --shell /usr/sbin/nologin flightwall
sudo mkdir -p /opt/flightwall
sudo cp -a app.py config.json static /opt/flightwall/
sudo chown -R root:root /opt/flightwall
sudo chmod 644 /opt/flightwall/config.json
sudo cp systemd/flightwall.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now flightwall.service
systemctl status flightwall.service
curl http://127.0.0.1:8765/api/aircraft
```

If `useradd` says the account already exists, continue. To update the application later, copy the changed files into `/opt/flightwall/` and run `sudo systemctl restart flightwall.service`. Logs: `journalctl -u flightwall.service -f`.

### Kiosk on the local monitor

Log in to the T630 graphical desktop as your normal user. Run:

```sh
mkdir -p ~/.config/openbox
cp systemd/openbox-autostart ~/.config/openbox/autostart
chmod +x ~/.config/openbox/autostart
```

On the LightDM login screen, select the **Openbox** session. On subsequent logins the screen opens Chromium in kiosk mode automatically. For unattended startup, add a LightDM autologin configuration using your actual desktop account name:

```sh
sudo mkdir -p /etc/lightdm/lightdm.conf.d
sudoedit /etc/lightdm/lightdm.conf.d/50-flightwall.conf
```

Contents (replace `YOUR_DESKTOP_USERNAME`):

```ini
[Seat:*]
autologin-user=YOUR_DESKTOP_USERNAME
autologin-user-timeout=0
autologin-session=openbox
user-session=openbox
```

Reboot to verify the graphical session and service start. Use `Ctrl+Alt+F2` to reach a text console if kiosk setup needs changing. On the T630 keep `listen_host` at `127.0.0.1`; to view the interface from another computer while designing, set it to `0.0.0.0` on a trusted LAN and visit the T630 IP on port 8765.

## Airline logos

The project includes vetted standalone SVG symbols for several common carriers; see `static/logos/README.md` for the airline inventory, reviewed exclusions, sources, and trademark notice. Symbols are named by the airline ICAO code returned by ADSBDB, such as `DAL.svg` or `AAL.svg`. If a carrier has only a wordmark or an inseparable combination mark, the interface uses the same ICAO-code badge used for any missing symbol.

To add another carrier, place a trusted SVG in `static/logos` (or `/opt/flightwall/static/logos` once installed), name it with the airline's uppercase ICAO code, and add that code to `BUNDLED_AIRLINE_LOGOS` in `app.py`. Do not place untrusted SVGs in this folder.

## Configuration

`config.json` is local to each installation. `poll_seconds` defaults to 3, `max_aircraft` to 12, `max_seen_seconds` to 45, and `max_position_age_seconds` to 90. Set `show_airport_names` to `true` to show shortened airport names beneath route identifiers or `false` for identifiers only; it defaults to `true`. `listen_port` defaults to 8765. The backend never changes the PiAware machine. `examples/aircraft.json` illustrates receiver input fields; its `now` value is deliberately zero and is not a live feed.

## Tests

Run the offline standard-library unit tests from the project directory:

```sh
python3 -m unittest discover -s tests -v
```

The suite uses mocked network responses, synthetic FAA archives, and temporary cache directories. It does not contact PiAware, ADSBDB, adsb.fi, FAA, or Wikimedia and does not modify the real caches.

## Source references

- [dump1090-fa JSON format](https://github.com/edgeofspace/dump1090-fa/blob/master/README-json.md)
- [ADSBDB API response documentation](https://github.com/mrjackwills/adsbdb#readme)
- [FAA Releasable Aircraft Database](https://www.faa.gov/licenses_certificates/aircraft_certification/aircraft_registry/releasable_aircraft_download)
- [Debian 13 Python package](https://packages.debian.org/trixie/python3)
- [Debian 13 Chromium package](https://packages.debian.org/trixie/chromium)
