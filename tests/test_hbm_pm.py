"""Idle HBM power: one feature, in the files that can never see each other.

The patch widens two privilege masks during GSP bootstrap and asks for a registry key
before it will do even that; the module is what runs afterwards and writes through them;
the keeper is the userspace half that asks the driver the question the kernel cannot
answer on this chip, and whose answer the module waits for before it touches a card.
None of the three can check the others at build time, so this is where the seams between
them are checked: the key, which of the booter's masks is gated, the module's name as
every script has to spell it, the activity counters, which belong to GSP-RM and are only
ever read, and the lease, which is the only thing allowed to say a card is unclaimed.

Nothing here needs a GPU. The module is compiled by test_compile.py, and read-constants.py
is what proves a declared register is really in the file that claims it, so that runs here
too rather than being reimplemented.
"""
import ctypes
import os
import re
import subprocess
import sys

import pytest
import yaml

import repo

PATCH = (repo.ROOT / "driver/patches/sec2-postbl-plm-ss-cfg.patch").read_text()
BUILD = (repo.ROOT / "driver/build.sh").read_text()
MODULE = (repo.ROOT / "driver/hbm-pm/cmp_hbm_pm.c").read_text()
MAKEFILE = (repo.ROOT / "driver/hbm-pm/Makefile").read_text()
SETUP = (repo.ROOT / "tools/hbm-pm-setup.sh").read_text()
INSTALL = (repo.ROOT / "install.sh").read_text()
REMOVE = (repo.ROOT / "remove.sh").read_text()
VERIFY = (repo.ROOT / "verify.sh").read_text()
KEEPER = (repo.ROOT / "tools/hbm-lease.py").read_text()
UNIT = (repo.ROOT / "tools/cmp-hbm-lease.service").read_text()
DOC = (repo.ROOT / "docs/HBM-PM.md").read_text()
README = (repo.ROOT / "README.md").read_text()
CREDITS = (repo.ROOT / "CREDITS.md").read_text()
CONSTANTS = yaml.safe_load((repo.ROOT / "common/constants.yaml").read_text())
MASKS = CONSTANTS["unlocks"]["privilege_masks"]["registers"]
GOVERNOR = CONSTANTS["unlocks"]["hbm_pm"]


def between(start, end, text=MODULE):
    """One function's body, named by the two signatures on either side of it.

    The checks below are about where a decision is made and not merely whether the words
    appear: a guard that exists somewhere in the file but sits after the assignment it is
    meant to gate says nothing about what the governor does.
    """
    opened = text.index(start)
    closing = text.find(end, opened + len(start))
    assert closing != -1, "%s is not followed by %s" % (start, end)
    return text[opened:closing]


def test_the_gate_the_booter_asks_for_is_the_gate_the_build_writes():
    """The patch reads a registry key and build.sh is the only thing that sets it.

    A name that drifted on either side fails quietly in the worst direction: the masks
    stay closed, the governor reports a closed window once, and every card goes on
    drawing the current it drew before the feature existed.
    """
    asked = set(re.findall(r'osReadRegistryDword\(pGpu, "([A-Za-z0-9]+)"', PATCH))
    written = set(re.findall(r'REGISTRY_DWORDS\+=";([A-Za-z0-9]+)=1"', BUILD))
    assert asked == {"CMPEnableHbmPm"}, asked
    missing = asked - written
    assert not missing, "the booter asks for %s and driver/build.sh never sets it" % missing
    assert "--no-hbm-pm" in INSTALL, "the switch that drops the key is gone from the installer"


def test_only_the_two_masks_the_governor_writes_through_are_gated():
    """Every other mask in the table is opened by every build, because every other
    feature needs it at boot.  These two are opened for the governor alone, so the rows
    the gate names have to be the rows the governor writes through, and no more: a gate
    that grew past them would widen a block nothing uses, and one that shrank would leave
    the module unable to move the clock it came to move.
    """
    gated = {int(a, 16) for a in
             re.findall(r"plmTable\[plmIdx\]\.addr == (0x[0-9a-fA-F]+)U", PATCH)}
    declared = {int(MASKS[name]["addr"], 16)
                for name in ("fbpa_cfg_plm", "hbm_pll_plm")}
    assert gated == declared, [hex(a) for a in gated ^ declared]
    for name in ("fbpa_cfg_plm", "hbm_pll_plm"):
        addr = MASKS[name]["addr"].lower()
        assert re.search(r"\{ %sU, 0xffffffffU, \"[A-Z_]+_PLM\" \}" % addr, PATCH, re.I), \
            "%s is gated but is not a row of the table the gate walks" % name


def test_the_borrowed_counters_are_read_and_never_written():
    """The governor has no counter to arm: it watches the ones GSP-RM already runs.

    Writing a counter's mask or control would silently reconfigure a PMU collection
    RM thinks it owns, which is a much worse thing to discover from a wattmeter than
    a card that stays busy.  Note what this no longer claims: reading somebody else's
    counters is what makes a *refusal* cheap to check, not what makes an idle decision
    trustworthy -- that half moved to the lease, below, when the counters were
    measured reporting nothing at all on a card serving requests at 178 W.
    """
    for name in ("cmp_pmu_counter_addr", "CMP_PMU_COUNT"):
        written = [line.strip() for line in MODULE.splitlines()
                   if name in line and "writel" in line]
        assert not written, written
    assert "readl(bar + CMP_PMU_COUNT" in MODULE
    # And a counter is only trusted while its own words say firmware armed it.
    assert "after[3] != 2" in MODULE, "an unarmed counter would be read as traffic"


def test_the_counters_hold_a_veto_and_are_never_a_vote():
    """Any movement in any borrowed counter says "not now", and nothing the counters
    can say is ever the reason a card goes idle.

    Two designs failed here, in opposite directions.  The port's own treated every
    counter as traffic, and a card with a serving framework resident switched its
    memory PLL three times a second because a counter that only completes the coverage
    set ticks itself at 22 to 30 events a sample.  Restricting the wake decision to the
    counters that watch the framebuffer request path stopped that churn -- and then
    took the framebuffer away from a card at 100% utilisation, where both borrowed
    counters had been read reporting `active 0x0` for 8.4 s and 19.5 s stretches.  A
    counter that cannot see a workload is not evidence of its absence.

    So the set is never filtered, and the veto never grants anything.  What was added
    later is a floor on magnitude, which is a different thing and is measured rather
    than guessed: a resident process at rest moves a borrowed counter by 7 to 54 events
    a sample about once a second, the same counters reported 2395 to 170862 while that
    framework was writing weights, and millions across a served request.  Which is also
    why the floor defaults to zero -- one event refusing a card is the rule every watt
    in this file was measured under -- and why a movement the floor refuses must not be
    spent: ignoring a small reading cannot be allowed to also forget it.
    """
    assert "traffic_counters" not in MODULE, \
        "a wake subset is back, and it is the thing that wedged the card"
    assert "busy_events" not in MODULE, \
        "the veto is being filtered by which counter moved rather than by how far"
    # The set that holds the veto is every borrowed counter, so the reading function is
    # checked from its definition: there is a prototype for it near the knobs.
    counters = between("static int counters_moved(struct cmp_hbm *hbm, void __iomem *bar)\n{",
                       "static int lease_allows")
    assert "if (!(hbm->counters & (1u << i)))" in counters, \
        "the set being read is no longer every borrowed counter"
    assert "if (!delta)" in counters and "moved = true;" in counters
    assert counters.index("if (!delta)") < counters.index("moved = true;"), \
        "a counter that moved is no longer the thing that makes the card busy"
    assert "if (delta < wake_events)" in counters, \
        "the magnitude floor is gone, and without it a resident card is woken by its own "\
        "housekeeping once a second forever"
    assert counters.index("if (delta < wake_events)") < counters.index("*previous = c;"), \
        "a movement the floor refused also spent its events, so a workload whose first "\
        "sample is small would have to produce the whole floor again to be noticed"
    assert "return -EAGAIN;" in counters
    assert "return moved;" in counters, "the veto now reports a judgement rather than a reading"
    # The floor is a knob and its shipped answer is the old rule: every event is work.
    assert "static unsigned int wake_events;" in MODULE, \
        "the floor no longer defaults to zero, which changes the behaviour every column "\
        "in docs/HBM-PM.md was measured under"
    assert "kp->arg == (const void *)&wake_events" in MODULE, \
        "wake_events=0 is no longer writable, so the shipped rule cannot be put back"
    assert "wake_events > CMP_HBM_MAX_WAKE_EVENTS" in MODULE, \
        "the floor has no upper bound, and a floor no real sample reaches is the counters' veto "\
        "switched off by a policy write rather than a looser policy"
    # The coverage gate stays: a card whose firmware arms nothing useful must not be
    # able to reach the idle clock even with a keeper willing to vouch for it.
    discovery = between("static bool discover_counters", "static void rebase_counters")
    assert "activity counter coverage missing" in discovery
    assert "if ((covered[bank] & cmp_pmu_required[bank]) != cmp_pmu_required[bank])" in discovery, \
        "the coverage gate became a suggestion"
    # The counters are read a second time at the two points where a stale answer is
    # still cheap to act on: immediately before the stop, and immediately after it.
    go_idle = between("static int go_idle", "static bool read_counter")
    assert "counters_moved(hbm, bar)" in go_idle, \
        "the decision is being taken from a sample up to poll_ms old and nothing newer"
    assert "HBM idle refused: activity at the moment of the stop request" in go_idle
    transition = between("static int hbm_transition", "static void reset_state")
    assert "fb_proved_busy(hbm, bar)" in transition, \
        "nothing checks the card after the framebuffer has been taken"
    # ...and that second check compares configuration, not counts: a counter whose own
    # block is stopped cannot advance, so a delta check there could only ever say
    # "quiet", which is worse than no check because it looks like a guard.
    proved = between("static int fb_proved_busy", "static int hbm_transition")
    assert "memcmp(c.mask" in proved and "HBM counter re-armed after FB stop" in proved
    assert "c.count" not in proved, \
        "the post-stop check reads a count that a stopped block cannot advance"
    for phrase in ("The counters are the veto", "The lease is the permission"):
        assert phrase in DOC, "the doc no longer says which half decides what: %s" % phrase


