# Idle HBM power

An idle CMP 170HX draws over 40 W, and most of that is HBM2E being refreshed at the rate it
is refreshed at when it is serving a job: the card's VBIOS carries one performance level, so
nothing ever asks the memory PLL to come down and GSP-RM has no low power state to ask for
one either. That single level is not an inference from what the tools print: the table is
decoded from the card itself under
[What the card's performance table says](#what-the-cards-performance-table-says). The
unlocked parts of this driver change what the card *reports* and what it *addresses*; none of
them changes that, so an idle card has gone on paying full refresh rate forever.

This feature stops that, with a module and a keeper. The patched driver opens two privilege
masks during GSP bootstrap and then never touches the memory PLL again, so the decision has to
be made repeatedly, by something that is still running: `driver/hbm-pm/cmp_hbm_pm.c` watches the
activity counters GSP-RM already keeps armed, and `tools/hbm-lease.py` asks the driver once a
second whether anything is using each card. When the counters have seen nothing for a while
*and* the keeper says the card is unclaimed, the module moves the memory PLL to a low
coefficient, and moves it back the moment either of them says otherwise.

The two are not redundant with each other, and neither is redundant with the card's own wattmeter.
The counters were measured reporting nothing at all for seconds on a card serving requests at 178 W
and 100 % utilisation, so they can stop a switch and cannot license one: silence from a counter is not
evidence of an idle card. What they do see, measured again later and at the other end of the scale, is
the *onset* of work -- the smallest movement any request has produced on this hardware was 81981 events
in one 20 ms sample, from an eight-token completion -- so a card that is down and gets asked for
something is back at the busy clock within one poll, 26 to 49 ms of a request being sent. The keeper
covers the case the counters do not, because NVML reported 90 to 96 % utilisation on every one of the
rounds taken while a request was in flight.

The hardware sequence is ported from `gpu-unlock 3.0.0-beta` (GPL-2.0-only,
[Zagitta/gpu-unlock](https://github.com/Zagitta/gpu-unlock/tree/3.0.0-beta)), where the same
path is reported to take an idle CMP 170HX from about 40 W to about 23 W. Measured here, on two
CMP 170HX at their 180 W limit with nothing running on the machine and the clock lowered on the
keeper's answer alone, it is **13.8 W and 13.7 W, about 27 W for the pair** — see "Measuring it"
for the four columns and for why the comparison has to be made inside one boot. A card with something
holding it, including a serving framework that is resident but idle, earns the same saving: **37.3 W
and 35.9 W against 51.2 W and 49.5 W**, on the policy rather than by hand, holding the idle clock for
39 seconds of a 40-second window. It costs, when the judgement is wrong and nothing wakes the card, a
request that takes about twice as long and comes back with an uncorrected ECC count unmoved; the
worst measured case is in "The resident card that stays down", and it is the reason the judgement is
allowed to be made at all.

That upstream has two halves, and this page is about only one of them. The HBM clock policy
below is its memory-PLL path; the other half adds an idle performance level — a P8 row — to the
VBIOS' perf table, and it is the half this repository investigated and did not adopt, at length,
in [Why this does not also add a P8](#why-this-does-not-also-add-a-p8).

## Enable or disable

```bash
sudo ./install.sh                 # on, like the wide layout and the fused link speed
sudo ./install.sh --no-hbm-pm     # leave every card at the clock it boots to
```

`CMPUNLOCKER_HBM_PM=0` is the environment spelling, and `driver/build.sh` accepts it
directly. The feature has three halves and the three are deliberately separable:

| Half | Written by | What it does |
| --- | --- | --- |
| The key | `driver/build.sh`, as `CMPEnableHbmPm=1` in `NVreg_RegistryDwords` | tells the booter to widen the FBPA refresh block and the memory PLL |
| The module | `tools/hbm-pm-setup.sh`, as `/lib/modules/$(uname -r)/updates/cmpunlocker/cmp_hbm_pm.ko` | holds the veto and performs the switch, at run time, on a card somebody has told it is unclaimed |
| The keeper | `tools/hbm-pm-setup.sh`, as `/usr/local/lib/cmpunlocker/hbm-lease.py` and `cmp-hbm-lease.service` | asks the driver which cards nobody is using, once a second, and holds the permission |

Deleting the key without rebuilding is enough to make the governor harmless: the masks stay
closed, the module reads one word, reports `refresh access closed` once per card and leaves
the card on the clock it booted with. Deleting the module without touching the key is also
safe, and is what `--no-hbm-pm` on a machine that already has a built driver amounts to: two
masks are opened that nothing writes through. The key is read during GSP bootstrap, so
changing it takes a driver reload or a reboot, not a `modprobe -r`.

Stopping the keeper is the interesting case, because it is the one that happens by accident
when a unit fails to start or a machine comes up without the driver ready. The lease expires
after `lease_ms` and every card returns to the busy clock, which is the outcome the feature is
designed to degrade to rather than the one it is designed to reach. Nothing has to notice:
the status file says `lease stale` or `lease none` on each card line, and the governor cannot
be argued out of that by anything the counters report.

The installer runs `tools/hbm-pm-setup.sh` as its own step, after the patched modules are
installed. A governor that will not build, or will not load, is reported and does not fail
the install: the card is left on the clock a machine without this feature is on anyway. The
setup script itself exits non-zero in that case, so running it by hand says the same thing
twice rather than reporting success for a module that is not running. It also checks that the
keeper actually got an answer out of the cards, since a feature whose other half is not
running is indistinguishable on a wattmeter from one that is working.

## What it does, in the order the hardware requires

NVIDIA's own HBM clock-switch path, run from the host:

1. Ask the framebuffer to stop (`NV_PFB_STOP_REQUEST`) and wait for the acknowledge. Nothing
   may be moving the memory while the clock moves, and this is the handshake firmware uses
   for the same purpose, so it is shared with RM rather than claimed.
2. Lengthen the refresh interval in every FBPA's refresh block, while the memory clock is
   still the busy one, so the interval in nanoseconds is already the right one for the clock
   the card is about to run at.
3. Move the PLL coefficients — `NV_PFB_PLL_CFG` and `NV_PFB_PLL_COEFF` — through the
   serial-privilege aperture (`0x98bc90`/`0x98bc98`) inside a `NV_PTIMER_PRI_FENCE_ACQUIRE`
   window, with the post divider bypassed when it changes. Bypassing is what keeps the VCO
   and the trained delay lines: the M divider is left alone, so the idle clock is a pair of
   dividers rather than a frequency, and the loop never has to re-lock from nothing.
4. Shorten the refresh interval again, for the clock the card is now running at.
5. Force one refresh by software, release the fence, and clear the stop request.

Going back up is the same sequence with the saved values, and it happens on the next sample
that sees traffic. If the way back fails, the module gives up on that card: one `KERN_EMERG`
line carrying the two handshake registers, no further MMIO to it, and no more samples. The
port this came from kept the stop held instead, reasoning that a card on an unknown memory
clock is worse than a card out of service; the card that started holding a stop at 11:16 was
needing a reset by 11:21, and the 24814 failed release attempts it took to get there are the
argument against that reasoning. An unknown PLL is a card the next driver reload fixes. A
framebuffer nobody can start is a card the reboot operator fixes, and the retrying is what
turned one into the other.

The idle decision is taken from two sources that are not allowed to take each other's word,
because the one this feature used to have was measured failing at it.

The counters are the veto. The module borrows whichever counters GSP-RM already has armed, as
long as between them they cover the graphics engine, the copy engines and the framebuffer
request path, and it trusts a counter only when the five words describing it are unchanged
across the read of its count and say that firmware armed it. Any movement in any of them, at
any magnitude, means the card is working — including the counter that only completes the
coverage set and ticks itself, which is what the earlier attempt to filter out here got wrong.
A card whose firmware arms nothing useful is a card the veto can never lift, so the governor
stays out and the card stays busy.

The lease is the permission, and it comes from userspace: `tools/hbm-lease.py` asks the driver
through NVML the three questions the kernel cannot answer on this chip — utilisation, power
draw, and whether any process holds the card — and writes a per-card verdict to
`/sys/kernel/cmp_hbm_pm/lease` once a second. The module acts on it only when it is present,
fresh within `lease_ms`, well formed, says `quiet`, has been said more than once in a row, and
has held that way for the whole `idle_ms` window. On the bench that mattered because a card
whose keeper reported two compute clients and 97% utilisation was the same card whose
borrowed counters had reported `active 0x0` for nineteen seconds.

`dmesg` prints which counters were borrowed and what each watches, and the status file reports
`active`, the largest `deltas` each ever saw, and the lease as it was received, so neither
half of the decision is something a reader has to take on faith.

## The knobs

All ten are runtime-writable under `/sys/module/cmp_hbm_pm/parameters/`, and writing any of
them reschedules the poll immediately, so a measurement does not need a reload.

| Knob | Default | What it is |
| --- | --- | --- |
| `enable` | `Y` | Governor at all. `N` restores every card and stops polling; it is the same switch as `--no-hbm-pm`, without a rebuild. |
| `mode` | `0` | `0` auto. `1` pins the busy clock and `2` pins the idle clock: both stop the governor, and both exist so a wattmeter reading can be attributed to the card rather than to a policy. |
| `poll_ms` | `20` | Milliseconds between activity samples. |
| `idle_ms` | `1000` | Milliseconds during which nothing at all may be seen on the borrowed counters before the clock drops. Gaps of 8.4 s and 19.5 s have been measured on a serving card drawing 178 W at 100 % utilisation and 74 W at 24 %, so a window of a few hundred milliseconds is a window that can be silent on a card that is working. What a second costs is one second of the idle clock after a card has genuinely gone quiet. |
| `lease_ms` | `3000` | How stale the keeper's answer may be before it counts as the answer it did not give. `0` waives the lease and leaves the counters as the only evidence: a bench setting for pricing the keeper, printed as `lease waived` on every card line. On a machine anybody uses, waiving it permits switches the lease exists to prevent. |
| `idle_ndiv` | `20` | PLL N divider while idle. The clock is `27 MHz * ndiv / mdiv / pldiv`, with `mdiv` the card's own. |
| `idle_pldiv` | `1` | PLL post divider while idle. Changing it is the case that needs the bypass, and so the one that costs a few more microseconds. |
| `idle_refresh` | `48` | Refresh interval while idle, in units of eight busy-clock periods. Too small and the memory draws refresh current for nothing; 0 is refused. |
| `wake_events` | `0` | Events one sample of a borrowed counter has to report before the governor calls that movement work. `0` is what the module loads with and what the empty-card columns of this file were measured under: one event refuses the card, and on a card nobody holds that costs nothing. The keeper raises it to `resident_wake_events` when it arms the resident rule, and refuses to arm that rule if the governor will not take the floor -- see below, because the number is bounded at both ends by measurements rather than tuned. A movement under the floor is neither spent nor forgotten: the baseline is not advanced, so the events accumulate and the first sample that crosses the floor sees all of them. Above `1<<20` the write is refused: that ceiling is already twelve times the smallest movement any request has produced on this hardware (81981 events, from an eight-token completion), so a floor at the cap is a floor that could swallow a health check whole, and the largest sample a request has produced is 7580103. What the keeper arms, 8192, is fifteen times the noisiest keeper round ever read (545 events) and a tenth of the quietest request. |
| `log_transitions` | `Y` | Log each switch as it happens, with the clock it moved to, the two framebuffer handshake registers around it, why a wake happened when the counters did not explain it (`because idle lease stale`), and how long the register sequence took. On by default: an unlogged switch cannot be attributed to a wattmeter column afterwards, and this is the only record of the switch's own timing. |

The keeper's own numbers are not knobs: `interval_s`, `window`, `util_floor_pct`, `power_floor_mw`,
`clients_veto`, `peer_veto` and `resident_wake_events` live in `common/constants.yaml` under
`unlocks.hbm_pm.keeper`, next to every other number in this feature, and `tools/hbm-lease.py` reads
them from there. The file it read them from, the floors in force, and the two rules it is applying --
to a client on the card, and to a busy neighbour -- make up the first lines the keeper writes to its
journal; a keeper that cannot read them refuses to run, which leaves every card on its busy clock.

`interval_s` is not a tuning knob either, and shortening it is not a way to save more: every round
is three NVML calls per card, and on GSP-RM each one is an RPC into the firmware that shares its
serialised path with a workload's launches. Running a second keeper at 0.2 s beside the shipped one
on a box serving with two cards -- 30 calls a second against 6 -- was followed inside a minute by a
tensor-parallel worker that stopped reporting, and by an engine that gave up four minutes later. No
clock moved during it: `switches` was unmoved until that container went down, there was no Xid, and
no card halted, so what it measures is NVML and GSP-RM rather than this module, and the cause of the
worker's stall was not isolated. The shipped rate is the rate observed harmless here, and it is a
limit rather than a preference: one keeper per machine, and do not raise its rate on a box somebody
is using.

## Reading what it is doing

```bash
cat /sys/kernel/cmp_hbm_pm/status
systemctl status cmp-hbm-lease.service
dmesg | grep cmp_hbm_pm
```

The status file starts with the policy in effect and then one line per card, both lines below
recorded verbatim on the bench: the first is a card two tensor-parallel workers hold with no
request in flight, the second a card the keeper has granted the low clock to.

```
mode=auto enable=1 idle_ms=1000 poll_ms=20 lease_ms=3000 idle_ndiv=20 idle_pldiv=1 idle_refresh=48
0000:05:00.0 1728 MHz busy, 540 MHz idle target, busy, quiet 15856 ms, counters 0x3 active 0x0 deltas 15147986/7772554, switches 10 declines 18 errors 0, lease 88 ms busy clients1, fb 0x3ffffc00/0x501408, idle lease refused: the keeper reports work on the card
0000:06:00.0 1728 MHz busy, 540 MHz idle target, idle, quiet 240340 ms, counters 0x3 active 0x0 deltas 0/0, switches 3 declines 3 errors 0, lease 474 ms quiet clients0,util0,power29W, fb 0x3ffffc00/0x501408, ok
```

`busy` means the card is at the clock it booted with, `idle` that it is at the target, and
`fb-stopped` that this module is holding the framebuffer path out of service. On its own that
is a transition in progress. Next to `halted` it is the state to act on: the module asked for
the stop, could not get it back, and has stopped touching that card until the module is
reloaded; see [How it fails](#how-it-fails). The two handshake registers are printed on every
card line as `fb`, and repeated in the `KERN_EMERG` line, because they are the only values that
say which side of the handshake stopped answering.

`counters` is the set of GSP-RM's activity counters this card lets the governor read, `active`
is which of them moved during the last sample, and `deltas` is the largest single-sample
movement each has reported since the card last changed state, in slot order. `deltas` is a
high-water mark, not a rate: it stays where it was through a quiet period, and it is the number
that answers "can these counters see this workload at all" rather than "what is the card doing
now". On this card the borrowed pair is `counter 0 watches 0x380001 0x700000 0x7` and
`counter 1 watches 0x80 0x0 0x0`, and that pair has been recorded reporting `active 0x0` with
`quiet` above eight seconds on a card at 100 % utilisation. A counter moving is evidence of
work. A counter being silent is not evidence of no work, and nothing in this module decides
anything on that direction.

`lease` is the keeper's answer as the governor saw it: how long ago, whether it said `quiet`
or `busy`, whether it has gone `stale`, and the note of what it looked at. `none` means the
keeper has never answered for this card, and that card will not go idle; `waived` means
`lease_ms=0`; `forced` means somebody wrote `force` by hand, which is how `mode=2` is meant to
be reached. `stale` is its own answer: the note beside it is history rather than news, and a
stale `quiet` is not permission, the same as no answer at all. The lease is re-asked for as long
as a card stays at the idle clock, not only on the way in, so a keeper that stops while a card is
idle brings that card back up within `lease_ms` plus one poll rather than leaving it there
indefinitely. Every way the lease can fail is in the same direction: keeper not running, keeper
crashed, card unreadable, answer refused, all leave it drawing full memory power. That is the
trade this feature makes — a broken keeper costs watts, and the counters are not trusted to
have ever cost anything else.

These files answer or they say they could not; they do not wait for a person indefinitely. All
three of them (`status`, `perf_table`, and the writer side of `lease`) walk the same list of
cards, and a read that cannot have it within about two seconds prints `status unavailable` /
`perf table unavailable`, or takes `-EBUSY` in the keeper's case and tries again a second later.
A monitoring script must not be able to become an unkillable task because this module was holding
its own list, which is what those two strings are for.

`flaps` prints on a card line only when it is not zero, and it counts the stays that were not stays:
times the card went to the idle clock and was pulled back before it had been down as long as the
quiet period that earned it, which is the signature of a card that was never idle and is being asked
anyway. Each one defers the next attempt to lower the clock, doubling per consecutive short stay up
to 30 s, and one stay long enough to be real clears the count. It is on the line because `switches`
by itself cannot tell a card that will not stay down from a governor that is broken: 660 idle/busy
pairs in twenty minutes looked like an active machine and saved no watts at all.

`switches` counts clock moves the module made in either direction, including the one a pinned
`mode` or a policy write causes, so the field is what ties a wattmeter column to a line in
`dmesg`. `declines` is the card saying not yet, or not on this driver, or the governor refusing
itself for one of the reasons below, counted one per *saying* rather than one per poll: an
unleased card answers `-EAGAIN` every 20 ms, and counting attempts made that field read 1222 in
thirty seconds in which nothing happened. `errors` is the hardware not confirming something the
module wrote, and it is the number that should stay at zero. It is counted per attempt on purpose,
because a card that was retrying a dead framebuffer for five minutes finished at `errors 24814`,
and a number that climbs there is the alarm whether it arrives one at a time or forty a second. The
last reason is printed at the end of the line, and the reasons worth knowing:

| Reason | What it means |
| --- | --- |
| `refresh access closed`, `HBM PLL access closed` | The masks are closed: the driver was built without `CMPEnableHbmPm=1`, or the geometry rewrite's single-point write did not reach every FBPA window. The printed register is the mask that said no, so the two are told apart by reading it. This is what a plain install looks like: the module reads the mask, says so once per card, and leaves the card alone; the frequencies on its line read 0, because it stopped before the point where it reads the PLL. |
| `activity counter unavailable` | Firmware reconfigured a counter mid-read, or is not running one. The governor waits rather than guessing. |
| `activity counter coverage missing` | No armed counter sees engines this card needs to see before it may be called idle. |
| `idle lease absent: no keeper has answered for this card` | The keeper is not running, is not installed, or does not know this card. The card stays at the busy clock; `systemctl status cmp-hbm-lease.service` is where to look, and if it is running, this line means the keeper is failing to read something rather than the machine being unmeasured — its own log names the reading. |
| `idle lease stale` | The keeper stopped answering within `lease_ms`. The value printed is how long it has been silent for, in ms. Same consequence: nothing is allowed to be assumed about a card nobody is reporting on. |
| `idle lease refused: the keeper reports work on the card` | Somebody is using it. This is the lease doing the one job it exists for. |
| `idle lease is too young to be believed` | One report of `quiet` is a sample, not a period. The keeper asks once a second, so the second report is the first one that could have disagreed. |
| `idle lease has not held quiet for the whole idle window` | The verdict flipped to `quiet` partway through `idle_ms`, so the silence the counters vouched for and the silence the keeper vouched for are not the same stretch of time. The value is how long it has held, in ms. |
| `HBM idle refused: activity at the moment of the stop request` | The counters moved between the sample that decided and the instruction to stop. This is the check that runs last before the write, and it is why the transition did not happen under a card that had just started working. |
| `HBM counter re-armed after FB stop` | Firmware changed what a counter watches after the stop was granted, which is firmware doing something with the memory. The stop is released and the PLL is never touched. |
| `idle transition waiting out the backoff after a failed one` | A transition failed recently and the governor is not hammering the card for one. The value is the current backoff in ms; it doubles up to 5 s and resets as soon as the card is seen working. |
| `waiting for GFW boot` | The card's firmware is not far enough along for a clock switch to be safe. Normal at boot and after a driver reload. |
| `FB stop ACK timeout` | The framebuffer did not acknowledge the stop. The stop request is withdrawn, the PLL is never touched, and the card goes on its backoff. |
| `fb resume ACK timeout`, `fb resume request stuck` | Shown as `halted` on the card line and as one `KERN_EMERG` line in `dmesg`. The module has stopped touching this card, and the framebuffer may still be held out of service, which is a card nobody can use. Save `dmesg` first — it is the evidence — then reset the GPU or reload the module. Nothing this module can do is going to unstick it, and trying again every 20 ms is what turned the first occurrence into five minutes of a dead card. |

Any line whose last field is not `ok` came with a register and a value in `dmesg`, and the
rest of the list (`invalid refresh`, `HBM PLL not ready`, `refresh readback mismatch`) is
that kind of answer: the module checked something the hardware did not confirm, put the card
back, and will try again on the next idle period.

### What the card's performance table says

The claim this whole feature rests on — that the card carries one performance level, so nothing
ever asks the memory PLL to come down — is a claim about a table. `/sys/kernel/cmp_hbm_pm/perf_table`
reads that table out of the card instead of inferring it from what `nvidia-smi` chooses to print:
it decodes the VBIOS container sitting in the PROM window of BAR0, by the same walk
`gpu-unlock 3.0` uses to find the image it edits. It writes nothing, to the card or to the image,
and it takes about 0.7 s here, which is why it is a file of its own rather than more lines in
`status`, where somebody would pay a megabyte to ask what clock a card is at.

```
0000:05:00.0 perf table 0x60: 4 rows, 1 states, 3 free, vbios 92.00.6d.00.0a
  P0 id=0xf policy=08 03 00 00 floor MHz: GPC>=210 XBAR>=615 HBM?>=1728 SYS>=630 d4>=27 NVD>=585 d6>=540 d7>=27 d8>=1 HOST>=555
  a generated P8 would lower: GPC XBAR SYS NVD HOST to 135 MHz, leaving the rest of the P0 row alone
  P8 insert: not possible on this image (-95): needs a free first row, exactly one P0, and the policy bytes 08 03 00 00
```

Four things come out of that block, and they are why it is worth a file. **One state in four
rows** is the claim above measured rather than asserted. The domain whose floor is 1728 is the
memory tuple — labelled `HBM?` because the label is earned by matching the clock the governor
reads back off the PLL, not by an offset somebody guessed — and it is the number three
independent paths agree on: the table's floor, the PLL coefficient (`0x14001`, 1728 MHz), and the
single frequency `--query-supported-clocks=memory` reports. The GPC floor of 210 matches the
lowest of the hundred points NVML lists for the graphics domain, which is the same cross-check on
a second domain. And **540 MHz, the idle target this feature drives the memory to, appears
nowhere in the table**: the card has never been told by its own VBIOS to run its memory that
slowly, which is exactly why the feature needs the two widened privilege windows rather than a
request.

A read that costs a megabyte of PROM ought to be seen not to cost the card anything, and it does
not: `switches`, `declines` and `errors` were identical before and after the reading quoted above,
and the card held 31.9 W and 29.7 W through it.

## Measuring it

A wattmeter is the only instrument that means anything here. `nvidia-smi` reads this SKU's memory
clock out of the same single performance level the feature works around, so it reports 1728 MHz
whether the HBM is at that clock or not; the module's own `busy`/`idle` field is the state and the
meter is the evidence. Columns must be taken in one boot, from one sensor, with nothing else on the
machine: idle against idle across a reboot is not a comparison, because leakage in HBM2E in a card
that has been serving a job for hours is worth more than the refresh rate being saved.

| Column | Configuration | What it establishes |
| --- | --- | --- |
| C | `rmmod cmp_hbm_pm` | the reference: what the card draws with nothing governing it |
| A | `insmod`, keeper not running | the resident cost of the module: the gate must hold every card at the busy clock, so this column must equal C |
| D | `insmod`, keeper running | the delivered saving: the clock lowered on the keeper's answer alone |
| B | `insmod`, `<bdf> force` rewritten every second | the floor: the same clock reached without the keeper, as the cross-check that D is a saving and not a nicer measurement of the same thing |

Each column is settled for about 45 s and then sampled at 1 Hz for 120 samples. No column is valid
with a compute client attached; `nvidia-smi --query-compute-apps=pid` must print nothing first. On
the bench, two CMP 170HX at their 180 W limit, at the shipped defaults (`idle_ms=1000`,
`lease_ms=3000`, `poll_ms=20`):

| Column | `0000:05:00.0` | `0000:06:00.0` |
| --- | --- | --- |
| C governor absent | 45.46 W | 43.20 W |
| A governor present, no keeper | 46.03 W | 43.64 W |
| D keeper-granted idle | **31.70 W** (median 31.58) | **29.55 W** (median 29.58) |
| B forced to the floor | 31.64 W | 29.61 W |

The saving is 13.8 W and 13.7 W, about 27 W for the pair. A equals C within the resolution of the
meter, so the module costs nothing by being resident. D equals B within 0.06 W, so the policy
reaches the floor the hardware offers rather than a point of its own choosing. The meter belongs to
the board and includes whatever else the slot draws, so only the difference between columns is
attributable to this feature.

The same two states, reached by pinning rather than by policy. One of the two cards read
1458 MHz busy then and reads 1728 MHz like its neighbour now, which is a second reason to read the
clock off the PLL coefficient rather than off `nvidia-smi`:

| `mode` | `0000:05:00.0` | `0000:06:00.0` |
| --- | --- | --- |
| `1` pinned busy, what the card does without this feature | 46.4 → 47.7 W | 42.3 → 43.3 W |
| `2` pinned idle, the floor | 31.1 → 33.0 W | 29.0 → 30.5 W |

Ranges are given because a card drifts about a watt across its own sample window. Pinned idle needs
a `force` lease, which is the operator saying the card is empty:

```bash
echo '0000:06:00.0 force' | sudo tee /sys/kernel/cmp_hbm_pm/lease   # say the pin is meant
echo 2 | sudo tee /sys/module/cmp_hbm_pm/parameters/mode             # the floor
sleep 5                                                              # read the meter
echo 1 | sudo tee /sys/module/cmp_hbm_pm/parameters/mode             # the baseline
sleep 5                                                              # read the meter
echo 0 | sudo tee /sys/module/cmp_hbm_pm/parameters/mode             # the policy
sleep 15                                                             # read the meter again
```

Both directions of the wake path belong with every column. Kill the keeper, or stop refreshing a
`force`, and the card must be back on its busy clock within `lease_ms` plus one poll, with the
transition logged as `because idle lease stale`; a card that comes down and stays down is a broken
feature that looks like a working one until somebody sends it a request. Under load the same two
files are the check: with requests in flight the card must read `lease … busy` with the keeper's
client count in its note and `switches` must not move, and between requests on a card somebody
owns the counters go silent — `active 0x0`, `quiet` climbing — without the card switching.
`verify.sh` prints the status file and reports a non-zero `errors` or a halted card.

### What the floor is once a job owns the memory

A resident serving framework changes what is available, not what the policy does. With a container
holding 75 GB of the 80 GB and no request in flight, the same card pinned to the idle clock reads
6 to 7 W above the empty-card floor, on both cards:

| Card holding 75 GB, `mode=2` pinned idle | `0000:05:00.0` | `0000:06:00.0` |
| --- | --- | --- |
| the container's process running | 37.3 → 37.9 W | 35.6 → 36.1 W |
| frozen by `docker pause` | 38.0 → 38.5 W | 35.9 → 36.7 W |

`docker pause` separates the two possible causes: it stops every process in the container and
leaves the allocation, the page tables and the driver's context where they were. Freezing changed
nothing, so those watts are what it costs the card to hold memory that has been given out, not a
workload being polled, and no policy takes them back.

What that leaves on the table has now been held down and metered rather than got by subtraction, and
is no longer being forgone: the shipped keeper arms the resident rule. The readings below were taken
before that, on a card held down by hand so that the number would be the hardware's answer and not a
prediction from the empty-card case -- the governor itself made the switch, on a card whose container
held its 75 GB and whose serving engine sat with no request in flight, with the board's own sensor read
at 1 Hz in two windows taken seconds apart and the neighbouring card left alone as the control:

| `0000:05:00.0`, resident and at rest | at the busy memory clock | held on the idle clock |
| --- | --- | --- |
| this card, three readings | 56.94, 57.44, 59.01 W | 41.02, 41.12, 41.31 W |
| its neighbour, same two windows | 49.23, 49.32, 49.43 W | 49.34, 49.54, 49.63 W |

15.9 to 17.7 W, against 0.4 W of drift on the card nobody touched -- the same size as the empty
card's 13 to 14 W, and the first time it has been realised on a card somebody holds. It was obtained
by holding the card down with a hand-written `force` lease and `wake_events` raised to 512, so that
the resident process's own housekeeping would not pull it back up; the last section of this page takes
the same measurement through the policy instead of by hand. What the hold showed is the other half of
the story: on 11 of 12 one-second samples the card was on the idle clock, its `quiet` field climbing
to 10836 ms, and it was pulled up exactly once, 10.8 s into the hold -- which is the accumulation the
floor is designed to produce, 7 to 54 events a sample about once a second reaching 512 in about ten
seconds. With the floor at its shipped `0` the same hold produced about 26 switches in 25 s and no
watt saved. The permission is the missing part, and the two candidate sources of it are not
equivalent:

- The counters are a veto and not a vote, and that remains the rule -- but what they cannot see is
  not "the work of a resident workload". On a card whose serving framework was resident and idle they
  saw every request the moment it was sent (`active 0x3`, 249983, 473659 and 2785189 events in a
  sample) and raised the card again in 26 ms. What they cannot be trusted with is the negative: the
  same pair has been recorded silent for 8.4 s on a card drawing 178 W, and a silent counter is not
  evidence of an idle card.
- Utilisation sees the work and the client list does not, so `clients > 0` could be demoted from a
  refusal to a note, and on the strength of the shadow run below it is the only thing that does the
  judging. What cannot be claimed is that the two readings together are conservative: power says
  nothing at all in this comparison, and `clients == 0` is not the absence of a workload either.
  While a serving framework was writing weights into the framebuffer, before it had registered a
  compute context, NVML reported `clients0` on both cards while the borrowed counters advanced by
  9640 and 170862 events in a 20 ms sample. The counter veto woke those cards, within about a second
  and with no error. A policy that stopped believing the client list would be resting on a reading
  that had just been wrong in the direction of idle.

`tools/hbm-lease.py --resident-idle` runs the relaxed rule and writes its trace to a file instead of
the lease, which measures how often it would have been wrong without asking the hardware anything;
`clients_veto` in `common/constants.yaml` is what makes it the shipped rule, and a shadow run is how a
change to the floors above is vetted before it is armed. One shadow run is one keeper: stop the unit,
run the shadow at the shipped `interval_s` with `--lease /dev/null`, and accept that the cards stay on
their busy clock while it reads, because an unanswered question is the safe answer. Two keepers at once
is how a serving worker above this paragraph stalled, and the keeper now refuses a rate that fast rather
than leaving it to documentation. A shadow run is exempt from arming the counter floor, since it writes
nothing to the governor.

That run has been made -- two bursts of six requests each against the live service, both cards, the
keeper reading at the shipped one-second rate and answering nothing -- and it says the relaxed rule is
judged by one field and not by the other two. Twenty-four requests, both cards, 46 rounds taken
wholly inside a request: **the verdict turned to `busy` between 0.08 s and 0.97 s after every one of
them**, and exactly one of those 46 rounds said `quiet` -- the first round of the first request of a
burst, 1.09 s in, carrying `resident2,util0,power49W`, which is the round whose sample was taken
before the request existed. Nothing in a burst came back to `quiet` once it had left.

| What the keeper looked at | While serving | While the same cards were at rest |
| --- | --- | --- |
| `utilization.gpu` | 90 to 96 %, and 1 % often enough to refuse (12 rounds at `util1`) | 0 %, in all 79 quiet rounds |
| `power.draw` | 56 to 57 W, and once 74 W | 56 to 57 W |
| `clients` | 1 and 2, always, in both cases | 1 and 2, always |

So utilisation is the discriminator, the client list never moves and therefore decides nothing, and
the power floor is not a discriminator at these levels: the card being served read the same watts as
the card doing nothing. Which is the calibration the shipped 70 000 mW floor needs to be read against
-- it sits above both, so it is a backstop against a card that is drawing real power and reporting no
utilisation, and it is not the thing keeping a serving card busy. It cannot be lowered to catch the
case that motivated lowering it either: a resident card whose peer was reading it sat at 61 to 67 W
at `util0`, and a resident card at rest reads 56.9 to 59.0 W, so the two overlap and no single
threshold separates them.

`interval_s` is what those 0.08 to 0.97 s are bounded by -- one round -- and `window` is what stops a
single unlucky round from meaning anything: at `window=12` the round that slipped through at 1.09 s
could not have lowered the clock, because lowering needs twelve of them in a row and the next round
of that same burst said `busy`. The price of that many rounds is measured too: the first quiet
verdict after the traffic stopped arrived 3.3 s later at `window=3` and 12.4 s later at `window=12`,
which is watts given up, not safety.

What the shadow cannot answer, because it asks the hardware nothing, is what a mistaken switch costs a
request that is already in flight. That was measured separately, by pinning a card to the idle clock and
sending the request afterwards (below), because the transition itself cannot be requested underneath a
running one: the counter veto reads the borrowed counters immediately before it asks the hardware, and a
request in flight moves them by more than a million events a sample, so the veto refuses and the request
is served at full speed. Which is worth stating as a result and not a dead end -- the stage where a
request is being served by a card whose framebuffer was stopped underneath it is a stage this governor
cannot be put into through the normal path, and the two ways to reach it deliberately (`wake_events` at
its ceiling, or pinning the clock) both showed the request coming back fine.

What a wrong answer would cost is now measured rather than argued, and it is not what this file said
earlier. On a resident card the counters are not silent about *requests*: raised from below the floor
they reported `active 0x3 events 249983`, `473659` and `2785189` within one sample of a request being
sent, and the module had the card back at the busy clock 26 ms later. What they are silent about is the
keeper's own polling, which is the thing that turns a resident card into a wobbling one (below). So the
cost of a mistaken switch is not one `interval_s` of servitude at the idle clock; it is one poll, and
the 26 ms measured here is the whole exposure on the path that has the counters.

That has to be read beside the older observation two sections up, which is that the same pair of counters
went silent for 8.4 s and 19.5 s on a card drawing 178 W at full utilisation. Both are true, and they are
the two halves of why the counters are a veto and not a vote. They notice the *onset* of work
dependably -- every request tried at a lowered card moved them by hundreds of thousands of events in the
first sample. They cannot be trusted to report its *continuation*, because a sample can stall for seconds
while the card is saturated. A card wrongly put to sleep between two requests is therefore raised within
one poll of the next one arriving, which is the 26 ms; a card wrongly thought idle while it is in the
middle of sustained work is caught instead by the keeper, which saw 90 to 96 % utilisation on every one of
the serving rounds measured above. Neither reading covers the other, and neither one alone is the
safety net.

The framebuffer is not what is at risk either, and an earlier paragraph of this file said it was. The
stop is asserted and released inside one transition -- `hbm_transition()` calls `fb_start()` on the way
out whatever happened in between -- so a card sitting at the idle clock has its framebuffer request path
running normally, and `fb-stopped` is a flag set for microseconds in the middle of the switch. That is
what a released stop looks like, which is what every one of the thousand-odd transitions recorded in this
file did; the case where the release is not acknowledged is the `halted` card, and that is a different
failure, in [How it fails](#how-it-fails). A card wrongly called idle, when the hardware co-operates, is
a card with slow memory and not a card with no memory. What slow memory costs was then measured on
purpose, on the same prompt at temperature 0, five requests each way:

| `0000:05:00.0` serving 1500 tokens | at 1728 MHz | pinned at 540 MHz |
| --- | --- | --- |
| median time, 5 requests | 3.199 s | **6.238 s, 1.95 times** |
| individual times | 3.059, 3.095, 3.199, 3.457, 3.721 s | 5.135, 5.344, 6.238, 6.388, 7.127 s |
| results | 5/5 HTTP 200 | 5/5 HTTP 200, `errors 0`, no halted card |
| board power while serving | the peer card, same work, 158.20 W | 110.91 W |
| uncorrected ECC, before and after | 0, 0 | 0, 0 |

So the worst case a mistaken idle decision can produce -- the card down, nothing waking it, the whole
request served from the idle clock -- is a request that takes about twice as long and comes back fine,
with no ECC movement and no complaint from the engine. Byte-comparing the answers against a baseline was
tried first and abandoned as a corruption check: two identical requests at temperature 0 did not produce
identical text, so that test would have failed everything and passed nothing. Uncorrectable ECC, pending
row remap and the engine's own log are the checks that mean something, and they are the ones above.

### The resident card that stays down

With that priced, the policy was armed on the live box -- `clients_veto` to `n`, one threshold file
edit and a keeper restart -- and watched for 40 s at a time with the container up, loaded, and asking
for nothing:

| `wake_events` | on the idle clock, of 40 s | card 05 while idle | card 06 while idle | a request's time | raised by the module in |
| --- | --- | --- | --- | --- | --- |
| shipped `0`, veto on | 0 s | 51.2 W | 49.5 W | 3.26, 4.23 s | n/a, never lowered |
| `512` | 25 s (62 %) | 38.5 W | 37.9 W | 3.56 to 3.99 s | 26 ms |
| `2048` | 35 s (88 %) | 37.6 W | 37.0 W | 2.98 to 3.63 s | 26, 27 ms |
| `8192` | **39 s (98 %)** | **37.3 W** | **35.9 W** | 3.36 to 4.29 s | 26 to 49 ms |

Fifteen to fourteen watts a card taken off a card somebody holds, at 98 % dwell, with the requests that
follow inside their normal spread and `errors 0` throughout. The three request latencies at each floor
are the same request as the baselines above, so the "price of being wrong" column is the price of being
wrong *and* being caught by the counters, which is what actually happens.

Why the floor has to sit at a few thousand and not at the few dozen the noise measurement suggested: the
noise is the keeper. The card was being pulled back up every second on `active 0x2 events 517` to
`events 520`, which is the resident process's housekeeping -- and the reason that number is forty times
the 7 to 54 events a sample measured earlier is that the earlier measurement was taken with the keeper
stopped. Three NVML calls a round is three RPCs into GSP-RM, and the daemon that decides whether the
card is idle is itself the busiest thing on a card that is doing nothing. That is also the mechanism
behind the stalled tensor-parallel worker above, seen from the other side: the rate at which the keeper
asks is not free, and on a resident card it is what moves the counters the governor watches.

The floor being wrong in that direction is visible rather than silent, but only after a second look at
the guard that was supposed to make it visible. The guard asked whether a stay was shorter than the
quiet period that earned it, and at `wake_events=512` the card stayed down about 1.2 s against a 1000 ms
quiet period, so it said nothing at all -- the card line prints `flaps N` only when N is non-zero -- for a
card that changed state 33 times in 45 s and spent 38 % of its life at the busy clock. The answer to a
card like that is the floor and not a pause: a pause makes it spend longer at the busy clock, which is
the thing being complained about. So the guard's second question is asked about a different shape -- a
card up more than it is down, which the comparison against `idle_ms` cannot see whenever `idle_ms` is
short. A stay is therefore measured against the interval that preceded it as well, and a wake whose
sample was at least eight times the floor is exempt from that second question, because a card that served
a request and came back down ten seconds later is the feature working and the only thing a pause could
delay is its next watt. At `wake_events=0`, the shipped rule every watt in this file was measured under,
there is no floor to compare against and the duty-cycle half stays off.

Neither half has been seen to fire on the configuration the feature ships in, and the absence of
`flaps` from the card line is what that looks like -- a card down 64 % of the time is not the shape the
second question was written for, and the floor is the answer to it. So the guard was made to fire, on
purpose, on the installed module, by reproducing the shape that produced the original 660 switches in
twenty minutes: the floor set to nothing, with the armed keeper's own housekeeping ending every stay as
soon as it began.

```
HBM idle -> busy at 1728 MHz after 0 ms quiet, counters active 0x2 events 22, switch took 1610 us
0000:05:00.0 ... switches 48 declines 21 errors 0 flaps 5, lease 358 ms quiet resident1,util0,power56W,
               idle transition waiting out the pause after a stay too short to count as idle
```

`flaps 5` on one card and `flaps 4` on the other, the wakes all at 0 ms quiet and 22 to 33 events --
nothing a card with a resident process does is 22 events, which is the whole reason the floor exists --
and the pause doing the only thing a pause can usefully do: 8 switches in the first twenty seconds of
the window and 2 in the last twenty. A guard that only counted would have left those two numbers equal.
The card line then says why the card is not being asked, which is the difference between a feature that
has given up on a card and a card that is not idle. When the floor was restored through the keeper, the
count went away with it and both cards were back on the idle clock within two seconds.

The same run at the shipped floor is the other half of the comparison: 44 seconds of 45 on the idle
clock, 2 state changes, a mean of 41.85 W and 41.52 W while down -- taken a minute after the engine
restarted, and so a few watts above the 37.55 W and 35.79 W the same cards measured an hour later.

The ceiling on the floor is worth a line of its own, because it is the difference between a loose policy
and no policy. `wake_events` accepts writes up to `1<<20` and refuses above it; on the installed module
`echo 2000000 > /sys/module/cmp_hbm_pm/parameters/wake_events` was refused and the file read `0`
afterwards. The same number is printed in the status header, so the rule a card is being judged against
can be read without rerunning anything, and a floor high enough to be above everything is not a loose
threshold but a veto switched off -- which is the one thing in this file that has no measurement next to
it, and so is not offered as a setting.

It also means the floor is not a knob to wave at. At `512` the keeper's own polling was under it by
minutes and the card spent 38 % of its life climbing back to the busy clock; at `8192` the smallest real
workload seen here was 30 times above it. The guard in the governor counts a stay that saved nothing --
a stay shorter than the quiet period that earned it, or, when the wake was the keeper rather than work,
shorter than the interval the card spent up -- and prints `flaps N`, so a card that is not idle reads as
that rather than as a broken governor.

#### The two ends of the floor, measured

The floor is bounded by measurements and not by taste, and the lower bound is the interesting one
because it is the one that would be wrong quietly: a floor above what a small request produces is a
floor that lets a health check be served at the idle clock forever. The card was lowered by the policy
with the floor dropped to 1024 -- low enough that nothing could hide under it -- and each request size
was sent three times against a card that was down when it went out. The number is the event count the
module named in the `idle -> busy` line that the request itself caused:

| `max_tokens` | events in the sample that woke the card | the request's time |
| --- | --- | --- |
| 1 (`"hi"`) | 110963, 1889203, 6537794 | 0.035, 0.043, 0.090 s |
| 8 (`"Say OK."`, the health-check shape) | 81981, 7253313, 7580103 | 0.060, 0.118, 0.125 s |
| 64 | 360998, 1898460, 4057617 | 0.219, 0.239, 0.350 s |
| 1500 | 1472252, 3835010, 3945024 | 4.023, 4.045, 4.052 s |

The smallest movement a request has ever made on this hardware is **81981 events**, from an eight-token
completion -- exactly the request a load balancer sends every few seconds, which is the one that would
have been invisible. The largest movement the keeper has ever made by asking whether the card was idle
is **545 events**. The armed floor, 8192, sits at a factor of fifteen above the second and a factor of
ten below the first, and the governor refuses a write at or above `1<<20` so that nobody can park the
floor in the range where a health check would disappear into it.

Every one of those twelve requests came back HTTP 200, and the 1500-token ones came back in 4.02 to
4.05 s against 3.26 to 4.23 s up-clock, because the module had the card back at its busy clock within
one 20 ms sample of the first memory access.

#### The shipped configuration, on the box

Both halves then went in through the installer's own path -- the constants file the installer copies,
the keeper it installs, the unit it starts -- with nothing hand-written and no `force` lease. The
keeper's first act was to write the floor it needs:

```
hbm-lease: floors from /usr/local/share/cmpunlocker/constants.yaml: util>0% or power>70000mW
           (clients reported, not a veto) means busy, 3 rounds 1.0s apart
hbm-lease: cross-card rule a busy neighbour holds this card busy, counter floor in force 8192
```

Over 62 s with the container loaded and asking for nothing, both cards on their own:

| 62 seconds, resident engine at rest | `0000:05:00.0` | `0000:06:00.0` |
| --- | --- | --- |
| on the idle clock | 52 s (84 %) | 52 s (84 %) |
| mean over the whole window | 38.48 W | 37.34 W |
| mean while on the idle clock | 37.55 W | 35.79 W |
| mean while at the busy clock | 54.14 W | 56.69 W |
| state changes | 6 down-and-ups in 62 s, both cards | |

About 15 W a card off the mean, and the mean is the number that matters: it includes the samples where
the card was climbing back up. Six down-and-ups in 62 s is the floor doing what a floor does rather
than a bug in it: four of those wakes were the keeper's own polling, which ticks about 520 events on
that counter a round and does not stop while the card is down, and the baseline is not advanced below
the floor, so the accumulation that ends a stay is `floor / rate` = 8192 / 520 = 15.8 s -- against the
observed 62 / 4 = 15.5 s. Two of the six were requests, at `events 972344` and `events 1471561`.
Raising the floor would stretch that interval and buy the last few watts of dwell; the number shipped
is the one that keeps the widest gap beneath the quietest real workload, which is the direction that
could cost something other than watts.

Then, on the same configuration and without touching anything: three 1500-token requests at 3.33, 3.37
and 3.73 s (against 4.30 s on the same policy with the cards held up), three health-check-sized
requests at 0.053, 0.094 and 0.096 s, all HTTP 200, both cards back on the idle clock five seconds
after the last of them and holding it for the following thirteen seconds. `errors 0` on both cards,
uncorrected ECC `0, 0` before and after, no halted card, no XID, nothing in the engine's log that was
not an INFO line.

That is the argument the relaxed rule rests on, and it is why `clients_veto` is now `n`. It is not a
setting an operator should be able to get half of, so it is not one they can: the keeper sets the counter
floor at the same moment it relaxes the client list, checks the governor took it, and puts the strict
rule back if it did not (`ensure_wake_floor`, and the test that runs it). Three things travel with the
relaxation and the constants file is checked for all three -- the floor, `peer_veto`, and a utilisation
floor of zero. The first is what stops the wobble. The second is what covers the one measured case of a
held card being harmed (0 % utilisation at 61 to 67 W while its neighbour streamed through its memory)
that no reading of the harmed card can tell from a card at rest: a card waits for its busy neighbour.
The third is because utilisation is the only signal that ever worked here, and 1 % refused a card twelve
times in one run.

What remains genuinely unproven about it, and what the shipped configuration therefore assumes: every
number above is one container, one serving engine, one request shape, in 40-second windows. A box whose
cards serve unrelated work loses the idle card's watts to the peer veto. A workload whose entire
footprint is smaller than 81981 events in a sample would be invisible to the counter veto and would be
left at the idle clock until utilisation or the power floor noticed it, which is the 1.95-times case
rather than a failure, and no such workload has been measured on this hardware -- the smallest thing
tried against it was an eight-token completion, and it moved 81981.

### What a switch costs a client

Three requests of one size with the card already busy, and three more after it had been left alone
long enough to drop its memory clock: 0.272 to 0.292 s warm against 0.283 to 0.316 s cold, on
requests of about a quarter of a second. The 1.6 to 2.1 ms the register sequence takes is inside
that spread.

## How it fails

The failure modes all point the same way: the card keeps its busy clock, and in one case it stops
being usable until somebody intervenes.

- **A refusal is the common answer, not an anomaly.** Every reason in the table above means the
  card stays at the clock it booted with; `declines` grows and nothing else happens. A given
  refusal is printed once and then at most every 30 s while it stays the same refusal, and a
  change of reason prints immediately, so a card that is being used does not fill the journal and
  a card that has started failing for a new reason is not hidden by the one it is failing for.
- **A refused or absent lease is terminal for the idle decision.** The governor re-asks every poll
  while a card sits at the idle clock, so a keeper that dies wakes the card instead of leaving it
  down. Counters can wake it too. They did not once while a tensor-parallel engine was serving,
  across gaps of 8.4 s and 19.5 s at 178 W, and they did twice, at 9640 and 170862 events a sample,
  while that engine was writing weights into the framebuffer and NVML still reported no client on
  either card. That asymmetry is the argument for a veto rather than a vote: the counters answer at
  the moments where answering wrong would cost the most, and nowhere else.
- **A failed transition backs off.** Waits double up to 5 s and reset as soon as the card is seen
  working; the handshake registers are printed with the reason. A card is never polled by a timer
  that is retrying something it cannot do.
- **A card that will not stay down stops being asked.** Being woken by the counters is the correct
  answer; being woken before the card has stayed down as long as the quiet period that put it there
  means the card was never idle, and asking it again a second later buys the register sequence and
  no watts. Measured: 660 idle/busy pairs in twenty minutes on a card whose resident framework ticks
  a borrowed counter about once a second. The next attempt is now deferred by `idle_ms` doubled for
  each consecutive short stay, capped at 30 s, and the count prints on the card line as
  `flaps N`. Nothing about a real request is delayed by this: the counters still veto the moment
  they cross `wake_events`, and the pause stands only between two attempts to *lower* the clock.
- **A framebuffer that cannot be resumed halts the card.** The first failed release latches the
  card off: one `KERN_EMERG` line with the two handshake registers, no further MMIO to it, and the
  poll stops rescheduling. Retrying the release every `poll_ms` instead is what took a card from
  first failure to unrecoverable in five minutes and produced 24814 error counts on the way; the
  state is reported as `halted` on the card line, and it needs a GPU reset or a module reload, not
  patience.
- **The interface never hangs a reader.** `status` and `perf_table` print `status unavailable` /
  `perf table unavailable` rather than blocking, and the lease writer takes `-EBUSY` and retries.
  An unreadable status line means the worker list is held, which is a thing to know rather than a
  thing to wait for.
- **Readers may hold the card's lock, so they are bounded too.** `perf_table` reads a megabyte of
  PROM in a worker with `usleep_range` between chunks and takes about 0.7 s, during which `status`
  may answer `status unavailable`; `switches`, `declines` and `errors` do not move across it.

Nothing in the module reaches RM: it writes registers and reads registers. On a driver built
without `CMPEnableHbmPm=1` it reads one masked word, prints `refresh access closed` once per card,
prints 0 for both frequencies, and does not touch anything; the card is left on the clock a machine
without this feature is on anyway.

## What it does not do

It does not touch the engine clock, the voltage, or the perf table.

### Why this does not also add a P8

`gpu-unlock 3.0.0-beta` ([Zagitta/gpu-unlock](https://github.com/Zagitta/gpu-unlock/tree/3.0.0-beta))
also adds an idle performance level to the VBIOS' perf table: `gpu_unlock_vbios_p8_overrides()`
copies the P0 row, calls the copy P8, and lowers the floor of five domains — GPC, XBAR, SYS, NVD
and HOST, indexes 0, 1, 3, 5 and 9 — to 135 MHz. The two halves are easy to confuse from a
distance, and this section exists to stop that: what follows is the answer this bench produced,
with the measurement that produced each part of it, because "we did not try it" and "we tried it
and the card says it cannot work" are different answers and only one of them closes a line of
work.

The card reports `P0` whether it is serving, idle, or pinned to the idle clock, so nothing
coming out of `nvidia-smi` can say whether a lower level exists on this board at all. The table
can, and it is read from the card rather than inferred: see
[What the card's performance table says](#what-the-cards-performance-table-says). Three things
came back, and each one on its own is enough to stop.

**One, this image is in the one shape upstream's generator declines.** It writes its row into the
first slot of the table and nowhere else — `empty != offset + 10` returns `-EOPNOTSUPP`, on the
ground that "RM's idle limit uses insertion level zero, so do not put P8 after an existing state
or reorder the factory entries". This card's P0 *is* the first row and its three free rows follow
it, so the insertion point does not exist. The decoder prints that verdict as the `-95` line
above, and it is worth saying what it did *not* refuse over: every other precondition upstream
checks is satisfied here — format `0x60`, header 10, five domains, stride 6, rows of 65, exactly
one P0, and P0's first five bytes are `0f 08 03 00 00`, one of the two byte patterns upstream
accepts. So the refusal is positional, not a layout this decoder misread, and the card's table is
not secretly in some unsupported form: it is in the supported form with the free rows in the wrong
order for an edit that only writes row zero.

**Two, a P8 that could be inserted would not move the memory clock, which is the only clock this
feature is about.** Upstream copies the P0 row and lowers those five domains, and its own comment
says the rest stays: "Voltage policy and the HBM tuple stay intact; the original P0 row is never
edited". The HBM tuple on this card is domain 2 of the P0 row, at a floor of 1728 MHz, which is
the number RM offers as the card's only memory clock and the number the governor reads off the PLL
— so the table, the register and NVML agree, and a P8 built by copying this row keeps the memory
where it is and asks five *core and fabric* domains to come down instead. One of those five is settled on this board: GPC's floor is already
210, and the lowest of the hundred graphics points NVML lists for this card is 210, so a floor of
135 has no selectable clock beneath it — which is what `nvidia-smi -lgc 135,135` demonstrated by
accepting the request, holding the graphics clock at 210, and moving the meter by nothing worth
the name (31.4 W against the 31.1 to 31.8 W it idles at). Nor can the row itself carry a voltage:
all 65 bytes of it are accounted for by the level id, the four policy bytes and ten six-byte
domain tuples, and upstream's phrase for what copying the row copies is "the tested P0 power/link
policies" — a policy, not a number of volts. Upstream's own map of the same data image says where
the rest of it lives: it reads the perf table at pointer 0 and goes elsewhere for everything else,
to 8 for the clock table, 12 for the clock programs and 0x2c for a power-policy table of its own
(format `0x30`, TGP in milliwatts). What a copied row does not reach, the mechanism does not reach.

**Three, what is left of the idea cannot be delivered on the driver this repository supports.**
The override is published to RM through `NVreg_RegistryBinaryFilePerDevice`, which the 610 branch
does not have; it appears in the open kernel driver at 615.78.08. So the price is changing the
driver version every installation is pinned to, *and* writing a row into a table RM owns, in order
to find out whether RM would ever select it.

**What none of this measured** is the one lever still theoretically live: whether XBAR (615), SYS
(630), NVD (585) and HOST (555) would come down to 135 and take watts with them, if a row existed
that RM would select. That is untested, and nothing on this bench can test it without hand-forging
a row into a firmware-owned table, which is the point where a driver patch becomes a reflashing
risk on a card whose recovery procedure is in this repository because it can go wrong. Against
that unknown stand 13.7 W on an empty card, measured, and already collected by this module. Anyone
who does forge a row has the decode above as a starting point and
`/sys/kernel/cmp_hbm_pm/perf_table` as the way to check whether it landed where they meant and
carries the floors they wrote.

Nothing here changes what the card reports about its memory, and nothing here calls into RM.

## What has been checked

The register sequence is the one `gpu-unlock 3.0.0-beta`
([Zagitta/gpu-unlock](https://github.com/Zagitta/gpu-unlock/tree/3.0.0-beta)) runs; the
counter-borrowing, the fence and the bypass are all there because that implementation found the
other versions of those failed, and the ROM walk the perf-table file uses is copied rather than
reconstructed. The rollback policy that kept traffic stopped when the way back failed is the one
part deliberately not upstream's, because it is the code that held a card's framebuffer out of
service for five minutes; it is replaced by the halt described above. Everything else about the
register sequence is byte for byte what upstream does. What this repository has checked without a
card is the shape of the safety: that no path reaches the stop request without a lease, that every
way the lease can fail lands on `busy`, that the veto is absolute and has no subset to configure,
that a halted card is never read again and its poll stops rescheduling, that failures back off,
that the keeper's own unknowns are refusals, that the module builds against the kernels CI has
headers for and the kernel the cards run, that every symbol it asks for exists there, that the two
masks it writes through are the two the patch opens, and that the installer and uninstaller agree
about its name.

On a bench with two CMP 170HX in it, against hardware:

- The booter opens both masks on both cards (`SEC2_DEBUG: PLM[12] FBPA_CFG_PLM … status=0xffff`
  and `PLM[13] HBMPLL_PLM … status=0xffff`), and the governor afterwards reads the refresh mask of
  every FBPA the card reports — twenty of them, `fbpas=0x3fcfff` — with none saying closed. Loaded
  against a driver built *without* the key it stops where it should:
  `refresh access closed (-11) reg=0x900168 value=0xffffffcf`, one line per card, no clock touched.
- Both directions of the switch run, each checked against the meter as it was produced: the busy
  line with the card drawing 46.9 W, the idle line at 32.8 W, same card, same boot, 1.6 to 2.1 ms
  for the register sequence. The idle clock read back off the PLL is 540 MHz.
- Durability of that sequence, recorded from an accident rather than designed: one card was driven
  through 660 idle/busy pairs in twenty minutes, about one a second, with a serving framework
  resident on it. Every transition logged its own register-sequence time (1.57 to 1.72 ms),
  `errors` stayed 0 on both cards, no card halted, no Xid and no NVRM timeout appeared in the
  journal for the window, and ECC volatile and aggregate uncorrectable counts were 0 afterwards with
  no pending row remap on either card.
- The empty-card columns above, at the shipped defaults, with the wake path checked by killing the
  keeper and by letting a `force` lease age out.
- Under load, with two tensor-parallel workers of a serving framework resident: three concurrent
  clients sent thirty-six completions and every one returned HTTP 200 while the cards drew 150 to
  195 W at up to 97 % utilisation. Across that window the borrowed counters came alive
  (`active 0x3`, deltas of fifteen million and five million) and every sample read
  `switches 0 errors 0`; a later 60 s observation of the same service at rest between requests read
  `switches` unmoved and `errors` 0 throughout. Between requests the counters do go silent on a
  card somebody owns — `active 0x0` with `quiet` at 15856 ms, 0 % utilisation and 51.8 W, below the
  keeper's own 70 W floor — and that card held the busy clock on `lease … busy clients1` alone. The
  keeper's client counts were cross-checked against `nvidia-smi`'s own process list: the card
  holding the worker said `clients1`, the card holding the peer mapping said `clients2`.
- Attaching a compute context to one card of a pair that was already idle wakes that card within
  about a second, on the keeper's `busy clients1`, while its neighbour stays at the idle clock;
  detaching lets it go back down. Both were observed with `errors 0`.
- The same service restarting while the governor was loaded, observed read-only: while it was
  writing weights into the framebuffer, before any compute context had registered, the borrowed
  counters advanced by 9640 and 170862 events in a single 20 ms sample and woke both cards, at a
  moment when NVML reported `clients0` on both. Fourteen transitions in eighty seconds, `errors 0`,
  no halt, and the handshake registers read `0x3ffffc00/0x501408` on every one of them but one,
  where the ACK word read `0x101408` a single time with a counter active.
- The boot path: with no lease file the keeper logs that the governor has registered nothing,
  exits `EX_TEMPFAIL`, systemd retries it, and the lease registers itself once the module is loaded
  without the unit being restarted. `Restart=always` was checked by `SIGKILL`.
- The installer, the unit and `verify.sh` were run against the failure they exist for: a keeper
  that cannot read its thresholds, and a keeper that is installed but not answering, are both
  reported as errors rather than as a machine with nothing idle on it.

The counters are in this module as a veto and not as a vote, which is the only claim about them the
recordings support. They see a saturated request stream; they have been recorded reporting nothing
for 8.4 s on a card `nvidia-smi` said was at 100 % and 178.2 W. A load test that samples once at the
peak says nothing about the stretches a serving loop contains, and those stretches are what the
lease is for.

## What has not been checked yet

Four items are open. None of them is a reason to leave a card alone today; each is a reason not to say
more about a card than today's evidence supports. One that used to be on this list has since been
measured out of it.

**Whether a transition started underneath in-flight traffic is survivable** was the open question, and
it was framed wrongly here as "is the framebuffer handshake itself the idle test". The stop is asserted
and released inside one transition, so there is no state of a card being served while its framebuffer is
held out of service: the window being asked about is the transition itself, about 1.7 ms of register
sequence, bounded at roughly 201 ms when the release is not acknowledged -- after which the module
releases the stop and the card serves at whatever clock it has. The two things that could be reached by
hand have been: serving a whole request from the idle clock costs 1.95 times the time and returns HTTP
200 with `errors 0` and ECC unmoved (above), and a card whose counters are held up by `force` while a
request is in flight completes the request in 3.62 s and never leaves the journal a line. The stage that
has *not* been reached is a real transition issued while a request is in flight, and it cannot be reached
by sending an ordinary request: the counter veto reads the borrowed counters immediately before it asks
the hardware, and those same requests produce samples of 250 000 to 2 800 000 events, so the veto refuses
first and the hardware is never asked. Reaching it means making the counters blind deliberately, at the
ceiling the module allows, on a container that can be restarted and with a request somebody is willing to
lose. That is the honest state of it, and the 1.95-times figure above is what bounds the cost if the
answer turns out to be that the card can be lowered while busy.

The second is the halt latch, by holding a stop that cannot be released on purpose on a spare card
and checking that exactly one `KERN_EMERG` line appears and the retry counter does not run away.

Third, and specific to the guard rather than to the hardware: of the two questions the flap guard asks,
only the older one has been seen to fire. The duty-cycle half -- a stay shorter than the interval the
card spent up -- has been read on no machine, nor has its exemption for a wake that was plainly work,
because the wobbling configurations measured here were all cards that spent more of their time down
than up. It is the half that would decide whether a short `idle_ms` is a setting or a trap, and it is
the kind of arithmetic a text match in a test file passes while the comparison is the wrong way round.
What would exercise it is a card asked to come down again about as often as it manages, which is
reproducible on a bench with `idle_ms` low and the keeper's floor in the middle of its measured range.

Fourth, the armed resident policy is one container, one serving engine, one request shape, in windows of
40 to 60 seconds. It has not been run against a second engine on the box, against a training job, or
with the two cards serving unrelated work -- where `peer_veto` would hold the idle card up for the busy
one, which is the safe direction and the expensive one. Neither has anyone checked what the armed floor
does at a keeper rate other than the shipped one, and it is the keeper's own polling rate that sets the
noise the floor has to clear: the two are coupled, and the coupling is in the constant
(`resident_wake_events`) rather than derived from `interval_s`, so a machine that doubles the poll rate
without doubling the floor is a machine that will wobble again.

The rest of the safety is stated above with the measurement that produced it, and what is asserted
without one is limited to what is written in this section and in **What none of this measured**.
