#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KVER="$(uname -r)"
INSTALL_MOD_DIR="/lib/modules/${KVER}/updates/cmpunlocker"
INVENTORY_FILE="${INSTALL_MOD_DIR}/gpu_inventory"

source "${SCRIPT_DIR}/common/lib.sh"

is_unlocked_memory() {
    local profile="$1"
    local mem_mib="$2"
    [[ "${mem_mib}" =~ ^[0-9]+$ ]] || return 1
    case "${profile}" in
        8gb)
            (( mem_mib >= 60000 )) && return 0
            ;;
        10gb)
            # Any unlocked size counts, including a fuse-burned card whose
            # dynamic geometry exceeds the 40 GB profile table.
            (( mem_mib >= 35000 )) && return 0
            ;;
    esac
    return 1
}

is_stock_memory() {
    local profile="$1"
    local mem_mib="$2"
    [[ "${mem_mib}" =~ ^[0-9]+$ ]] || return 1
    case "${profile}" in
        8gb)
            (( mem_mib >= 7680 && mem_mib <= 8704 )) && return 0
            ;;
        10gb)
            (( mem_mib >= 9728 && mem_mib <= 10752 )) && return 0
            ;;
    esac
    return 1
}

banner
step_init 4

step "Locating GPU inventory"
command -v nvidia-smi &>/dev/null || die "nvidia-smi not found"
SMI_MEM_CACHE="$(nvidia-smi --query-gpu=pci.bus_id,memory.total --format=csv,noheader,nounits 2>/dev/null || true)"
[[ -n "${SMI_MEM_CACHE}" ]] || die "nvidia-smi returned no GPU memory data"

GPU_BDFS=()
GPU_DEVIDS=()
GPU_PROFILES=()
GPU_EXPECTED=()