def test_a_card_that_will_not_stay_down_stops_being_asked():
    """660 switches in twenty minutes, and not one watt saved by any of them.

    A resident process at rest moves a borrowed counter about once a second, so a card
    held by a serving framework went down, came back up on that tick, and went down
    again: every pair is 3.3 ms of register sequence, and the card spent the interval
    between them at the busy clock anyway.  This was measured rather than imagined --
    an operator-shaped force loop drove exactly that on a live card while a service was
    running on it.

    The counters still veto, the coverage gate still stands, and the lease still has to
    be current on the way in and on every sample while the card is down.  What the guard
    below adds is the refusal to run that experiment once a second forever: a pause that
    steps with the number of short stays, an upper bound so that one bad hour cannot
    disqualify a card for a day, and the count on the card line, because somebody
    reading a card that will not stay idle has to be able to tell that the card is not
    idle from that the governor is broken.
    """
    sample = between("\tbusy = ret > 0;", "\tif (hbm->idle) {\n\t\t/* The card is down")
    assert "hbm->flaps++" in sample, \
        "a card woken before it had earned the quiet period that got it down is no longer counted"
    assert "hbm->idle_since" in sample and "hbm->calm_at" in sample, \
        "the short stay is counted but nothing is delayed, which is a statistic and not a guard"
    assert "CMP_HBM_MAX_FLAP_PAUSE_MS" in sample, \
        "the pause doubles with no ceiling, so a card that wobbled once is never asked again"
    assert "hbm->flaps = 0" in sample, \
        "one short stay now disqualifies the card forever instead of being forgotten"
    # The second question the guard asks, and what it is for. The comparison the first version made --
    # a stay against the quiet period that earned it -- goes blind on the resident card that started
    # wobbling at `wake_events=512`, whose stays cleared `idle_ms` by a hair; that card is answered by
    # the floor, not by a pause. What the duty-cycle question covers is the shape `idle_ms` cannot
    # describe: a card up more than it is down, which is a card the feature is not saving anything on.
    assert "stayed < worked" in sample and "hbm->idle_since - hbm->busy_since" in sample, \
        "a stay is back to meaning something only beside the quiet period that earned it, so a card " \
        "that spends more of its life at the busy clock than at the idle one is counted as normal"
    assert "hbm->busy_since = now" in sample, \
        "the interval up is never recorded, so the comparison next to it compares against zero"
    # And the exemption, which is what keeps this from punishing the thing the feature is
    # for: a card woken by 2785189 events served a request, and delaying its next watt is
    # the only effect a pause has.
    assert "woken_by_work" in sample and "CMP_HBM_FLAP_WORK_FACTOR" in MODULE, \
        "a stay ended by the keeper's own polling is now indistinguishable from one ended "\
        "by a served request, and pausing both is pausing the feature"
    assert "!wake_events ||" in sample, \
        "the duty-cycle half of the guard is not disabled at wake_events=0, which is the " \
        "shipped rule every column in docs/HBM-PM.md was measured under"
    gate = between("if (!hbm->idle && now - hbm->quiet_since", "static bool held_by_nvidia")
    assert "now < hbm->calm_at" in gate, "the pause is not consulted at all"
    assert gate.index("now < hbm->calm_at") < gate.index("go_idle(hbm, bar)"), \
        "the pause is checked after the transition is attempted, which is not a pause"
    assert "retry_at" in gate and gate.index("retry_at") < gate.index("now < hbm->calm_at"), \
        "the failed-transition backoff and the flap pause have collapsed into one field, and "\
        "they are different judgements: one is a card whose hardware did not answer"
    assert "hbm->idle_since = now" in gate, \
        "nothing records when the card went down, so nothing can tell a short stay from a long one"
    assert re.search(r"scnprintf\(flaps, sizeof\(flaps\), \" flaps %u\"", MODULE), \
        "the count is not on the card line, and the wobbling is invisible to whoever has to read it"
    assert "wake_events=%u" in MODULE, \
        "the status header no longer prints the floor, so a card refusing to go idle cannot be "\
        "told apart from a card whose floor is set above its own housekeeping"


def test_declines_counts_sayings_and_errors_counts_attempts():
    """`declines` growing with the poll rate is a number nobody can read.

    An eligible card with no keeper answers `-EAGAIN` on every poll, so counting
    polls put 1222 declines on a card in the thirty seconds after it went quiet,
    with nothing happening at all -- which is indistinguishable, at a glance at
    the status file, from hardware that refused forty times a second. The two
    fields answer different questions and are therefore counted differently:
    `errors` stays per-attempt, because the card that wedged finished at
    `errors 24814` and a number climbing there is the alarm whether the reason
    ever changed or not.
    """
    reported = between("static void report_result", "static void govern")
    assert "bool newsaying" in reported, "the edge the refusal is counted on is gone"
    assert "} else if (unprinted ||" in reported, \
        "the log line is deciding its own rate from the last poll's return value"
    edge = reported.index("if (newsaying)")
    declines = reported.index("stats.declines")
    errors = reported.index("stats.errors")
    assert edge < declines < errors, \
        "declines is no longer guarded by the edge, or the guard reached errors"
    assert "newsaying" not in reported[declines:errors], \
        "errors became edge-triggered too, and a dead card would then count one attempt"
    assert "1222" in DOC, "the doc no longer records why the two counters differ"


def test_a_refusal_that_keeps_being_true_is_printed_once():
    """The 30 s latch was real and did not hold, and the reason is two conditions that look
    interchangeable and are not.

    A card with a resident client alternates a poll that had nothing to do, which returns
    0, with one the lease refuses.  A print latch comparing against the last poll's return
    value therefore sees a changed error on every refusal and prints every one: measured on
    hardware holding two tensor-parallel workers, one line every two to six seconds per
    card, 71 lines in one boot, with the reason never once changing.  Counting
    declines on that same edge is right; printing on it is not.
    """
    reported = between("static void report_result", "static void govern")
    printed = reported[reported.index("} else if (unprinted ||"):reported.index("if (ret) {")]
    assert "worker->logged_error = ret" in printed and \
           "worker->logged_reason = hbm->reason" in printed, \
        "the print latch remembers something other than what it printed"
    assert "last_error" not in printed and "last_reason" not in printed, \
        "the print latch is back to reading the previous poll, which is what made every "\
        "refusal on a serving card look like the first one"
    assert reported.index("worker->last_error = ret") > reported.index("if (ret) {"), \
        "the decline edge is now being updated before the counter that uses it"


def test_every_script_spells_the_module_the_same_way():
    """Five files name this module and none of them can see the others.

    The Makefile's obj-m line is the truth, so everything else is compared to it: a setup
    script that installs one name and a remove script that rmmods another leaves a loaded
    governor mapping BAR0 after the rest of the install is gone.
    """
    name = re.search(r"obj-m \+= (\S+)\.o", MAKEFILE).group(1)
    assert name == "cmp_hbm_pm"
    assert 'MODULE="%s"' % name in SETUP
    assert 'kobject_create_and_add("%s", kernel_kobj)' % name in MODULE
    status = "/sys/kernel/%s/status" % name
    assert status in INSTALL, "the installer promises a different file than the module makes"
    assert status in VERIFY
    assert re.search(r"rmmod %s\b" % name, REMOVE), "remove.sh leaves the governor loaded"
    loaded = re.search(r"> (/etc/modules-load\.d/\S+)\n", SETUP).group(1)
    assert loaded in REMOVE, "%s would outlive the install that wrote it" % loaded


