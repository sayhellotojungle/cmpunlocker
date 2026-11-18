"""The link helper's register decode: is the link already at its target.

Values are measured. A Gen2 x16 link carrying traffic on the bench reads LnkSta 0x1102
at the GPU and 0x2102 at its upstream bridge. The helper previously required bit 15 of
that register, on the assumption that it is Data Link Layer Active; it is Link
Autonomous Bandwidth Status, it reads clear on a settled link, and the two ends of the
link do not implement the same optional status bits. The predicate therefore never
passed on this hardware: every card was retrained on every boot, none was reported as a
success, and the card the driver had trained to Gen4 was retrained back to Gen2. The
service that ran it has been removed, which test_the_helper_is_a_tool_and_not_a_boot_step
asserts. The decode remains under test because it is what allows this helper to be run
by hand without repeating that retrain.
"""
import re
import subprocess

import pytest

import repo

HAMMER = repo.ROOT / "tools" / "hammer.sh"

# LnkSta, the minimum generations it must satisfy, the ones it must not, and what it
# is.  Speed is bits 3:0, negotiated width bits 9:4, Link Training bit 11.
CASES = [
    ("1102", [2], [3], "Gen2 x16 carrying traffic, measured on the bench"),
    ("1103", [2, 3], [4], "Gen3 x16, the speed a Gen3 bridge caps the card to"),
    ("1104", [2, 3, 4], [5], "Gen4 x16, what the driver's Gen4 sequence produced"),
    ("F103", [2, 3], [4], "Gen3 x16 with DL_Active and both event bits set: still up"),
    ("1902", [], [2], "Gen2 x16 with Link Training set: not settled, so not yet"),
    ("0002", [], [2], "Gen2 at width zero: that is not a link"),
    ("1101", [], [2], "x16 at Gen1: below every target this package sets"),
]

# The helper is sourced, so the predicate under test is the one in tools/hammer.sh and
# not a copy of it; only the register read is replaced -- and it has to be replaced
# with a variable, because inside the function $2 is the function's own argument, and
# hammer.sh runs with set -u, so a slipped one there is an error rather than a lie.
SCRIPT = "\n".join([
    "set -uo pipefail",
    'source "$1"',
    'CMPUNLOCKER_TEST_LNKSTA="$2"',
    'read_link_status() { echo "${CMPUNLOCKER_TEST_LNKSTA}"; }',
    'for t in $3; do',
    '    if link_is_up_at "0000:05:00.0" "$t"; then echo "$t up"; else echo "$t down"; fi',
    "done",
])


def classify(status, targets):
    r = subprocess.run(["bash", "-c", SCRIPT, "hammer", str(HAMMER), status,
                        " ".join(str(t) for t in targets)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    verdicts = {}
    for line in r.stdout.splitlines():
        target, verdict = line.split()
        verdicts[int(target)] = verdict == "up"
    assert set(verdicts) == set(targets), \
        "the harness answered %r for %r (stdout %r)" % (verdicts, targets, r.stdout)
    return verdicts


@pytest.mark.parametrize("status,up,down,what", CASES,
                         ids=[case[3] for case in CASES])
def test_link_state_is_read_as_measured(status, up, down, what):
    verdicts = classify(status, sorted(set(up) | set(down)))
    for target in up:
        assert verdicts[target], "%s (%s) is up at Gen%d" % (status, what, target)
    for target in down:
        assert not verdicts[target], "%s (%s) is not up at Gen%d" % (status, what, target)


def test_the_event_bits_are_the_write_one_to_clear_pair():
    """The acknowledge has to write the event bits, or it acknowledges nothing.

    Bits 14 and 15 are what a retrain sets; the pair the helper used to write (11/12)
    are Link Training and Slot Clock Configuration, both read-only, so the write was
    ignored and every later read still looked like a link that had just changed.
    """
    verdicts = classify("F103", [4])          # event bits and DL_Active set, Gen3
    assert verdicts[4] is False, "bits 13/14/15 must not be mistaken for a speed"
    r = subprocess.run(["bash", "-c", 'source "$1"; echo $LINKSTA_EVENT_MASK',
                        "hammer", str(HAMMER)], capture_output=True, text=True)
    assert r.stdout.strip() == "0x6000", r.stdout


def test_the_helper_is_a_tool_and_not_a_boot_step():
    """No boot-time unit ships, and an install that had one gets it disarmed.

    The driver is the only component that can bring a card up at boot: BAR0 is mapped
    by the driver and this helper reaches only config space. The bench boot with the
    unit disabled is the measurement that established this: cards 05 and 06 at
    LnkSta=0x1102, card 09 at 0x1104, and no gen2.service entry in the journal. A unit
    added back to the package would introduce a second writer that runs after the
    driver, which is the condition that retrained the trained Gen4 link back to Gen2.
    """
    assert not (repo.ROOT / "systemd" / "gen2.service").exists()
    assert not (repo.ROOT / "tools" / "service.sh").exists()
    install = (repo.ROOT / "install.sh").read_text()
    disarm = re.search(r"for legacy_unit in ([^;]+);", install)
    assert disarm and "gen2.service" in disarm.group(1), \
        "install.sh must disarm the unit older installs left enabled"
    assert "systemctl enable" not in install, "install.sh must not arm anything at boot"
    assert "/usr/local/sbin/gen2-hammer" in install, "the manual tool is still installed"


def test_verify_owns_the_link_floor():
    """verify.sh judges the link itself, with Gen2 as the floor and nothing above it
    called a failure.

    That judgement used to be borrowed from tools/service.sh, the installer for the
    deleted unit; delegating to a script that no longer exists would silently skip the
    check, and demanding exactly Gen2 would report the driver's own Gen4 as a fault.
    """
    verify = (repo.ROOT / "verify.sh").read_text()
    assert "generation >= 2" in verify, "Gen2 is the floor"
    assert "GEN${generation} OK" in verify, "whatever was negotiated above it is reported"
    code = "\n".join(line.split("#")[0] for line in verify.splitlines())
    assert "service.sh" not in code, "the link check cannot be delegated to a deleted file"
