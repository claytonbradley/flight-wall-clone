#!/usr/bin/env bash

set -Eeuo pipefail

APP_USER="flightwall"
APP_GROUP="flightwall"
INSTALL_DIR="/opt/flightwall"
SERVICE_NAME="flightwall.service"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_URL="https://github.com/claytonbradley/flight-wall-clone.git"
KIOSK_USER="kiosk"
SKIP_PACKAGES=false

usage() {
    cat <<'EOF'
Usage: sudo bash deploy.sh [options]

Options:
  --kiosk-user USER  Dedicated account that will run Chromium. The account is
                     created if needed. Defaults to "kiosk".
  --skip-packages    Do not run apt update/install.
  -h, --help         Show this help.
EOF
}

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

status() {
    printf '\n==> %s\n' "$*"
}

warn() {
    printf '\nWARNING: %s\n' "$*" >&2
}

while (($#)); do
    case "$1" in
        --kiosk-user)
            (($# >= 2)) || die "--kiosk-user requires an account name"
            KIOSK_USER="$2"
            shift 2
            ;;
        --skip-packages)
            SKIP_PACKAGES=true
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            usage >&2
            die "unknown option: $1"
            ;;
    esac
done

[[ ${EUID} -eq 0 ]] || die "run this script with sudo"
[[ "$KIOSK_USER" =~ ^[a-z_][a-z0-9_-]*$ && "$KIOSK_USER" != "root" ]] || \
    die "invalid kiosk account name: $KIOSK_USER"

for required in app.py update_faa_registry.py config.json static systemd/flightwall.service systemd/faa-registry-update.service systemd/faa-registry-update.timer systemd/openbox-autostart tests; do
    [[ -e "$SCRIPT_DIR/$required" ]] || die "missing project file: $required"
done
cd "$SCRIPT_DIR"

if [[ "$SKIP_PACKAGES" == false ]]; then
    status "Installing Debian packages"
    command -v apt-get >/dev/null 2>&1 || die "this installer requires a Debian-based system with apt"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y python3 chromium lightdm openbox x11-xserver-utils openssh-server git sudo
fi

status "Validating configuration and running unit tests"
command -v python3 >/dev/null 2>&1 || die "python3 is not installed"
python3 -c 'import json, pathlib; json.loads(pathlib.Path("config.json").read_text())' \
    2>/dev/null || die "config.json is not valid JSON"
API_PORT="$(python3 -c 'import json; print(json.load(open("config.json")).get("listen_port", 8765))')"
python3 -m unittest discover -s "$SCRIPT_DIR/tests" -v

status "Preparing service and kiosk accounts"
if ! getent passwd "$KIOSK_USER" >/dev/null; then
    useradd --create-home --shell /bin/bash --comment "Local Air Traffic kiosk" "$KIOSK_USER"
fi

KIOSK_SHELL="$(getent passwd "$KIOSK_USER" | cut -d: -f7)"
case "$KIOSK_SHELL" in
    */nologin|*/false) die "kiosk account '$KIOSK_USER' cannot start a graphical session" ;;
esac

for device_group in audio video render input; do
    if getent group "$device_group" >/dev/null; then
        usermod --append --groups "$device_group" "$KIOSK_USER"
    fi
done

if ! getent passwd "$APP_USER" >/dev/null; then
    useradd --system --user-group --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin "$APP_USER"
fi

status "Installing Local Air Traffic application files"
install -d -o root -g root -m 0755 "$INSTALL_DIR"
install -d -o "$APP_USER" -g "$APP_GROUP" -m 0755 /var/cache/flightwall
install -o root -g root -m 0644 "$SCRIPT_DIR/app.py" "$INSTALL_DIR/app.py"
install -o root -g root -m 0755 "$SCRIPT_DIR/update_faa_registry.py" "$INSTALL_DIR/update_faa_registry.py"
install -o root -g root -m 0644 "$SCRIPT_DIR/config.json" "$INSTALL_DIR/config.json"
cp -a "$SCRIPT_DIR/static" "$INSTALL_DIR/"
chown -R root:root "$INSTALL_DIR/static"
find "$INSTALL_DIR/static" -type d -exec chmod 0755 {} +
find "$INSTALL_DIR/static" -type f -exec chmod 0644 {} +

install -o root -g root -m 0644 \
    "$SCRIPT_DIR/systemd/flightwall.service" "/etc/systemd/system/$SERVICE_NAME"
install -o root -g root -m 0644 \
    "$SCRIPT_DIR/systemd/faa-registry-update.service" "/etc/systemd/system/faa-registry-update.service"
install -o root -g root -m 0644 \
    "$SCRIPT_DIR/systemd/faa-registry-update.timer" "/etc/systemd/system/faa-registry-update.timer"

KIOSK_HOME="$(getent passwd "$KIOSK_USER" | cut -d: -f6)"
KIOSK_GROUP="$(id -gn "$KIOSK_USER")"
[[ -n "$KIOSK_HOME" ]] || die "home directory for '$KIOSK_USER' is unavailable"
install -d -o "$KIOSK_USER" -g "$KIOSK_GROUP" -m 0755 "$KIOSK_HOME"
status "Preparing the kiosk-managed Git checkout"
REPO_DIR="$KIOSK_HOME/flight-wall-clone"
if [[ -L "$REPO_DIR" ]]; then
    die "refusing to use a symbolic link as the kiosk repository: $REPO_DIR"
fi
if [[ -e "$REPO_DIR" && ! -d "$REPO_DIR/.git" ]]; then
    die "$REPO_DIR exists but is not a Git repository"
fi
if [[ ! -d "$REPO_DIR/.git" ]]; then
    runuser -u "$KIOSK_USER" -- git clone "$REPO_URL" "$REPO_DIR"
fi
[[ "$(stat -c %U "$REPO_DIR")" == "$KIOSK_USER" ]] || \
    die "$REPO_DIR must be owned by $KIOSK_USER"
runuser -u "$KIOSK_USER" -- git -C "$REPO_DIR" remote set-url origin "$REPO_URL"

status "Configuring restricted kiosk administration commands"
printf '%s\n' \
    '#!/bin/sh' \
    "exec /usr/bin/bash '$REPO_DIR/deploy.sh' --kiosk-user '$KIOSK_USER' \"\$@\"" \
    > /usr/local/sbin/flightwall-deploy
printf '%s\n' \
    '#!/bin/sh' \
    'exec /usr/bin/systemctl reboot' \
    > /usr/local/sbin/flightwall-reboot
chmod 0755 /usr/local/sbin/flightwall-deploy /usr/local/sbin/flightwall-reboot
printf '%s ALL=(root) NOPASSWD: /usr/local/sbin/flightwall-deploy, /usr/local/sbin/flightwall-reboot\n' \
    "$KIOSK_USER" > /etc/sudoers.d/flightwall-kiosk
chmod 0440 /etc/sudoers.d/flightwall-kiosk
visudo -cf /etc/sudoers.d/flightwall-kiosk >/dev/null || die "kiosk sudo configuration is invalid"
status "Configuring kiosk maintenance aliases"
KIOSK_BASHRC="$KIOSK_HOME/.bashrc"
if [[ ! -f "$KIOSK_BASHRC" ]]; then
    install -o "$KIOSK_USER" -g "$KIOSK_GROUP" -m 0644 /dev/null "$KIOSK_BASHRC"
fi
sed -i '/^# BEGIN LOCAL AIR TRAFFIC ALIASES$/,/^# END LOCAL AIR TRAFFIC ALIASES$/d' "$KIOSK_BASHRC"
printf '\n%s\n' \
    '# BEGIN LOCAL AIR TRAFFIC ALIASES' \
    "alias deploy='sudo /usr/local/sbin/flightwall-deploy --skip-packages'" \
    "alias deploy-initial='sudo /usr/local/sbin/flightwall-deploy'" \
    '# END LOCAL AIR TRAFFIC ALIASES' \
    >> "$KIOSK_BASHRC"
chown "$KIOSK_USER:$KIOSK_GROUP" "$KIOSK_BASHRC"
chmod 0644 "$KIOSK_BASHRC"

status "Configuring SSH access for the kiosk account"
[[ -x /usr/sbin/sshd ]] || die "OpenSSH server is not installed; rerun without --skip-packages"
INITIAL_KIOSK_PASSWORD_SET=false
PASSWORD_STATE="$(passwd -S "$KIOSK_USER" | awk '{print $2}')"
if [[ "$PASSWORD_STATE" == "L" || "$PASSWORD_STATE" == "NP" ]]; then
    printf '%s:%s\n' "$KIOSK_USER" 'kiosk' | chpasswd
    INITIAL_KIOSK_PASSWORD_SET=true
fi
install -d -o root -g root -m 0755 /etc/ssh/sshd_config.d
printf '%s\n' \
    '# Managed by Local Air Traffic deploy.sh' \
    'PermitRootLogin no' \
    'PubkeyAuthentication yes' \
    'PasswordAuthentication yes' \
    'KbdInteractiveAuthentication no' \
    "AllowUsers $KIOSK_USER" \
    > /etc/ssh/sshd_config.d/00-local-air-traffic.conf
chmod 0644 /etc/ssh/sshd_config.d/00-local-air-traffic.conf
install -d -o root -g root -m 0755 /run/sshd
ssh-keygen -A
/usr/sbin/sshd -t || die "OpenSSH configuration validation failed"
SSHD_EFFECTIVE="$(/usr/sbin/sshd -T)"
grep -Fqx 'permitrootlogin no' <<<"$SSHD_EFFECTIVE" || die "SSH root-login restriction was not applied"
grep -Fqx 'pubkeyauthentication yes' <<<"$SSHD_EFFECTIVE" || die "SSH public-key authentication was not enabled"
grep -Fqx 'passwordauthentication yes' <<<"$SSHD_EFFECTIVE" || die "SSH password authentication was not enabled"
grep -Fqx "allowusers $KIOSK_USER" <<<"$SSHD_EFFECTIVE" || die "SSH was not restricted to $KIOSK_USER"
systemctl enable ssh.service
systemctl restart ssh.service
systemctl is-enabled --quiet ssh.service || die "SSH service was not enabled at boot"
systemctl is-active --quiet ssh.service || {
    journalctl -u ssh.service -n 30 --no-pager >&2
    die "SSH service did not start"
}
install -d -o "$KIOSK_USER" -g "$KIOSK_GROUP" -m 0755 "$KIOSK_HOME/.config"
install -d -o "$KIOSK_USER" -g "$KIOSK_GROUP" -m 0755 "$KIOSK_HOME/.config/openbox"
install -o "$KIOSK_USER" -g "$KIOSK_GROUP" -m 0755 \
    "$SCRIPT_DIR/systemd/openbox-autostart" "$KIOSK_HOME/.config/openbox/autostart"

install -d -o root -g root -m 0755 /etc/lightdm/lightdm.conf.d
printf '%s\n' \
    '[Seat:*]' \
    "autologin-user=$KIOSK_USER" \
    'autologin-user-timeout=0' \
    'autologin-session=openbox' \
    'user-session=openbox' \
    > /etc/lightdm/lightdm.conf.d/50-local-air-traffic.conf
chmod 0644 /etc/lightdm/lightdm.conf.d/50-local-air-traffic.conf

[[ -f /usr/share/xsessions/openbox.desktop ]] || die "the Openbox X session is not installed"
install -d -o root -g root -m 0755 /etc/X11
printf '/usr/sbin/lightdm\n' > /etc/X11/default-display-manager

status "Reloading systemd service definitions"
systemctl daemon-reload

status "Checking and, when needed, updating the FAA aircraft index"
if ! runuser -u "$APP_USER" -- /usr/bin/python3 "$INSTALL_DIR/update_faa_registry.py" \
    --output /var/cache/flightwall/faa-aircraft.json; then
    warn "FAA aircraft index update failed; deployment will continue using the other enrichment sources. The nightly timer will retry."
fi

status "Enabling the nightly FAA update timer"
systemctl enable --now faa-registry-update.timer
systemctl reset-failed faa-registry-update.service 2>/dev/null || true

status "Starting the Local Air Traffic backend and display manager"
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"
systemctl enable --force lightdm.service
systemctl set-default graphical.target

LIGHTDM_UNIT="$(systemctl show lightdm.service --property=FragmentPath --value)"
DISPLAY_MANAGER_UNIT="$(readlink -f /etc/systemd/system/display-manager.service || true)"
[[ -n "$LIGHTDM_UNIT" && "$DISPLAY_MANAGER_UNIT" == "$(readlink -f "$LIGHTDM_UNIT")" ]] || \
    die "LightDM was not selected as the default display manager"
grep -Fqx "autologin-user=$KIOSK_USER" /etc/lightdm/lightdm.conf.d/50-local-air-traffic.conf || \
    die "LightDM autologin configuration was not applied"

systemctl is-active --quiet "$SERVICE_NAME" || {
    journalctl -u "$SERVICE_NAME" -n 30 --no-pager >&2
    die "$SERVICE_NAME did not start"
}

status "Verifying the display page and aircraft API"
python3 - "$API_PORT" <<'PY'
import json
import sys
import time
import urllib.request

base_url = f"http://127.0.0.1:{sys.argv[1]}"
for attempt in range(10):
    try:
        with urllib.request.urlopen(base_url + "/", timeout=3) as response:
            page = response.read().decode("utf-8")
        if "LOCAL AIR TRAFFIC" not in page:
            raise RuntimeError("display page did not contain the expected heading")
        with urllib.request.urlopen(base_url + "/api/aircraft", timeout=3) as response:
            payload = json.load(response)
        if payload.get("error"):
            raise RuntimeError(payload["error"])
        print(f"Verified display page and application API at {base_url}")
        break
    except Exception as exc:
        if attempt == 9:
            raise RuntimeError(f"deployment verification failed: {exc}") from exc
        time.sleep(1)
PY

printf '\nDeployment complete. Reboot to verify automatic login and kiosk startup:\n'
printf '  sudo /usr/local/sbin/flightwall-reboot\n\n'
printf 'Kiosk account: %s\n' "$KIOSK_USER"
printf 'SSH login: ssh %s@<device-ip>\n' "$KIOSK_USER"
if [[ "$INITIAL_KIOSK_PASSWORD_SET" == true ]]; then
    printf 'Initial SSH password: kiosk (change it immediately with passwd)\n'
fi
printf 'Kiosk repository: %s\n\n' "$REPO_DIR"
printf 'Update and deploy:\n'
printf '  cd %s && git pull\n' "$REPO_DIR"
printf '  deploy\n\n'
printf 'Full deployment including package installation:\n'
printf '  deploy-initial\n\n'
printf 'Default display manager: %s\n\n' "$DISPLAY_MANAGER_UNIT"
printf 'Service logs:\n'
printf '  journalctl -u %s -f\n' "$SERVICE_NAME"
printf '  journalctl -u faa-registry-update.service\n'