@pytest.mark.parametrize("profile", sorted(CONSTANTS["profiles"]))
def test_declared_registers_are_still_where_the_constants_say_they_are(profile):
    """read-constants.py is the build's own check, so it is run rather than imitated.

    It fails the build, not just a test, when a register listed in constants.yaml has
    stopped appearing in the patch or module that claims it, which is how a documented
    address and the code that uses it are kept from splitting apart.
    """
    r = subprocess.run(
        [sys.executable, str(repo.ROOT / "tools/read-constants.py"),
         str(repo.ROOT / "common/constants.yaml"), str(repo.ROOT / "driver/patches"),
         str(repo.ROOT / "driver/build.sh"), profile],
        capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_documented_knobs_are_the_knobs_the_module_takes():
    """Every parameter a user can turn is described in the doc, and the doc invents none.

    These are the values an idle measurement is taken with, so a doc that names a knob the
    module renamed is worse than no doc: it sends someone to write to a file that is not
    there, on a machine that is already drawing more than they expected.
    """
    turned = set(re.findall(r"^(?:module_policy|module_param|module_param_cb)\((\w+),", MODULE, re.M))
    described = set(re.findall(r'^(?:module_policy|MODULE_PARM_DESC)\((\w+), "', MODULE, re.M))
    assert turned, "the module declares no parameters at all"
    assert turned == described, "a knob has no description: %s" % sorted(turned ^ described)
    undocumented = {name for name in described if "`%s`" % name not in DOC}
    assert not undocumented, "docs/HBM-PM.md does not document %s" % sorted(undocumented)
    invented = {name for name in re.findall(r"parameters/(\w+)", DOC)
                if name not in described}
    assert not invented, "docs/HBM-PM.md names %s, which the module does not take" % sorted(invented)
    assert GOVERNOR["source"] == "hbm-pm/cmp_hbm_pm.c"


def test_the_governor_is_a_step_of_its_own():
    """The feature asks the driver for something at build time and the kernel for
    something at run time, so the installer says which of the two it is doing while it
    does each, and reports what it got: a governor that would not build costs watts and
    nothing else, which is not the same message as a card that failed its unlock.
    """
    assert 'step "Lowering idle HBM power"' in INSTALL
    assert "tools/hbm-pm-setup.sh" in INSTALL
    assert "HBM_PM_STATUS" in INSTALL, "the summary no longer says what happened"
    assert "hbm-pm/cmp_hbm_pm.c" in DOC


def test_the_perf_table_decoder_cannot_touch_the_card():
    """The decoder exists to be trusted with an answer about the card, so it is not
    allowed any way of changing it.

    This is what makes the read safe to run on a machine that is serving: a file that
    only formats a buffer somebody else filled cannot disturb a transition, and the
    claim is worth more checked than commented.  The copy itself lives in the one
    function below it, which is the only place the image may be read out of the card.
    """
    decoder = between("static u16 cmp_le16", "static int cmp_perf_table_fill")
    copier = between("static int cmp_perf_table_fill", "static ssize_t perf_table_show")
    writes = ["writel", "writew", "writeq", "iowrite", "memcpy_toio", "ioremap", "pci_write"]
    found = [name for name in writes + ["readl", "worker->bar"] if name in decoder]
    assert not found, "the decoder reached for the card itself: %s" % found
    assert "memcpy_fromio" in copier, "nothing copies the image out any more"
    assert not [name for name in writes if name in copier], "the copy became a write path"


def test_the_decoder_reads_the_layout_upstream_refused_to_edit_without():
    """A row decoded at the wrong width produces numbers that look like clocks.

    Every constant here is one gpu-unlock checks before it will touch a table at all: if
    the module drifts from them it is describing some other structure in the image, and
    it would say so with the same confidence, which is worse than saying nothing.
    """
    for name, value in (("CMP_PERF_FORMAT", "0x60"), ("CMP_PERF_HEADER", "10"),
                        ("CMP_PERF_ROW_SIZE", "65"), ("CMP_PERF_DOMAINS", "10"),
                        ("CMP_PERF_MIN_MASK", "0x3fffu"), ("CMP_PERF_ROW_EMPTY", "0xff"),
                        ("CMP_PERF_ID_P0", "15"), ("CMP_PERF_ID_P8", "7"),
                        ("CMP_HBM_PROM_OFFSET", "0x00300000u")):
        assert re.search(name + r"\s+" + value + r"\b", MODULE), \
            "%s is no longer %s" % (name, value)
    # The five domains whose floors a generated P8 lowers, and no others: that set is
    # the difference between an idle core and a card that never comes back.
    assert re.search(r"wake_domains\[[^\]]*\]\s*=\s*\{\s*0,\s*1,\s*3,\s*5,\s*9\s*\}", MODULE), \
        "the domains a generated P8 would lower are no longer GPC, XBAR, SYS, NVD, HOST"
    assert GOVERNOR["registers"]["prom_window"]["addr"] == "0x00300000"


def test_the_table_pointer_is_followed_the_way_the_firmware_means_it():
    """Two mistakes here produce a confident wrong answer, and both were made once.

    The pointer to the table is read from the data image where the BIT token points at,
    unadjusted, and the NPDS extension is added to the value it holds: add it to the
    address instead and the decoder describes bytes that are not a table.  And every
    offset recorded on the way was measured from the PCI image, so the base has to move
    before any of them is trusted.
    """
    body = between("static int cmp_perf_table_read", "static int cmp_perf_table_format")
    assert body.index("rom += out->image") < body.index("cmp_le32(rom + out->data)"), \
        "the table pointer was read before the base moved to the PCI image"
    assert body.index("cmp_le32(rom + out->data)") < body.index("offset += out->adjust"), \
        "adjust was applied to the pointer's location rather than to where it points"
    assert "rom + out->data + out->adjust" not in body


def test_a_generated_p8_is_reported_impossible_for_the_real_reason():
    """Whether a P8 could be added is the question this decoder was written to answer, so
    it answers with the reason, in the terms the unlock itself uses to refuse.

    A bare "no" would have ended a line of work on an artefact of the walk.
    """
    body = between("static int cmp_perf_table_read", "static int cmp_perf_table_format")
    for marker in ("CMP_PERF_ID_P8", "-EALREADY", "-EBADMSG", "-EOPNOTSUPP",
                   "\\x08\\x03\\x00\\x00", "\\x08\\x03\\xff\\x00", "free_slots"):
        assert marker in body, "the P8 precondition no longer checks %s" % marker
    printed = between("static int cmp_perf_table_format", "static int cmp_perf_table_fill")
    for phrase in ("the board already provides a P8", "more than one P0",
                   "not possible on this image"):
        assert phrase in printed, "the verdict is no longer printed as words: %s" % phrase


def test_the_perf_table_is_a_file_of_its_own():
    """Reading it copies a megabyte out of the card, so it must not ride along on the
    status file, where somebody would pay that just to ask what clock a card is at.
    """
    assert "__ATTR_RO(perf_table)" in MODULE
    assert "sysfs_create_file(status_dir, &perf_table_attr.attr)" in MODULE
    assert "sysfs_remove_file(status_dir, &perf_table_attr.attr)" in MODULE
    status = between("static ssize_t status_show", "static struct kobj_attribute status_attr")
    assert "perf_table" not in status, "the status file reads the PROM again"
    assert "/sys/kernel/cmp_hbm_pm/perf_table" in DOC, \
        "the doc does not tell anyone that the file exists"


UPSTREAM = "https://github.com/Zagitta/gpu-unlock/tree/3.0.0-beta"


def test_the_port_says_where_the_port_was_taken_from():
    """Two of the three things this feature does are somebody else's design: the memory
    clock sequence, and the perf-table walk with the P8 layout it decodes.

    Naming the project is the honest thing to do and also the useful one, because the
    questions a reader has about a ported sequence are questions about where it came from.
    A credit that is not checked is a credit that goes away in a refactor.
    """
    assert UPSTREAM in DOC, "docs/HBM-PM.md no longer says which project this came from"
    assert UPSTREAM in MODULE, "the source file no longer says which project it was ported from"
    assert UPSTREAM in README, "the README no longer says which project this came from"
    assert UPSTREAM in CREDITS, \
        "CREDITS.md no longer carries the project this feature was ported from"
    for half in ("hbm.c", "vbios.c"):
        assert half in MODULE, "the file no longer names the upstream source of the %s path" % half


def test_the_p8_answer_keeps_every_reason_and_the_one_thing_unproven():
    """The perf-table question was asked, answered, and is expensive to re-ask, so the
    doc has to keep carrying all of the answer -- including the part that was never
    measured, which is the part a reader would otherwise conclude was ruled out.

    Each marker is one of the three negatives, in the form that makes it checkable: a
    reader should be able to re-derive every one of them from the card and from upstream's
    source rather than take them on faith.
    """
    for evidence in ("empty != offset + 10",             # insertion point, upstream's own test
                     "HBM tuple stay intact",            # upstream's comment: P8 is not a memory edit
                     "0f 08 03 00 00",                   # what this card's P0 row actually is
                     "lowest of the hundred graphics points",
                     "-lgc 135,135",                     # the clamp, measured
                     "0x2c",                             # where the power data really is
                     "NVreg_RegistryBinaryFilePerDevice",
                     "615.78.08"):
        assert evidence in DOC, "the P8 answer no longer carries %r" % evidence
    assert "What none of this measured" in DOC, \
        "the answer now reads as if the fabric domains were tested too"
    assert UPSTREAM in DOC


def test_the_two_power_tables_are_labelled_by_card_and_not_by_clock():
    """Both cards read 1728 MHz busy now, so a column that used to be described as "the
    1458 MHz card" describes a reading somebody took, not a thing a card is.

    Labelling the columns by bus id keeps the numbers usable and stops the table from
    being read as a clock comparison, which it never was.
    """
    assert DOC.count("| `mode` | `0000:05:00.0` | `0000:06:00.0`") == 1
    assert "1458 MHz busy then" in DOC
    # Checked by paragraph rather than by line: prose here is hard-wrapped, and a check
    # that reads one line at a time is a check that fails on a reflow.
    for paragraph in DOC.split("\n\n"):
        if "1458" in paragraph:
            assert any(hint in paragraph for hint in ("then", "until", "were read",
                                                      "quiet 75240")), \
                "1458 is being asserted as a property of a card: %s" % paragraph[:160]


LEASE_REASONS = ("idle lease absent: no keeper has answered for this card",
                 "idle lease stale",
                 "idle lease refused: the keeper reports work on the card",
                 "idle lease is too young to be believed",
                 "idle lease has not held quiet for the whole idle window")


def test_nothing_reaches_the_stop_request_without_an_answer_from_userspace():
    """The permission to idle a card comes from outside the kernel, and every way of not
    getting one counts as the answer "somebody is using this".

    The counters were measured on a card serving requests reporting `active 0x0` with
    `quiet` at 8444 ms while `nvidia-smi` read 100% and 178.2 W, and again at 19536 ms
    at 24% and 74.7 W.  There is no in-kernel replacement for the question either: the
    ampere headers this driver ships define no engine-busy status at all, because on this
    chip that answer lives inside GSP-RM.  So the gate below is not belt-and-braces over
    the counters -- it is the only positive evidence the feature has, and the failure it
    is written against is a card that needed a reboot.
    """
    lease = between("static int lease_allows", "static void cmp_hbm_backoff")
    for reason in LEASE_REASONS:
        assert reason in lease, "the lease lost a way of refusing: %s" % reason
        assert reason in DOC, "a refusal nobody can look up is a refusal nobody can fix: %s" % reason
    # Absent is the default, so the order of the checks is the order of the failure
    # modes: no answer, then an old one, then a refusal, then a young one.
    assert lease.index("if (!written)") < lease.index("idle lease stale") < \
        lease.index("idle lease refused"), "a stale or absent lease is being read as a verdict"
    assert "if (!lease_ms)" in lease, "waiving the lease has to stay possible, loudly, for a measurement"
    # One report is a sample and two is a period, and the period has to cover the
    # window the counters were asked to vouch for rather than arrive at the end of it.
    assert "worker->lease_streak < CMP_HBM_LEASE_MIN_REPORTS" in lease
    assert "#define CMP_HBM_LEASE_MIN_REPORTS\t2" in MODULE, \
        "the minimum that can disagree is no longer two"
    assert "now - since < (u64)idle_ms * 1000000" in lease
    # Both ways *onto* the idle clock go through it, including the pin that exists to
    # measure the watts: pinning must not be reachable by accident on a used card.  The
    # third call below is the way out, and has its own test.
    sampling = between("static int sample_state", "static bool held_by_nvidia")
    assert sampling.count("lease_allows(worker, now)") == 3, \
        "one of the two ways onto the idle clock, or the re-ask while idle, no longer asks"
    assert "write `force` to /sys/kernel/cmp_hbm_pm/lease" in sampling, \
        "the pinned idle clock has no lease to ask for and no way for an operator to say it is meant"
    # The file is write-only: what was said is read back next to the state it produced,
    # and a second file holding the same words could disagree with the first.
    assert "__ATTR_WO(lease)" in MODULE
    assert "sysfs_create_file(status_dir, &lease_attr.attr)" in MODULE
    assert "sysfs_remove_file(status_dir, &lease_attr.attr)" in MODULE
    store = between("static ssize_t lease_store", "static struct kobj_attribute lease_attr")
    assert 'sscanf(buf, "%31s %15s %47[^\\n]"' in store, "an overlong write must not be truncated into a verdict"
    for verb in ('"quiet"', '"busy"', '"force"'):
        assert verb in store, "the verb set changed shape: %s" % verb
    assert "return -EINVAL;" in store and "return -ENODEV;" in store, \
        "an unreadable line or an unknown card must not look like an accepted answer"
    # Content published before the timestamp, with release semantics: a reader that sees
    # the new stamp has to see the new verdict with it.
    assert store.index("WRITE_ONCE(worker->lease_quiet, quiet)") < \
        store.index("smp_store_release(&worker->lease_written, stamp)")
    assert "smp_store_release" in store, "the stamp is published without ordering the content behind it"
    # And the lease is on the status line, because an operator has to be able to see
    # the answer the governor saw rather than infer it from the watts.
    status = between("static ssize_t status_show", "static struct kobj_attribute status_attr")
    assert "lease %s" in status and "lease_ms=%u" in status
    for word in ("waived", "none", "forced", "stale"):
        assert word in status, "the status file cannot say the lease failed as %s" % word
    assert "systemctl status cmp-hbm-lease.service" in DOC, \
        "the doc tells you to read the lease without saying where the keeper is"


def test_a_card_at_the_idle_clock_is_asked_again_every_sample():
    """The permission to be idle has to be current, not historical.

    The lease used to be asked only on the way *into* idle, and never again.  A keeper that
    died while a card was down therefore left that card down indefinitely: the bench recorded
    `lease 10050 ms quiet stale` beside a card still sitting at the low clock and drawing
    fourteen watts less, with nothing left to bring it up but the borrowed counters -- the
    one piece of evidence this module does not trust, having measured them report `active 0x0`
    for 8.4 s on a card serving requests at 178 W.  Being late to wake costs a watt; being
    unable to wake costs the card, which is the accident this feature is named for.
    """
    decision = between("static int sample_state", "static bool held_by_nvidia")
    while_idle = between("\tif (hbm->idle) {",
                         "\tif (!hbm->idle && now - hbm->quiet_since", decision)
    assert "lease_allows(worker, now)" in while_idle, \
        ("an idle card is not being re-asked: the counters are not trusted to notice work, "
         "so a keeper that stops answering has to be the thing that takes the card back up")
    assert "go_busy(hbm, bar)" in while_idle, \
        "the re-ask refuses but nothing moves the clock back"
    assert decision.count("lease_allows(worker, now)") >= 3, \
        ("the way in, the way out and the pin ask the same question; one of them has gone "
         "and each of them is a different card being let down")

    # A card pulled back up by the lease rather than by traffic arrives in the log with
    # `active 0x0 events 0`, which is indistinguishable from a governor wobbling on its own.
    assert '", because "' in MODULE and "hbm->reason && !hbm->idle" in MODULE, \
        "the reason the clock moved is not in the transition line, so the record cannot say "\
        "whether the wake was traffic or a keeper that stopped answering"


def test_the_keeper_refuses_rather_than_guessing():
    """The userspace half is trusted with one judgement, so it is checked to fail the way
    a refusal fails.

    Every unknown in here -- no NVML, no driver, an unreadable card, a process list the
    driver will not give, a missing constants block -- ends as `busy` or as silence, and
    the module treats silence as `busy` too.  A keeper that invented floors after a bad
    YAML file, or that treated a failed NVML call as an empty process list, would be
    deciding when a card may be disturbed by itself.
    """
    for call in ("nvmlDeviceGetComputeRunningProcesses_v3",
                 "nvmlDeviceGetGraphicsRunningProcesses_v3",
                 "nvmlDeviceGetUtilizationRates", "nvmlDeviceGetPowerUsage"):
        assert call in KEEPER, "the keeper stopped asking %s" % call
    verdict = between("def verdict(", "def report(", KEEPER)
    for refusal in ('"busy", "unreadable"', '"busy", "noproclist"', '"busy", "nosample"',
                    '"busy", "noutil"', '"busy", "nopower"'):
        assert refusal in verdict, "an unknown reading is no longer a refusal: %s" % refusal
    assert 'raise RuntimeError("NVML reports no cards' in between("def report(", "def emit(", KEEPER), \
        "an empty card list is being taken for a list of idle cards"
    assert "if clients > 0" in verdict, "a compute or graphics client is no longer enough to refuse"
    assert '"quiet"' in verdict and verdict.index('"busy"') < verdict.index('"quiet"'), \
        "`quiet` has become the default answer"
    # The window belongs to the caller: several consecutive rounds agreeing is the claim,
    # and a dict rebuilt every round would be a claim about one sample.
    main = between("def main(", "if __name__", KEEPER)
    assert "history = {}" in main and main.index("history = {}") < main.index("while True")
    assert "window < 2" in main, "one sample is being allowed to be the evidence again"
    assert 'if floors["power_floor_mw"] <= 0 or interval <= 0' in main, \
        "a keeper configured to keep nothing busy is still a keeper that can say quiet"
    assert "refusing to run" in main
    assert "emit(lines" in main
    assert KEEPER.count("history") >= 4, "the rounds stopped accumulating"
    # It writes two things and reads nothing it could turn into a weapon: the lease, and the
    # counter floor the resident rule is armed with. Both are files the governor itself put in
    # /sys, both are opened write-mostly with no create, and there is no way from here to a
    # register -- no subprocess, no nvidia-smi, no /dev/nvidia0, no mmap. The floor is the
    # second one and it is the keeper's because the noise it has to clear is the keeper's own
    # polling: a policy whose two halves can be tuned apart is a policy that gets tuned wrong.
    assert 'os.open(path, os.O_WRONLY)' in KEEPER
    assert "SystemExit(75)" in KEEPER, "a missing lease file should exit for restart, not die"
    assert "/sys/module/cmp_hbm_pm/parameters/wake_events" in KEEPER, \
        "the keeper no longer names the floor it arms the resident rule with, so the rule and " \
        "the floor can be set on different machines"
    floor_writer = between("def ensure_wake_floor(", "def main(", KEEPER)
    assert 'open(path, "w"' in floor_writer, \
        "the keeper reads the floor and claims to have set it without writing it"
    arming = between("The relaxed rule and the counter floor", "One line at the start", main)
    assert "return None" in floor_writer and "clients_veto = True" in arming, \
        "a resident rule that could not get its floor set stays relaxed anyway, which is the " \
        "wobbling card rather than the quiet one"
    assert KEEPER.count('open(') <= 8, "the keeper has started opening things nobody named it to"
    assert "O_CREAT" not in KEEPER and "O_RDWR" not in KEEPER, \
        "the keeper opened something it should only ever read"
    imported = set(re.findall(r"^\s*(?:import|from) ([\w.]+)", KEEPER, re.M))
    assert imported <= {"argparse", "ctypes", "os", "re", "sys", "time", "yaml"}, imported
    # Its numbers are the project's numbers.
    keeper = GOVERNOR["keeper"]
    for key in ("interval_s", "window", "util_floor_pct", "power_floor_mw", "clients_veto"):
        assert key in keeper, "unlocks.hbm_pm.keeper lost %s, which the keeper refuses to run without" % key
    # Relaxing the client list is allowed only as a package, and the package is what is asserted here
    # rather than the setting.  `clients_veto: "y"` used to be pinned, on the grounds that nothing could
    # replace the client list as a refusal; what replaced it is three measurements -- utilisation read
    # 87 to 96 % on every serving round and 0 % on every quiet one, the smallest movement any request
    # ever made on the borrowed counters was 81981 events, and a card served at the idle memory clock
    # from beginning to end came back HTTP 200 in 1.95 times the time with its ECC counts unmoved.
    # Those hold the relaxation up, and each condition below is one of them falling over.
    clients_relaxed = str(keeper["clients_veto"]).strip().lower() in ("n", "no", "false", "0")
    if clients_relaxed:
        assert str(keeper.get("peer_veto", "y")).strip().lower() in ("y", "yes", "true", "1"), \
            "the client list was relaxed while a card may still be lowered under a busy neighbour, " \
            "which is the one measured case of a held card being harmed (0% utilisation at 61 to " \
            "67 W while its peer read its memory) that no floor on that card can see"
        floor = int(keeper["resident_wake_events"])
        assert 545 * 10 <= floor <= 81981 // 8, \
            "the counter floor the keeper arms with is outside the window between the keeper's own " \
            "noisiest round (545 events a sample) and the quietest request ever measured (81981): %d" % floor
        assert int(keeper["util_floor_pct"]) == 0, \
            "utilisation is the discriminator the resident rule rests on, and a non-zero floor would " \
            "wave through the 1 %% rounds that refused a card twelve times in one run"
    assert int(keeper["window"]) >= 2, int(keeper["window"])
    assert int(keeper["power_floor_mw"]) > 50000, \
        "the power floor is at or below what an idle card at busy clocks measures here (50-57 W)"
    assert 'keeper.get("clients_veto"' in KEEPER, \
        "the keeper stopped reading the rule from the file that argues for it, so the flag is "\
        "now the only way to say it and the shipped answer depends on which host ran what"
    assert "unlocks.hbm_pm.keeper" in DOC, "the keeper's thresholds are not documented where its knobs are"


def test_the_resident_watts_are_recorded_with_the_readings_that_produced_them():
    """The resident case spent most of this port's life as an arithmetic result.

    "About 14 W a card" was the empty-card saving re-used for a card with a container on it, and the
    first version of that number came from a policy whose idle decision rested on counters later
    shown to say nothing at all on a card drawing 178 W.  What is in the doc now is a pair of readings
    off the board's own sensor, taken seconds apart on one card, with the neighbour left alone to
    show what drift looks like, plus the two shadow runs that say what the permission costs to
    believe.  Those strings are the evidence, so they are pinned: a doc that quietly drops back to a
    derived figure is a doc that has stopped describing this machine.
    """
    resident = between("### What the floor is once a job owns the memory",
                       "### What a switch costs", DOC)
    for reading in ("56.94, 57.44, 59.01 W", "41.02, 41.12, 41.31 W", "49.23, 49.32, 49.43 W",
                    "15.9 to 17.7 W", "10836 ms", "10.8 s", "26 switches in 25 s"):
        assert reading in resident, "the resident saving lost the reading it came from: %s" % reading
    assert "51.8 W at the busy clock" not in resident, \
        "the subtraction has come back to stand in for a measurement"
    # The shadow runs, and the one thing they settled: which of the keeper's readings is load-bearing.
    for evidence in ("0.08 s and 0.97 s", "46 rounds", "resident2,util0,power49W",
                     "3.3 s later at `window=3`", "12.4 s later at `window=12`"):
        assert evidence in resident, "the shadow run's evidence is missing: %s" % evidence
    assert "the card being served read the same watts as" in resident, \
        "power is being described as a discriminator again, and 46 rounds of it reading 56 to 57 W " \
        "on a serving card is what says it is not"
    # And the honest half: what this file still does not know, including the experiment that was
    # supposed to settle the transition-under-traffic question and did not reach it.
    unchecked = between("## What has not been checked yet",
                        "**What none of this measured**", DOC)
    for evidence in ("3.62 s", "cannot be reached", "1.95 times", "halt latch"):
        assert evidence in unchecked, "the open question is no longer described honestly: %s" % evidence
    # The pilot, on the box, with the policy doing the switching rather than a hand-written lease.
    for reading in ("39 s (98 %)", "37.3 W", "35.9 W", "26 to 49 ms", "events 517"):
        assert reading in resident, "the resident pilot lost the number it was scored on: %s" % reading
    # The shipped configuration going through the installer's own path, and the two measurements that
    # bound the floor from either side. Without the second pair the floor is a tuned number, and a
    # tuned number in the counter veto is a veto somebody can walk through.
    for reading in ("38.48 W", "37.34 W", "37.55 W", "54.14 W", "56.69 W",
                    "counter floor in force 8192", "81981", "545 events",
                    "8192 / 520 = 15.8 s", "0.053, 0.094 and 0.096 s"):
        assert reading in resident, "the armed run lost the reading that justifies it: %s" % reading
    # The module that carries the guard, installed, and what it was asked to do on hardware: refuse the
    # ceiling, and fire when a stay was too short to have earned anything -- including the part of a
    # guard that matters, which is that it then stops asking.
    for reading in ("echo 2000000", "flaps 5", "events 22", "waiting out the pause after a stay too",
                    "8 switches in the first twenty seconds", "44 seconds of 45", "41.85 W"):
        assert reading in resident, "the installed module was let to claim the guard works without %s" % reading
    assert "noise is the keeper" in resident, \
        "the wobble is being described without its measured cause, which is the three NVML calls a " \
        "round the keeper makes on the card it is asking whether the card is idle"
    # What being wrong costs, from the section that priced it by pinning the clock and asking anyway.
    for reading in ("6.238 s, 1.95 times", "3.199 s", "5/5 HTTP 200", "110.91 W"):
        assert reading in resident, "the price of a mistaken switch lost its measurement: %s" % reading
    assert "fb-stopped` is a flag set for microseconds" in resident, \
        "the framebuffer is being described as held out of service again, which is what the first " \
        "version of this file said before hbm_transition() was read"


def test_the_keeper_refuses_to_poll_faster_than_the_box_survived():
    """`interval_s` is a load limit on GSP-RM, not a responsiveness preference.

    Executed rather than read, because what is being pinned is a refusal: a keeper that
    prints a warning and polls anyway is the same keeper that preceded a stalled
    tensor-parallel worker, and it would pass every text match in this file.  The rate
    that was harmless is the shipped one -- three NVML calls per card per round, each an
    RPC into the firmware on the path a workload's launches take -- so anything faster
    has to be asked for by name.
    """
    keeper_path = repo.ROOT / "tools/hbm-lease.py"

    def run(*extra):
        return subprocess.run([sys.executable, str(keeper_path), "--lease", os.devnull]
                              + list(extra), capture_output=True, text=True, timeout=60)

    refused = run("--interval", "0.2")
    assert refused.returncode != 0, "the keeper polled at the rate that preceded a stalled worker"
    assert "--faster-than-measured" in refused.stderr, \
        "it refused without saying that a named flag is the way past, which leaves the operator" \
        " editing the shipped thresholds to get a faster answer"
    assert "refusing to run" in refused.stderr
    allowed = run("--interval", "0.4", "--faster-than-measured")
    assert "refusing to run: --interval" not in allowed.stderr, \
        "the named flag no longer names what it overrides"
    assert int(str(yaml.safe_load((repo.ROOT / "common/constants.yaml").read_text())
                    ["unlocks"]["hbm_pm"]["keeper"]["interval_s"])) >= 1, \
        "the shipped interval itself is now faster than the rate the keeper refuses to run at"


def test_the_veto_is_what_refuses_a_resident_card_and_nothing_else_does():
    """The relaxed rule has to stay relaxed about exactly one thing.

    Executed rather than read, because the difference between the two policies is four
    words in one function and a text match on them would pass while the clause moved
    above the checks it is supposed to sit behind.  A card somebody holds, reading 0%
    and 52 W, is the whole question: refused under the shipped rule, quiet under the
    shadow one, and in neither case is an unreadable process list allowed to read as
    nobody being home.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("hbm_lease", repo.ROOT / "tools/hbm-lease.py")
    lease = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lease)
    floors = {"util_floor_pct": 0, "power_floor_mw": 70000}
    held = (0, 52000, 1)                     # resident, idle: util 0, under the floor, one client
    free = (0, 52000, 0)
    assert lease.verdict([held, held], floors) == ("busy", "clients1")
    assert lease.verdict([free, free], floors) == ("quiet", "clients0,util0,power52W")
    state, note = lease.verdict([held, held], floors, clients_veto=False)
    assert state == "quiet" and note.startswith("resident1"), (state, note)
    # Work still refuses it: the relaxed rule is about clients, not about the floors.
    assert lease.verdict([held, (100, 52000, 1)], floors, clients_veto=False)[0] == "busy"
    assert lease.verdict([(0, 74000, 1)], floors, clients_veto=False)[0] == "busy"
    # And an unknown is never a permission, in either mode.
    assert lease.verdict([(0, 52000, None)], floors, clients_veto=False)[0] == "busy"
    assert lease.verdict([(None, 52000, 1)], floors, clients_veto=False)[0] == "busy"


class TwoCards:
    """A stand-in for the NVML handle set: one card working, one at rest.

    The readings are the measured ones. The busy card is a card serving -- 95 % utilisation in the
    shadow run's serving rounds. The idle card is the resident card at rest, at 0 % and 56.9 to 59 W,
    which is the same power range as the one reading this feature has of a card being harmed: a hung
    worker whose neighbour was still reading its memory, 0 % utilisation at 61 to 67 W. That pair is
    why the answer to "is this card idle" cannot be given by asking only this card.
    """

    def __init__(self):
        self.readings = {"0000:05:00.0": (0, 57000, 1), "0000:06:00.0": (95, 158000, 1)}

    def cards(self):
        # The handle is opaque to the keeper and is only ever handed back to the three
        # methods below, so the bus id is used as the handle here rather than a token
        # this stand-in would have to invent.
        return [(bus, bus) for bus in self.readings]

    def utilisation(self, handle):
        return self.readings[handle][0]

    def power_mw(self, handle):
        return self.readings[handle][1]

    def processes(self, handle):
        return self.readings[handle][2]


def test_a_card_is_not_lowered_while_a_neighbour_is_working():
    """The cross-card rule, executed.

    The floors cannot see the one case that has harmed a held card, because in that case the traffic
    belongs to the other card: 0 % utilisation at 61 to 67 W for 98.6 s while its peer streamed through
    its memory, and the borrowed counters stayed silent through it. Power cannot separate that from a
    card at rest, and neither can utilisation, so the neighbour's answer is borrowed instead -- and a
    rule that borrows it has to be run and not read, because the whole of it is which card gets to
    decide about which.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("hbm_lease", repo.ROOT / "tools/hbm-lease.py")
    lease = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lease)
    floors = {"util_floor_pct": 0, "power_floor_mw": 70000}
    quiet, working = "0000:05:00.0", "0000:06:00.0"

    lines = dict((bus, (state, note)) for bus, state, note
                 in lease.report(floors, 3, TwoCards(), {quiet: [], working: []},
                                 clients_veto=False, peer_veto=True))
    assert lines[working][0] == "busy", "the card that is working was not called busy"
    assert lines[quiet][0] == "busy", \
        "a card was lowered on its own readings while its neighbour was serving, which is the case " \
        "no floor on this card can see"
    assert lines[quiet][1].startswith("peerbusy:" + working), \
        "the refusal does not name the neighbour that caused it, and an operator reading the lease " \
        "would go and check the wrong card"
    assert "util0,power57W" in lines[quiet][1], \
        "the overridden evidence is thrown away instead of being carried in the note, which is where " \
        "a shadow run reads the rule back from"

    # The knob goes one way only: off is a bench setting, and the trace has to show what it gave up.
    lines = dict((bus, (state, note)) for bus, state, note
                 in lease.report(floors, 3, TwoCards(), {quiet: [], working: []},
                                 clients_veto=False, peer_veto=False))
    assert lines[quiet][0] == "quiet", "the knob does not actually let the card down, so a shadow " \
        "run measures a rule nobody can arm"

    # A single card on the box is not held up by itself, and two quiet cards are not held up by
    # each other: the rule borrows a busy answer, and there is none to borrow here.
    one = TwoCards()
    one.readings = {quiet: (0, 57000, 1)}
    assert lease.report(floors, 3, one, {quiet: []}, clients_veto=False)[0][1] == "quiet"
    both = TwoCards()
    both.readings = {quiet: (0, 57000, 1), working: (0, 57000, 2)}
    assert [state for _, state, _ in lease.report(floors, 3, both, {quiet: [], working: []},
                                                   clients_veto=False)] == ["quiet", "quiet"]


