#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mapfile -t SUPPORTED_VERSIONS < <(grep -E '^[0-9]+\.[0-9]+\.[0-9]+$' "${SCRIPT_DIR}/driver/VERSION")
SUPPORTED_VERSIONS_CSV="$(IFS=', '; echo "${SUPPORTED_VERSIONS[*]}")"
LOG_DIR="${SCRIPT_DIR}/logs"
mkdir -p "${LOG_DIR}"
LOG_FILE="${LOG_DIR}/install_$(date +%Y%m%d_%H%M%S).log"

PROFILE_OVERRIDE=""
P2P_MODE=off
CONFIGURE_IOMMU=1
CONFIGURE_PASSTHROUGH=1
DEPRECATED_GEN2_FLAG=0
for arg in "$@"; do
    case "${arg}" in
        --profile=8gb|--profile=8GB) PROFILE_OVERRIDE="8gb" ;;
        --profile=10gb|--profile=10GB) PROFILE_OVERRIDE="10gb" ;;
        --p2p) P2P_MODE="bar1" ;;
        --p2p=*)
            P2P_MODE="${arg#*=}"
            case "${P2P_MODE}" in
                off|bar1|mailbox) ;;
                *)
                    echo "Unknown --p2p value: ${arg#*=} (use off, bar1 or mailbox)" >&2
                    exit 1
                    ;;
            esac
            ;;
        --no-iommu) CONFIGURE_IOMMU=0 ;;
        --no-gen2-service) DEPRECATED_GEN2_FLAG=1 ;;
        --no-passthrough) CONFIGURE_PASSTHROUGH=0 ;;
        -h|--help)
            cat <<'EOF'
Usage: sudo ./install.sh [--profile=8gb|10gb] [--no-iommu]
                        [--no-passthrough] [--p2p=bar1|mailbox|off]

  --profile=8gb   Force 8GB metadata label (geometry is still chosen per PCI ID)
  --profile=10gb  Force 10GB metadata label (geometry is still chosen per PCI ID)
  --p2p          Same as --p2p=bar1.
  --p2p=bar1     GPU-to-GPU P2P through a static BAR1 that maps the whole
                  framebuffer, with the copy engine DMAing into the peer. Needs a
                  BAR1 at least as large as each card's framebuffer, and the card's
                  resizable-BAR capability table stops at 64 GiB, so a card on the
                  80 GiB layout cannot take this path.
  --p2p=mailbox  GPU-to-GPU P2P through the NV_P2P write-mailbox aperture instead:
                  64 KiB per peer at the bottom of BAR1 and no framebuffer mapping,
                  which is what leaves it as the route for a card whose framebuffer
                  is bigger than any BAR1 it can be given. Needs the PRI decode
                  trap that the closed GSP-RM is denied for those registers to be
                  armable; where it is not, the driver leaves P2P unsupported rather
                  than reporting copies that move garbage. Measured at Gen2 only.
                  See docs/P2P.md before choosing.
  --p2p=off      No P2P overrides (the default).
                  Whichever path: a capability report is not a transfer, so verify
                  real peer copies as docs/P2P.md describes.
  --no-iommu      Do not touch the kernel command line (leave IOMMU settings alone)
  --no-gen2-service
                  Accepted and ignored. There is no boot-time retrain service any
                  more: the driver retrains the link at init and an upgrade from an
                  install that had one gets that unit disarmed. The manual helper
                  stays installed at /usr/local/sbin/gen2-hammer.
  --no-passthrough
                  Do not set the cards up for VM passthrough. By default the
                  unlock is made to survive being handed to vfio-pci, so a VM
                  sees an unlocked card with only a stock NVIDIA driver in it

By default the installer appends intel_iommu=on / amd_iommu=on plus iommu=pt to
the kernel command line so the IOMMU runs in passthrough mode. This takes effect
on the next reboot.

Without --profile, each unlockable GPU is classified by PCI device ID:
  10de:20c2 → 8gb / 64GB unlock
  10de:2082 → 10gb / 40GB unlock

