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
link_misses=0
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
        # Gen2 is the floor; the ceiling is this card's own speed fuses, and the two
        # are different questions.  A card whose fuses allow Gen4 that ended up on a
        # lower speed is not broken (the port above it may cap it, and a Gen3 slot is
        # a legitimate answer), but it is worth one honest column: that card's
        # bring-up did not reach what the card may ask for, and the driver said why.
        cap2="$(setpci -s "${bdf}" CAP_EXP+2c.l 2>/dev/null || true)"
        if [[ "${cap2}" =~ ^[0-9a-fA-F]{8}$ ]] && (( 0x${cap2} & 0x10 )) &&
           (( generation < 4 )); then
            result="GEN${generation} OK, Gen4 missed"
            link_misses=$((link_misses + 1))
        fi
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
if (( link_misses > 0 )); then
    warn "${link_misses} card(s) carry Gen4 in their fuses and run below it."
    warn "dmesg | grep 'CMP Gen' names the end that capped the link, and says whether"
    warn "the bring-up was never asked (a card left on Gen2 by its own fuse vector)"
    warn "or asked and refused. cmp_pcie_retries walks the ladder again at init; a port"
    warn "whose BIOS pins its speed is not something a driver can argue with."
fi

# Idle power, per card.  Informational, and it stays that way: a card the governor has not
# retimed is a card drawing exactly what it drew before this feature existed, while a card
# the memory unlock lost is a failed install, and the two do not belong in one exit code.
# What is worth reading here is whether anything is running at all, and what it says about
# the last thing it tried.  "refresh access closed" is the one answer that means something
# is wrong rather than merely idle: the masks the governor writes through are opened by the
# driver, on request, and that request is what the key in the modprobe config is.
echo ""
step "Checking the idle HBM governor"
if ! grep -q '^cmp_hbm_pm ' /proc/modules; then
    warn "cmp_hbm_pm is not loaded: an idle card keeps the memory clock it booted with"
    info "sudo tools/hbm-pm-setup.sh builds and starts it; install.sh does that unless --no-hbm-pm"
