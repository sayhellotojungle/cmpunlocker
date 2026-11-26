#!/usr/bin/env python3
"""Ask the driver the question the governor cannot ask the hardware.

  sudo python3 tools/hbm-lease.py            # one report, then exit
  sudo python3 tools/hbm-lease.py --loop     # what systemd runs

cmp_hbm_pm.ko lowers a card's HBM clock by taking its framebuffer path away for
the moment of the change.  On a card somebody is using, that is not a slow
failure but a total one -- GSP-RM loses its own route to memory, its RPCs time
out, and the card needs a reset.  So somebody has to know, continuously, whether
anything is using the card, and it cannot be the module:

  * the borrowed PMU activity counters were measured on a card serving requests
    reading nothing at all for 8.4 s while it drew 178 W (docs/HBM-PM.md has the
    recording).  They veto a switch -- any movement, any counter, any magnitude
    -- and they are never allowed to license one;
  * the ampere register headers this driver ships contain no engine-busy status
    at all, because on this chip that answer lives inside GSP-RM.

NVML asks RM that question properly, and it is the same answer nvidia-smi prints
-- which is the answer that said 100% and 178 W while the counters said quiet.
This script turns it into a per-card verdict written to
/sys/kernel/cmp_hbm_pm/lease, once a second.  Everything about the protocol is
fail-safe: if this program is not running, crashes, hangs, cannot read a card, or
cannot parse an answer, the module never goes idle.

Nothing here lowers a clock or touches a register.  It only ever refuses, and the
one thing it is trusted to say is "somebody is using this".
"""
import argparse
import ctypes
import os
import re
import sys
import time

LEASE = "/sys/kernel/cmp_hbm_pm/lease"
LIBRARY = "libnvidia-ml.so.1"
# Where the thresholds live, in the order they are tried.  The first is the checkout
# this file was run from, which is the one a person editing `common/constants.yaml`
# expects to take effect without reinstalling anything.  The second is where
# `tools/hbm-pm-setup.sh` installs the copy the shipped unit is pointed at, and it is
# in this list because the first one does not exist once the installer has put the
# script under /usr/local/lib: a keeper run by hand there was reading
# `/usr/local/common/constants.yaml`, finding nothing, refusing every card, and
# saying so only on stderr -- the debugging command in the documentation was a
# permanently busy machine.  `--constants` still overrides the whole list.
CONSTANTS_CANDIDATES = (
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "common", "constants.yaml"),
    "/usr/local/share/cmpunlocker/constants.yaml",
)

# One NVML call costs more than it looks: on GSP-RM every one of them is an RPC into the
# firmware, serialised against the path a workload's own launches take.  Three calls per
# card per round at the shipped one-second interval is the rate observed harmless on a box
# serving with two cards; a second keeper run beside it at 0.2 s -- five times that rate --
# was followed inside a minute by a tensor-parallel worker that stopped reporting, which is
# written up in docs/HBM-PM.md.  So the interval is a limit rather than a preference, and
# below it the keeper asks to be told twice.
NVML_MIN_INTERVAL_S = 0.5

# The counter floor the relaxed rule needs, and why the keeper is the one to set it.
#
# A card whose serving framework is resident is not silent on the borrowed counters: the keeper's own
# three NVML calls a round are three RPCs into GSP-RM, and the resident process's housekeeping ticks the
# counter it watches.  Measured, one card, one keeper at the shipped rate: 517 to 545 events a sample,
# which is why the same card at `wake_events=512` changed state 33 times in 45 s and spent 38 % of its
# life climbing back to the busy clock -- every watt the documentation claims was measured at a floor
# high enough that this did not happen.
#
# The number is chosen between two measured ends and not from a feeling: 8192 is fifteen times the
# noisiest keeper round seen and a tenth of the smallest movement any request has ever produced on this
# hardware, which was 81981 events, from an eight-token completion.  The two ends are 517 or 545 and
# 81981, so almost any number in between is defensible; the geometric middle of that range is 6500, and
# 8192 is the power of two nearest it.
#
# It is the keeper that sets it because it is the keeper that creates the noise, and a policy whose two
# halves are tuned apart is a policy that gets tuned wrong: an operator who relaxed the client list in
# the constants file and left the module alone would get the wobbling card, not the idle one.  So the
# relaxed rule comes with its floor or it does not come at all -- see ensure_wake_floor(), which puts
# the strict rule back if the floor cannot be reached.
RESIDENT_WAKE_EVENTS = 8192
WAKE_FLOOR_PATH = "/sys/module/cmp_hbm_pm/parameters/wake_events"