Multi-GPU and mixed 8GB+10GB systems are supported in one install.
EOF
            exit 0
            ;;
        *)
            echo "Unknown argument: ${arg}" >&2
            echo "Try: sudo ./install.sh --help" >&2
            exit 1
            ;;
    esac
done

exec > >(tee -a "${LOG_FILE}") 2>&1

source "${SCRIPT_DIR}/common/lib.sh"

banner
step_init 7

step "Verifying root privileges"
[[ "${EUID}" -eq 0 ]] || die "Run as root: sudo ./install.sh"
ok "Running as root"

step "Detecting CMP 170HX GPU(s)"
mapfile -t PCI_LINES < <(lspci -nn 2>/dev/null | grep -iE '10de:20b0|10de:20c2|10de:2082' || true)
[[ ${#PCI_LINES[@]} -gt 0 ]] || die "No CMP 170HX GPU found (10de:20b0 / 10de:20c2 / 10de:2082)"

SMI_MEM_CACHE=""
if command -v nvidia-smi &>/dev/null; then
    SMI_MEM_CACHE="$(nvidia-smi --query-gpu=pci.bus_id,memory.total --format=csv,noheader,nounits 2>/dev/null || true)"
fi

GPU_BDFS=()
GPU_DEVIDS=()
GPU_PROFILES=()
GPU_EXPECTED=()
GPU_CURRENT=()
COUNT_8GB=0
COUNT_10GB=0
COUNT_UNSUPPORTED=0

for PCI_LINE in "${PCI_LINES[@]}"; do
    PCI="$(echo "${PCI_LINE}" | awk '{print $1}')"
    PCI_FULL="$(normalize_bus_id "${PCI}")"
    DEVID="$(echo "${PCI_LINE}" | grep -oE '10de:[0-9a-fA-F]{4}' | head -1 | cut -d: -f2 | tr '[:upper:]' '[:lower:]')"
    PROF="$(profile_from_devid "${DEVID}")"
    CUR_MEM="$(smi_memory_for_bus "${PCI_FULL}" || true)"
    [[ -n "${CUR_MEM}" ]] || CUR_MEM="?"

    if [[ "${PROF}" == "unsupported" ]]; then
        COUNT_UNSUPPORTED=$((COUNT_UNSUPPORTED + 1))
        warn "GPU ${PCI_FULL} (10de:${DEVID}) — unlock path not gated for this ID; skipping"
        continue
    fi

    EXP="$(expected_mib_for_profile "${PROF}")"
    GPU_BDFS+=("${PCI_FULL}")
    GPU_DEVIDS+=("${DEVID}")
    GPU_PROFILES+=("${PROF}")
    GPU_EXPECTED+=("${EXP}")
    GPU_CURRENT+=("${CUR_MEM}")

    if [[ "${PROF}" == "8gb" ]]; then
        COUNT_8GB=$((COUNT_8GB + 1))
    else
        COUNT_10GB=$((COUNT_10GB + 1))
    fi

    if [[ "${CUR_MEM}" != "?" ]]; then
        ok "GPU ${PCI_FULL} (10de:${DEVID}) → ${PROF} (current ${CUR_MEM} MiB, expect ~${EXP} MiB unlocked)"
    else
        ok "GPU ${PCI_FULL} (10de:${DEVID}) → ${PROF} (expect ~${EXP} MiB unlocked)"
    fi
done

[[ ${#GPU_BDFS[@]} -gt 0 ]] || die "No unlockable CMP 170HX GPUs found (need 10de:20c2 and/or 10de:2082)"
if (( COUNT_UNSUPPORTED > 0 )); then
    info "Inventory: ${#GPU_BDFS[@]} unlockable (${COUNT_8GB}× 8gb, ${COUNT_10GB}× 10gb), ${COUNT_UNSUPPORTED} unsupported"
else
    info "Inventory: ${#GPU_BDFS[@]} unlockable (${COUNT_8GB}× 8gb, ${COUNT_10GB}× 10gb)"
fi

step "Selecting card memory profile"
CARD_PROFILE=""
if (( COUNT_8GB > 0 && COUNT_10GB > 0 )); then
    CARD_PROFILE="mixed"
    ok "Mixed variants detected → profile mixed (runtime geometry by PCI ID)"
    if [[ -n "${PROFILE_OVERRIDE}" ]]; then
        warn "--profile=${PROFILE_OVERRIDE} ignored for mixed inventory; card_profile stays mixed (each card unlocks by PCI ID)"
    fi
elif (( COUNT_8GB > 0 )); then
    CARD_PROFILE="8gb"
elif (( COUNT_10GB > 0 )); then
    CARD_PROFILE="10gb"
else
    die "Internal error: no unlockable profiles counted"
fi

if [[ -n "${PROFILE_OVERRIDE}" && "${CARD_PROFILE}" != "mixed" ]]; then
    if [[ "${PROFILE_OVERRIDE}" != "${CARD_PROFILE}" ]]; then
        warn "Inventory is ${CARD_PROFILE} but --profile=${PROFILE_OVERRIDE} was forced (metadata only; geometry follows PCI ID)"
    else
        ok "Profile forced via --profile=${CARD_PROFILE}"
    fi
    CARD_PROFILE="${PROFILE_OVERRIDE}"
fi

case "${CARD_PROFILE}" in
    8gb)
        info "Unlock geometry: 64GB per card (CFG1=0x02779000 LMR=0x0000020B)"
        ;;
    10gb)
        info "Unlock geometry: 40GB per card (CFG1=0x02669000 LMR=0x0000028A)"
        ;;
    mixed)
        info "Unlock geometry: 64GB for 20c2 / 40GB for 2082"
        ;;
    *)
        die "Internal error: bad profile ${CARD_PROFILE}"
        ;;
esac

GPU_INVENTORY_LINES=()
for i in "${!GPU_BDFS[@]}"; do
    GPU_INVENTORY_LINES+=("${GPU_BDFS[$i]} ${GPU_DEVIDS[$i]} ${GPU_PROFILES[$i]} ${GPU_EXPECTED[$i]}")
done
export CMPUNLOCKER_CARD_PROFILE="${CARD_PROFILE}"
CMPUNLOCKER_GPU_INVENTORY="$(printf '%s\n' "${GPU_INVENTORY_LINES[@]}")"
export CMPUNLOCKER_GPU_INVENTORY

step "Verifying nvidia-open (${SUPPORTED_VERSIONS_CSV})"
[[ ${#SUPPORTED_VERSIONS[@]} -gt 0 ]] || die "No supported versions listed in driver/VERSION"
if [[ -d /sys/firmware/efi ]] && command -v mokutil &>/dev/null; then
    # No pipeline here on purpose: grep -q would close the pipe early, and with
    # pipefail set that can read as "Secure Boot is off" on a machine where it is on.
    sb_state="$(mokutil --sb-state 2>/dev/null || true)"
    if [[ "${sb_state}" == *"SecureBoot enabled"* ]]; then
        die "Secure Boot is enabled. Disable it before installing unsigned patched modules."
    fi
fi

version_supported() {
    local v="$1"
    local s
    for s in "${SUPPORTED_VERSIONS[@]}"; do
        [[ "${v}" == "${s}" ]] && return 0
    done
    return 1
}

detected=""
if [[ -r /proc/driver/nvidia/version ]]; then
    detected="$(grep -oE '[0-9]+\.[0-9]+\.[0-9]+' /proc/driver/nvidia/version | head -1 || true)"
fi
if [[ -z "${detected}" ]] && command -v nvidia-smi &>/dev/null; then
    smi_version="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1 | tr -d '[:space:]' || true)"
    [[ "${smi_version}" =~ ^[0-9]+(\.[0-9]+)+$ ]] && detected="${smi_version}"
fi
if [[ -z "${detected}" ]]; then
    for cand in "${SUPPORTED_VERSIONS[@]}"; do
        if [[ -d "/lib/firmware/nvidia/${cand}" ]]; then
            detected="${cand}"
            break
        fi
    done
    if [[ -z "${detected}" ]]; then
        fw="$(ls -d /lib/firmware/nvidia/*/ 2>/dev/null | sed 's|.*/nvidia/||;s|/||' | sort -rV | head -1 || true)"
        detected="${fw}"
    fi
fi

[[ -n "${detected}" ]] || die "Could not detect an installed NVIDIA driver. Install nvidia-open ${SUPPORTED_VERSIONS_CSV} first."
version_supported "${detected}" || die "Installed driver is ${detected}, but cmpunlocker requires one of: ${SUPPORTED_VERSIONS_CSV}."
ok "NVIDIA driver ${detected} is supported"

[[ -d "/lib/modules/$(uname -r)/build" ]] || die "Kernel headers missing for $(uname -r). Install linux-headers-$(uname -r) or kernel-devel."
ok "Kernel headers present for $(uname -r)"

info "Removing conflicting NVIDIA DKMS modules (not all systems have any)"
for ver in "${SUPPORTED_VERSIONS[@]}"; do
    dkms remove nvidia/"${ver}" --all 2>/dev/null || true
done
depmod -a "$(uname -r)"
ok "DKMS conflicting modules resolution complete"

step "Building and installing patched modules"
if [[ "${P2P_MODE}" == "bar1" ]]; then
    info "Enabling GPU-to-GPU BAR1 P2P"
    warn "P2P capability reports alone do not prove transfers work."
    warn "Check BAR1 on every GPU and run the transfer test in docs/P2P.md."
elif [[ "${P2P_MODE}" == "mailbox" ]]; then
    info "Enabling GPU-to-GPU mailbox P2P (no large BAR1 needed)"
    warn "This route lives or dies on one fused register: watch for the CMP-P2P"
    warn "lines in dmesg, and check copied bytes, not just cuMemcpyPeer's return."
    warn "tools/p2p-content-check.py is what catches a mailbox that was never armed."
else
    info "P2P overrides disabled (use --p2p=bar1 or --p2p=mailbox to enable)"
fi
if [[ "${CMPUNLOCKER_EXPERIMENTAL_GEN4:-0}" == "1" ]]; then
    info "Asking each card for the PCIe speed its own fuses allow (cmp_pcie_gen=0)"
else
    info "Every card stays at Gen2 (CMPUNLOCKER_EXPERIMENTAL_GEN4=1 asks per card)"
fi
chmod +x "${SCRIPT_DIR}/driver/build.sh"
CMPUNLOCKER_DRIVER_VERSION="${detected}" \
CMPUNLOCKER_CARD_PROFILE="${CARD_PROFILE}" \
CMPUNLOCKER_GPU_INVENTORY="${CMPUNLOCKER_GPU_INVENTORY}" \
CMPUNLOCKER_ENABLE_P2P="${P2P_MODE}" \
CMPUNLOCKER_EXPERIMENTAL_80G="${CMPUNLOCKER_EXPERIMENTAL_80G:-0}" \
CMPUNLOCKER_EXPERIMENTAL_GEN4="${CMPUNLOCKER_EXPERIMENTAL_GEN4:-0}" \
    "${SCRIPT_DIR}/driver/build.sh"
ok "Patched modules installed (profile ${CARD_PROFILE})"

step "Setting up VM passthrough"
PASSTHROUGH_STATUS="skipped"
if (( CONFIGURE_PASSTHROUGH == 1 )); then
    chmod +x "${SCRIPT_DIR}/tools/passthrough-setup.sh"
    if CMPUNLOCKER_KVER="$(uname -r)" "${SCRIPT_DIR}/tools/passthrough-setup.sh"; then
        PASSTHROUGH_STATUS="armed"
    else
        PASSTHROUGH_STATUS="failed"
        warn "passthrough setup failed; the unlock still works on this host"
    fi
else
    warn "--no-passthrough given; cards are not prepared for VM passthrough"
fi

info "Disarming boot-time PCIe retrain, and installing the manual helper"
# driver/build.sh writes the combined Gen2/P2P module options before it
# rebuilds initramfs and reloads NVIDIA. Do not overwrite those options here.
#
# Nothing here retrains the link at boot any more, and gen2.service is in the
# disarm list rather than the install list: nv_cmp170hx_retrain_gen2() does that
# step from nv.c's init path, with the BAR0 writes a userspace helper cannot make,
# and an upgrade that left the old unit enabled would retrain the link again after
# the driver had trained it.  Measured on the bench on 2026-11-18 with the unit
# disabled: both soft cards came up at LnkSta=0x1102 and the fused card at 0x1104,
# with no userspace write to the link at all.
#
# The helper remains installed for two operations that are performed by hand after
# boot and do not require a unit:
#
#   sudo /usr/local/sbin/gen2-hammer                  bring every card to its floor
#   sudo CMP170HX_GEN2_TARGET=3 /usr/local/sbin/gen2-hammer
#                                                     pin a card that dropped below Gen3
#
for legacy_unit in gen2.service cmpretrain.service cmp-gen2-retrain.service; do
    systemctl disable --now "${legacy_unit}" 2>/dev/null || true
    systemctl reset-failed "${legacy_unit}" 2>/dev/null || true
done
rm -f /etc/systemd/system/gen2.service \
      /etc/systemd/system/cmpretrain.service \
      /etc/systemd/system/cmp-gen2-retrain.service \
      /etc/cmpunlocker/gen2.env \
      /usr/local/sbin/retrain.sh \
      /usr/local/sbin/cmp-gen2-retrain.sh
systemctl daemon-reload
# /etc/cmpunlocker/gen-policy is left alone: it is older than this change and the
# helper still reads it through CMP170HX_GEN2_POLICY, so a box that pinned a card
# keeps its pin. Nothing reads it at boot now.
if [[ -e /etc/cmpunlocker/gen-policy ]]; then
    info "Keeping /etc/cmpunlocker/gen-policy as the helper's per-card target list"
fi
if (( DEPRECATED_GEN2_FLAG == 1 )); then
    info "--no-gen2-service is accepted and ignored: there is no retrain unit to skip"
fi
command -v install >/dev/null || die "install command not found"
# -D because an installer should not require the directory to have been made by
# somebody else; on a normal box it is already there and the flag does nothing.
install -D -m 0755 "${SCRIPT_DIR}/tools/hammer.sh" /usr/local/sbin/gen2-hammer
if grep -aFq 'CMP Gen2:' \
        "/lib/modules/$(uname -r)/updates/cmpunlocker/nvidia.ko" 2>/dev/null; then
    ok "Driver trains the link at init; /usr/local/sbin/gen2-hammer installed for manual use"
else
    warn "Installed module has no Gen2 probe-retrain patch: nothing trains Gen2 at boot,"
    warn "and the helper only writes config space, so a card can sit at Gen1 until the"
    warn "patched module is loaded. Re-run the build (see driver/build.sh) if this"
    warn "surprises you."
fi

info "Configuring IOMMU (passthrough)"
IOMMU_STATUS="skipped"
IOMMU_PARAMS=""

iommu_params_for_cpu() {
    local vendor=""
    vendor="$(awk -F': ' '/^vendor_id/{print $2; exit}' /proc/cpuinfo 2>/dev/null || true)"
    case "${vendor}" in
        GenuineIntel) echo "intel_iommu=on iommu=pt" ;;
        AuthenticAMD) echo "amd_iommu=on iommu=pt" ;;
        *) echo "" ;;
    esac
}

cmdline_merge() {
    local current="$1"
    local token out=()
    for token in ${current}; do
        case "${token}" in
            intel_iommu=*|amd_iommu=*|iommu=*) continue ;;
            *) out+=("${token}") ;;
        esac
    done
    for token in ${IOMMU_PARAMS}; do
        out+=("${token}")
    done
    echo "${out[*]}"
}