def test_the_relaxed_rule_arrives_with_its_counter_floor_or_does_not_arrive():
    """The two halves of the resident policy are one setting, executed.

    The floor is what separates the resident card that held the idle clock for 39 seconds of 40 from
    the one that changed state 33 times in 45 seconds and saved a third of what it thought it was
    saving, and the noise it has to clear is the keeper's own polling. An operator who relaxed the
    client list and left the governor alone gets the wobbling card and a documentation page full of
    watts nobody is collecting -- so the keeper sets the floor when it arms the rule, checks it took,
    and puts the strict rule back when it cannot.
    """
    import importlib.util
    import tempfile

    spec = importlib.util.spec_from_file_location("hbm_lease", repo.ROOT / "tools/hbm-lease.py")
    lease = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lease)

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "wake_events")
        assert lease.ensure_wake_floor(path=path) is None, \
            "a governor without the parameter was treated as a governor with a floor: the keeper " \
            "would have relaxed the client list against counters that report nothing"

        with open(path, "w", encoding="utf-8") as handle:
            handle.write("0\n")
        assert lease.ensure_wake_floor(path=path) == lease.RESIDENT_WAKE_EVENTS, \
            "the floor was not raised, or was raised and not read back"
        with open(path, encoding="utf-8") as handle:
            assert handle.read().strip() == str(lease.RESIDENT_WAKE_EVENTS), \
                "the value the keeper claims to have set is not the value the governor holds"

        # Raising it is the only direction this goes: a floor somebody put in by hand above the
        # keeper's number is their answer to the same question, and a keeper that overwrote it would
        # be a daemon tuning a machine it was asked to observe.
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("65536\n")
        assert lease.ensure_wake_floor(path=path) == 65536

    # The number itself is bounded by two measurements, one at each end, and not by preference: the
    # keeper's noisiest round seen (545 events a sample) and the quietest request the hardware has
    # ever produced (81981 events, an eight-token completion). A floor outside that window is wrong
    # in one of two measurable ways -- too low and the card wobbles, too high and the veto goes blind.
    assert lease.RESIDENT_WAKE_EVENTS >= 545 * 10, \
        "the floor is within a factor of ten of the keeper's own polling, which is the wobble"
    assert lease.RESIDENT_WAKE_EVENTS <= 81981 // 8, \
        "the floor is within a factor of eight of the smallest movement any request has made, which " \
        "is a counter veto that can be walked through by a health check"
    assert lease.RESIDENT_WAKE_EVENTS < 1 << 20, \
        "the floor is at or past the ceiling the governor refuses, so the rule would not start"