# The two spellings NVML uses for one card.  These are validators rather than
# parsers: the keeper's job is to name a card exactly, and a name that does not
# match one of these is refused, not repaired.
BUS_ID = re.compile(r"^([0-9a-f]{1,4}):([0-9a-f]{2}):([0-9a-f]{2}\.[0-7])$")
BUS_ID_PADDED = re.compile(r"^([0-9a-f]{1,8}):([0-9a-f]{2}):([0-9a-f]{2}\.[0-7])$")


def defaults_from_constants(path):
    """The numbers live in common/constants.yaml with every other one.

    Returning {} rather than inventing a default is deliberate: the caller refuses
    to run on an empty hand, which leaves every card on its busy clock.  A keeper
    that made up its own floors after a bad YAML file would be a keeper that
    decides when a card may be disturbed, by itself, on a machine whose maintainer
    thought they had configured it not to.
    """
    try:
        import yaml
    except ImportError:
        sys.stderr.write("hbm-lease: python3-yaml is required to read %s\n" % path)
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            constants = yaml.safe_load(handle)
    except OSError as exc:
        sys.stderr.write("hbm-lease: %s: %s\n" % (path, exc))
        return None
    except Exception as exc:				# yaml.YAMLError, without importing yaml
        sys.stderr.write("hbm-lease: %s does not parse: %s\n" % (path, exc))
        return None
    node = constants
    for key in ("unlocks", "hbm_pm", "keeper"):
        node = (node or {}).get(key)
        if not isinstance(node, dict):
            sys.stderr.write("hbm-lease: %s has no unlocks.hbm_pm.keeper block\n" % path)
            return None
    return node


class NvmlPciInfo(ctypes.Structure):
    """nvmlPciInfo_t, as laid out by the driver actually on the bench.

    The field order is not a detail: with one uint missing from this list the
    `bus_id` read landed four bytes early, in the tail of `pciSubSystemId`, and
    the keeper wrote a lease for a card whose name began with three bytes of
    subsystem id.  The kernel answered -ENODEV for every card on the machine,
    the keeper exited 75, and the cards stayed on their busy clock -- which is
    the safe direction, but it is a safe direction that means the feature does
    not exist.  So the layout below is the one measured off 610.57.04, byte for
    byte, and the offsets are commented because a header read from another
    release will not agree with it:

        0   char busIdLegacy[16]      "0000:05:00.0"  -- the kernel's own spelling
        16  unsigned domain, bus, dev  0, 5, 0
        28  unsigned pciDeviceId      0x20c210de
        32  unsigned pciSubSystemId   0x158510de
        36  char busId[32]            "00000000:05:00.0"

    `busIdLegacy` is what the governor wants and `busId` is what nvidia-smi
    prints; both are read, and both are checked, below.
    """

    _fields_ = [("bus_id_legacy", ctypes.c_char * 16),
                ("domain", ctypes.c_uint),
                ("bus", ctypes.c_uint),
                ("device", ctypes.c_uint),
                ("pci_kind", ctypes.c_uint),
                ("subsystem", ctypes.c_uint),
                # Declared far bigger than the header's 32 bytes, and that is the
                # point: NVML writes *its* struct into whatever we hand it, and the
                # size of that struct is not in the header I can read from here.  A
                # buffer sized to the documented 68 bytes segfaulted python on the
                # bench, which is what a too-short ctypes object is -- heap
                # corruption in somebody else's code, with no exception to catch.
                # 1024 is what the probe that found the offsets used, and it came
                # back rc=0 with both strings where they belong.  Nothing reads
                # past the first NUL of either string, and bus_name() checks that
                # the strings and the numbers agree, so a layout that ever grows
                # again shows up as a refusal rather than a wrong card name.
                ("bus_id", ctypes.c_char * 1024)]


class NvmlUtilization(ctypes.Structure):
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


NVML_ERROR_NOT_SUPPORTED = 3
# What NVML answers to a process-list call it filled as far as the caller allowed.
# It is not an error and it is not a zero: it is NVML saying there was more to
# report than the buffer could hold, which for this file is the same answer as one.
NVML_ERROR_INSUFFICIENT_SIZE = 7

# Only ever used to size the buffer the two process lists are written into.  An
# entry size here would be a declaration of NVML's struct, and the last one was
# five times too small; see Nvml.processes().
PROCESS_CAPACITY = 64
PROCESS_ENTRY_BYTES = 256


