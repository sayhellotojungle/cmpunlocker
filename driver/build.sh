#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mapfile -t SUPPORTED_VERSIONS < <(grep -E '^[0-9]+\.[0-9]+\.[0-9]+$' "${SCRIPT_DIR}/VERSION")
DEFAULT_VERSION="${SUPPORTED_VERSIONS[0]:-}"
VERSION="${CMPUNLOCKER_DRIVER_VERSION:-${DEFAULT_VERSION}}"
ENABLE_P2P="${CMPUNLOCKER_ENABLE_P2P:-off}"
ENABLE_80G="${CMPUNLOCKER_EXPERIMENTAL_80G:-0}"
ENABLE_GEN4="${CMPUNLOCKER_EXPERIMENTAL_GEN4:-0}"
PATCH_DIR="${SCRIPT_DIR}/patches"
BUILD_ROOT="${CMPUNLOCKER_BUILD_DIR:-${SCRIPT_DIR}/.build}"
SRC_NAME="open-gpu-kernel-modules-${VERSION}"
SRC_DIR="${BUILD_ROOT}/${SRC_NAME}"
TARBALL="${BUILD_ROOT}/${SRC_NAME}.tar.gz"
TARBALL_URL="https://github.com/NVIDIA/open-gpu-kernel-modules/archive/refs/tags/${VERSION}.tar.gz"
KVER="${CMPUNLOCKER_KVER:-$(uname -r)}"
BUILD_ONLY="${CMPUNLOCKER_BUILD_ONLY:-0}"
KSRC="/lib/modules/${KVER}/build"
INSTALL_MOD_DIR="/lib/modules/${KVER}/updates/cmpunlocker"
PREVIOUS_P2P="$(cat "${INSTALL_MOD_DIR}/p2p_enabled" 2>/dev/null || true)"

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
else
    RED=""; GREEN=""; YELLOW=""; CYAN=""; NC=""
fi

info() { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()   { echo -e "${GREEN}[ OK ]${NC}  $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC}  $*"; }
die()  { echo -e "${RED}[FAIL]${NC}  $*" >&2; exit 1; }

version_supported() {
    local v="$1"
    local s
    for s in "${SUPPORTED_VERSIONS[@]}"; do
        [[ "${v}" == "${s}" ]] && return 0
    done
    return 1
}

# P2P is a path, not a switch.  off and 1 keep their old meaning (1 is what
# --p2p has always selected); mailbox is the second route, documented in
# docs/P2P.md.  The variable is still the one install.sh exports, so nothing that
# ever ran this script has to change the way it asks for a build.
case "${ENABLE_P2P}" in
    0|off|none|'') P2P_MODE="off" ;;
    1|bar1)        P2P_MODE="bar1" ;;
    mailbox)       P2P_MODE="mailbox" ;;
    *) die "CMPUNLOCKER_ENABLE_P2P must be off, bar1 or mailbox (got: '${ENABLE_P2P}')" ;;
esac
case "${ENABLE_80G}" in
    0|1) ;;
    *) die "CMPUNLOCKER_EXPERIMENTAL_80G must be 0 or 1" ;;
esac
case "${ENABLE_GEN4}" in
    0|1) ;;
    *) die "CMPUNLOCKER_EXPERIMENTAL_GEN4 must be 0 or 1" ;;
esac
case "${BUILD_ONLY}" in
    0|1) ;;
    *) die "CMPUNLOCKER_BUILD_ONLY must be 0 or 1" ;;
esac

# Is any P2P in play, in this build or in the one already installed here?  Both
# routes rewrite per-peer state that a live module reload cannot be trusted to
# unwind, so either one turns the end of this script into "install and stop".
# p2p_enabled held 0 or 1 before --p2p grew a second path, which is why the test
# is "not one of the ways to say off" rather than a comparison against a list.
p2p_needs_cold_boot() {
    local value
    for value in "${P2P_MODE}" "${PREVIOUS_P2P}"; do
        case "${value}" in
            ''|0|off|none) ;;
            *) return 0 ;;
        esac
    done
    return 1
}