def test_the_keeper_names_a_card_from_bytes_it_can_prove():
    """Before the keeper can vouch for a card it has to know which card it is holding.

    The first time it ran against hardware it wrote a lease for every card on the
    machine and every write came back `-ENODEV`, the process exited 75, and what
    the status file showed was a governor that had decided not to idle. The cause
    was one uint missing from the ctypes picture of `nvmlPciInfo_t`: `bus_id` was
    read four bytes early, out of the tail of `pciSubSystemId`, and
    `decode("ascii", "replace")` turned those bytes into a string that looked like
    a bus id, so the keeper believed it. A safe failure that nobody can see is not
    much better than an unsafe one, which is why this is tested against the bytes
    the driver on the bench really returned rather than against the source text.
    """
    lease = repo.keeper()

    assert lease.NvmlPciInfo.bus_id_legacy.offset == 0
    assert lease.NvmlPciInfo.bus_id.offset == 36, \
        "busId is at 36 in nvmlPciInfo_t: a field between the two strings moved it"
    assert ctypes.sizeof(lease.NvmlPciInfo) >= 68, ctypes.sizeof(lease.NvmlPciInfo)
    assert lease.NvmlPciInfo.bus_id.size >= 256, \
        ("bus_id is sized to the documented 32 bytes again: NVML writes its own struct "
         "into this buffer, and a ctypes object that is too short segfaults python "
         "instead of raising anything the keeper could catch")

    def name_of(raw):
        # __new__ rather than Nvml(): the reading half needs no library, and a
        # machine without a GPU has no libnvidia-ml to open.
        reader = object.__new__(lease.Nvml)
        return reader.bus_name(lease.NvmlPciInfo.from_buffer_copy(raw), 3)

    def raw_of(legacy=b"0000:05:00.0", padded=b"00000000:05:00.0",
               domain=0, bus=5, device=0):
        """nvmlPciInfo_t the way the bench filled it: two strings and five uints.

        Padded out to the size the keeper actually allocates, since
        `from_buffer_copy` refuses anything smaller than the struct -- and the
        struct is deliberately larger than the documented 68 bytes, for the
        reason commented in the file.
        """
        def pad(value, width):
            return value + b"\x00" * (width - len(value))

        body = (pad(legacy.encode() if isinstance(legacy, str) else legacy, 16)
                + b"".join(n.to_bytes(4, "little") for n in
                           (domain, bus, device, 0x20C210DE, 0x158510DE))
                + pad(padded.encode() if isinstance(padded, str) else padded, 32))
        return body + b"\x00" * (ctypes.sizeof(lease.NvmlPciInfo) - len(body))

    # Verbatim from the bench: /tmp/nvml-pci-probe.sh on 610.57.04, one card.
    bench = raw_of()
    assert name_of(bench) == "0000:05:00.0", \
        "the kernel's spelling is the one the lease file takes"

    # The padded form on its own, normalized to four digits of domain rather than
    # passed straight on: the module compares against pci_name().
    assert name_of(raw_of(legacy=b"\x00" * 16)) == "0000:05:00.0", \
        "the fallback does not normalize the domain"
    assert name_of(raw_of(legacy=b"\x00" * 16, padded=b"00000001:05:00.0", domain=1)) == \
        "0001:05:00.0"

    # Both strings garbage: no name, and the card gets no lease, which is the
    # answer the module reads as `busy`.  This is the case the old code turned into
    # a fabricated name, because `errors="replace"` made garbage printable.
    assert name_of(raw_of(legacy=b"\xde\x10\x85\x15", padded=b"")) is None, \
        "a bus id nobody can read is being repaired into a name"

    # The strings have to agree with the numbers in the same struct: that is what
    # says the layout above still describes this particular libnvidia-ml.
    assert name_of(raw_of(bus=6)) is None, \
        "the bus id and NVML's own bus number disagree and the keeper believed the string"
    assert name_of(raw_of(padded=b"00000000:09:00.0")) == "0000:05:00.0", \
        "the padded string overrode the legacy one: only the field that is used is checked"

    # And a card the keeper could not name is skipped, not written up as idle.
    cards = between("def cards(self)", "def processes(", KEEPER)
    assert "if bus is None" in cards and "continue" in cards, \
        "an unnamed card is being reported anyway"