if [[ -r "${INVENTORY_FILE}" ]] && [[ -s "${INVENTORY_FILE}" ]]; then
    info "Using inventory: ${INVENTORY_FILE}"
    while read -r bdf devid profile expected || [[ -n "${bdf:-}" ]]; do
        [[ -n "${bdf:-}" ]] || continue
        [[ "${bdf}" =~ ^# ]] && continue
        GPU_BDFS+=("$(normalize_bus_id "${bdf}")")
        GPU_DEVIDS+=("${devid}")
        GPU_PROFILES+=("${profile}")
        GPU_EXPECTED+=("${expected}")
    done < "${INVENTORY_FILE}"
else
    info "No installed gpu_inventory; enumerating via lspci"
    mapfile -t PCI_LINES < <(lspci -nn 2>/dev/null | grep -iE '10de:20c2|10de:2082' || true)
    [[ ${#PCI_LINES[@]} -gt 0 ]] || die "No unlockable CMP 170HX GPU found (10de:20c2 / 10de:2082)"
    for PCI_LINE in "${PCI_LINES[@]}"; do
        PCI="$(echo "${PCI_LINE}" | awk '{print $1}')"
        PCI_FULL="$(normalize_bus_id "${PCI}")"
        DEVID="$(echo "${PCI_LINE}" | grep -oE '10de:[0-9a-fA-F]{4}' | head -1 | cut -d: -f2 | tr '[:upper:]' '[:lower:]')"
        PROF="$(profile_from_devid "${DEVID}")"
        [[ "${PROF}" != "unsupported" ]] || continue
        EXP="$(expected_mib_for_profile "${PROF}")"
        GPU_BDFS+=("${PCI_FULL}")
        GPU_DEVIDS+=("${DEVID}")
        GPU_PROFILES+=("${PROF}")
        GPU_EXPECTED+=("${EXP}")
    done
fi

[[ ${#GPU_BDFS[@]} -gt 0 ]] || die "No unlockable GPUs to verify"

step "Checking memory unlock status"
failures=0
printf "\n%-16s %-8s %-8s %-12s %-12s %s\n" "BDF" "PCI ID" "Variant" "Expect" "Actual" "Status"
for i in "${!GPU_BDFS[@]}"; do
    bdf="${GPU_BDFS[$i]}"
    devid="${GPU_DEVIDS[$i]}"
    profile="${GPU_PROFILES[$i]}"
    expected="${GPU_EXPECTED[$i]}"
    actual="$(smi_memory_for_bus "${bdf}" || true)"
    [[ -n "${actual}" ]] || actual="?"

    status="FAIL"
    if is_unlocked_memory "${profile}" "${actual}"; then
        status="OK"
        ok "${bdf}: ${actual} MiB (unlocked ${profile})"
    elif is_stock_memory "${profile}" "${actual}"; then
        status="STOCK"
        err "${bdf}: still stock ${actual} MiB (expect ~${expected})"
        failures=$((failures + 1))
    elif [[ "${actual}" == "?" ]]; then
        status="MISSING"
        err "${bdf}: not found in nvidia-smi"
        failures=$((failures + 1))
    else
        status="UNEXPECTED"
        err "${bdf}: unexpected ${actual} MiB (expect ~${expected} for ${profile})"
        failures=$((failures + 1))
    fi

    printf "%-16s %-8s %-8s ~%-11s %-12s %s\n" "${bdf}" "${devid}" "${profile}" "${expected}" "${actual}" "${status}"
done

step "Checking unlock logs and installed profile"
sec2_logs="$(dmesg 2>/dev/null | grep 'SEC2_DEBUG' || true)"
if [[ -n "${sec2_logs}" ]]; then
    ok "dmesg contains SEC2_DEBUG unlock logs"
    info "Sample:"
    printf '%s\n' "${sec2_logs}" | tail -n 8 | sed 's/^/  /'
else
    warn "No SEC2_DEBUG lines in dmesg (logs may have rotated; unlock can still be OK if memory is unlocked)"
fi

dyn_logs="$(dmesg 2>/dev/null | grep -E 'CMP-DYN|CMP-BAR1' || true)"
if [[ -n "${dyn_logs}" ]]; then
    ok "dmesg contains CMP-DYN per-card decisions:"
    printf '%s\n' "${dyn_logs}" | tail -n 12 | sed 's/^/  /'

    # DECISION is the last word on what a card ended up with: it reads the layout
    # back from CFG1/LMR instead of reporting what the driver meant to write.
    wide="$(printf '%s\n' "${dyn_logs}" | grep -c 'decision=wide-layout' || true)"
    back="$(printf '%s\n' "${dyn_logs}" | grep -c 'stepping back to stage=' || true)"
    bar1="$(printf '%s\n' "${dyn_logs}" | grep -c 'CMP-BAR1' || true)"
    if (( wide > 0 )); then
        ok "${wide} card(s) booted with the wide (80 GiB) layout"
    else
        info "no card took the wide layout: no card reported the spare FBP pair, or"
        info "this build was installed with --no-80g and has the path compiled out"
    fi
    if (( back > 0 )); then
        warn "the booter gave up on a layout ${back} time(s) and stepped back; that"
        warn "stage is recorded in the registry, so the next init starts one lower"
        warn "the card is alive on a smaller framebuffer rather than dead"
    fi
    if (( bar1 > 0 )); then
        info "${bar1} CMP-BAR1 line(s): forced static BAR1 was skipped for those"
        info "cards (framebuffer larger than BAR1), so BAR1 P2P is off for them"
    fi
else
    info "No CMP-DYN lines in dmesg (driver predates dynamic unlock, or logs rotated)"
fi

# What the link actually runs at is a separate question from the memory layout, and
# neither nvidia-smi nor the driver's own SKU view answers it: the bridge does.  The
# per-card table is printed below, after the memory verdict, so one failing check
# does not hide the other.
link_logs="$(dmesg 2>/dev/null | grep -E 'CMP Gen[0-9]:' || true)"
if [[ -n "${link_logs}" ]]; then
    ok "dmesg contains CMP PCIe link bring-up:"
    printf '%s\n' "${link_logs}" | tail -n 6 | sed 's/^/  /'
fi
if [[ -r /sys/module/nvidia/parameters/cmp_pcie_gen ]]; then
    info "cmp_pcie_gen=$(cat /sys/module/nvidia/parameters/cmp_pcie_gen)" \
         "(0 asks each card for what its own fuses allow)"
fi

echo ""
if [[ -r "${INSTALL_MOD_DIR}/card_profile" ]]; then
    info "Installed profile: $(cat "${INSTALL_MOD_DIR}/card_profile") / geometry: $(cat "${INSTALL_MOD_DIR}/unlock_geometry" 2>/dev/null || echo '?')"
fi
P2P_BUILT="$(cat "${INSTALL_MOD_DIR}/p2p_enabled" 2>/dev/null || true)"
case "${P2P_BUILT}" in
    ''|0|off|none) ;;
    *)
        info "Installed modules were built with ${P2P_BUILT} P2P enabled"
        info "P2P capability matrices (diagnostic only):"
        nvidia-smi topo -p2p r || warn "Could not query P2P read capabilities"
        nvidia-smi topo -p2p w || warn "Could not query P2P write capabilities"
        if [[ "${P2P_BUILT}" == "mailbox" ]]; then
            # On this path the caps are an override, guarded by one fused register,
            # and a mailbox the trap failed to program reports success while it
            # moves garbage. The matrix is therefore worth nothing here; the bytes
            # and the two CMP-P2P lines in dmesg are what say whether it works.
            warn "mailbox P2P: expect 'CMP-P2P ... trap31 armed on NV_P2P' in dmesg,"
            warn "and check copied bytes, not return codes: tools/p2p-content-check.py"
        else
            warn "These matrices do not verify peer transfers; run the test in docs/P2P.md."
        fi
        ;;
esac

if (( failures > 0 )); then
    echo ""
    die "${failures} GPU(s) failed unlock verification. Cold reboot if modules were just installed."
fi

echo ""
ok "All ${#GPU_BDFS[@]} unlockable GPU(s) report unlocked memory"

# The link, per card.  Gen2 is the floor every CMP card has to clear. Above Gen2 the
# target is per card: the card's fuses state what it may request and the slot states
# what it carries, so a card at Gen4 is reported as Gen4 and a card held at Gen2 by a
# Gen3 bridge is not a failure.  This check was previously provided by
# tools/service.sh, the installer for the boot-time retrain unit; the driver performs
# that retrain at init, and this section is what remains of that script.
echo ""
step "Checking the negotiated PCIe link"
link_failures=0
printf '%-16s %-16s %-10s %-14s %-8s %s\n' \
       "GPU" "Upstream" "LnkSta" "Current speed" "Width" "Result"
for sysdev in /sys/bus/pci/devices/*/device; do
    device="$(cat "${sysdev}" 2>/dev/null || true)"
    [[ "${device}" == "0x20c2" || "${device}" == "0x2082" ]] || continue
    card="${sysdev%/device}"
    bdf="$(basename "${card}")"
    bridge="$(basename "$(dirname "$(readlink -f "${card}")")")"
    speed="$(cat "${card}/current_link_speed" 2>/dev/null || echo unknown)"
    width="$(cat "${card}/current_link_width" 2>/dev/null || echo unknown)"
    # setpci needs root for config space, so the raw register is a bonus and sysfs is
    # the fallback; LnkSta wins when both are available.
    status="$(setpci -s "${bdf}" CAP_EXP+12.w 2>/dev/null || true)"
    if [[ "${status}" =~ ^[0-9a-fA-F]{4}$ ]]; then
        generation=$((0x${status} & 0x0f))
        shown="0x${status}"
    else
        shown="n/a"
        case "${speed}" in
            2.5*) generation=1 ;;  5.0*)  generation=2 ;; 8.0*)  generation=3 ;;
            16.0*) generation=4 ;; 32.0*) generation=5 ;; 64.0*) generation=6 ;;
            *) generation="" ;;
        esac
    fi
    if [[ "${generation}" =~ ^[0-9]+$ ]] && (( generation >= 2 )); then
        result="GEN${generation} OK"
    else
        result="below Gen2"
        link_failures=$((link_failures + 1))
    fi
    printf '%-16s %-16s %-10s %-14s x%-7s %s\n' \
           "${bdf}" "${bridge}" "${shown}" "${speed}" "${width}" "${result}"
done
if (( link_failures > 0 )); then
    warn "Memory unlock is healthy, but ${link_failures} card(s) did not reach Gen2."
    warn "sudo /usr/local/sbin/gen2-hammer retrains by hand; docs/BOOT-RECOVERY.zh-CN.md"
    warn "says what to read before blaming the card (LnkSta at both ends, not nvidia-smi)."
    exit 1
fi
ok "Every CMP card is negotiated at Gen2 or better"
exit 0