configure_iommu_grub() {
    local grub_file="/etc/default/grub"
    local key="GRUB_CMDLINE_LINUX_DEFAULT"
    local current merged

    grep -q "^${key}=" "${grub_file}" || key="GRUB_CMDLINE_LINUX"
    if grep -q "^${key}=" "${grub_file}"; then
        current="$(sed -n "s/^${key}=\"\(.*\)\"$/\1/p" "${grub_file}" | head -1)"
    else
        current=""
    fi
    merged="$(cmdline_merge "${current}")"

    if [[ "${current}" == "${merged}" ]]; then
        ok "GRUB already has ${IOMMU_PARAMS} (${key})"
        IOMMU_STATUS="already-set"
        return 0
    fi

    cp -a "${grub_file}" "${grub_file}.cmpunlocker.bak"
    if grep -q "^${key}=" "${grub_file}"; then
        local escaped="${merged//\//\\/}"
        sed -i "s/^${key}=.*/${key}=\"${escaped}\"/" "${grub_file}"
    else
        printf '%s="%s"\n' "${key}" "${merged}" >> "${grub_file}"
    fi
    ok "Set ${key}=\"${merged}\" (backup: ${grub_file}.cmpunlocker.bak)"

    if command -v update-grub &>/dev/null; then
        update-grub
    elif command -v grub2-mkconfig &>/dev/null; then
        local cfg="/boot/grub2/grub.cfg"
        local efi_cfg
        efi_cfg="$(ls /boot/efi/EFI/*/grub.cfg 2>/dev/null | head -1 || true)"
        [[ -n "${efi_cfg}" ]] && cfg="${efi_cfg}"
        grub2-mkconfig -o "${cfg}"
    elif command -v grub-mkconfig &>/dev/null; then
        grub-mkconfig -o /boot/grub/grub.cfg
    else
        warn "No grub config generator found — regenerate grub.cfg manually"
        IOMMU_STATUS="needs-grub-regen"
        return 0
    fi
    ok "Regenerated GRUB config"
    IOMMU_STATUS="configured"
}