[[ "${EUID}" -eq 0 ]] || die "Run as root: sudo ${SCRIPT_DIR}/build.sh"
[[ -n "${VERSION}" ]] || die "No driver version set (driver/VERSION empty and CMPUNLOCKER_DRIVER_VERSION unset)"
version_supported "${VERSION}" || die "Unsupported driver version '${VERSION}' (supported: ${SUPPORTED_VERSIONS[*]})"
[[ -d "${PATCH_DIR}" ]] || die "Missing patches directory: ${PATCH_DIR}"
[[ -d "${KSRC}" ]] || die "Kernel headers not found at ${KSRC}. Install linux-headers-${KVER} (or kernel-devel)."
command -v python3 &>/dev/null || die "python3 is required to apply the card memory profile"
python3 -c "import yaml" 2>/dev/null || die "python3 PyYAML is required to read common/constants.yaml (apt install python3-yaml)"
command -v sha256sum &>/dev/null || die "sha256sum is required"
info "Building against open-gpu-kernel-modules ${VERSION}"

PATCH_ORDER=(
    sec2-postbl-plm-ss-cfg.patch
    booter-verify.patch
    late-pma.patch
    bar0-pramin-clamp.patch
    ce-scrub-workarounds.patch
    persistent-sw-state.patch
    pcie-gen2.patch
    pcie-gen2-probe-retrain.patch
    name-string.patch
    bar1-resize-unlock.patch
    cmp-sku-mask.patch
    ecc-enable.patch
    ecc-fbpa-static.patch
    ecc-reporting.patch
)
# The two P2P routes share no patch, and they must not share one: RM carries a
# single PCIe P2P protocol between two GPUs at a time (gate 4 of
# kbusIsPcieBar1P2PMappingSupported_GH100 refuses the BAR1 route over a peer
# number the mailbox route already took), so a build that carried both patches
# would not get both paths — it would get whichever one the peer pair reached
# first, with the other silently refused.  bar1 is the CE-DMA path into a BAR1
# that maps the whole framebuffer; mailbox programs the NV_P2P aperture instead,
# which needs 64 KiB per peer and nothing else, and it is the only route left on
# a card whose framebuffer outgrows the 64 GiB ceiling the resizable-BAR
# capability table offers (docs/P2P.md).
P2P_BAR1_PATCH_ORDER=(
    p2p/0011-p2p-bar1.patch
    p2p/0013-skip-mailbox-peer-preinit.patch
    p2p/0015-bar1p2p-readcap-override.patch
)
P2P_MAILBOX_PATCH_ORDER=(
    p2p/0020-mailbox-trap31.patch
    p2p/0021-mailbox-trap31-plm.patch
)
P2P_PATCH_ORDER=()
case "${P2P_MODE}" in
    bar1)
        P2P_PATCH_ORDER=("${P2P_BAR1_PATCH_ORDER[@]}")
        info "GPU-to-GPU BAR1 P2P enabled"
        ;;
    mailbox)
        P2P_PATCH_ORDER=("${P2P_MAILBOX_PATCH_ORDER[@]}")
        info "GPU-to-GPU mailbox P2P enabled (needs no large BAR1; verify real transfers)"
        ;;
    *)
        info "GPU-to-GPU P2P overrides disabled"
        ;;
