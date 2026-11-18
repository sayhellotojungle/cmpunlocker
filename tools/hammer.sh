#!/bin/bash
set -uo pipefail

PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# Manual PCIe link retrain for CMP 170HX / A16 (10de:20c2, 10de:2082).
#
# This helper is run manually and is not installed as a boot-time unit. The
# Gen1 -> Gen2 step is performed by the driver: nv_cmp170hx_retrain_gen2() runs from
# nv.c's init path for every card and writes BAR0 (CYA_0, LINK_CONFIG_0) before
# changing LNKCTL2 at both ends of the link. BAR0 is mapped by the driver, so a
# userspace helper cannot perform that step. The unit that used to arm this helper
# ran after the driver; with that unit disabled, the 2026-11-18 bench boot produced
# LnkSta=0x1102 on the two soft-unlocked cards and 0x1104 on the fused card, with no
# userspace write to the link, which is the measurement that removed the unit.
#
# Usage, as root:
#
#   gen2-hammer                                  every card to its floor (Gen2)
#   CMP170HX_GEN2_TARGET=3 gen2-hammer           every card to at least Gen3
#   CMP170HX_GEN2_POLICY=/path/to/file gen2-hammer
#                                                per-card targets, "<BDF> <gen>"
#
# The target is a floor. A link already above the target is left unchanged, so
# running this after the driver has trained a card to Gen4 leaves it at Gen4.

readonly VENDOR_ID="10de"
readonly LOG_FILE="/var/log/gen2.log"
readonly POLICY_FILE="${CMP170HX_GEN2_POLICY:-/etc/cmpunlocker/gen-policy}"
readonly TARGET_OVERRIDE="${CMP170HX_GEN2_TARGET:-auto}"
readonly MAX_ITERATIONS="${CMP170HX_GEN2_MAX_ITERATIONS:-20}"
readonly RETRAIN_INTERVAL="${CMP170HX_GEN2_RETRAIN_INTERVAL:-0.25}"

log() {
    local line
    line="[$(date -Is)] $*"
    echo "${line}"
    echo "${line}" >> "${LOG_FILE}" 2>/dev/null || true
}

read_link_status() {
    setpci -s "$1" CAP_EXP+12.w 2>/dev/null || true
}

link_generation() {
    local status
    status="$(read_link_status "$1")"
    if [[ "${status}" =~ ^[[:xdigit:]]{4}$ ]]; then
        echo $((0x${status} & 0x0f))
    else
        echo "?"
    fi
}

# Highest generation the device itself advertises (LnkCap bits 3:0).  This is the
# card's own firmware view, i.e. its fuse-programmed speed limits, which is not the
# same question as what the slot can carry -- see target_for().
advertised_generation() {
    local cap
    cap="$(setpci -s "$1" CAP_EXP+0c.l 2>/dev/null || true)"
    if [[ "${cap}" =~ ^[[:xdigit:]]{8}$ ]]; then
        echo $((0x${cap} & 0x0f))
    else
        echo "?"
    fi
}

policy_generation() {
    [[ -r "${POLICY_FILE}" ]] || return 1
    local gen
    gen="$(awk -v bdf="$1" 'tolower($1)==tolower(bdf) {print $2; exit}' "${POLICY_FILE}")"
    case "${gen}" in
        2|3|4) echo "${gen}" ;;
        *) return 1 ;;
    esac
}

# Per-card target: explicit pin > policy file > Gen2.  The floor this helper can
# hold on its own is Gen2; above that the link needs the PHY-init sequence the
# driver runs (driver/dynfb), so a card below a Gen3/Gen4 target is reported, not
# hammered.  LnkCap does not prove PHY initialization, which is why the default
# here stays Gen2 whatever the card advertises.
target_for() {
    local gpu="$1" gen
    case "${TARGET_OVERRIDE}" in
        2|3|4) echo "${TARGET_OVERRIDE}"; return ;;
        auto) : ;;
        *)
            echo "CMP170HX_GEN2_TARGET must be 2, 3, 4 or auto (got ${TARGET_OVERRIDE})" >&2
            exit 1
            ;;
    esac
    if gen="$(policy_generation "${gpu}")"; then
        echo "${gen}"
        return
    fi
    echo 2
}