configure_iommu_kernel_cmdline() {
    local file="/etc/kernel/cmdline"
    local current merged
    current="$(tr -d '\n' < "${file}")"
    merged="$(cmdline_merge "${current}")"

    if [[ "${current}" == "${merged}" ]]; then
        ok "${file} already has ${IOMMU_PARAMS}"
        IOMMU_STATUS="already-set"
        return 0
    fi

    cp -a "${file}" "${file}.cmpunlocker.bak"
    printf '%s\n' "${merged}" > "${file}"
    ok "Set ${file} to \"${merged}\" (backup: ${file}.cmpunlocker.bak)"

    if command -v kernel-install &>/dev/null && [[ -d /boot/loader/entries ]]; then
        for kdir in /lib/modules/*/; do
            kver="$(basename "${kdir}")"
            [[ -f "${kdir}/vmlinuz" ]] || continue
            kernel-install add "${kver}" "${kdir}/vmlinuz" 2>/dev/null || true
        done
        ok "Refreshed systemd-boot entries"
        IOMMU_STATUS="configured"
    else
        warn "Update your boot entries so ${file} takes effect"
        IOMMU_STATUS="needs-boot-refresh"
    fi
}

if (( CONFIGURE_IOMMU == 0 )); then
    warn "--no-iommu given; leaving kernel command line untouched"
else
    IOMMU_PARAMS="$(iommu_params_for_cpu)"
    if [[ -z "${IOMMU_PARAMS}" ]]; then
        warn "Unrecognized CPU vendor — cannot pick IOMMU kernel parameters; skipping"
    elif [[ -f /etc/default/grub ]]; then
        info "Target: ${IOMMU_PARAMS} (GRUB)"
        configure_iommu_grub
    elif [[ -f /etc/kernel/cmdline ]]; then
        info "Target: ${IOMMU_PARAMS} (systemd-boot)"
        configure_iommu_kernel_cmdline
    else
        warn "No /etc/default/grub or /etc/kernel/cmdline found"
        warn "Add these to your kernel command line manually: ${IOMMU_PARAMS}"
        IOMMU_STATUS="manual"
    fi

    if grep -qw iommu=pt /proc/cmdline 2>/dev/null && [[ -d /sys/class/iommu ]] && [[ -n "$(ls -A /sys/class/iommu 2>/dev/null)" ]]; then
        ok "IOMMU is already active in passthrough mode on the running kernel"
    elif [[ "${IOMMU_STATUS}" != "skipped" ]]; then
        info "IOMMU passthrough takes effect after the next reboot"
        warn "IOMMU must also be enabled in BIOS/UEFI (VT-d / AMD-Vi / SVM)"
    fi
fi

step "Done"
banner
echo "cmpunlocker install finished!"
echo "Profile: ${CARD_PROFILE}  |  ${#GPU_BDFS[@]} GPU(s): ${COUNT_8GB}× 8gb, ${COUNT_10GB}× 10gb"
echo "Passthrough: ${PASSTHROUGH_STATUS}"
if [[ "${P2P_MODE}" == "off" ]]; then
    echo "P2P: disabled"
else
    echo "P2P: ${P2P_MODE} (verify actual transfers using docs/P2P.md)"
    if [[ "${P2P_MODE}" == "mailbox" ]]; then
        echo "     dmesg | grep CMP-P2P is the difference between armed and not"
    fi
fi
if [[ -n "${IOMMU_PARAMS}" && "${IOMMU_STATUS}" != "skipped" ]]; then
    echo "IOMMU:   ${IOMMU_PARAMS} (${IOMMU_STATUS})"
else
    echo "IOMMU:   not configured"
fi
echo ""
echo "Per-GPU expectations after unlock:"
printf "  %-16s %-8s %-8s %s\n" "BDF" "PCI ID" "Variant" "Expect MiB"
for i in "${!GPU_BDFS[@]}"; do
    printf "  %-16s %-8s %-8s ~%s\n" "${GPU_BDFS[$i]}" "${GPU_DEVIDS[$i]}" "${GPU_PROFILES[$i]}" "${GPU_EXPECTED[$i]}"
done
if [[ "${CMPUNLOCKER_EXPERIMENTAL_80G:-0}" == "1" ]]; then
    echo "  That table is the floor, not the ceiling: a card whose framebuffer fuses"
    echo "  were burned takes the wide layout and comes up larger (80 GiB on an 8 GB"
    echo "  SKU).  dmesg's CMP-DYN DECISION line is the per-card answer, read back"
    echo "  from the card's own CFG1/LMR rather than from this table."
fi
echo ""
echo "Next:"
echo -e "  1. Cold reboot recommended: ${CYAN}sudo shutdown -h now${NC}  (then power on)"
echo -e "  2. Verify all GPUs: ${CYAN}sudo ./verify.sh${NC}"
if [[ "${CMPUNLOCKER_EXPERIMENTAL_GEN4:-0}" == "1" ]]; then
    echo -e "  3. PCIe: Gen2 or better everywhere; a card whose speed fuses were cleared asks for"
    echo -e "     Gen4 and keeps the speed its link actually trains (Gen3 under a Gen3 bridge),"
    echo -e "     falling back to Gen2 only if nothing above Gen2 holds.  Read the negotiated link,"
    echo -e "     not what the SKU claims: ${CYAN}cat /sys/bus/pci/devices/<bdf>/current_link_speed${NC},"
    echo -e "     or ${CYAN}sudo setpci -s <bdf> CAP_EXP+12.w${NC} (0x1102 Gen2, 0x1103 Gen3, 0x1104 Gen4)"
else
    echo -e "  3. Verify PCIe Gen2: ${CYAN}nvidia-smi --query-gpu=pcie.link.gen.current,pcie.link.gen.max --format=csv${NC}  (expect 2,2)"
fi
echo -e "  4. Or check manually: ${CYAN}nvidia-smi${NC}"
echo -e "  5. Unlock logs: ${CYAN}sudo dmesg | grep SEC2_DEBUG${NC}"
echo -e "  6. Verify IOMMU after reboot: ${CYAN}cat /proc/cmdline${NC} and ${CYAN}ls /sys/class/iommu${NC}"
echo -e "  7. Verify the negotiated link: ${CYAN}sudo ./verify.sh${NC}  (reads LnkSta at both ends, not nvidia-smi)"
echo -e "     Retrain a link by hand, if one ever droops: ${CYAN}sudo /usr/local/sbin/gen2-hammer${NC}"
echo ""
echo "This script removed the nvidia DKMS kernel modules. Re-run with the same options (including --p2p, if enabled) after each kernel upgrade."
echo "Log saved to: ${LOG_FILE}"
echo ""