elif [[ -r /sys/kernel/cmp_hbm_pm/status ]]; then
    # Read once, and with a deadline.  Every check below is made against this text rather
    # than by opening the file again, for two reasons: the file's answers have to agree
    # with each other, and a read that finds the governor's worker list busy says so in
    # the text instead of waiting in the kernel for a card (docs/HBM-PM.md).  A verifier
    # that could sit down in the kernel would be a worse version of the thing it checks.
    hbm_status="$(timeout 15 cat /sys/kernel/cmp_hbm_pm/status 2>&1 || true)"
    hbm_says() { printf '%s\n' "${hbm_status}" | grep -q "$1"; }
    if [[ -z "${hbm_status}" ]]; then
        warn "cmp_hbm_pm is loaded but its status file could not be read at all, within 15 s"
        warn "or otherwise: sudo journalctl -k -b | grep cmp_hbm_pm says what it is doing."
    elif hbm_says 'status unavailable'; then
        warn "The governor's worker list was held for the whole wait, so nothing below was"
        warn "checked.  A perf table read or a policy change is still running; the cards are"
        warn "not necessarily wrong.  Read /sys/kernel/cmp_hbm_pm/status again after it ends."
    else
        while IFS= read -r line; do
            info "  ${line}"
        done <<< "${hbm_status}"
        if hbm_says 'errors [1-9]'; then
            warn "A transition has failed since the module loaded; docs/HBM-PM.md says what each"
            warn "reason means and which of them are a closed mask rather than the card."
        fi
        # The state that costs a card rather than watts.  `halted` means the module asked
        # for the framebuffer and could not hand it back, so it has stopped touching the
        # card -- which is the right thing to have done, and is also a card nobody can
        # use until it is reset.  Nothing else in this file is worth exiting for.
        if hbm_says 'halted'; then
            err "cmp_hbm_pm has halted a card: it stopped that card's framebuffer and could"
            err "not start it again, and will not touch it until the module is reloaded. Save"
            err "'sudo journalctl -k -b | grep cmp_hbm_pm' first, then reset the GPU."
        fi
        # The failure mode the wattmeter cannot show: the governor loaded, and the half of
        # the feature that knows whether anything is using a card is not running, so every
        # card keeps its busy clock forever and looks like a machine with nothing idle on
        # it.  Both directions have to be reported, because both are silent.
        #
        # `stale` is matched with its leading space because that is how the status file
        # prints it -- `lease 74825 ms busy stale clients65` -- and an answer that has aged
        # out is a keeper that stopped answering while the card kept the state it was last
        # told, which is the one case where reading the note alone would be reassuring.
        if hbm_says 'lease none' || hbm_says ' stale'; then
            # "Is the keeper meant to be here" is not the same question as "is it alive
            # this instant".  A keeper that fails every round -- no NVML, no card it can
            # name, a thresholds file it cannot read -- spends most of its life
            # `activating` behind RestartSec, which is precisely the state the three
            # keeper bugs that shipped today lived in, and `is-active` files that away
            # as an absent keeper.  What says somebody asked for this feature is the
            # unit being installed, so that is the gate, and the state is printed
            # rather than used to decide which paragraph the operator deserves.
            keeper_meant=0
            if command -v systemctl >/dev/null 2>&1; then
                if [[ -e /etc/systemd/system/cmp-hbm-lease.service ]] ||
                       systemctl is-enabled --quiet cmp-hbm-lease.service 2>/dev/null; then
                    keeper_meant=1
                fi
            fi
            if [[ "${keeper_meant}" == 1 ]]; then
                # A second read, on purpose: the first one is the record everything
                # above is judged from, this one asks only whether anybody is home,
                # and it waits a round first because a module loaded three seconds
                # ago has not been given the chance to be answered yet.
                #
                # Every card has to be answered, and answered *currently*: a lease is
                # printed with its age and its verdict, so `lease 4775 ms quiet stale`
                # matches a test that only looks for a dated lease and congratulates a
                # keeper that died a minute ago.  This check was written to catch
                # exactly that and did not, until it was run against the failure.
                sleep 3
                hbm_recheck="$(timeout 15 cat /sys/kernel/cmp_hbm_pm/status 2>&1 || true)"
                if ! printf '%s\n' "${hbm_recheck}" | grep -qE '^[0-9a-f]{4}:'; then
                    warn "The keeper could not be checked: /sys/kernel/cmp_hbm_pm/status gave"
                    warn "no card lines this time, and an unreadable status file is not the"
                    warn "same thing as a working keeper.  Read it by hand once the card has"
                    warn "come up."
                elif printf '%s\n' "${hbm_recheck}" | grep -E '^[0-9a-f]{4}:' |
                        grep -qE 'lease none|lease [0-9]+ ms .* stale'; then
                    err "cmp-hbm-lease.service is installed (state: $(systemctl is-active cmp-hbm-lease.service 2>&1), restarted $(systemctl show -p NRestarts --value cmp-hbm-lease.service 2>&1) times) and a card still has no current answer, which is a keeper that cannot read something rather than a machine with an idle card on it."
                    err "journalctl -u cmp-hbm-lease.service -b names the card it could not"
                    err "identify or the reading it could not take.  Three failures like this"
                    err "shipped at once, and every one of them looked like the warning this"
                    err "block otherwise prints: docs/HBM-PM.md, under \"What has been"
                    err "checked\", is the record of that."
                else
                    ok "The keeper is answering: every card holds a dated, unexpired lease"
                fi
            else
                warn "Some card has no current answer from the lease keeper, so it stays at its"
                warn "busy clock: systemctl status cmp-hbm-lease.service, and the keeper's own"
                warn "journal says why it stopped answering.  tools/hbm-pm-setup.sh installs it."
            fi
        elif hbm_says 'lease waived'; then
            warn "lease_ms=0: the governor is deciding to idle a card on its borrowed counters"
            warn "alone.  Those were measured reading nothing for 8.4 s on a card serving"
            warn "requests at 178 W, so this is a bench setting and not a way to run a box."
        fi
        # The other way the two halves of this feature can disagree: the governor is
        # loaded, so the install asked for it, but the driver that is installed never got
        # the key that lets the booter open the masks it writes through.  Reading the
        # status file alone cannot tell that apart from a card that has not come up yet,
        # and the answer is a rebuild rather than a wait, so say which.
        if hbm_says 'refresh access closed' &&
           ! grep -q 'CMPEnableHbmPm=1' /etc/modprobe.d/cmp-pcie-gen2.conf 2>/dev/null; then
            warn "The installed driver was built without CMPEnableHbmPm=1, so the governor"
            warn "cannot open the masks it writes through: sudo install.sh to rebuild both."
        fi
    fi
else
    warn "cmp_hbm_pm is loaded but has written no status file; the firmware is still coming up"
fi
exit 0