is_supported_gpu() {
    local bdf="$1"
    local vendor device
    [[ -r "/sys/bus/pci/devices/${bdf}/vendor" ]] || return 1
    [[ -r "/sys/bus/pci/devices/${bdf}/device" ]] || return 1
    vendor="$(<"/sys/bus/pci/devices/${bdf}/vendor")"
    device="$(<"/sys/bus/pci/devices/${bdf}/device")"
    [[ "${vendor,,}" == "0x${VENDOR_ID}" ]] || return 1
    [[ "${device,,}" == "0x20c2" || "${device,,}" == "0x2082" ]]
}

upstream_bridge() {
    local bdf="$1"
    local parent
    parent="$(basename "$(dirname "$(readlink -f "/sys/bus/pci/devices/${bdf}")")")"
    [[ "${parent}" != "${bdf}" ]] || return 1
    [[ -e "/sys/bus/pci/devices/${parent}" ]] || return 1
    echo "${parent}"
}

is_pci_bridge() {
    local bdf="$1"
    local class
    [[ -r "/sys/bus/pci/devices/${bdf}/class" ]] || return 1
    class="$(<"/sys/bus/pci/devices/${bdf}/class")"
    [[ "${class,,}" == 0x0604* ]]
}

# LnkSta layout (PCIe cap + 0x12, PCIe Base 4.0 7.5.3.14): bits 3:0 current speed,
# bits 9:4 negotiated width, bit 11 Link Training, bit 12 Slot Clock Configuration,
# bit 13 Data Link Layer Link Active, bit 14 Link Bandwidth Management Status, bit 15
# Link Autonomous Bandwidth Status.  The last two are the write-1-to-clear event
# bits: a retrain *sets* them, so they say "the link changed", not "the link is still
# training".  Bit 11 is the one that says the latter and it reads clear once the link
# has settled, which is what makes "active at the target speed with a non-zero width
# and not still training" a condition this helper can actually observe.
#
# These bits are not academic, and they were wrong here: the event bits were read as
# 11/12 (so the acknowledge wrote two read-only bits), and DL_Active was read as bit
# 15.  Measured on the bench, on a link carrying traffic, LnkSta is 0x1102 at the GPU
# and the same at its upstream bridge -- Gen2, x16, slot clock configured, and no
# bits 13, 14 or 15.  Requiring bit 15 therefore made link_is_up_at return false for
# every card on this box, forever: each boot retrained all three, none was ever
# reported as a success, and the card the driver had just brought up at Gen4 was
# pulled back down to Gen2 in the same second it got there.
readonly LINKSTA_SPEED_MASK="0x000f"
readonly LINKSTA_WIDTH_MASK="0x03f0"
readonly LINKSTA_EVENT_MASK="0x6000"
readonly LINKSTA_TRAINING_MASK="0x0800"

clear_link_events() {
    setpci -s "$1" "CAP_EXP+12.w=${LINKSTA_EVENT_MASK}:${LINKSTA_EVENT_MASK}" \
        >/dev/null 2>&1 || true
}

# link_is_up_at <bdf> <min generation>: the one condition a retrain has to meet.
# A link that already carries at least the target speed, at a non-zero width, and is
# not mid-training, needs nothing from us -- which is what makes the target a floor
# rather than a ceiling, and leaves a Gen4 link alone on the way to Gen2 being enough.
link_is_up_at() {
    local status
    status="$(read_link_status "$1")"
    [[ "${status}" =~ ^[[:xdigit:]]{4}$ ]] || return 1
    (( (0x${status} & LINKSTA_TRAINING_MASK) == 0 )) &&
        (( (0x${status} & LINKSTA_WIDTH_MASK) != 0 )) &&
        (( (0x${status} & LINKSTA_SPEED_MASK) >= "$2" )) &&
        (( (0x${status} & LINKSTA_SPEED_MASK) <= 6 ))
}

