#!/usr/bin/env bash

set -Eeuo pipefail

STATE_FILE="${FLIGHTWALL_MONITOR_STATE_FILE:-/var/cache/flightwall/resource-monitor.samples}"
HISTORY_SAMPLES="${FLIGHTWALL_MONITOR_HISTORY_SAMPLES:-12}"
MINIMUM_SAMPLES="${FLIGHTWALL_MONITOR_MINIMUM_SAMPLES:-6}"
MAXIMUM_SAMPLE_AGE_SECONDS="${FLIGHTWALL_MONITOR_MAXIMUM_SAMPLE_AGE_SECONDS:-5400}"
CHROMIUM_LIMIT_KB="${FLIGHTWALL_MONITOR_CHROMIUM_LIMIT_KB:-524288}"
CHROMIUM_GROWTH_KB="${FLIGHTWALL_MONITOR_CHROMIUM_GROWTH_KB:-131072}"
BACKEND_LIMIT_KB="${FLIGHTWALL_MONITOR_BACKEND_LIMIT_KB:-262144}"
BACKEND_GROWTH_KB="${FLIGHTWALL_MONITOR_BACKEND_GROWTH_KB:-65536}"
PROC_ROOT="${FLIGHTWALL_MONITOR_PROC_ROOT:-/proc}"
MEMINFO_FILE="${FLIGHTWALL_MONITOR_MEMINFO_FILE:-/proc/meminfo}"
DRY_RUN="${FLIGHTWALL_MONITOR_DRY_RUN:-false}"

if [[ -r /etc/default/flightwall-resource-monitor ]]; then
    # This optional root-owned file may override the thresholds above.
    # shellcheck disable=SC1091
    source /etc/default/flightwall-resource-monitor
fi

process_pss_kb() {
    local command_pattern="$1"
    local command_line
    local process_directory
    local process_pss
    local total_pss=0

    for process_directory in "$PROC_ROOT"/[0-9]*; do
        [[ -r "$process_directory/cmdline" ]] || continue
        if ! command_line="$(tr '\0' ' ' < "$process_directory/cmdline" 2>/dev/null)"; then
            continue
        fi
        [[ "$command_line" == *"$command_pattern"* ]] || continue

        if [[ -r "$process_directory/smaps_rollup" ]]; then
            process_pss="$(awk '/^Pss:/ { total += $2 } END { print total + 0 }' "$process_directory/smaps_rollup" 2>/dev/null || printf '0')"
        elif [[ -r "$process_directory/status" ]]; then
            process_pss="$(awk '/^VmRSS:/ { print $2; found = 1 } END { if (!found) print 0 }' "$process_directory/status" 2>/dev/null || printf '0')"
        else
            process_pss=0
        fi
        total_pss=$((total_pss + process_pss))
    done

    printf '%s\n' "$total_pss"
}

restart_component() {
    local unit_name="$1"
    local reason="$2"

    printf 'status=leak-detected unit=%s reason=%s\n' "$unit_name" "$reason"
    if [[ "$DRY_RUN" != "true" ]]; then
        systemctl restart "$unit_name"
    fi
}

current_epoch="$(date +%s)"
available_kb="$(awk '/^MemAvailable:/ { print $2 }' "$MEMINFO_FILE")"
swap_total_kb="$(awk '/^SwapTotal:/ { print $2 }' "$MEMINFO_FILE")"
swap_free_kb="$(awk '/^SwapFree:/ { print $2 }' "$MEMINFO_FILE")"
swap_used_kb=$((swap_total_kb - swap_free_kb))
chromium_pss_kb="$(process_pss_kb '/usr/lib/chromium/chromium')"
backend_pss_kb="$(process_pss_kb '/opt/flightwall/app.py')"

mkdir -p -- "$(dirname -- "$STATE_FILE")"
if [[ -s "$STATE_FILE" ]]; then
    read -r oldest_epoch _ < "$STATE_FILE"
    if ((current_epoch - oldest_epoch > MAXIMUM_SAMPLE_AGE_SECONDS)); then
        : > "$STATE_FILE"
    fi
fi

state_temp="$(mktemp "${STATE_FILE}.XXXXXX")"
trap 'rm -f -- "$state_temp"' EXIT
tail -n "$((HISTORY_SAMPLES - 1))" "$STATE_FILE" > "$state_temp" 2>/dev/null || true
printf '%s %s %s %s %s\n' \
    "$current_epoch" "$available_kb" "$swap_used_kb" "$chromium_pss_kb" "$backend_pss_kb" \
    >> "$state_temp"
chmod 0644 "$state_temp"
mv -f -- "$state_temp" "$STATE_FILE"
trap - EXIT

sample_count="$(wc -l < "$STATE_FILE")"
chromium_growth_kb=0
backend_growth_kb=0
if ((sample_count >= MINIMUM_SAMPLES)); then
    read -r _ _ _ oldest_chromium_pss_kb oldest_backend_pss_kb < "$STATE_FILE"
    chromium_growth_kb=$((chromium_pss_kb - oldest_chromium_pss_kb))
    backend_growth_kb=$((backend_pss_kb - oldest_backend_pss_kb))
fi

restarted_component=false
if ((sample_count >= MINIMUM_SAMPLES && chromium_pss_kb >= CHROMIUM_LIMIT_KB && chromium_growth_kb >= CHROMIUM_GROWTH_KB)); then
    restart_component lightdm.service "chromium_pss_kb=${chromium_pss_kb},growth_kb=${chromium_growth_kb}"
    restarted_component=true
fi

if ((sample_count >= MINIMUM_SAMPLES && backend_pss_kb >= BACKEND_LIMIT_KB && backend_growth_kb >= BACKEND_GROWTH_KB)); then
    restart_component flightwall.service "backend_pss_kb=${backend_pss_kb},growth_kb=${backend_growth_kb}"
    restarted_component=true
fi

if [[ "$restarted_component" == "true" ]]; then
    printf '%s %s %s %s %s\n' \
        "$current_epoch" "$available_kb" "$swap_used_kb" "$chromium_pss_kb" "$backend_pss_kb" \
        > "$STATE_FILE"
else
    printf 'status=healthy samples=%s available_kb=%s swap_used_kb=%s chromium_pss_kb=%s backend_pss_kb=%s chromium_growth_kb=%s backend_growth_kb=%s\n' \
        "$sample_count" "$available_kb" "$swap_used_kb" "$chromium_pss_kb" "$backend_pss_kb" \
        "$chromium_growth_kb" "$backend_growth_kb"
fi