esac
# The P2P sets have no version gate: every hunk of them, and the dynfb hooks the
# features add, places on every release in driver/VERSION.  tests/test_patches.py
# applies both sets against all of them with --fuzz=0, which is what keeps this
# claim honest across a branch bump.
#
# Three sites used to reject on 615.x and are dynfb hooks now, because a context diff
# cannot express them: each lands where the neighbouring line is something another
# release edits.  615 added a line below the P2P caps assignments, edited an
# unrelated registry default two lines below the resizable-BAR default, and
# regenerated a func table with one more method between the entry and the comment the
# hunk carried.  None of that was a P2P change; the hunks only asked for context they
# had no right to.  A branch that really does move one of these sites is now a build
# error naming the hook, rather than a release nobody is allowed to build P2P on.
if [[ ${#P2P_PATCH_ORDER[@]} -gt 0 ]]; then
    PATCH_ORDER+=("${P2P_PATCH_ORDER[@]}")
fi
PATCH_FILES=()
for name in "${PATCH_ORDER[@]}"; do
    p="${PATCH_DIR}/${name}"
    [[ -f "${p}" ]] || die "Missing patch: ${p}"
    PATCH_FILES+=("${p}")
done
# The dynamic policy is not a patch, so it has to enter the cache key itself:
# without this, editing cmp_dynamic.h reuses a stale tree and the change silently
# does not reach the built module.
DYNFB_DIR="${SCRIPT_DIR}/dynfb"
DYNFB_HEADER_DST="$(python3 "${DYNFB_DIR}/apply.py" --header-dest)" ||
    die "dynfb/apply.py does not report its header destination"
DYNFB_HASH_FILES=("${DYNFB_DIR}/apply.py" "${DYNFB_DIR}/cmp_dynamic.h")
while IFS= read -r -d '' f; do
    DYNFB_HASH_FILES+=("${f}")
done < <(find "${DYNFB_DIR}/hooks" -type f -print0 | sort -z)
PATCH_HASH="$(cat "${PATCH_FILES[@]}" "${DYNFB_HASH_FILES[@]}" |
              sha256sum | cut -d' ' -f1)"

PROFILE="${CMPUNLOCKER_CARD_PROFILE:-8gb}"
case "${PROFILE}" in
    8GB) PROFILE="8gb" ;;
    10GB) PROFILE="10gb" ;;
    MIXED) PROFILE="mixed" ;;
esac

CONSTANTS="${SCRIPT_DIR}/../common/constants.yaml"
[[ -r "${CONSTANTS}" ]] || die "Missing ${CONSTANTS}"
CONSTANTS_ENV="$(python3 "${SCRIPT_DIR}/../tools/read-constants.py" "${CONSTANTS}" "${PATCH_DIR}" "${SCRIPT_DIR}/build.sh" "${PROFILE}")" || die "common/constants.yaml rejected (see error above)"
eval "${CONSTANTS_ENV}"

BUILD_STAMP="${VERSION}:${KVER}:${PROFILE}:p2p=${P2P_MODE}:experimental80g=${ENABLE_80G}:${PATCH_HASH}:$(sha256sum "${SCRIPT_DIR}/build.sh" | cut -d' ' -f1)"

mkdir -p "${BUILD_ROOT}"

if [[ ! -f "${TARBALL}" ]]; then
    info "Downloading open-gpu-kernel-modules ${VERSION}..."
    curl -L --fail -o "${TARBALL}.partial" "${TARBALL_URL}"
    mv "${TARBALL}.partial" "${TARBALL}"
    ok "Downloaded ${TARBALL}"
else
    ok "Using cached tarball ${TARBALL}"
fi

STAMP_FILE="${SRC_DIR}/.cmpunlocker-stamp"
if [[ -d "${SRC_DIR}" ]] && [[ "$(cat "${STAMP_FILE}" 2>/dev/null || true)" == "${BUILD_STAMP}" ]]; then
    SKIP_PREP=1
    ok "Source tree already extracted and patched for this exact build; reusing it"