retrain_one() {
    local gpu="$1" target="$2"
    local bridge status generation cap2 iteration initial target_word
    if ! is_supported_gpu "${gpu}"; then
        log "${gpu}: vendor/device guard failed; refusing to touch the device"
        return 1
    fi

    initial="$(link_generation "${gpu}")"
    if link_is_up_at "${gpu}" "${target}"; then
        clear_link_events "${gpu}"
        log "${gpu}: already Gen${initial}; no retrain needed (target Gen${target})"
        return 0
    fi

    # Above Gen2 the link needs the PHY-init sequence the driver runs (dynfb),
    # which this helper cannot replay; ask for it only where the driver was told
    # to provide it, and report the miss instead of hammering at it.
    if [[ "${target}" != 2 ]]; then
        log "${gpu}: Gen${initial} below target Gen${target}; the driver's Gen4 sequence owns that retrain (dmesg | grep 'CMP Gen')"
        return 1
    fi

    bridge="$(upstream_bridge "${gpu}" || true)"
    if [[ -z "${bridge}" ]] || ! is_pci_bridge "${bridge}"; then
        log "${gpu}: no valid upstream PCI bridge found; skipping"
        return 1
    fi

    target_word="$(printf '%04x' "${target}")"
    log "${gpu}: start via upstream ${bridge}; initial Gen${initial}; target Gen${target}"
    for ((iteration = 1; iteration <= MAX_ITERATIONS; iteration++)); do
        if ! is_supported_gpu "${gpu}"; then
            log "${gpu}: disappeared during retrain; stopping immediately"
            return 1
        fi

        status="$(read_link_status "${gpu}")"
        if ! [[ "${status}" =~ ^[[:xdigit:]]{4}$ ]] || [[ "${status,,}" == ffff ]]; then
            log "${gpu}: unreadable link status; stopping"
            return 1
        fi
        # Acknowledge the previous attempt's event bits before asking again, so
        # the next read says something about this one.
        clear_link_events "${gpu}"
        if ! setpci -s "${bridge}" CAP_EXP+30.w="${target_word}:000f" ||
           ! setpci -s "${gpu}" CAP_EXP+30.w="${target_word}:000f" ||
           ! setpci -s "${bridge}" CAP_EXP+10.w=0020:0020; then
            log "${gpu}: PCI config write failed; stopping"
            return 1
        fi
        sleep "${RETRAIN_INTERVAL}"

        status="$(read_link_status "${gpu}")"
        if [[ "${status}" =~ ^[[:xdigit:]]{4}$ ]]; then
            generation=$((0x${status} & LINKSTA_SPEED_MASK))
            if link_is_up_at "${gpu}" "${target}"; then
                cap2="$(setpci -s "${gpu}" CAP_EXP+2c.l 2>/dev/null || echo unreadable)"
                log "${gpu}: SUCCESS Gen${generation} at iteration ${iteration}; LnkSta=0x${status}; LnkCap2=0x${cap2}"
                return 0
            fi
        fi
    done

    generation="$(link_generation "${gpu}")"
    log "${gpu}: no Gen${target} window caught after ${MAX_ITERATIONS} attempts; final Gen${generation}"
    return 1
}

main() {
    local rc=0
    local -a gpus=()

    if [[ "${EUID}" -ne 0 ]]; then
        echo "gen2-hammer must run as root" >&2
        exit 1
    fi
    if ! [[ "${MAX_ITERATIONS}" =~ ^[1-9][0-9]*$ ]] || (( MAX_ITERATIONS > 20 )); then
        echo "CMP170HX_GEN2_MAX_ITERATIONS must be between 1 and 20" >&2
        exit 1
    fi
    if ! [[ "${RETRAIN_INTERVAL}" =~ ^(0\.[0-9]+|1(\.0+)?)$ ]] ||
       ! awk -v n="${RETRAIN_INTERVAL}" 'BEGIN {exit !(n >= 0.1 && n <= 1)}'; then
        echo "CMP170HX_GEN2_RETRAIN_INTERVAL must be between 0.1 and 1 seconds" >&2
        exit 1
    fi
    command -v lspci >/dev/null || { echo "lspci is required" >&2; exit 1; }
    command -v setpci >/dev/null || { echo "setpci is required" >&2; exit 1; }

    : > "${LOG_FILE}"
    log "retrain pass started (attempts=${MAX_ITERATIONS}, interval=${RETRAIN_INTERVAL}s, mode=${TARGET_OVERRIDE}, policy=${POLICY_FILE})"

    mapfile -t gpus < <(
        for id in 20c2 2082; do
            lspci -D -d "${VENDOR_ID}:${id}" 2>/dev/null | awk '{print $1}'
        done
    )
    if [[ ${#gpus[@]} -eq 0 ]]; then
        log "no supported CMP 170HX found; nothing touched"
        return 0
    fi

    for gpu in "${gpus[@]}"; do
        retrain_one "${gpu}" "$(target_for "${gpu}")" || rc=1
    done
    log "retrain pass finished (rc=${rc})"
    return "${rc}"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