class Nvml:
    def __init__(self):
        try:
            self.lib = ctypes.CDLL(LIBRARY)
        except OSError as exc:
            raise RuntimeError("%s could not be loaded (%s)" % (LIBRARY, exc))
        for name in ("nvmlInit_v2", "nvmlDeviceGetCount_v2", "nvmlDeviceGetHandleByIndex_v2",
                     "nvmlDeviceGetPciInfo_v3", "nvmlDeviceGetUtilizationRates",
                     "nvmlDeviceGetPowerUsage",
                     "nvmlDeviceGetComputeRunningProcesses_v3",
                     "nvmlDeviceGetGraphicsRunningProcesses_v3"):
            try:
                getattr(self.lib, name)
            except AttributeError:
                raise RuntimeError("%s lacks %s: this driver is too old for the question"
                                   % (LIBRARY, name))
        # A non-zero return here is how NVML says no, and every call site below
        # treats it as "unknown", which means busy.
        self.lib.nvmlInit_v2.restype = ctypes.c_int
        for name in ("nvmlDeviceGetCount_v2", "nvmlDeviceGetHandleByIndex_v2",
                     "nvmlDeviceGetPciInfo_v3", "nvmlDeviceGetUtilizationRates",
                     "nvmlDeviceGetPowerUsage"):
            getattr(self.lib, name).restype = ctypes.c_int
        for name in ("nvmlDeviceGetComputeRunningProcesses_v3",
                     "nvmlDeviceGetGraphicsRunningProcesses_v3"):
            fn = getattr(self.lib, name)
            fn.restype = ctypes.c_int
            # (device, unsigned int *count, nvmlProcessInfo_t *infos).  The third
            # type is POINTER(c_char) rather than a pointer to a declared struct:
            # the count is the only thing read back out of the array, and a
            # declaration of NVML's struct that is smaller than NVML's struct is
            # memory corruption in the driver's write.  These argtypes are what
            # turned the swapped arguments into an exception instead of a
            # segfault, so they are written in the same order as the call.
            fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint),
                           ctypes.POINTER(ctypes.c_char)]
        if self.lib.nvmlInit_v2() != 0:
            raise RuntimeError("NVML did not initialise: is the driver loaded, and is "
                               "this the user permitted to see the cards?")

    def cards(self):
        count = ctypes.c_uint()
        if self.lib.nvmlDeviceGetCount_v2(ctypes.byref(count)) != 0:
            return []
        for index in range(count.value):
            handle = ctypes.c_void_p()
            if self.lib.nvmlDeviceGetHandleByIndex_v2(index, ctypes.byref(handle)) != 0:
                continue
            pci = NvmlPciInfo()
            if self.lib.nvmlDeviceGetPciInfo_v3(handle, ctypes.byref(pci)) != 0:
                continue
            bus = self.bus_name(pci, index)
            if bus is None:
                # Not a card to skip quietly: the governor never hears about this one
                # and keeps it at the busy clock, which is the right answer and the
                # one nobody would notice.  bus_name() has already said so.
                continue
            yield bus, handle

    def bus_name(self, pci, index=0):
        """The name the governor will accept for this card, or None.

        Strict on purpose.  The first version of this decoded with
        `errors="replace"`, which turned three bytes of `pciSubSystemId` into a
        plausible-looking name when the struct layout above was one field short;
        the keeper then wrote a lease nobody accepted and exited, and the failure
        looked like a governor that had decided not to idle.  A card whose name
        cannot be read is a card that must not be vouched for, so the two strings
        NVML gives are checked against the numeric fields in the same struct, and
        anything that does not match is refused out loud.

        NVML offers the same bus id twice: `busIdLegacy` in the spelling the
        kernel uses (`0000:05:00.0`) and `busId` with an eight-digit domain
        (`00000000:05:00.0`), which is the form nvidia-smi prints.  The legacy
        one is used when it is well-formed, because it needs no translation to be
        compared with `pci_name()`; the padded one is normalized rather than
        trusted, and a domain that would not fit the kernel's four digits is
        refused instead of clipped.
        """
        legacy = pci.bus_id_legacy.split(b"\0")[0]
        padded = pci.bus_id.split(b"\0")[0]
        name = None

        try:
            text = legacy.decode("ascii").strip().lower()
        except UnicodeDecodeError:
            text = ""
        match = BUS_ID.match(text)
        if match:
            name = text
        else:
            try:
                text = padded.decode("ascii").strip().lower()
            except UnicodeDecodeError:
                text = ""
            match = BUS_ID_PADDED.match(text)
            if match and int(match.group(1), 16) <= 0xffff:
                name = "%04x:%s:%s" % (int(match.group(1), 16), match.group(2), match.group(3))

        if name is None:
            self._complain(index, "no bus id NVML could be read: legacy %r, busId %r"
                           % (legacy, padded))
            return None

        # The strings and the numbers in the same struct have to agree.  If they do
        # not, the struct above no longer describes this library, and every card in
        # the machine is being named from somebody else's bytes.
        domain, bus, dev = name.split(":")
        dev = dev.split(".")[0]
        if (int(domain, 16) != pci.domain or int(bus, 16) != pci.bus or
                int(dev, 16) != pci.device):
            self._complain(index, "bus id %s disagrees with NVML's own numbers "
                           "(domain=%d bus=%d dev=%d): the nvmlPciInfo_t layout no longer "
                           "matches libnvidia-ml" % (name, pci.domain, pci.bus, pci.device))
            return None
        return name

    def _complain(self, index, why):
        """Once per distinct failure, not once per second.

        A card that cannot be named will not become nameable while the driver is up,
        and 86,000 identical journal lines a day would bury the one line that says
        the governor has no keeper for that card.
        """
        if not hasattr(self, "_complained"):
            self._complained = set()
        if index in self._complained:
            return
        self._complained.add(index)
        sys.stderr.write("hbm-lease: card %d: %s; nothing will be written for it, so the "
                         "governor keeps it on its busy clock\n" % (index, why))
        sys.stderr.flush()

    def processes(self, handle):
        """Everything NVML will admit is using the card, compute or graphics.

        The compute list alone is not enough: it is a list of CUDA clients, and a
        card can be busy feeding somebody's video decoder without appearing in
        it.  Two lists is not a proof of idle either -- it is one more thing that
        has to say no.

        One call per list, into a buffer with room, rather than the documented
        two-step probe.  The probe call is the trap: with processes to report,
        NVML answers `NVML_ERROR_INSUFFICIENT_SIZE` (7), not 0, so a caller that
        accepts only 0 reads "there are processes on this card" as "the driver
        would not answer", which this file treats as a refusal -- safe, and the
        reason the keeper spent its first week on hardware never once saying
        `quiet`.  Ask once with room for 64, and if even that came back full the
        answer is the same answer as one.

        The entries are never read, which is why the buffer is sized in bytes
        rather than by a picture of `nvmlProcessInfo_t`: the count is the whole
        question, and a struct declaration that is smaller than NVML's is heap
        corruption in the driver's write, not an exception in mine.  The first
        version declared that struct, the count question was answered from its
        return code alone, and it segfaulted python on the bench.
        """
        total = 0
        for name in ("nvmlDeviceGetComputeRunningProcesses_v3",
                     "nvmlDeviceGetGraphicsRunningProcesses_v3"):
            fn = getattr(self.lib, name)
            room = ctypes.c_uint(PROCESS_CAPACITY)
            buf = ctypes.create_string_buffer(PROCESS_ENTRY_BYTES * PROCESS_CAPACITY)
            # count second, array third: `nvmlDeviceGet...RunningProcesses_v3(device,
            # unsigned int *count, nvmlProcessInfo_t *infos)`.  Swapping the two is
            # not an exception -- NVML writes a dozen entries' worth of bytes
            # through whichever pointer it was handed, which is a segfault with a
            # delay.  It took the bench to find this: the unit test's fake library
            # had the same wrong belief the code did, and they were consistent.
            ret = fn(handle, ctypes.byref(room), buf)
            if ret == NVML_ERROR_NOT_SUPPORTED:
                return None		# this driver will not answer: nobody is vouched for
            if ret not in (0, NVML_ERROR_INSUFFICIENT_SIZE):
                return None
            if ret == NVML_ERROR_INSUFFICIENT_SIZE or room.value >= PROCESS_CAPACITY:
                return PROCESS_CAPACITY + 1
            total += room.value
        return total

    def utilisation(self, handle):
        rates = NvmlUtilization()
        if self.lib.nvmlDeviceGetUtilizationRates(handle, ctypes.byref(rates)) != 0:
            return None
        return max(rates.gpu, rates.memory)

    def power_mw(self, handle):
        """Milliwatts, or None.

        The handle is not optional.  `nvmlDeviceGetPowerUsage(device, *power)`
        called with only the output pointer made NVML follow the pointer as if it
        were a device: usually an error, so a `None` here and a card held busy
        forever, and sometimes a segfault, which is the shape this took in the
        first hardware run of the keeper and is the reason every call below is
        checked to pass the handle it was given.
        """
        milli = ctypes.c_uint(0)
        ret = self.lib.nvmlDeviceGetPowerUsage(handle, ctypes.byref(milli))
        if ret != 0:
            return None
        return milli.value