else
    SKIP_PREP=0
    info "Extracting sources..."
    rm -rf "${SRC_DIR}"
    tar -xzf "${TARBALL}" -C "${BUILD_ROOT}"
    if [[ ! -d "${SRC_DIR}" ]]; then
        extracted="$(find "${BUILD_ROOT}" -maxdepth 1 -type d -name "${SRC_NAME}*" | head -1)"
        [[ -n "${extracted}" ]] || die "Extracted source tree not found"
        mv "${extracted}" "${SRC_DIR}"
    fi
    ok "Sources ready: ${SRC_DIR}"

    info "Applying unlock patches..."
    cd "${SRC_DIR}"
    for i in "${!PATCH_ORDER[@]}"; do
        info "  ${PATCH_ORDER[$i]}"
        # Optional P2P hunks must match their complete context. Never use
        # fuzz to hide a dependency on an absent patch or a changed driver API.
        if [[ "${PATCH_ORDER[$i]}" == p2p/* ]]; then
            patch --batch --forward --fuzz=0 -p1 < "${PATCH_FILES[$i]}"
        else
            patch --batch --forward -p1 < "${PATCH_FILES[$i]}"
        fi
    done
    ok "All patches applied"

    # The per-card capacity policy is placed by anchor rather than by a patch:
    # dynfb/apply.py copies its header into the tree and checks every hook
    # landed, so a driver release that moved the code costs a line number, not a
    # silently misplaced hunk.
    python3 "${DYNFB_DIR}/apply.py" "--p2p=${P2P_MODE}" "${SRC_DIR}" ||
        die "dynfb hooks did not land"
    DYNFB_H="${SRC_DIR}/${DYNFB_HEADER_DST}"

    GSP_C="${SRC_DIR}/src/nvidia/src/kernel/gpu/gsp/kernel_gsp.c"
    [[ -f "${GSP_C}" ]] || die "Missing ${GSP_C} after patching"
    [[ -f "${DYNFB_H}" ]] || die "Missing ${DYNFB_H} after dynfb"
    # The wide geometry is opt-in twice over: the card has to report the extra
    # FBP pair in NV_PFB_PRI_MMU_LOCAL_MEMORY_RANGE (LMR), and RegistryDwords has
    # to carry CMPEnable80G=1 (added below).  Unburned cards never take the path,
    # so the path is compiled in and only switched on by request.
    python3 - "${DYNFB_H}" "${ENABLE_80G}" <<'PY'
import pathlib, re, sys
path, want = pathlib.Path(sys.argv[1]), sys.argv[2]
text = path.read_text()
text2, n = re.subn(r"#define CMP_DYN_ENABLE_80G\s+[01]",
                   "#define CMP_DYN_ENABLE_80G       %s" % want, text, count=1)
if n != 1:
    print("CMP-DYN: no 80 GiB switch in this tree; leaving as patched")
else:
    path.write_text(text2)
    print("CMP-DYN: 80 GiB capacity path = %s" % want)
PY
    if [[ "${ENABLE_80G}" -eq 0 ]]; then
        info "80 GiB capacity path off; a burned card keeps the 64 GiB layout"
    fi

    info "Applying memory profile ${PROFILE} (${UNLOCK_LABEL} geometry)..."
    if [[ "${SKIP_GEOMETRY_REWRITE}" -eq 1 ]]; then
        info "mixed profile: runtime device-id geometry (no build-time CFG1/LMR rewrite)"
    else
        python3 - "${GSP_C}" "${CFG1}" "${LMR}" "${FB_BYTES}" "${UNLOCK_LABEL}" <<'PY'
import pathlib, re, sys
path, cfg1, lmr, fb, label = sys.argv[1:6]
text = pathlib.Path(path).read_text()
if (
    "SEC2_POSTBL_TIMING_CMP_170HX_8GB_PCI_DEVICE_ID" in text
    and "SEC2_POSTBL_TIMING_CMP_170HX_10GB_PCI_DEVICE_ID" in text
    and "0x02779000U" in text
    and "0x02669000U" in text
    and "0x0000001000000000ULL" in text
    and "0x0000000A00000000ULL" in text
):
    print(f"runtime device-id geometry (profile metadata={label})")
    raise SystemExit(0)

text2, n1 = re.subn(
    r"(NvU32 cfg1Value = )0x[0-9A-Fa-f]+(U;)",
    rf"\g<1>{cfg1}\g<2>",
    text,
    count=1,
)
text2, n2 = re.subn(
    r"(NvU32 lmrValue\s*=\s*)0x[0-9A-Fa-f]+(U;)",
    rf"\g<1>{lmr}\g<2>",
    text2,
    count=1,
)
text2, n3 = re.subn(
    r"(NvU64 targetFbBytes = )0x[0-9A-Fa-f]+ULL;\s*/\*[^*]*\*/",
    rf"\g<1>{fb}ULL;  /* {label} */",
    text2,
    count=1,
)
if n1 != 1 or n2 != 1 or n3 != 1:
    raise SystemExit(
        f"geometry rewrite failed (cfg1={n1} lmr={n2} fb={n3}); check kernel_gsp.c markers"
    )
pathlib.Path(path).write_text(text2)
print(f"cfg1={cfg1} lmr={lmr} fb={fb} ({label})")
PY
    fi
    ok "Memory profile ${PROFILE}: unlock_geometry=${UNLOCK_LABEL}"

    printf '%s\n' "${BUILD_STAMP}" > "${STAMP_FILE}"
fi

cd "${SRC_DIR}"
if [[ "${BUILD_ONLY}" -eq 0 ]]; then
    mkdir -p "${INSTALL_MOD_DIR}"
    printf '%s\n' "${VERSION}" > "${INSTALL_MOD_DIR}/driver_version"
    printf '%s\n' "${PROFILE}" > "${INSTALL_MOD_DIR}/card_profile"
    printf '%s\n' "${UNLOCK_LABEL}" > "${INSTALL_MOD_DIR}/unlock_geometry"
    if [[ -n "${CMPUNLOCKER_GPU_INVENTORY:-}" ]]; then
        printf '%s\n' "${CMPUNLOCKER_GPU_INVENTORY}" > "${INSTALL_MOD_DIR}/gpu_inventory"
        ok "Wrote gpu_inventory ($(echo "${CMPUNLOCKER_GPU_INVENTORY}" | grep -c . || true) GPU(s))"
    else
        : > "${INSTALL_MOD_DIR}/gpu_inventory"
    fi
fi

info "Building modules for kernel ${KVER}..."
find . -name "*.sh" -exec chmod +x {} + 2>/dev/null || true
if [[ "${SKIP_PREP}" -eq 0 ]]; then
    rm -rf src/nvidia/_out src/nvidia-modeset/_out kernel-open/conftest 2>/dev/null || true
else
    info "Reusing prior build output — incremental rebuild"
fi

JOBS="$(nproc)"
CC_CMD="gcc"
if command -v ccache &>/dev/null; then
    CC_CMD="ccache gcc"
    info "ccache detected — compiler output will be cached for faster rebuilds"
fi
make -j"${JOBS}" modules SYSSRC="${KSRC}" CC="${CC_CMD}"
ok "Modules built"
if command -v ccache &>/dev/null; then
    ccache -s 2>/dev/null | sed 's/^/  /' || true
fi

if [[ "${BUILD_ONLY}" -eq 1 ]]; then
    # Nothing was installed and this machine's modules, initramfs and modprobe
    # config are untouched, so say what the build proves and stop here. The markers
    # are the difference between "it compiled" and "this change reached the module":
    # a hook that stopped applying still compiles, and so does a policy header that
    # nobody includes. Look for data, not code symbols: a feature string and a
    # module parameter both survive a stripped module, a static function name does
    # not. And grep the file rather than strings | grep, because with pipefail set
    # grep -q closes the pipe on its first match and strings dies of SIGPIPE, which
    # reads back as "marker missing" on a module that has it.
    MODULES=()
    while IFS= read -r candidate; do
        MODULES+=("${candidate}")
    done < <(find . -name nvidia.ko -not -path '*/conftest/*')
    [[ ${#MODULES[@]} -gt 0 ]] || die "Build produced no nvidia.ko"
    for marker in 'CMP-DYN' 'CMP-BAR1' 'cmp_pcie_gen'; do
        if grep -a -q -- "${marker}" "${MODULES[@]}"; then
            ok "nvidia.ko contains ${marker}"
        else
            die "nvidia.ko does not contain ${marker}; the change did not reach the module"
        fi
    done
    ok "Build only: module at ${SRC_DIR}/${MODULES[0]#"./"}; nothing installed"
    exit 0
fi
info "Installing modules to ${INSTALL_MOD_DIR}..."
mkdir -p "${INSTALL_MOD_DIR}"

mapfile -t KO_FILES < <(find "${SRC_DIR}" -type f \( \
    -name 'nvidia.ko' -o -name 'nvidia-modeset.ko' -o -name 'nvidia-uvm.ko' \
    -o -name 'nvidia-drm.ko' -o -name 'nvidia-peermem.ko' \) \
    ! -path '*/conftest/*' | sort -u)
[[ ${#KO_FILES[@]} -gt 0 ]] || die "No built nvidia*.ko found"

for ko in "${KO_FILES[@]}"; do
    base="$(basename "${ko}")"
    install -m 0644 "${ko}" "${INSTALL_MOD_DIR}/${base}"
    ok "Installed ${base}"
done

# Keep one RegistryDwords option: separate modprobe entries can replace each
# other. Rewrite it on every install so a later build without P2P removes the
# static-BAR1/P2P settings. This must precede initramfs generation and modprobe.
# RMPcieLinkSpeed is a MAX-speed allow-mask and the one PCIe knob RM itself
# honours, so it has to agree with what the link code does.  0x1 is the value
# verified with the GDS/P2PDMA host path on the reference rig: RM never asks for
# more than the Gen2 link that pcie-gen2.patch and the early-boot retrain helper
# produce.  0xF goes with the Gen4 bring-up (dynfb), which trains the link itself
# and only on a card whose PCIe speed fuses allow it; on any other card RM would
# ask for a speed the PHY cannot train.  Do not drop the key entirely:
# SET_PCIE_SPEED then fails with NV_ERR_NOT_SUPPORTED.
# RmForceDisableIomapWC=1 and NVreg_EnableResizableBar=1 are required by the
# GPUDirect-Storage / P2PDMA host path (verified on the reference rig).
REGISTRY_SPEED="0x1"
MODULE_PARAMS=""
if [[ "${ENABLE_GEN4}" -eq 1 ]]; then
    # cmp_pcie_gen=0 asks for whatever each card's own fuses allow, which is what
    # a box with both burned and soft-unlocked cards needs: the soft ones stay on
    # Gen2 and are never asked to train.  Set cmp_pcie_gen=2 here to keep every
    # card at Gen2, or 3/4 to ask every 0x20C2 for that speed.
    REGISTRY_SPEED="0xF"
    MODULE_PARAMS="cmp_pcie_gen=0 "
fi
REGISTRY_DWORDS="RmForceEnableGen2=1;RMPcieLinkSpeed=${REGISTRY_SPEED};RmForceDisableIomapWC=1"
if [[ "${ENABLE_80G}" -eq 1 ]]; then
    # Opt-in for the wide (80 GiB) geometry in the dynamic policy (dynfb).
    # Without this key the burned card stays on the 64 GiB layout, so the
    # experiment can be turned off from this file alone, no rebuild needed.
    REGISTRY_DWORDS+=";CMPEnable80G=1"
fi
case "${P2P_MODE}" in
    bar1)
        # The BAR1 route is RM's own: static BAR1 covering the whole framebuffer,
        # and the CE DMAing into the peer through it.  Both keys have to be there,
        # and the framebuffer has to fit — see docs/P2P.md.
        REGISTRY_DWORDS+=";RMForceStaticBar1=1;RMPcieP2PType=1"
        ;;
    mailbox)
        # The mailbox route is the opposite choice, so it sets the opposite things:
        # RMPcieP2PType stays at its default (MAILBOX) and static BAR1 stays off,
        # because the aperture this path writes needs 64 KiB per peer at the bottom
        # of BAR1 and no mapping of the framebuffer at all.  CMPEnableP2PMailbox is
        # ours: it is what p2p/0021 opens the PRI trap's privilege mask for and what
        # dynfb/hooks/p2p_caps_mailbox.c claims caps for, so removing it from this
        # line turns the whole feature off without a rebuild.  ForceP2P is
        # deliberately not here — RM's own override would report the caps OK past
        # the guard that knows better on a card whose trap cannot be armed.
        REGISTRY_DWORDS+=";CMPEnableP2PMailbox=1"
        ;;
esac
mkdir -p /etc/modprobe.d
printf 'options nvidia %sNVreg_EnableResizableBar=1 NVreg_RegistryDwords="%s"\n' \
    "${MODULE_PARAMS}" "${REGISTRY_DWORDS}" \
    > /etc/modprobe.d/cmp-pcie-gen2.conf
printf '%s\n' "${P2P_MODE}" > "${INSTALL_MOD_DIR}/p2p_enabled"
ok "Configured Gen2/P2P module options (P2P=${P2P_MODE}, Gen4=${ENABLE_GEN4})"

depmod -a "${KVER}"
ok "depmod complete"
rebuild_initramfs() {
    if command -v update-initramfs &>/dev/null; then
        info "Rebuilding initramfs (update-initramfs)..."
        update-initramfs -u -k "${KVER}" || return 1
        ok "initramfs rebuilt"
        return 0
    fi
    if command -v dracut &>/dev/null; then
        info "Rebuilding initramfs (dracut)..."
        dracut --force --kver "${KVER}" || return 1
        ok "initramfs rebuilt"
        return 0
    fi
    if command -v mkinitcpio &>/dev/null; then
        info "Rebuilding initramfs (mkinitcpio)..."
        mkinitcpio -P || return 1
        ok "initramfs rebuilt"
        return 0
    fi
    warn "No initramfs tool found — rebuild manually before rebooting"
    return 1
}

if ! rebuild_initramfs; then
    if p2p_needs_cold_boot; then
        die "Modules and settings installed, but initramfs was not rebuilt. Fix initramfs generation and rerun this build before cold rebooting."
    fi
    warn "Modules installed; rebuild initramfs manually before rebooting."
fi
if p2p_needs_cold_boot; then
    ok "Modules and P2P settings installed for the next boot"
    info "P2P builds require a cold reboot; skipping live NVIDIA module reload."
    exit 0
fi
resolved="$(modprobe -n -v nvidia 2>/dev/null | awk '/insmod/ {print $2; exit}' || true)"
if [[ -n "${resolved}" ]]; then
    info "modprobe will load: ${resolved}"
    if [[ "${resolved}" != *"/updates/cmpunlocker/"* ]]; then
        warn "Resolved nvidia.ko is not under updates/cmpunlocker/"
    fi
fi
info "Attempting to unload NVIDIA modules..."
systemctl stop nvidia-persistenced 2>/dev/null || true
systemctl stop nvidia-fabricmanager 2>/dev/null || true
reload_ok=0
if grep -q '^nvidia' /proc/modules; then
    for mod in nvidia_drm nvidia_uvm nvidia_modeset nvidia; do
        modprobe -r "${mod}" 2>/dev/null || true
    done
    sleep 1
fi

if ! grep -q '^nvidia ' /proc/modules; then
    if modprobe nvidia && modprobe nvidia-modeset; then
        modprobe nvidia-uvm 2>/dev/null || true
        modprobe nvidia-drm 2>/dev/null || true
        reload_ok=1
        ok "Patched NVIDIA modules loaded"
        running_src="$(cat /sys/module/nvidia/srcversion 2>/dev/null || true)"
        patched_src="$(modinfo -F srcversion "${INSTALL_MOD_DIR}/nvidia.ko" 2>/dev/null || true)"
        if [[ -n "${running_src}" && -n "${patched_src}" && "${running_src}" != "${patched_src}" ]]; then
            warn "Loaded nvidia srcversion (${running_src}) != patched (${patched_src})"
            reload_ok=0
        fi
    else
        warn "modprobe failed"
    fi
else
    warn "Could not unload nvidia modules"
fi
echo ""
if [[ "${reload_ok}" -eq 1 ]]; then
    ok "Build and install finished. Verify with: nvidia-smi"
    info "If memory shows stock size, do cold reboot."
else
    warn "Modules installed but running driver is still stock."
    info "Perform cold reboot: shutdown -h now"
fi
echo ""
