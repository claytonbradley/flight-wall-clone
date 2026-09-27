#!/usr/bin/env bash

set -Eeuo pipefail

KIOSK_USER="kiosk"
WIFI_SSID="${FLIGHTWALL_WIFI_SSID:-}"
WIFI_PASSWORD="${FLIGHTWALL_WIFI_PASSWORD:-}"
WIFI_CONNECTION="flightwall-wifi"
REPO_URL="https://github.com/claytonbradley/flight-wall-clone.git"

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

status() {
    printf '\n==> %s\n' "$*"
}

usage() {
    cat <<'EOF'
Usage: sudo bash flightwall-initial-deploy.sh --wifi-ssid SSID --wifi-password PASSWORD
EOF
}

while (($#)); do
    case "$1" in
        --wifi-ssid)
            (($# >= 2)) || die "--wifi-ssid requires a value"
            WIFI_SSID="$2"
            shift 2
            ;;
        --wifi-password)
            (($# >= 2)) || die "--wifi-password requires a value"
            WIFI_PASSWORD="$2"
            shift 2
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

[[ -n "$WIFI_SSID" ]] || die "--wifi-ssid is required"
[[ -n "$WIFI_PASSWORD" ]] || die "--wifi-password is required"

[[ ${EUID} -eq 0 ]] || die "run this script with sudo"
[[ -r /etc/os-release ]] || die "/etc/os-release is unavailable"
# shellcheck disable=SC1091
source /etc/os-release
[[ "${ID:-}" == "debian" && "${VERSION_ID:-}" == 13* ]] || \
    die "this bootstrap requires Debian 13"

status "Configuring Wi-Fi"
if ! command -v nmcli >/dev/null 2>&1; then
    status "Installing NetworkManager from configured Debian media"
    apt-get install -y network-manager || \
        die "NetworkManager is unavailable; connect Ethernet or make the Debian installation media available to apt"
fi
command -v nmcli >/dev/null 2>&1 || die "nmcli is unavailable after installing NetworkManager"
systemctl enable --now NetworkManager.service
WIFI_DEVICE="$(nmcli -t -f DEVICE,TYPE device status | awk -F: '$2 == "wifi" {print $1; exit}')"
[[ -n "$WIFI_DEVICE" ]] || die "no Wi-Fi adapter was detected by NetworkManager"
nmcli radio wifi on

if ! nmcli -t -f NAME connection show | grep -Fxq "$WIFI_CONNECTION"; then
    nmcli connection add type wifi ifname "$WIFI_DEVICE" con-name "$WIFI_CONNECTION" ssid "$WIFI_SSID"
fi
nmcli connection modify "$WIFI_CONNECTION" \
    802-11-wireless.ssid "$WIFI_SSID" \
    wifi-sec.key-mgmt wpa-psk \
    wifi-sec.psk "$WIFI_PASSWORD" \
    ipv4.method auto \
    ipv6.method auto \
    connection.autoconnect yes \
    connection.autoconnect-priority 100
nmcli connection up "$WIFI_CONNECTION" ifname "$WIFI_DEVICE"

for attempt in {1..15}; do
    if getent hosts github.com >/dev/null 2>&1; then
        break
    fi
    ((attempt < 15)) || die "Wi-Fi connected, but internet name resolution is unavailable"
    sleep 2
done

status "Installing bootstrap dependencies"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates git network-manager openssh-server sudo

status "Preparing the kiosk account and repository"
if ! getent passwd "$KIOSK_USER" >/dev/null; then
    useradd --create-home --shell /bin/bash --comment "Local Air Traffic kiosk" "$KIOSK_USER"
fi
KIOSK_HOME="$(getent passwd "$KIOSK_USER" | cut -d: -f6)"
KIOSK_GROUP="$(id -gn "$KIOSK_USER")"
[[ -n "$KIOSK_HOME" ]] || die "kiosk home directory is unavailable"
install -d -o "$KIOSK_USER" -g "$KIOSK_GROUP" -m 0755 "$KIOSK_HOME"

REPO_DIR="$KIOSK_HOME/flight-wall-clone"
if [[ -e "$REPO_DIR" && ! -d "$REPO_DIR/.git" ]]; then
    die "$REPO_DIR exists but is not a Git repository"
fi
if [[ ! -d "$REPO_DIR/.git" ]]; then
    runuser -u "$KIOSK_USER" -- git clone "$REPO_URL" "$REPO_DIR"
else
    [[ "$(stat -c %U "$REPO_DIR")" == "$KIOSK_USER" ]] || \
        die "$REPO_DIR must be owned by $KIOSK_USER"
    runuser -u "$KIOSK_USER" -- git -C "$REPO_DIR" pull --ff-only
fi

status "Running the full Local Air Traffic deployment"
/usr/bin/bash "$REPO_DIR/deploy.sh" --kiosk-user "$KIOSK_USER"

printf '\nInitial deployment complete.\n'
printf 'Wi-Fi: %s\n' "$WIFI_SSID"
printf 'SSH login: ssh %s@<device-ip>\n' "$KIOSK_USER"
printf 'Initial SSH password: kiosk\n'
printf 'Repository: %s\n' "$REPO_DIR"
printf 'Change the kiosk password immediately with: passwd\n'
printf 'Reboot with: sudo /usr/local/sbin/flightwall-reboot\n'