def test_the_keeper_asks_nvml_the_questions_it_thinks_it_is_asking():
    """Three bugs that all ended as the safe answer, which is why all three survived.

    `nvmlDeviceGetPowerUsage(device, *power)` was called with the output pointer
    and no device, so NVML followed that pointer as a handle: usually an error,
    which meant the power reading was always unknown, and an unknown power reading
    is busy -- so the keeper could never have said `quiet` even with everything
    else fixed.  The process lists were called as `(handle, infos, *count)` when
    the signature is `(device, *count, infos)`: NVML wrote a dozen entries' worth
    of bytes through whichever pointer it had been given, which is a segfault with
    a delay, and it took the bench to say so, because the fake library in the unit
    test below had the same wrong belief the code did and they were consistent.
    And the probe call it used before that answered a card with clients on it with
    NVML_ERROR_INSUFFICIENT_SIZE, not 0, which a caller that accepts only 0 reads
    as "the driver will not say" -- and a keeper that cannot ask whether anything
    is attached answers busy, forever, on every card, on every round.

    The fake is worth keeping for the third case, which is a matter of return
    codes and can be decided here; the first two are pinned as text as well, since
    the only thing that caught them was a card.
    """
    lease = repo.keeper()
    HANDLE = ctypes.c_void_p(0x5A5A)

    def keeper_with(compute=(0, 0), graphics=(0, 0)):
        n = object.__new__(lease.Nvml)		# no library: no GPU needed here
        calls = []

        def answer(spec, tag):
            ret, count = spec

            def fn(handle, room, buf):
                # (device, unsigned int *count, nvmlProcessInfo_t *infos) -- the
                # order NVML actually has, not the order the code believed.  The
                # count is read before it is overwritten: it is an in/out, and the
                # value the caller offered is the thing under test here.
                ptr = ctypes.cast(room, ctypes.POINTER(ctypes.c_uint))
                calls.append((tag, handle, ptr.contents.value, buf))
                ptr.contents.value = count
                return ret
            return fn

        class Lib:
            pass

        lib = Lib()
        lib.nvmlDeviceGetComputeRunningProcesses_v3 = answer(compute, "compute")
        lib.nvmlDeviceGetGraphicsRunningProcesses_v3 = answer(graphics, "graphics")
        n.lib = lib
        return n, calls

    # An empty card, asked properly: the count is the whole question, and both
    # lists have to be asked, since a graphics client is a client.
    n, calls = keeper_with()
    assert n.processes(HANDLE) == 0
    assert [tag for tag, _, _, _ in calls] == ["compute", "graphics"], calls
    assert all(handle == HANDLE for _, handle, _, _ in calls), \
        "NVML was asked about some card other than the one it was handed"
    assert all(buf is not None for _, _, _, buf in calls), \
        "the list is being probed with a NULL array, which is an answer disguised as an error"
    assert all(capacity == lease.PROCESS_CAPACITY for _, _, capacity, _ in calls), \
        "the count was not offered as the second argument, so NVML is writing entries " \
        "through whichever pointer it was handed instead of through the array"

    n, _ = keeper_with(compute=(0, 2))
    assert n.processes(HANDLE) == 2, "two compute clients were counted as none"
    n, _ = keeper_with(compute=(0, 1), graphics=(0, 1))
    assert n.processes(HANDLE) == 2, "the two lists are not being added"

    # The return code that is not an error: NVML filled what it was given and
    # there was more.  Anything above zero is a card somebody is using.
    n, _ = keeper_with(compute=(lease.NVML_ERROR_INSUFFICIENT_SIZE, lease.PROCESS_CAPACITY))
    got = n.processes(HANDLE)
    assert got is not None and got > 0, \
        "INSUFFICIENT_SIZE is being read as a driver that will not answer, which is the " \
        "bug that made every card busy forever"

    # A driver that will not answer, and a driver that errors, are both unknowns,
    # and an unknown is busy.
    n, _ = keeper_with(compute=(lease.NVML_ERROR_NOT_SUPPORTED, 0))
    assert n.processes(HANDLE) is None
    n, _ = keeper_with(graphics=(1, 0))
    assert n.processes(HANDLE) is None, "an NVML failure is being read as no processes"

    # The same handle discipline on the other per-card question.
    calls_made = []
    class PowerLib:
        def nvmlDeviceGetPowerUsage(self, handle, out):
            calls_made.append(handle)
            return 0

    n = object.__new__(lease.Nvml)
    n.lib = PowerLib()
    assert n.power_mw(HANDLE) == 0
    assert calls_made == [HANDLE], "nvmlDeviceGetPowerUsage is not being told which card"

    # And the argument order, pinned as text too: the fake above can only be as
    # right about the signature as the person who wrote it.
    reading = between("def processes(self, handle)", "def utilisation(", KEEPER)
    assert "fn(handle, ctypes.byref(room), buf)" in reading, \
        ("the count and the array have been swapped again: NVML writes entries through "
         "whichever argument it thinks is the array, which is a segfault, not an error")
    assert ("fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint), "
            "ctypes.POINTER(ctypes.c_char)]") in " ".join(KEEPER.split()), \
        ("the argtypes no longer match the call: they are what turns a swapped argument "
         "into an exception instead of memory the driver writes through")
    for name, args in re.findall(r"self\.lib\.(nvmlDevice\w+)\(([^)]*)\)", KEEPER):
        if name == "nvmlDeviceGetCount_v2":
            continue		# the one call that is about the machine, not a card
        first = args.split(",")[0].strip()
        assert first in ("handle", "index"), \
            "%s is called with %r where the card belongs" % (name, first)