def verdict(sample, floors, clients_veto=True):
    """`quiet` only if every reading in the window says quiet.

    The window is what makes a once-a-second question honest: NVML's utilisation
    is a sample, so a request that started just after one is not in it, and the
    only thing that catches that is the next one agreeing.  Any None is an
    unknown, and an unknown is never a permission.

    `clients_veto=False` is the resident question asked out loud: a card somebody
    holds is judged on what it is doing rather than on being held, so the floors
    decide and the note says `resident` to record that a card with a client on it
    was called quiet.  An unknown process list still refuses, in both modes: what
    is being withheld there is the knowledge that nobody is home, which is the one
    thing the relaxed rule cannot afford to guess about.
    """
    if not sample:
        return "busy", "nosample"
    util = power = 0
    clients = None
    busiest = 0
    for reading in sample:
        if reading is None:
            return "busy", "unreadable"
        util, power, clients = reading
        # Each of the three is a separate question, and each one the driver refuses
        # to answer is its own reason to refuse: `unreadable` three states deep would
        # be the same answer, and a journal full of it would say nothing about which
        # reading to go and check.
        if util is None:
            return "busy", "noutil"
        if power is None:
            return "busy", "nopower"
        if clients is None:
            return "busy", "noproclist"
        busiest = max(busiest, clients)
        if clients > 0 and clients_veto:
            return "busy", "clients%d" % clients
        if util > floors["util_floor_pct"]:
            return "busy", "util%d" % util
        if power > floors["power_floor_mw"]:
            return "busy", "power%dW" % (power // 1000)
    if busiest:
        return "quiet", "resident%d,util%d,power%dW" % (busiest, util, power // 1000)
    return "quiet", "clients0,util%d,power%dW" % (util, power // 1000)


def report(floors, window, nvml, history, clients_veto=True, peer_veto=True):
    """One round: ask every card, remember, decide, prepare the lines.

    `peer_veto` is the cross-card half of the rule and it exists because of one
    measured reading: a resident card whose tensor-parallel worker had hung, whose
    *neighbour* was still reading its memory, sat at 0 % utilisation and 61 to 67 W
    -- inside the range a resident card at rest reads (56.9 to 59.0 W), and under the
    70 W power floor. Nothing this card can read about itself distinguishes the two,
    because the traffic in the second case belongs to the other card. So the other
    card's answer is borrowed: while anything else on this box is working, a card that
    would otherwise be called quiet is not, and the note says which one held it up.
    That costs watts in the case where two cards really do serve unrelated work and
    one of them is genuinely idle -- and buys the only defence there is against
    lowering the memory a busy neighbour is reading. It is a knob, and the safe side
    of the knob is `y`.

    `history` belongs to the caller and survives between rounds: the window is a
    claim about several consecutive samples, and a fresh dict per round would make
    that claim about one.
    """
    lines = []
    seen = set()
    for bus, handle in nvml.cards():
        seen.add(bus)
        reading = (nvml.utilisation(handle), nvml.power_mw(handle), nvml.processes(handle))
        samples = history.setdefault(bus, [])
        samples.append(reading)
        del samples[:-window]
        state, note = verdict(samples, floors, clients_veto)
        lines.append((bus, state, note))
    if peer_veto and len(lines) > 1:
        # One round's answer from one card is another card's permission, so the whole
        # set is decided before any of it is published: a card the floors call quiet is
        # held anyway while a neighbour of its own box is working, and the note keeps the
        # evidence that was overridden rather than throwing it away, because the shadow
        # runs are read out of these lines.
        held_up_by = sorted(bus for bus, state, _ in lines if state == "busy")
        if held_up_by:
            lines = [
                (bus, state, note) if state == "busy" or bus in held_up_by
                else (bus, "busy", "peerbusy:%s,was:%s" % (",".join(held_up_by), note))
                for bus, state, note in lines
            ]
    # NVML saying "no cards" is not the same answer as NVML saying "no clients", and
    # it is the answer that arrives during a driver reload or after a GPU fell off
    # its bus.  Writing nothing would be safe by accident; this makes it safe on
    # purpose, and says so in the journal, by dropping the library and starting the
    # round again from an initialised one.
    if not seen:
        raise RuntimeError("NVML reports no cards at all: not an empty card list, a card list nobody could read")
    # A card that has gone -- unbound, reset, or taken by passthrough -- has no
    # reading to report, so it gets no line, and the governor, which has heard
    # nothing, keeps it on the busy clock.  Writing `busy` here would be the same
    # answer by a longer route, and would hide the fact that nobody asked.
    for bus in [b for b in history if b not in seen]:
        history.pop(bus, None)
    return lines


def emit(lines, path=LEASE):
    """One write per card.  A partial write is a refused write: the kernel only
    accepts the whole line, and a card it has never heard of stays busy."""
    try:
        fd = os.open(path, os.O_WRONLY)
    except OSError as exc:
        sys.stderr.write("%s: %s -- the governor is not loaded, so nothing is being asked\n"
                         % (path, exc))
        raise SystemExit(75)		# EX_TEMPFAIL: systemd restarts us after it appears
    failed = []
    try:
        for bus, state, note in lines:
            try:
                os.write(fd, ("%s %s %s\n" % (bus, state, note)).encode())
            except OSError as exc:
                # -EBUSY is a legitimate answer for the governor to give: it answers a
                # status file, a perf table read or a policy change first, and it will not
                # be waited for.  A card the governor never heard about stays on its busy
                # clock, which is the safe direction, and the next round is a second away,
                # so the remaining cards still get their answer this round.
                sys.stderr.write("%s: %s %s: %s\n" % (path, bus, state, exc))
                failed.append(bus)
    finally:
        os.close(fd)
    if lines and len(failed) == len(lines):
        raise SystemExit(75)		# EX_TEMPFAIL: nothing at all got through


def ensure_wake_floor(path=WAKE_FLOOR_PATH, floor=RESIDENT_WAKE_EVENTS):
    """Put a floor under the counters, or say why the relaxed rule cannot be run.

    Returns the floor actually in force, or None when it could not be established.
    The caller treats None as the signal to put the strict rule back, because a card
    whose counters are silent about the keeper owning polling is a card that will be
    asked to come down again about as often as it is asked whether it is idle: 26
    switches in 25 s and no watt saved, which is what the first resident run did at
    floor 0, and 33 in 45 s at floor 512.

    Reading the file first rather than writing blindly is the point: a module without
    the parameter is an older governor than this keeper, and discovering that by
    having written to a path that does not exist would be the same bug the keeper was
    written to stop -- a policy half in place, invisible in the watts.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            current = int(handle.read().strip() or "0")
        if current < floor:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("%d" % floor)
            with open(path, encoding="utf-8") as handle:
                current = int(handle.read().strip() or "0")
    except (OSError, ValueError) as exc:
        sys.stderr.write("hbm-lease: %s could not be set to %d (%s): the resident rule needs a"
                         " counter floor and\n  the governor has none, so the client list stays a"
                         " refusal. See\n  docs/HBM-PM.md, \"The resident card that stays down\".\n"
                         % (path, floor, exc))
        return None
    if current < floor:
        sys.stderr.write("hbm-lease: %s reads %d after being written %d: the floor did not take,\n"
                         "  so the client list stays a refusal rather than a card that wobbles\n"
                         % (path, current, floor))
        return None
    return current


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--loop", action="store_true", help="keep reporting, which is the job")
    ap.add_argument("--once", action="store_true", help="report one round and exit (the default)")
    ap.add_argument("--interval", type=float, default=None,
                    help="seconds between rounds (default: from constants.yaml)")
    ap.add_argument("--window", type=int, default=None,
                    help="how many consecutive rounds must agree before a card is quiet")
    ap.add_argument("--util-floor", type=int, default=None,
                    help="utilisation percent above which a card is working")
    ap.add_argument("--power-floor-mw", type=int, default=None,
                    help="milliwatts above which a card is working")
    ap.add_argument("--constants", default=None,
                    help="the constants.yaml the keeper's floors are read from (default: this"
                         " file's own checkout, then %s, which is where the installer puts it)"
                         % CONSTANTS_CANDIDATES[1])
    ap.add_argument("--lease", default=LEASE,
                    help="where to write the verdict (default: %s)" % LEASE)
    ap.add_argument("--resident-idle", action="store_true",
                    help="judge a card somebody holds on utilisation and power alone, whatever"
                         " `clients_veto' says in the constants file, and say `resident' in the"
                         " note when that is what made it quiet.  The shipped rule is read from"
                         " that key, not from this flag; the flag is for the shadow run in"
                         " docs/HBM-PM.md, which is pointed at --trace and --lease /dev/null and"
                         " so does not arm the counter floor.")
    ap.add_argument("--no-peer-veto", action="store_false", dest="peer_veto",
                    help="let a card come down while another card on this box is working.  The"
                         " one reading this feature has of a card being harmed while its own"
                         " answers said idle -- 0%% utilisation at 61 to 67 W, for 98.6 s, with"
                         " its neighbour still reading its memory -- is that case, and the"
                         " counter veto did not catch it either.  Bench only.")
    ap.add_argument("--trace", default=None, metavar="FILE",
                    help="append one line per card per round (`epoch bdf state note`) to FILE."
                         " Only useful with --loop, and it is how a shadow run is read: a"
                         " verdict that changed is a fact, and a verdict that held is the"
                         " thing being measured.")
    ap.add_argument("--faster-than-measured", action="store_true", dest="fast",
                    help="accept --interval below the rate this feature has been observed"
                         " surviving beside a serving workload (%ss per round). Every round is"
                         " three NVML calls per card and each one is an RPC into GSP-RM; a"
                         " shadow keeper at 0.2 s on a box serving with two cards preceded a"
                         " stalled tensor-parallel worker. Name this flag on a bench, not on"
                         " a machine somebody is using." % NVML_MIN_INTERVAL_S)
    args = ap.parse_args()

    # The list is tried rather than one path being assumed, and every path that was
    # tried is named in the complaint: an installed keeper run by hand used to read a
    # path that had never existed, refuse every card on the machine, and say only that
    # the one file it had guessed at was missing -- which reads as a configuration
    # problem the reader cannot reproduce, because they were not the one who guessed.
    paths = [args.constants] if args.constants else list(CONSTANTS_CANDIDATES)
    keeper, read_from = None, paths[-1]
    for candidate in paths:
        keeper = defaults_from_constants(candidate)
        read_from = candidate
        if keeper is not None:
            break
    if keeper is None:
        # Not a reason to write anything: the governor keeps every card at the
        # busy clock until somebody tells it otherwise, and an unconfigured keeper
        # is that somebody not telling it.
        sys.stderr.write("hbm-lease: no keeper configuration in %s, so no lease. The\n"
                         "  governor will leave every card on its busy clock, which is\n"
                         "  the outcome this program exists to be careful about. See\n"
                         "  unlocks.hbm_pm.keeper in common/constants.yaml, or point\n"
                         "  --constants at the file you mean.\n" % " or ".join(paths))
        return 1
    floors = {"util_floor_pct": int(keeper.get("util_floor_pct", 0)),
              "power_floor_mw": int(keeper.get("power_floor_mw", 0))}
    # A client on the card is the refusal by default, and the reason is in the comment
    # on the key.  The flag overrides it for a shadow run and nothing else: a keeper
    # installed by `tools/hbm-pm-setup.sh` runs without it, so the shipped rule is the
    # configured rule, and the configured rule is the one the constants file argues for.
    clients_veto = str(keeper.get("clients_veto", "y")).strip().lower() not in ("n", "no", "false", "0")
    peer_veto = str(keeper.get("peer_veto", "y")).strip().lower() not in ("n", "no", "false", "0")
    if args.resident_idle:
        clients_veto = False
    if args.peer_veto is False:
        peer_veto = False
    interval = float(keeper.get("interval_s", 0))
    window = int(keeper.get("window", 0))
    for name, value in (("--util-floor", args.util_floor),
                        ("--power-floor-mw", args.power_floor_mw),
                        ("--interval", args.interval),
                        ("--window", args.window)):
        if value is None:
            continue
        if name == "--util-floor":
            floors["util_floor_pct"] = int(value)
        elif name == "--power-floor-mw":
            floors["power_floor_mw"] = int(value)
        elif name == "--interval":
            interval = float(value)
        else:
            window = int(value)
    if floors["power_floor_mw"] <= 0 or interval <= 0 or window < 2:
        sys.stderr.write("refusing to run: unlocks.hbm_pm.keeper in %s has\n"
                         "  power_floor_mw=%d interval_s=%s window=%d.\n"
                         "  A power floor of zero keeps every card busy forever, and a window\n"
                         "  below 2 rounds would make a single sample the evidence a clock\n"
                         "  gets lowered on. Both are safe directions, which is why they are\n"
                         "  a refusal here rather than a default.\n"
                         % (read_from, floors["power_floor_mw"], interval, window))
        return 1
    if interval < NVML_MIN_INTERVAL_S and not args.fast:
        # The keeper is not the only thing that pays for a fast poll, and the bill arrives
        # somewhere else: three NVML calls per card per round, every one an RPC into GSP-RM
        # on the same serialised path a workload's launches use. This is the one place a
        # curious operator can put that rate, so it is the one place that asks first.
        sys.stderr.write("refusing to run: --interval %ss means %.1f rounds a second, %d NVML"
                         " calls a second per card.\n"
                         "  The fastest rate observed harmless beside a serving workload here"
                         " is %ss.\n"
                         "  What happens above it is in docs/HBM-PM.md, and it is not a"
                         " measurement of this\n"
                         "  keeper's judgements: it is one tensor-parallel worker that stopped"
                         " reporting\n"
                         "  within a minute of the rate going up. If this really is a bench"
                         " and you mean it,\n"
                         "  add --faster-than-measured.\n"
                         % (interval, 1.0 / interval, 3 * (1.0 / interval), NVML_MIN_INTERVAL_S))
        return 1
    # The relaxed rule and the counter floor are one setting, not two.  A shadow run is
    # exempt: it writes nothing to the governor, so it cannot wobble a card, and a
    # measurement of what the rule *would* have said must not change the machine.
    floor_in_force = None
    if not clients_veto and args.lease != os.devnull:
        floor_in_force = ensure_wake_floor(
            floor=int(keeper.get("resident_wake_events", RESIDENT_WAKE_EVENTS)))
        if floor_in_force is None:
            clients_veto = True
    # One line at the start of the process naming the file the floors came from, the
    # numbers actually in force, and the rule about clients.  The keeper's failure mode
    # is invisible in the watts: a keeper that read nothing answers `busy` exactly like a
    # keeper that read the truth, and the three bugs that shipped today were all of the
    # first kind.  What it read has to be in the journal rather than inferred from the
    # unit's command line, and so does which rule it is running: a shadow run and the
    # shipped one differ only in that rule, and look identical in the lease file.
    sys.stderr.write("hbm-lease: floors from %s: util>%d%% or power>%dmW%s means busy, %d"
                     " rounds %.1fs apart\n"
                     % (read_from, floors["util_floor_pct"], floors["power_floor_mw"],
                        " or any client" if clients_veto else
                        " (clients reported, not a veto)",
                        window, interval))
    sys.stderr.write("hbm-lease: cross-card rule %s, counter floor in force %s\n"
                     % ("a busy neighbour holds this card busy" if peer_veto else
                        "off (--no-peer-veto): a card may come down while its neighbour works",
                        floor_in_force if floor_in_force is not None else
                        "unset, the strict counter rule" if clients_veto else
                        "FAILED to set"))

    # Opened once, before the loop and before NVML: the trace is the whole point of a
    # shadow run, so a path that cannot be written is a reason not to start, and an
    # OSError from inside the loop would not be caught by the handler below anyway --
    # it would take the keeper down mid-service, which is the opposite of what a
    # read-only observer is for.  Flushed every round because the way a shadow run
    # ends is usually somebody killing it, and buffered evidence is lost evidence.
    trace = None
    if args.trace:
        try:
            trace = open(args.trace, "a", encoding="utf-8")
        except OSError as exc:
            sys.stderr.write("refusing to run: --trace %s cannot be written (%s), and a"
                             " shadow run that cannot record its verdicts has measured"
                             " nothing\n" % (args.trace, exc))
            return 1

    nvml = None
    history = {}
    while True:
        try:
            if nvml is None:
                nvml = Nvml()
                history = {}
            lines = report(floors, window, nvml, history, clients_veto, peer_veto)
            if trace:
                # One line per card per round, unconditionally: a file that records only
                # the changes cannot answer the question a shadow run is run to ask, which
                # is how long a candidate verdict held while the card was working.
                for bus, state, note in lines:
                    trace.write("%.3f %s %s %s\n" % (time.time(), bus, state, note))
                trace.flush()
            emit(lines, args.lease)
        except RuntimeError as exc:
            # No NVML, no driver, no permission: say it in the journal, and say
            # nothing to the governor, whose default without an answer is the busy
            # clock.  Silence here is the correct failure.  The handle is dropped
            # so the next round starts from an initialised library again.
            sys.stderr.write("hbm-lease: %s\n" % exc)
            nvml = None
            if not args.loop:
                return 1
            lines = []
        if args.loop:
            time.sleep(interval)
            continue
        for bus, state, note in lines:
            print("%s %s %s" % (bus, state, note))
        return 0


if __name__ == "__main__":
    sys.exit(main())
