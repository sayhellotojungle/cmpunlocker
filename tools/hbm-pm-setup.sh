#!/bin/bash
# Builds, installs and starts the HBM idle-power governor.
#
# This is the only cmpunlocker feature that keeps working after boot: the patched
# driver opens two privilege masks during GSP bootstrap and then never touches the
# memory PLL again, so somebody has to decide, repeatedly, when an idle card is
# allowed to lower its HBM clock and when it is not.  That somebody is a module.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/../common/lib.sh"

KVER="${CMPUNLOCKER_KVER:-$(uname -r)}"
MOD_SRC="${SCRIPT_DIR}/../driver/hbm-pm"
MOD_DST="/lib/modules/${KVER}/updates/cmpunlocker"
MODULE="cmp_hbm_pm"
STATUS="/sys/kernel/cmp_hbm_pm/status"

[[ "${EUID}" -eq 0 ]] || die "hbm-pm-setup.sh must run as root"

# The masks this module writes through are opened by the patched driver, and only for a
# driver that was told the governor would be running: driver/build.sh puts
# CMPEnableHbmPm=1 on the nvidia module's RegistryDwords.  Without that key the module
# still loads and does no harm: it reads one word, finds the window closed, says so once
# per card, and leaves every card at the clock it booted with.  That is the state a
# machine without this feature is in anyway, so the absence of the key is a warning here
# rather than a reason to stop.
if ! grep -q 'CMPEnableHbmPm=1' /etc/modprobe.d/cmp-pcie-gen2.conf 2>/dev/null; then
    warn "the installed driver was not asked to open the HBM privilege masks"
    warn "(CMPUNLOCKER_HBM_PM=1 is what driver/build.sh does; without that key the"
    warn ' governor reports "refresh access closed" and stays out of the way)'
fi

info "Building ${MODULE}.ko"
make -C "${MOD_SRC}" KVER="${KVER}" clean &>/dev/null || true
if ! build_out="$(make -C "${MOD_SRC}" KVER="${KVER}" 2>&1)"; then
    warn "could not build ${MODULE}.ko"
    warn "the unlock itself is unaffected, and no other part of the install needs this"
    printf '%s\n' "${build_out}" | tail -n 25
    # Nonzero on purpose.  Nothing about the card is broken by it, and install.sh says so
    # in its own summary, but a setup script that reports success while the thing it was
    # asked to set up is not set up is the difference between a missing feature and a
    # feature that silently never runs: the wattmeter cannot tell those two apart.
    exit 1
fi
install -D -m 0644 "${MOD_SRC}/${MODULE}.ko" "${MOD_DST}/${MODULE}.ko"
depmod -a "${KVER}"
ok "Installed ${MODULE}.ko"

# One modules-load.d line is all the boot-time wiring the governor itself needs.  The
# governor registers a bus notifier, so it does not matter whether udev loads it before
# or after nvidia binds the card, and nothing has to be inside the initramfs: the
# decision is taken minutes later, by which time the card's firmware has been up for a
# while.
mkdir -p /etc/modules-load.d
printf '%s\n' "${MODULE}" > /etc/modules-load.d/cmpunlocker-hbm.conf
ok "Configured /etc/modules-load.d/cmpunlocker-hbm.conf"

# The keeper, which is the other half of the feature and not an optional extra.  The
# module will not take a card's framebuffer path away on the strength of its own
# counters: those were measured reading zero for 8.4 s on a card serving requests at
# 178 W.  Somebody has to ask the driver whether anything is using the card, once a
# second, and that somebody is this service.  Without it the governor loads, says
# `lease none` in the status file, and leaves every card on its busy clock -- which is
# the correct outcome for a feature whose other half is not running, and the reason the
# check below is a warning rather than a silence.
KEEPER_SRC="${SCRIPT_DIR}/hbm-lease.py"
KEEPER_UNIT_SRC="${SCRIPT_DIR}/cmp-hbm-lease.service"
KEEPER_DST="/usr/local/lib/cmpunlocker/hbm-lease.py"
install -D -m 0755 "${KEEPER_SRC}" "${KEEPER_DST}"
ok "Installed ${KEEPER_DST}"