def test_the_lease_writer_accepts_the_other_way_of_spelling_the_same_card():
    """`nvidia-smi --query-gpu=pci.bus_id` prints eight digits of domain; pci_name()
    prints four.  Both are the same card, and the operator holding the lease file is
    reading one of them off a screen.

    The refusal is the reason this is a test and not a nicety: `-ENODEV` says
    nothing about what was wrong, which is how the keeper lost a whole feature to
    the difference and looked, from the outside, like a governor that had simply
    decided not to idle.  Trailing parts of the name still have to match exactly,
    so the tolerance is only ever about leading zeros.
    """
    matcher = between("static bool lease_bdf_matches", "static ssize_t lease_store")
    assert "while (a[0] == '0'" in matcher and "while (b[0] == '0'" in matcher, \
        "the domain is being compared digit for digit again"
    assert "strcasecmp" in matcher
    store = between("static ssize_t lease_store", "static struct kobj_attribute lease_attr")
    assert "lease_bdf_matches(name, bdf)" in store, \
        "the lease writer went back to comparing the two spellings literally"
    assert '"all"' in store, "the wildcard that answers every card at once is gone"


def test_the_lease_survives_being_installed():
    """Two moving parts, one installer: a governor whose keeper was never installed is a
    governor that does nothing, and a wattmeter cannot tell that apart from one that does.
    """
    assert "hbm-lease.py" in SETUP and "cmp-hbm-lease.service" in SETUP
    assert "systemctl enable --now cmp-hbm-lease.service" in SETUP
    assert "lease none" in SETUP and "lease waived" in SETUP, \
        "the installer no longer checks that it got an answer out of the keeper"
    assert "@CONSTANTS@" in UNIT, "the unit does not know where the thresholds are, so the keeper cannot read them"
    assert "--loop" in UNIT and "Restart=always" in UNIT
    # The thresholds are an installed file, not a file the unit reaches into the tree
    # it was installed from: a keeper pointed at a checkout can be disabled by
    # `rm -rf`ing a directory, and the way it fails -- `busy` for every card, forever
    # -- is indistinguishable in the watts from a machine with nothing idle on it.
    INSTALLED_CONSTANTS = "/usr/local/share/cmpunlocker/constants.yaml"
    assert INSTALLED_CONSTANTS in SETUP, \
        "the installer points the keeper at a constants.yaml it did not install"
    assert 'install -D -m 0644 "${CONSTANTS_SRC}" "${CONSTANTS_DST}"' in SETUP, \
        "the thresholds are being referenced where they sit rather than installed"
    assert INSTALLED_CONSTANTS in KEEPER, \
        ("an installed keeper run by hand looks only next to its own script, which is"
         " not where the thresholds are once the installer has moved it")
    assert INSTALLED_CONSTANTS in REMOVE, \
        "remove.sh leaves the floors of an uninstalled keeper behind for the next one"
    assert "hbm-lease.py" in REMOVE and "cmp-hbm-lease.service" in REMOVE, \
        "remove.sh leaves a keeper running against a lease file it has deleted"
    assert "lease none" in VERIFY and "hbm_says ' stale'" in VERIFY, \
        "verify.sh prints the lease state but never says the keeper is missing"
    # Three keeper bugs went to hardware at once and every one of them produced the
    # same harmless-looking status line as a card nobody had asked about.  A keeper
    # whose unit is installed and whose answer still never arrives is the case the
    # wattmeter, the status file and a green verify all miss.
    assert "systemctl is-enabled --quiet cmp-hbm-lease.service" in VERIFY and \
           "/etc/systemd/system/cmp-hbm-lease.service" in VERIFY, \
        ("a keeper that fails every round is `activating` behind RestartSec rather than"
         " running, so gating this on liveness reports the shipped bug as an absent unit")
    assert "lease [0-9]+ ms" in VERIFY, \
        "the escalation does not actually look for a dated lease before it shouts"