# The thresholds come along too, rather than being read where they happen to sit in
# the tree this script was run from.  Pointing the unit at the source tree is the
# kind of wiring that survives every test and then fails on a machine whose owner
# moved the checkout: the keeper that cannot read its floors refuses, every card
# stays on its busy clock, and the status file cannot tell that apart from a machine
# with nothing idle on it -- which is precisely how the three keeper bugs reached
# hardware.  A refusal the operator can see is worth more than a refusal that happens
# to be invisible, and this one is invisible only because the file is somewhere else.
CONSTANTS_SRC="$(readlink -f "${SCRIPT_DIR}/../common/constants.yaml")"
CONSTANTS_DST="/usr/local/share/cmpunlocker/constants.yaml"
if ! install -D -m 0644 "${CONSTANTS_SRC}" "${CONSTANTS_DST}"; then
    warn "${CONSTANTS_SRC} could not be installed: the keeper has no thresholds to read,"
    warn "will answer \`busy' for every card, and no card will lower its HBM clock"
fi
sed "s|@CONSTANTS@|${CONSTANTS_DST}|" "${KEEPER_UNIT_SRC}" \
    > /etc/systemd/system/cmp-hbm-lease.service
ok "Configured /etc/systemd/system/cmp-hbm-lease.service (keeper reads ${CONSTANTS_DST})"
systemctl daemon-reload

if ! modprobe "${MODULE}"; then
    # -ENODEV is the honest answer from the module on a box with no unlocked CMP in it, and
    # it says so in the log.  The other answer worth knowing about is a signature one:
    # Secure Boot refuses an out-of-tree .ko nothing signed, which looks exactly like this.
    warn "${MODULE} did not load: no supported card, a signature Secure Boot rejects,"
    warn "or the card's firmware is not up yet. dmesg | grep ${MODULE} tells you which."
    exit 1
fi
ok "Governor running"
# Read once, with a deadline, and keep the text. Two reasons, both learned the hard way: the
# checks below have to be made against one moment rather than three opens of a file that can
# answer differently each time, and none of them may be able to sit in the kernel waiting for
# a card -- the file says `status unavailable` rather than hanging, and a setup script that
# read nothing must not report that everything is fine (docs/HBM-PM.md).
hbm_status="$(timeout 15 cat "${STATUS}" 2>/dev/null || true)"
if [[ -z "${hbm_status}" ]]; then
    warn "${STATUS} could not be read at all: the card's firmware may not be up yet, or the"
    warn "governor is busy with a hardware read. Read it by hand once the card has come up."
else
    while IFS= read -r line; do
        info "  ${line}"
    done <<< "${hbm_status}"
fi

# Start the keeper and then hold this script to having actually got an answer out of
# it.  A feature that needs two moving parts and installs one is a feature that does
# nothing, and the wattmeter cannot tell that apart from a feature that is working: the
# cards sit at the busy clock either way.  Two seconds is the two consecutive reports
# the module insists on before it believes one.
if systemctl enable --now cmp-hbm-lease.service; then
    ok "Lease keeper enabled and started"
else
    warn "cmp-hbm-lease.service would not start: no card will lower its HBM clock"
    warn "(journalctl -u cmp-hbm-lease.service is where the reason is)"
fi
sleep 2
hbm_status="$(timeout 15 cat "${STATUS}" 2>/dev/null || true)"
if [[ -z "${hbm_status}" ]]; then
    : # Already warned above about the status file; there is nothing here to check.
elif printf '%s\n' "${hbm_status}" | grep -q 'status unavailable'; then
    warn "the governor would not answer, so whether the keeper got through is unknown: a"
    warn "hardware read or a policy change has its worker list. Read ${STATUS} again by hand."
elif printf '%s\n' "${hbm_status}" | grep -q 'lease waived'; then
    warn "lease_ms=0: the governor is going idle on its counters alone"
    warn "That is a bench setting. On a machine anybody uses, unset it -- the counters"
    warn "have been measured failing at exactly this question."
elif printf '%s\n' "${hbm_status}" | grep -E '^[0-9a-f]{4}:' |
        grep -qE 'lease none|lease [0-9]+ ms .* stale'; then
    # `lease none` and an aged-out lease are the same outcome for a card and are not
    # the same outcome for an installer: one says the keeper never got there, the
    # other says it stopped on the way.  Listing both, per card, is what makes the
    # difference readable without opening the journal.
    warn "a card has no current answer from the keeper, and will not lower its HBM clock"
    printf '%s\n' "${hbm_status}" | grep -E '^[0-9a-f]{4}:' |
        grep -E 'lease none|lease [0-9]+ ms .* stale' | sed 's/^/      /'
    warn "systemctl status cmp-hbm-lease.service and its journal say which of the two this"
    warn "is: a keeper that never came, and a keeper that came and stopped, look the same"
    warn "here and cost the same watts, and three failures that looked like this shipped"
    warn "before anybody read a status file out loud."
else
    ok "every card has a current lease answer, and the governor may act on it"
fi
info "docs/HBM-PM.md is how to tell a wattmeter this is worth it, and what to read if not"