def test_a_stop_the_module_cannot_give_back_ends_the_module_for_that_card():
    """The one unrecoverable thing available here is holding a card's framebuffer out of
    service, so the module gives up on a card rather than retry it.

    The first time this happened on the bench, the release was attempted every `poll_ms`
    for five minutes against a card that could no longer answer -- `errors 24814` -- while
    a 30 second rate limiter turned all of it into one log line, and the card came back
    only after a reboot.  Holding the stop was also the port's own policy, on the reasoning
    that an unknown PLL state is worse than no traffic; against the alternative that
    reasoning is backwards, and the branch is gone with it.
    """
    transition = between("static int hbm_transition", "static void reset_state")
    assert "resumed = fb_start(hbm, bar);" in transition
    assert transition.index("if (ret) {") < transition.index("resumed = fb_start"), \
        "the release moved behind the rollback report again"
    assert "HBM left at an unknown PLL after rollback" in transition, \
        "the unnamed PLL is no longer reported, only guessed at"
    assert "return resumed ? resumed : ret;" in transition
    assert "return ret;" not in transition.split("resumed = fb_start")[1], \
        "a failed transition can now return without having released the stop"
    halt = between("static void cmp_hbm_halt", "static u32 coeff_mhz")
    assert "if (hbm->halted)" in halt and "hbm->halted = true;" in halt, \
        "the halt latch is not a latch"
    sampling = between("static int sample_state", "static bool held_by_nvidia")
    assert "if (hbm->halted)" in sampling and sampling.index("if (hbm->halted)") < \
        sampling.index("if (!firmware_booted(bar))"), \
        "a halted card is still being read"
    work = between("static void sample_work", "static void restore_locked")
    assert "if (READ_ONCE(worker->hbm.halted))" in work and \
        work.index("halted") < work.index("reschedule(worker"), \
        "the poll still reschedules itself around a halted card: this is the 24814"
    assert "halted" in between("static void restore_locked", "static int pci_notify"), \
        "the unbind path still does MMIO to a card the module has given up on"
    govern = between("static void govern", "static void sample_work")
    assert "pci_emerg" in govern and "fb_req=%#x fb_ack=%#x" in govern, \
        "the one line that has to survive a ring buffer full of assertions lost the registers"
    assert "halt_logged" in govern, "the emergency line is not latched to fire once"
    for phrase in ("24814", "halted"):
        assert phrase in DOC, "the doc no longer explains %s" % phrase


def test_a_failing_transition_is_not_tried_again_on_the_next_tick():
    """Nothing was hammering the card before it wedged, and this is the second half of the
    fix for there having been something.

    A transition that failed once will probably fail again, and the poll interval is not
    the shape of the wait a hardware handshake that is not answering needs.  The waits are
    bounded twice: the ACK hunt sleeps rather than spinning once its first millisecond of
    spinning has failed, and the next attempt waits for a backoff that doubles to seconds.
    """
    backoff = between("static void cmp_hbm_backoff", "static int sample_state")
    assert "min(hbm->backoff_ms * 2U, (unsigned int)CMP_HBM_MAX_BACKOFF_MS)" in backoff, \
        "the backoff no longer grows, or no longer stops growing"
    assert "#define CMP_HBM_MAX_BACKOFF_MS\t5000" in MODULE
    sampling = between("static int sample_state", "static bool held_by_nvidia")
    assert "cmp_hbm_backoff(hbm, now);" in sampling
    assert "idle transition waiting out the backoff after a failed one" in sampling
    assert "if (now < hbm->retry_at)" in sampling and \
        sampling.index("if (now < hbm->retry_at)") < sampling.rindex("lease_allows(worker, now)"), \
        "a card in backoff is being asked for a lease it is not going to act on"
    assert "hbm->backoff_ms = 0;" in sampling, "traffic does not clear a backoff earned while quiet"
    wait = between("static int fb_wait", "static int fb_stop")
    assert wait.count("return 0;") == 2, "the ACK hunt lost either its spin exit or its sleep exit"
    assert "udelay(1)" in wait and "usleep_range(4000, 6000)" in wait
    assert "in_atomic() || irqs_disabled()" in wait, \
        "the long hunt sleeps, so it has to know when it may not"
    assert "CMP_FB_WAIT_SLEEP_ROUNDS" in wait
    # A switch is the thing a measurement is attributed to, and the run that had to be
    # reconstructed from counters is the run that ended in a reboot.
    assert "static bool log_transitions = true;" in MODULE
    assert "log_transitions" in DOC and "`log_transitions`" in DOC
    # A policy write has to go through even while the driver is unwinding a dead card.
    resync = between("static void cmp_hbm_resync", "static bool supported")
    assert "pci_dev_lock_bounded(worker)" in resync and "policy change" in resync, \
        "a policy write is back to waiting on a lock somebody may be holding for a reset"


def test_the_status_file_reports_the_numbers_the_incident_was_missing():
    """Every field below exists because it was not there at 11:16 and was needed to say
    what had happened.  Dropping one is dropping the ability to answer the next question
    without a second boot.
    """
    status = between("static ssize_t status_show", "static struct kobj_attribute status_attr")
    for field in ("deltas %s", "lease %s", "fb %#x/%#x", "halted", "quiet %llu ms",
                  "active %#x", "switches %llu", "errors %llu"):
        assert field in status, "the status file lost %r" % field
    assert "lease none" not in status, "the status file is deciding what a missing lease means"
    for field in ("`deltas`", "`active`", "`lease`", "`errors`", "`halted`", "`fb`", "`flaps`"):
        assert field in DOC, "the doc does not explain the field the module prints: %s" % field


def test_no_local_pointer_in_the_governor_is_read_before_anything_has_set_it():
    """The bug this check exists for: a rewrite of the status file dropped the one line
    that gave `s` its value, and every use of it was a READ_ONCE(), which takes an address
    through a volatile cast and so gives the compiler nothing to complain about.  The
    module built clean under W=1, loaded, and the first read of that file printed an
    address of the stack's choosing while holding the worker list lock, which is how a
    box got its third reboot of the day.

    So the file is checked for what the compiler cannot see: a local pointer that is
    declared and then used without ever being assigned, taken as the base of a member
    access.  List iterators and pointers handed out by address are exempt because
    something else fills them in.
    """
    code = re.sub(r"/\*.*?\*/", "", MODULE, flags=re.S)
    code = re.sub(r'"(\\.|[^"\\])*"', '""', code)
    offenders = []
    for match in re.finditer(r"^[A-Za-z_][^\n;]*\)\s*$", code, flags=re.M):
        opened = code.find("{", match.end())
        if opened == -1 or code[match.end():opened].strip():
            continue
        depth, end = 0, opened
        for end in range(opened, len(code)):
            if code[end] == "{":
                depth += 1
            elif code[end] == "}":
                depth -= 1
                if not depth:
                    break
        name = re.search(r"([A-Za-z_]\w*)\s*\(", match.group(0))
        body = code[opened:end]
        for decl in re.finditer(r"^\t+struct\s+(\w+)\s*\*\s*(\w+)\s*;", body, flags=re.M):
            struct, var = decl.group(1), decl.group(2)
            if re.search(r"\b%s\s*=[^=]" % var, body) or \
               re.search(r"list_for_each_entry\w*\([^;]*\b%s\b" % var, body) or \
               re.search(r"&\s*\b%s\b" % var, body):
                continue
            offenders.append("%s(): struct %s *%s" % (name.group(1), struct, var))
    assert not offenders, "read before set: %s" % ", ".join(offenders)
    # And the one that got away from the compiler, pinned by name rather than by pattern.
    assert "s = &worker->stats;" in between(
        "static ssize_t status_show", "static struct kobj_attribute status_attr")


def test_reading_the_status_files_can_be_refused_but_never_hung():
    """A reader of these files is a person, verify.sh, or whatever watches the machine.
    None of them may become an unkillable task because the governor is busy with a card:
    the readers take the worker list with a bound and say they could not have it, and the
    paths that change policy keep waiting, because there the operator asked for work and
    the answer is to do it.
    """
    getter = between("static bool workers_lock_get", "static struct kobject *status_dir")
    assert "mutex_trylock(&workers_lock)" in getter and "CMP_HBM_LOCK_WAIT_MS" in getter
    assert "pr_warn_ratelimited" in getter, "a reader that gives up must say why in the log"
    for start, end, says in (
            ("static ssize_t status_show", "static struct kobj_attribute status_attr",
             "status unavailable"),
            ("static ssize_t perf_table_show", "static struct kobj_attribute perf_table_attr",
             "perf table unavailable")):
        body = between(start, end)
        assert "workers_lock_get(" in body and says in body
        assert "mutex_lock(&workers_lock)" not in body, "%s still waits forever" % start
    lease = between("static ssize_t lease_store", "static struct kobj_attribute lease_attr")
    assert "workers_lock_get(" in lease and "mutex_lock(&workers_lock)" not in lease
    assert "-EBUSY" in lease, "the keeper is told to try again rather than left here"
    assert "status unavailable" in DOC and "perf table unavailable" in DOC, \
        "a reader that can be refused has to say so in the file people read"
    # The keeper is the one reader that used to be able to hang: it has to survive the answer.
    assert "except OSError as exc" in between("def emit", "def main", KEEPER)
    assert "SystemExit(75)" in between("def emit", "def main", KEEPER)
    for start, end in (("static struct cmp_hbm_worker *worker_start",
                        "static void worker_stop(struct cmp_hbm_worker *worker)"),
                       ("static void cmp_hbm_resync", "static bool supported"),
                       ("static void cmp_hbm_requeue", "static bool supported")):
        assert "mutex_lock(&workers_lock)" in between(start, end), \
            "%s must not skip the work just because the list is busy" % start

