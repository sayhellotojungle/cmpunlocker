// SPDX-License-Identifier: GPL-2.0-only
/*
 * cmpunlocker: keep the HBM of an unclaimed CMP 170HX out of its busy clock.
 *
 * A CMP 170HX idles near 40 W and most of that is HBM2E being refreshed at full
 * rate: the card's VBIOS carries one performance level, so nothing ever asks
 * the FBPA memory PLL to come down and GSP-RM has no low state to ask it for.
 * This module asks, for a card that nobody is using.
 *
 * The hardware sequence is the one NVIDIA's own HBM clock-switch path uses, run
 * from the host: stop the framebuffer, retune the refresh interval, retune the
 * PLL through the MC2 serial aperture with the post divider bypassed so the VCO
 * and the trained delay lines are never lost, retune the refresh the other way,
 * release the stop.  Nothing here calls into RM and nothing here claims the PMU
 * counters: they are borrowed, and read twice per sample to prove nobody else
 * moved them between the reads.
 *
 * Taking the framebuffer path away from a card that is using it is not a slow
 * failure but a total one -- GSP-RM loses its own route to memory, its RPCs time
 * out, and the card ends up needing a reset -- so the two questions this module
 * asks before doing it are answered by two different things, and neither takes
 * the other's word:
 *
 *   The borrowed activity counters hold a veto and nothing more.  Any movement
 *   they report says "not now", at any magnitude.  They are not allowed to say
 *   "idle", because on a card serving requests they were measured reporting
 *   nothing at all for seconds while it drew 178 W: a counter that cannot see a
 *   workload is not evidence of its absence.
 *
 *   The permission to go idle comes from userspace, from a keeper that asks RM
 *   the question the kernel cannot (utilisation, power, and whether anything at
 *   all holds the card) and writes the verdict to /sys/kernel/cmp_hbm_pm/lease
 *   once a second.  An absent, stale, malformed or first-time answer all mean the
 *   card is working.
 *
 * And if the framebuffer cannot be handed back, this module stops touching that
 * card entirely -- one emergency line in the log with the handshake registers in
 * it, and nothing further -- rather than retry a card that has stopped answering.
 * A failed release is not a state the driver can be argued out of, and a card
 * held out of service costs a reboot: it is the reason to be certain before
 * asking, and the reason never to be certain on the counters alone.
 *
 * Two privilege masks make this possible, and the patched driver opens them
 * during the SEC2 post-BL replay it already runs for the unlock: FBPA_CFG for
 * the refresh windows and HBMPLL for the PLL.  It opens them only when it was
 * installed with CMPEnableHbmPm=1, which is what driver/build.sh and
 * tools/hbm-pm-setup.sh agree on, so a machine that does not run this module was
 * never asked to widen those two blocks.
 *
 * Ported from gpu-unlock 3.0.0-beta (kernel/gpu_unlock/hbm.c and hbm_work.c,
 * GPL-2.0-only, https://github.com/Zagitta/gpu-unlock/tree/3.0.0-beta), with
 * the policy made tunable at runtime and the state made
 * readable: /sys/kernel/cmp_hbm_pm/status is what to look at when the wattmeter
 * disagrees with you.  The perf-table reader near the end of this file comes
 * from the same project's vbios.c, whose P8 override it decodes without
 * performing.
 */
#include <linux/delay.h>
#include <linux/errno.h>
#include <linux/err.h>
#include <linux/io.h>
#include <linux/irqflags.h>
#include <linux/jiffies.h>
#include <linux/kernel.h>
#include <linux/kobject.h>
#include <linux/kstrtox.h>
#include <linux/limits.h>
#include <linux/list.h>
#include <linux/mm.h>
#include <linux/mutex.h>
#include <linux/notifier.h>
#include <linux/pci.h>
#include <linux/pm_runtime.h>
#include <linux/preempt.h>
#include <linux/sched.h>
#include <linux/slab.h>
#include <linux/string.h>
#include <linux/suspend.h>
#include <linux/sysfs.h>
#include <linux/timekeeping.h>
#include <linux/types.h>
#include <linux/vmalloc.h>
#include <linux/workqueue.h>

/*
 * BAR0, first 10 MiB: the FBPA windows, the PMU block the counters live in, and
 * the FB stop handshake.  Names follow the pinned NVIDIA register headers; the
 * FBPA windows are at a 16 KiB stride, and the 0x9a0000 alias is the one the
 * unlock's geometry write already uses.
 */
#define CMP_FBPA_BASE		0x00900000u
#define CMP_FBPA_STRIDE		0x00004000u
#define CMP_FBPA_CFG1		0x00000204u	/* NV_PFB_FBPA_CFG1: partition geometry */
#define CMP_REFRESH_PLM		0x00000168u	/* privilege mask of the refresh block */
#define CMP_REFRESH		0x000002a0u
#define CMP_REFRESH_MASK	0x00007ff8u
#define CMP_SOFTWARE_REF	0x00000310u	/* force one refresh by software */
#define CMP_PLL_PLM		0x00003c7cu
#define CMP_PLL_CFG		0x00003c90u
#define CMP_PLL_COEFF		0x00003c98u
#define CMP_PLL_CFG_MC2		0x0098bc90u	/* serial-priv aperture for CMP_PLL_CFG */
#define CMP_PLL_COEFF_MC2	0x0098bc98u	/* serial-priv aperture for CMP_PLL_COEFF */
#define CMP_PLL_LOCK		0x00000020u
#define CMP_PLL_BYPASS		0x00000400u
#define CMP_PLL_NDIV_MASK	0x0000ff00u
#define CMP_PLL_PLDIV_MASK	0x003f0000u
#define CMP_PLL_CLOCK_MASK	(CMP_PLL_NDIV_MASK | CMP_PLL_PLDIV_MASK)
#define CMP_PLM_HOST_WRITE	0x00000010u	/* level 0 write access */
#define CMP_PRI_FENCE		0x001211fcu

/* NV_PMU framebuffer stop: the handshake firmware uses to quiesce the memory
 * controllers, shared with RM rather than ours, so it is only ever released if
 * it was ours to take. */
#define CMP_FB_STOP_OUTPUT	0x0010a7c0u
#define CMP_FB_STOP_INPUT	0x0010a7c4u
#define CMP_FB_STOP_SET		0x0010a7e0u
#define CMP_FB_STOP_CLEAR	0x0010a7e4u
#define CMP_FB_STOP		(1u << 2)

/* GSP-RM's own boot progress: reading these is how the governor knows the
 * firmware is far enough along that a host clock switch cannot race bring-up. */
#define CMP_GFW_BOOT_OFFSET	0x00118234u
#define CMP_GSP_RISCV_STATUS_OFFSET	0x00111240u

/* The counters GSP-RM keeps armed, and the count itself. */
static const u32 cmp_pmu_counter_addr[] = {
	0x10be40, 0x10be80, 0x10bec0, 0x10bfc0, 0x10be00,
};
#define CMP_PMU_COUNT		0x10bf80u
#define CMP_PMU_COUNT_MASK	0x7fffffffu

/* GR, all nine copy engines, and framebuffer requests: three banks of the
 * counter mask, and every one of them has to be watched somewhere, because the
 * silence of a counter means nothing if the engine it watches was never in the
 * mask at all.
 *
 * Every accepted counter, and not a subset of them, is then allowed to say the
 * card is busy.  That is the arrangement the hardware measured: on a card
 * serving requests, the counter watching framebuffer requests reported nothing
 * whatsoever for 8.4 s while the card drew 178 W, and the card then lost its
 * framebuffer path to this governor's own release failure.  A counter moving is
 * evidence of work; a counter being silent is not evidence of no work.  The
 * positive evidence for that has to come from somewhere else -- see the lease
 * below. */
static const u32 cmp_pmu_required[3] = { 0x00380081, 0x00700000, 0x00000007 };
#define CMP_HBM_FB_BANK		2

#define CMP_HBM_COUNTERS	16
#define CMP_HBM_FBPAS		24
/* Stay well below a 31-bit counter wrap even at the highest PMU clock. */
#define CMP_HBM_MAX_SAMPLE_NS	1000000000ull
/* Longest silence the counters are asked to vouch for.  It used to be 100 ms and
 * that is what wedged a serving card: the same window in which the counters read
 * zero twice for seconds at a time while the GPU was at 100% utilisation.  What
 * this buys against 100 ms is one second of the idle clock on a card that has
 * just gone genuinely quiet, which is nothing; what it buys is that no single
 * request gap in a serving loop is short enough to be mistaken for an idle card. */
#define CMP_HBM_DEFAULT_IDLE_MS	1000
#define CMP_HBM_DEFAULT_POLL_MS	20
/* How stale the userspace lease may be before its answer counts as the answer it
 * did not give.  The keeper writes once a second; three of those rides out a slow
 * `nvidia-smi` without ever mistaking a dead keeper for an idle card. */
#define CMP_HBM_DEFAULT_LEASE_MS	3000
/* How many consecutive reports of the same verdict a lease needs before the
 * governor acts on `quiet`.  Two, because two is the fewest that can disagree,
 * and the keeper asks once a second against an idle window measured in seconds. */
#define CMP_HBM_LEASE_MIN_REPORTS	2
/* Longest the governor waits before it will touch a transition again after one it
 * did not like.  Without this it attempted the same release of a dead card every
 * poll_ms for five minutes -- 24814 times -- while the rate limiter turned that
 * into one log line per 30 s. */
#define CMP_HBM_MAX_BACKOFF_MS	5000
/* How long a policy write waits for a card's device lock before it says it could
 * not have it: see pci_dev_lock_bounded(). */
#define CMP_HBM_RESYNC_WAIT_MS	200
/* How long fb_wait keeps asking after the first millisecond of spinning, in
 * rounds of about 5 ms.  Two hundred milliseconds is long enough to discover that
 * the hardware is not answering at all, which is the only thing worth discovering
 * here, and short enough to hold inside the device lock while saying so. */
#define CMP_FB_WAIT_SLEEP_ROUNDS	40
/* Longest the governor stands aside from a card that goes down and comes straight
 * back up.  The number is not theoretical: with a serving framework resident on it,
 * one card was put down and pulled back up again every second -- 660 idle/busy pairs
 * in twenty minutes, 1.6 ms of register work each, and not one watt saved on any of
 * them -- because a borrowed counter ticks 7 to 54 events about once a second on a
 * card that is holding memory and doing nothing with it.  Doubling from `idle_ms`
 * with each short stay is what turns that loop into a card that is left alone. */
#define CMP_HBM_MAX_FLAP_PAUSE_MS	30000
#define CMP_HBM_FLAP_SHIFT		5	/* caps the doubling at 32 * idle_ms */
/* How far over `wake_events` a sample has to be before the wake it caused counts as
 * work rather than as the keeper's own polling, and so exempts a short stay from the
 * duty-cycle half of the flap guard.  The two ends are measured, not assumed: a
 * resident card at rest came back on `active 0x2 events 517` to `events 520` with the
 * floor at 512, and on `events 249983`, `473659` and `2785189` when something was
 * actually asked of it.  Eight times the floor sits between those, and the floor is
 * the only reason either number means anything. */
#define CMP_HBM_FLAP_WORK_FACTOR	8
/* Highest `wake_events` will accept.  The floor is a judgement about magnitude, and a
 * floor no real sample can reach is the counters' veto switched off by a typo -- so it
 * gets an upper bound rather than a warning.  The largest single 20 ms sample measured
 * here is 170862 events, on a framework loading weights, which leaves about six times
 * of headroom for a workload nobody has run here yet. */
#define CMP_HBM_MAX_WAKE_EVENTS		(1u << 20)
#define CMP_HBM_BAR_SIZE	0x00a00000u

enum cmp_hbm_mode {
	CMP_HBM_AUTO,
	CMP_HBM_PIN_BUSY,
	CMP_HBM_PIN_IDLE,
};

struct cmp_hbm_counter {
	u32 mask[3], control, threshold, count;
};

struct cmp_hbm {
	u32 busy_coeff, idle_coeff;
	u32 fbpas, first_fbpa;
	u32 busy_refresh[CMP_HBM_FBPAS];
	struct cmp_hbm_counter counter[CMP_HBM_COUNTERS];
	u16 counters, active_counters;
	/* Largest single-sample movement of each accepted counter since the last
	 * sample, kept for the status file: which counter moved, and by how much,
	 * is the difference between a card with traffic on it and a card whose
	 * firmware ticks a counter of its own.  Without these numbers on the line,
	 * finding out that only one of two counters can see a workload took a
	 * second boot and a dead card. */
	u32 delta[CMP_HBM_COUNTERS];
	u32 coverage[3];
	/* The largest single-sample movement of any borrowed counter: the headline
	 * figure for a transition log line, where naming every counter is too wide. */
	u32 events;
	u64 sampled, quiet_since;
	/* When this card last went down, when it last came back up, and when it may next
	 * be asked: the three are the flap guard.  `flaps` counts a stay that saved
	 * nothing, which the guard in sample_state() explains, and it is printed on the
	 * card line when it is non-zero, because an operator otherwise reads that card as
	 * a feature that does not work rather than as a card that is not as quiet as the
	 * keeper thinks. */
	u64 idle_since, calm_at, busy_since;
	unsigned int flaps;
	bool profile, baseline, idle, fb_stopped;
	/* The slots discover_counters() settled on, kept for the line govern()
	 * prints about them: which engine each one watches is the difference
	 * between a card being woken by traffic and being woken by firmware. */
	bool counters_live;
	/* This card is off limits until the module is reloaded: see cmp_hbm_halt(). */
	bool halted;
	const char *halt_reason;
	int halt_error;
	/* The framebuffer handshake as it looked the last time this module had it
	 * in its hands.  Kept because the first time this went wrong, the one
	 * number that explained it -- the request register still showing the stop
	 * this module had asked to clear -- was not printed anywhere. */
	u32 fb_req, fb_ack;
	/* Do not attempt another idle transition before this time: set after a
	 * transition this module did not like, and the reason it is not a loop. */
	u64 retry_at;
	unsigned int backoff_ms;
	/* Last failed hardware check, for the worker's rate-limited log. */
	const char *reason;
	u32 reg, value;
};

/* Written by the poll work, read by the status file without a lock: a torn
 * reading of one field of a diagnostic file is worth less than a lock taken
 * inside the transition path. */
struct cmp_hbm_stats {
	u32 busy_mhz, idle_mhz, active_counters;
	/* Consecutive stays on the idle clock shorter than the quiet period that
	 * earned them: see the flap guard in sample_state(), and the card line that
	 * prints it. */
	unsigned int flaps;
	u64 quiet_ms, transitions, errors, declines;
	int last_error;
	const char *reason;
	bool pinned, idle, profile, baseline, fb_stopped, halted;
	const char *halt_reason;
	u32 delta[CMP_HBM_COUNTERS];
	u32 fb_req, fb_ack;
};

struct cmp_hbm_worker {
	struct pci_dev *pdev;
	void __iomem *bar;
	struct cmp_hbm hbm;
	struct cmp_hbm_stats stats;
	struct delayed_work work;
	struct notifier_block pci_nb, pm_nb;
	struct list_head node;
	bool bound, paused, stopping, pci_registered, pm_registered, boot_seen;
	bool counters_printed;
	/* The halt line is printed once, by the work that noticed it, and never
	 * rate limited: see cmp_hbm_halt(). */
	bool halt_logged;
	/* The keeper's answer for this card, last time it gave one.  Nothing here
	 * is trusted as permission to be busy: the absence of an answer, a stale
	 * answer, and a malformed answer all mean the card is working. */
	u64 lease_written;
	unsigned int lease_writes, lease_bad;
	/* Consecutive reports of the same verdict, and when the streak of `quiet`
	 * began.  A single fresh answer is not evidence: the keeper asks RM once a
	 * second, so a request that begins just after one sample is invisible until
	 * the next one.  Requiring the verdict to have held across more than one
	 * sample, and across the whole idle window, is what turns a sampler into
	 * evidence -- and it is the only defence against the interval between two
	 * samples that a 1 Hz question cannot remove. */
	u32 lease_streak;
	u64 lease_quiet_since;
	bool lease_quiet, lease_forced;
	char lease_note[48];
	const char *last_reason;
	int last_error, last_access;
	/* The same two things, but as they were when a line was last *printed*, which
	 * is not the same question: see report_result(). */
	const char *logged_reason;
	int logged_error;
	u64 last_warn;
};

/* The list only changes at init and exit, which is what lets the status file
 * and a policy change walk it without holding the lock across hardware work. */
static LIST_HEAD(workers);
static DEFINE_MUTEX(workers_lock);

/* How long a reader of the status files waits for the list before saying it could not
 * have it, in milliseconds, instead of waiting forever.  These files are read by people,
 * by verify.sh, and by whatever is watching the machine, and a reader that cannot be
 * killed and cannot finish turns one bad moment inside the governor into a box that needs
 * a reboot -- which is not a hypothesis: the first read of the expanded status file on
 * hardware walked off through a pointer no line of code had set, while holding this lock,
 * and every reader after it is still in D.  The paths that change policy keep using plain
 * mutex_lock(), because there the operator asked for work to be done and waiting for it
 * is the right answer.
 *
 * The bound counts sleeps rather than reading a clock, so it is a floor on the wait and
 * not a promise, and it stays well under the 120 s the kernel takes to start complaining
 * about a blocked task by itself. */
#define CMP_HBM_LOCK_WAIT_MS	2000

static bool workers_lock_get(const char *who)
{
	unsigned int waited;

	for (waited = 0;; waited += 2) {
		if (mutex_trylock(&workers_lock))
			return true;
		if (waited >= CMP_HBM_LOCK_WAIT_MS)
			break;
		usleep_range(1000, 2000);
	}
	pr_warn_ratelimited("cmp_hbm_pm: %s waited %u ms for the worker list and gave up: something else is holding it, most likely a perf table read or a policy change\n",
			    who, waited);
	return false;
}

static struct kobject *status_dir;
static bool status_file;
static bool perf_table_file;
static bool lease_file;

/* Every one of these is live: writing a policy parameter resyncs the cards, so
 * a comparison of two idle clocks is an echo, not a reboot. */
static unsigned int poll_ms = CMP_HBM_DEFAULT_POLL_MS;
static unsigned int idle_ms = CMP_HBM_DEFAULT_IDLE_MS;
static unsigned int lease_ms = CMP_HBM_DEFAULT_LEASE_MS;
static unsigned int idle_ndiv = 20;
static unsigned int idle_pldiv = 1;
static unsigned int idle_refresh = 48;
/* Events a borrowed counter has to report in one sample before the governor calls
 * that movement work.  Zero, the shipped default, is what every column in the
 * documentation was measured with: one event is enough to refuse, and on a card with
 * nobody holding it that costs nothing.  It is not the right rule for a card somebody
 * does hold, where the same counters were seen reporting 7 to 54 events about once a
 * second from a process doing nothing -- which is why a resident policy wants a floor
 * between that and the 2395 events a sample that same framework produced while it was
 * writing weights, and why the floor is a knob rather than a new default. */
static unsigned int wake_events;
static unsigned int mode;
static bool enable = true;
/* On by default, because with it off the record of the accident that cost a
 * reboot had no switches, no timings and no register values in it. */
static bool log_transitions = true;

static void cmp_hbm_requeue(unsigned long delay);
static void cmp_hbm_resync(void);
static bool read_counter(struct cmp_hbm *hbm, void __iomem *bar, unsigned int slot,
			 struct cmp_hbm_counter *c);
static int counters_moved(struct cmp_hbm *hbm, void __iomem *bar);

/* The policy fields are read together, so they change together: one setter, and
 * it takes effect on the next sample rather than at the next reboot. */
static int policy_set(const char *val, const struct kernel_param *kp)
{
	unsigned int value;
	int ret = kstrtouint(val, 0, &value);

	if (ret)
		return ret;
	if (kp->arg == (const void *)&mode) {
		if (value > CMP_HBM_PIN_IDLE)
			return -EINVAL;
	} else if (kp->arg == (const void *)&idle_ndiv) {
		if (!value || value > 0xff)
			return -EINVAL;
	} else if (kp->arg == (const void *)&idle_pldiv) {
		if (!value || value > 0x3f)
			return -EINVAL;
	} else if (kp->arg == (const void *)&idle_refresh) {
		if (!value || value > 0xff)
			return -EINVAL;
	} else if (kp->arg == (const void *)&lease_ms) {
		/* Zero is a real answer here, and only for a measurement: it says
		 * the operator has taken the judgement the keeper makes away from
		 * it.  That is the way to price the lease, and it is not a way to
		 * run a card, which is why the status file says so out loud. */
	} else if (kp->arg == (const void *)&wake_events) {
		/* Zero here is the shipped answer -- every event is work -- so it has
		 * to stay writable: a card that is put back the way it was measured
		 * is a card whose columns still mean something.  The other end of the
		 * range is bounded, because a floor above what any real sample reports
		 * is not a looser policy, it is the veto gone. */
		if (value > CMP_HBM_MAX_WAKE_EVENTS)
			return -EINVAL;
	} else if (!value) {
		return -EINVAL;
	}
	*(unsigned int *)kp->arg = value;
	cmp_hbm_resync();
	return 0;
}

static const struct kernel_param_ops policy_ops = {
	.set = policy_set,
	.get = param_get_uint,
};

#define module_policy(name, description)					\
	module_param_cb(name, &policy_ops, &name, 0644);				\
	MODULE_PARM_DESC(name, description)

module_policy(poll_ms, "Milliseconds between activity samples (default 20)");
module_policy(idle_ms, "Milliseconds without any counter moving, and a fresh lease from the keeper, before the HBM clock drops (default 1000)");
module_policy(lease_ms, "Milliseconds a userspace lease that the card is unclaimed stays valid; 0 waives the lease for a measurement and is not a way to run a card (default 3000)");
module_policy(idle_ndiv, "HBM PLL N divider while idle: the clock is 27MHz * ndiv / mdiv / pldiv (default 20)");
module_policy(idle_pldiv, "HBM PLL post divider while idle (default 1)");
module_policy(idle_refresh, "HBM refresh interval while idle, in units of 8 busy-clock periods (default 48)");
module_policy(wake_events, "Events one sample of a borrowed counter must report to count as work; 0 means every event does (default 0, the shipped rule). A card somebody holds wants a few hundred: its resident process ticks 7 to 54 events a second while doing nothing");
module_policy(mode, "0=auto (default), 1=pin the busy clock, 2=pin the idle clock; 1 and 2 stop the governor and are there to measure against");

static int enable_set(const char *val, const struct kernel_param *kp)
{
	bool value;
	int ret = kstrtobool(val, &value);

	if (ret)
		return ret;
	enable = value;
	cmp_hbm_resync();
	return 0;
}

static const struct kernel_param_ops enable_ops = {
	.set = enable_set,
	.get = param_get_bool,
};

module_param_cb(enable, &enable_ops, &enable, 0644);
MODULE_PARM_DESC(enable, "Govern the cards at all (default: on; off leaves every card at its busy clock)");

module_param(log_transitions, bool, 0644);
MODULE_PARM_DESC(log_transitions, "Log every idle/busy switch with the register values around it (default: on)");

/* The card is off limits: this module will not touch it again until it is
 * reloaded.  Called when the framebuffer has been asked to stop and this module
 * cannot prove it asked them to start again -- which is the state that cost a
 * reboot, because the retries of that release ran every poll_ms for five minutes
 * against a card that could no longer answer, and the rate limiter turned
 * 24814 attempts into one log line per 30 s.
 *
 * It only records.  The line that explains it is printed by the caller that owns
 * the struct pci_dev, and it is never rate limited. */
static void cmp_hbm_halt(struct cmp_hbm *hbm, int error, const char *what)
{
	if (hbm->halted)
		return;
	hbm->halted = true;
	hbm->halt_reason = what;
	hbm->halt_error = error;
}

/* PLL coefficient to MHz: 27 MHz reference, times N over M, over the post
 * divider.  The coefficient keeps M, which is why the idle target is a divider
 * pair rather than a frequency. */
static u32 coeff_mhz(u32 coeff)
{
	unsigned int mdiv = coeff & 0xff, ndiv = (coeff >> 8) & 0xff;
	unsigned int pldiv = (coeff >> 16) & 0x3f;

	if (!mdiv || !pldiv)
		return 0;
	return 27 * ndiv / mdiv / pldiv;
}

static bool readable(u32 value)
{
	return value != ~0u && (value & 0xfff00000u) != 0xbad00000u;
}

static int problem(struct cmp_hbm *hbm, int error, const char *reason, u32 reg, u32 value)
{
	hbm->reason = reason;
	hbm->reg = reg;
	hbm->value = value;
	return error;
}

static u32 fbpa(unsigned int index)
{
	return CMP_FBPA_BASE + index * CMP_FBPA_STRIDE;
}

static u32 idle_coeff(const struct cmp_hbm *hbm)
{
	return (hbm->busy_coeff & ~CMP_PLL_CLOCK_MASK) |
		(idle_ndiv << 8) | (idle_pldiv << 16);
}

/* The card is up and its firmware is far enough along that reading the FBPA
 * windows means something.  GSP booting is also what closes the window in which
 * a host write could race the firmware's own memory bring-up. */
static bool firmware_booted(void __iomem *bar)
{
	u32 gfw = readl(bar + CMP_GFW_BOOT_OFFSET);
	u32 gsp = readl(bar + CMP_GSP_RISCV_STATUS_OFFSET);

	return readable(gfw) && readable(gsp) && (gfw & 0xff) == 0xff &&
	       ((gsp & 7) == 3 || (gsp & 7) == 5);
}

static void snapshot_stats(struct cmp_hbm_worker *worker)
{
	struct cmp_hbm *hbm = &worker->hbm;

	WRITE_ONCE(worker->stats.idle, hbm->idle);
	WRITE_ONCE(worker->stats.profile, hbm->profile);
	WRITE_ONCE(worker->stats.baseline, hbm->baseline);
	WRITE_ONCE(worker->stats.fb_stopped, hbm->fb_stopped);
	WRITE_ONCE(worker->stats.halted, hbm->halted);
	WRITE_ONCE(worker->stats.halt_reason, hbm->halt_reason);
	WRITE_ONCE(worker->stats.fb_req, hbm->fb_req);
	WRITE_ONCE(worker->stats.fb_ack, hbm->fb_ack);
	memcpy(worker->stats.delta, hbm->delta, sizeof(worker->stats.delta));
	WRITE_ONCE(worker->stats.pinned, mode != CMP_HBM_AUTO);
	WRITE_ONCE(worker->stats.active_counters, hbm->active_counters);
	WRITE_ONCE(worker->stats.flaps, hbm->flaps);
	if (hbm->profile) {
		WRITE_ONCE(worker->stats.busy_mhz, coeff_mhz(hbm->busy_coeff));
		WRITE_ONCE(worker->stats.idle_mhz, coeff_mhz(idle_coeff(hbm)));
	}
	if (hbm->baseline)
		WRITE_ONCE(worker->stats.quiet_ms,
			   (ktime_get_ns() - hbm->quiet_since) / 1000000);
}

/* What the card will do, and what it may do: the busy coefficient is whatever
 * the VBIOS left, so the idle target is checked against that rather than
 * against a table, and a card whose FBPA windows are not open says so here
 * instead of writing to registers nobody can see. */
static int capture_profile(struct cmp_hbm *hbm, void __iomem *bar)
{
	u32 coeff, cfg, plm, mask = 0, first = 0;
	unsigned int i;

	for (i = 0; i < CMP_HBM_FBPAS; i++) {
		u32 base = fbpa(i), geometry = readl(bar + base + CMP_FBPA_CFG1);

		if (!readable(geometry) || !geometry)
			continue;
		plm = readl(bar + base + CMP_REFRESH_PLM);
		if (!(plm & CMP_PLM_HOST_WRITE) || (plm & 0xfff00000u) == 0xbad00000u)
			return problem(hbm, -EAGAIN, "refresh access closed",
				       base + CMP_REFRESH_PLM, plm);
		hbm->busy_refresh[i] = readl(bar + base + CMP_REFRESH);
		if (!readable(hbm->busy_refresh[i]) || !(hbm->busy_refresh[i] & CMP_REFRESH_MASK))
			return problem(hbm, -EAGAIN, "invalid refresh",
				       base + CMP_REFRESH, hbm->busy_refresh[i]);
		if (!mask)
			first = base;
		mask |= 1u << i;
	}
	if (!mask)
		return problem(hbm, -EAGAIN, "no accessible memory controllers",
			       CMP_FBPA_BASE + CMP_FBPA_CFG1, 0);
	cfg = readl(bar + first + CMP_PLL_CFG);
	coeff = readl(bar + first + CMP_PLL_COEFF);
	plm = readl(bar + first + CMP_PLL_PLM);
	if (!readable(cfg) || (cfg & (CMP_PLL_LOCK | 1)) != (CMP_PLL_LOCK | 1) ||
	    (cfg & ((1u << 7) | (1u << 10))))
		return problem(hbm, -EAGAIN, "HBM PLL not ready", first + CMP_PLL_CFG, cfg);
	if (!readable(coeff) || !(coeff & 0xff) || !(coeff & 0x3f0000))
		return problem(hbm, -EAGAIN, "invalid HBM PLL coefficient",
			       first + CMP_PLL_COEFF, coeff);
	if (!(plm & CMP_PLM_HOST_WRITE) || (plm & 0xfff00000u) == 0xbad00000u)
		return problem(hbm, -EAGAIN, "HBM PLL access closed", first + CMP_PLL_PLM, plm);
	hbm->fbpas = mask;
	hbm->first_fbpa = first;
	hbm->busy_coeff = coeff;
	hbm->idle_coeff = idle_coeff(hbm);
	hbm->profile = true;
	return 0;
}

/* Shorten the refresh intervals before increasing the clock, lengthen them
 * after decreasing it.  Every field outside the interval is preserved: these
 * registers also carry the power-down policy. */
static int retune_refresh(struct cmp_hbm *hbm, void __iomem *bar, bool idle, bool before)
{
	unsigned int i;

	for (i = 0; i < CMP_HBM_FBPAS; i++) {
		u32 addr, value, target, old;

		if (!(hbm->fbpas & (1u << i)))
			continue;
		addr = fbpa(i) + CMP_REFRESH;
		value = readl(bar + addr);
		if (!readable(value))
			return problem(hbm, -EIO, "refresh read failed", addr, value);
		old = value & CMP_REFRESH_MASK;
		target = idle ? idle_refresh << 3 : hbm->busy_refresh[i] & CMP_REFRESH_MASK;
		if (target == old || (target < old) != before)
			continue;
		writel((value & ~CMP_REFRESH_MASK) | target, bar + addr);
		value = readl(bar + addr);
		if (!readable(value) || (value & CMP_REFRESH_MASK) != target)
			return problem(hbm, -EIO, "refresh readback mismatch", addr, value);
	}
	return 0;
}

/* MC2 addresses the VBIOS-configured serial-priv master group, which is the
 * aperture NVIDIA's own HBM sequence writes the PLL through. */
static void pll_write(void __iomem *bar, u32 reg, u32 value)
{
	writel(value, bar + reg);
	writel(0, bar + CMP_PRI_FENCE);
	/* NVIDIA's HBM sequence documents a serial-priv fence ordering hole. */
	ndelay(20);
}

static int pll_wait(struct cmp_hbm *hbm, void __iomem *bar, u32 coeff,
		    bool bypass, const char *phase)
{
	unsigned int i, poll;
	u32 expected = CMP_PLL_LOCK | 1 | (bypass ? CMP_PLL_BYPASS : 0);

	for (i = 0; i < CMP_HBM_FBPAS; i++) {
		u32 reg = fbpa(i) + CMP_PLL_CFG, value = 0;

		if (!(hbm->fbpas & (1u << i)))
			continue;
		for (poll = 0; poll < 200000; poll++) {
			reg = fbpa(i) + CMP_PLL_CFG;
			value = readl(bar + reg);
			if (!readable(value) ||
			    (value & (CMP_PLL_LOCK | 1 | CMP_PLL_BYPASS | (1u << 7))) != expected)
				continue;
			reg = fbpa(i) + CMP_PLL_COEFF;
			value = readl(bar + reg);
			if (value == coeff)
				break;
		}
		if (poll == 200000)
			return problem(hbm, -ETIMEDOUT, phase, reg, value);
	}
	return 0;
}

static int set_clock(struct cmp_hbm *hbm, void __iomem *bar, bool idle)
{
	u32 old = readl(bar + hbm->first_fbpa + CMP_PLL_COEFF);
	u32 cfg = readl(bar + hbm->first_fbpa + CMP_PLL_CFG);
	u32 value = idle ? hbm->idle_coeff : hbm->busy_coeff;
	bool bypass = (old ^ value) & CMP_PLL_PLDIV_MASK;
	unsigned int i;
	int ret;

	/* Anything other than the two profiles we know means firmware or another
	 * driver moved the PLL, and moving on from there is a guess. */
	if (old != hbm->busy_coeff && old != hbm->idle_coeff)
		return problem(hbm, -EIO, "HBM PLL configuration changed",
			       hbm->first_fbpa + CMP_PLL_COEFF, old);
	if (!readable(cfg) ||
	    (cfg & (CMP_PLL_LOCK | 1 | CMP_PLL_BYPASS | (1u << 7))) != (CMP_PLL_LOCK | 1))
		return problem(hbm, -EIO, "HBM PLL not ready for transition",
			       hbm->first_fbpa + CMP_PLL_CFG, cfg);
	if (value == old)
		return 0;
	if (bypass) {
		/* Isolate the post divider from the active output.  Keep the VCO
		 * enabled and retain the trained memory timings and delay lines. */
		pll_write(bar, CMP_PLL_CFG_MC2, cfg | CMP_PLL_BYPASS);
		ret = pll_wait(hbm, bar, old, true, "HBM bypass entry timeout");
		if (ret)
			goto rollback;
		udelay(2);
	}
	pll_write(bar, CMP_PLL_COEFF_MC2, value);
	for (i = 0; i < 500; i++)
		readl(bar + hbm->first_fbpa + CMP_PLL_CFG);
	ret = pll_wait(hbm, bar, value, bypass, "HBM PLL lock/coefficient timeout");
	if (ret)
		goto rollback;
	if (bypass) {
		pll_write(bar, CMP_PLL_CFG_MC2, cfg);
		udelay(2);
		ret = pll_wait(hbm, bar, value, false, "HBM bypass exit timeout");
		if (ret)
			goto rollback;
	}
	return 0;
rollback:
	/* Restore both coefficient fields and clock selection on either leg. */
	if (bypass) {
		pll_write(bar, CMP_PLL_CFG_MC2, cfg | CMP_PLL_BYPASS);
		udelay(2);
	}
	pll_write(bar, CMP_PLL_COEFF_MC2, old);
	for (i = 0; i < 500; i++)
		readl(bar + hbm->first_fbpa + CMP_PLL_CFG);
	if (bypass)
		pll_write(bar, CMP_PLL_CFG_MC2, cfg);
	return ret;
}

/* The two handshake registers, remembered as they were last seen.  A card that
 * will not release the framebuffer says so here even after the module has given
 * up on it, which is the number the first accident needed and did not have. */
static u32 fb_peek(struct cmp_hbm *hbm, void __iomem *bar, u32 reg)
{
	u32 value = readl(bar + reg);

	if (reg == CMP_FB_STOP_OUTPUT)
		hbm->fb_req = value;
	else if (reg == CMP_FB_STOP_INPUT)
		hbm->fb_ack = value;
	return value;
}

static int fb_wait(struct cmp_hbm *hbm, void __iomem *bar, bool stopped)
{
	u32 value = 0;
	unsigned int i;

	for (i = 0; i < 1000; i++) {
		value = fb_peek(hbm, bar, CMP_FB_STOP_INPUT);
		if (!readable(value))
			return problem(hbm, -EIO, "HBM FB stop ACK unreadable",
				       CMP_FB_STOP_INPUT, value);
		if (!!(value & CMP_FB_STOP) == stopped)
			return 0;
		udelay(1);
	}
	/* Past the first millisecond the register is not going to change because
	 * the CPU asked harder.  What the card was seen doing was holding this ACK
	 * up for five minutes at a 20 ms poll interval, so ask again more slowly
	 * and for longer, and only where sleeping is legal: the whole point is to
	 * find out within one attempt whether the hardware is answering at all. */
	if (in_atomic() || irqs_disabled())
		goto timeout;
	for (i = 0; i < CMP_FB_WAIT_SLEEP_ROUNDS; i++) {
		usleep_range(4000, 6000);
		value = fb_peek(hbm, bar, CMP_FB_STOP_INPUT);
		if (!readable(value))
			return problem(hbm, -EIO, "HBM FB stop ACK unreadable",
				       CMP_FB_STOP_INPUT, value);
		if (!!(value & CMP_FB_STOP) == stopped)
			return 0;
	}
timeout:
	return problem(hbm, -ETIMEDOUT, stopped ? "HBM FB stop ACK timeout" :
		       "HBM FB resume ACK timeout", CMP_FB_STOP_INPUT, value);
}

static int fb_stop(struct cmp_hbm *hbm, void __iomem *bar)
{
	u32 request = fb_peek(hbm, bar, CMP_FB_STOP_OUTPUT);
	u32 ack = fb_peek(hbm, bar, CMP_FB_STOP_INPUT);

	if (!readable(request) || !readable(ack))
		return problem(hbm, -EIO, "HBM FB stop state unreadable",
			       !readable(request) ? CMP_FB_STOP_OUTPUT : CMP_FB_STOP_INPUT,
			       !readable(request) ? request : ack);
	/* Reuse our own held request after a failed transition, and never clear
	 * a stop asserted by firmware or another owner. */
	if (request & CMP_FB_STOP) {
		if (hbm->fb_stopped)
			return fb_wait(hbm, bar, true);
		return problem(hbm, -EBUSY, "HBM FB stop already owned",
			       CMP_FB_STOP_OUTPUT, request);
	}
	/* Our previous release may have cleared the request without receiving the
	 * ACK falling edge.  Reassert the stop so rollback can still run. */
	if ((ack & CMP_FB_STOP) && !hbm->fb_stopped)
		return problem(hbm, -EBUSY, "HBM FB stop ACK still set",
			       CMP_FB_STOP_INPUT, ack);
	hbm->fb_stopped = true;
	writel(CMP_FB_STOP, bar + CMP_FB_STOP_SET);
	request = fb_peek(hbm, bar, CMP_FB_STOP_OUTPUT);
	if (!readable(request) || !(request & CMP_FB_STOP))
		return problem(hbm, -EIO, "HBM FB stop request did not latch",
			       CMP_FB_STOP_OUTPUT, request);
	return fb_wait(hbm, bar, true);
}

static int fb_start(struct cmp_hbm *hbm, void __iomem *bar)
{
	u32 request;
	unsigned int i;
	int ret;

	/* NVIDIA's FB-start sequence refreshes first, to keep memory clocks
	 * running across release of the stop request. */
	for (i = 0; i < CMP_HBM_FBPAS; i++)
		if (hbm->fbpas & (1u << i))
			writel(1, bar + fbpa(i) + CMP_SOFTWARE_REF);
	writel(0, bar + CMP_PRI_FENCE);
	ndelay(20);
	writel(CMP_FB_STOP, bar + CMP_FB_STOP_CLEAR);
	request = fb_peek(hbm, bar, CMP_FB_STOP_OUTPUT);
	if (!readable(request) || (request & CMP_FB_STOP))
		return problem(hbm, -EIO, "HBM FB resume request did not clear",
			       CMP_FB_STOP_OUTPUT, request);
	ret = fb_wait(hbm, bar, false);
	if (!ret)
		hbm->fb_stopped = false;
	return ret;
}

/* The counters as firmware left them, read a second time with the framebuffer
 * already stopped.
 *
 * The count cannot be used here: a counter whose own block is stopped cannot be
 * expected to advance, and a check that can only ever say "quiet" is worse than
 * no check, because it looks like a guard.  The configuration can be used.  What
 * the lost card was seen doing was having counter 0 re-armed from 0x380001 to
 * 0x380081 when compute started, and firmware that is re-arming a counter is
 * firmware doing something with the memory -- the one thing this transition must
 * not be started under. */
static int fb_proved_busy(struct cmp_hbm *hbm, void __iomem *bar)
{
	unsigned int i;

	for (i = 0; i < CMP_HBM_COUNTERS; i++) {
		struct cmp_hbm_counter c;

		if (!(hbm->counters & (1u << i)))
			continue;
		/* Not being able to read the counter is not proof that the card is
		 * idle either: it is the same unknown, arriving the other way. */
		if (!read_counter(hbm, bar, i, &c))
			return -EAGAIN;
		if (memcmp(c.mask, hbm->counter[i].mask, sizeof(c.mask)) ||
		    c.control != hbm->counter[i].control ||
		    c.threshold != hbm->counter[i].threshold)
			return problem(hbm, -EAGAIN, "HBM counter re-armed after FB stop",
				       cmp_pmu_counter_addr[0] + i * 4, c.mask[0]);
	}
	return 0;
}

static int hbm_transition(struct cmp_hbm *hbm, void __iomem *bar, bool idle)
{
	int ret = fb_stop(hbm, bar), resumed;

	/* Nothing has been changed yet, and here is the last chance to find out
	 * that the card should not have been asked at all. */
	if (!ret && idle && hbm->counters_live)
		ret = fb_proved_busy(hbm, bar);
	if (!ret)
		ret = retune_refresh(hbm, bar, idle, true);
	if (!ret)
		ret = set_clock(hbm, bar, idle);
	if (!ret)
		ret = retune_refresh(hbm, bar, idle, false);
	if (!hbm->fb_stopped)
		return ret;
	if (ret) {
		u32 coeff = readl(bar + hbm->first_fbpa + CMP_PLL_COEFF);

		/* Report what the rollback left behind, and then hand the
		 * framebuffer back anyway.
		 *
		 * This is the decision that cost the reboot.  The reasoning that
		 * was written here said that letting traffic run against a PLL
		 * state nobody can name is worse than holding traffic stopped, and
		 * measured against the alternative that is backwards: an unnamed
		 * PLL is a card on the wrong memory clock until the next driver
		 * reload, and a held stop is a card that GSP-RM cannot reach, which
		 * is how the machine lost the whole card.  Nothing this module can
		 * get wrong about the PLL may be allowed to cost more than the
		 * clock it was asked to change. */
		if (coeff != hbm->busy_coeff && coeff != hbm->idle_coeff)
			problem(hbm, -EIO, "HBM left at an unknown PLL after rollback",
				hbm->first_fbpa + CMP_PLL_COEFF, coeff);
		else
			/* Nothing to do with the answer but ask: pll_wait() reports a
			 * lock it did not get, and the release below happens whether it
			 * did or not.  The clock this card is left on is in the log either
			 * way, which is all the rollback can honestly promise. */
			(void) pll_wait(hbm, bar, coeff, false,
					"HBM not locked after rollback, FB released anyway");
	}
	resumed = fb_start(hbm, bar);
	if (resumed)
		cmp_hbm_halt(hbm, resumed, resumed == -ETIMEDOUT ?
			     "fb resume ACK timeout" : "fb resume request stuck");
	return resumed ? resumed : ret;
}

static void reset_state(struct cmp_hbm *hbm)
{
	hbm->baseline = false;
	/* The card's history is being dropped, so the flap guard goes with it: it is a
	 * claim about how this card has behaved, and a card that has just lost its
	 * firmware has not behaved either way.  All three timestamps go, because the
	 * guard now compares a stay against the interval that preceded it, and half a
	 * pair of those is a card that has never been asked. */
	hbm->calm_at = 0;
	hbm->flaps = 0;
	hbm->idle_since = 0;
	hbm->busy_since = 0;
	/* Keep the saved profile until the idle settings and our stop are clear. */
	if (!hbm->idle && !hbm->fb_stopped)
		hbm->profile = false;
}

static int go_busy(struct cmp_hbm *hbm, void __iomem *bar)
{
	int ret;

	if (!hbm->idle && !hbm->fb_stopped)
		return 0;
	if (!firmware_booted(bar))
		return -EAGAIN;
	ret = hbm_transition(hbm, bar, false);
	if (!ret)
		hbm->idle = false;
	return ret;
}

static int go_idle(struct cmp_hbm *hbm, void __iomem *bar)
{
	int ret;

	/* MDIV is retained, so the two clocks compare as divider pairs; asking
	 * for an idle clock above the busy one would raise the clock to lower
	 * the power. */
	if (((hbm->busy_coeff & CMP_PLL_NDIV_MASK) >> 8) * idle_pldiv <
	    idle_ndiv * ((hbm->busy_coeff & CMP_PLL_PLDIV_MASK) >> 16))
		return problem(hbm, -EINVAL, "HBM idle target exceeds busy clock", 0, 0);
	/* The decision to come here was taken from a sample up to poll_ms old, and
	 * what it decided was that the card had been quiet for a whole second.
	 * Ask one more time, immediately before taking the framebuffer away: a
	 * card that started working inside that window is the card this transition
	 * must not happen under, and this is the last point at which not doing it
	 * is still cheap. */
	ret = counters_moved(hbm, bar);
	if (ret < 0)
		return ret;
	if (ret > 0)
		return problem(hbm, -EAGAIN,
			       "HBM idle refused: activity at the moment of the stop request",
			       0, hbm->active_counters);
	hbm->idle_coeff = idle_coeff(hbm);
	hbm->idle = true;
	ret = hbm_transition(hbm, bar, true);
	if (ret) {
		go_busy(hbm, bar);
		hbm->baseline = false;
	}
	return ret;
}

/* One counter, read twice: the five configuration words have to be identical
 * across the read of the count, or the delta describes two different setups.
 * control == 2 is a counter firmware has armed, and the mask is what it counts. */
static bool read_counter(struct cmp_hbm *hbm, void __iomem *bar, unsigned int slot,
			 struct cmp_hbm_counter *c)
{
	u32 before[5], after[5];
	unsigned int i;

	for (i = 0; i < 5; i++)
		before[i] = readl(bar + cmp_pmu_counter_addr[i] + slot * 4);
	c->count = readl(bar + CMP_PMU_COUNT + slot * 4);
	for (i = 0; i < 5; i++)
		after[i] = readl(bar + cmp_pmu_counter_addr[i] + slot * 4);
	if (memcmp(before, after, sizeof(before)) || after[3] != 2 ||
	    (after[4] & ~CMP_PMU_COUNT_MASK) || (c->count & ~CMP_PMU_COUNT_MASK)) {
		problem(hbm, -EAGAIN, "activity counter unavailable",
			cmp_pmu_counter_addr[3] + slot * 4, after[3]);
		return false;
	}
	memcpy(c->mask, after, sizeof(c->mask));
	c->control = after[3];
	c->threshold = after[4];
	return true;
}

/* Borrow whichever counters GSP-RM already runs, as long as between them they
 * cover the graphics engine, the copy engines and the framebuffer request path.
 * A card whose firmware arms none of those is a card this module has no basis to
 * call idle, so it stays out.
 *
 * Every counter this accepts then gets a veto over going idle, and none of them
 * gets a vote the other way: the counters say nothing except "not now".  That is
 * the opposite of the arrangement this file used to have, in which only the
 * counter watching framebuffer requests could call the card busy, on the argument
 * that the others were firmware noise.  The measurement that settled it is in
 * docs/HBM-PM.md: on a card serving requests, that one counter reported nothing
 * at all for 8.4 s while the card drew 178 W, and the framebuffer request path
 * was taken away from underneath it.  The noise that motivated the subset costs a
 * few watts of missed opportunity; the subset cost a machine. */
static bool discover_counters(struct cmp_hbm *hbm, void __iomem *bar)
{
	u32 *covered = hbm->coverage;
	unsigned int i, bank;

	hbm->counters = 0;
	hbm->counters_live = false;
	memset(covered, 0, sizeof(hbm->coverage));
	memset(hbm->delta, 0, sizeof(hbm->delta));
	for (i = 0; i < CMP_HBM_COUNTERS; i++) {
		struct cmp_hbm_counter c;
		bool useful = false;

		if (!read_counter(hbm, bar, i, &c))
			continue;
		for (bank = 0; bank < 3; bank++)
			if (c.mask[bank] & cmp_pmu_required[bank] & ~covered[bank])
				useful = true;
		if (!useful)
			continue;
		hbm->counter[i] = c;
		hbm->counters |= 1u << i;
		for (bank = 0; bank < 3; bank++)
			covered[bank] |= c.mask[bank];
	}
	for (bank = 0; bank < 3; bank++)
		if ((covered[bank] & cmp_pmu_required[bank]) != cmp_pmu_required[bank]) {
			problem(hbm, -EAGAIN, "activity counter coverage missing",
				cmp_pmu_counter_addr[bank], covered[bank]);
			return false;
		}
	hbm->counters_live = true;
	hbm->reason = NULL;
	return true;
}

/* Everything the borrowed counters have seen is history: start the next
 * measurement from here rather than from the last sample the governor saw.
 * Needed on the way into the idle clock, where what the card's own firmware
 * generated while the transition was being decided would otherwise be charged to
 * whatever starts next. */
static void rebase_counters(struct cmp_hbm *hbm, void __iomem *bar)
{
	unsigned int i;

	for (i = 0; i < CMP_HBM_COUNTERS; i++)
		if (hbm->counters & (1u << i))
			hbm->counter[i].count = readl(bar + CMP_PMU_COUNT + i * 4);
}

/* The borrowed counters, read again against the last baseline: 0 when none of
 * them moved, 1 when any of them did, -EAGAIN when they cannot be trusted at all
 * -- which is neither quiet nor busy, and has to be answered as busy.
 *
 * One event from one counter is enough to refuse an idle transition, at any
 * magnitude, because that is the direction in which being wrong costs nothing.
 * The baselines advance as they are read, so a caller that asks twice -- once to
 * decide and once with the stop already requested -- measures the interval
 * between the two questions rather than the whole history behind them. */
static int counters_moved(struct cmp_hbm *hbm, void __iomem *bar)
{
	bool moved = false;
	unsigned int i;

	for (i = 0; i < CMP_HBM_COUNTERS; i++) {
		struct cmp_hbm_counter c, *previous = &hbm->counter[i];
		u32 delta;

		if (!(hbm->counters & (1u << i)))
			continue;
		if (!read_counter(hbm, bar, i, &c))
			return -EAGAIN;
		if (memcmp(c.mask, previous->mask, sizeof(c.mask)) ||
		    c.control != previous->control || c.threshold != previous->threshold) {
			problem(hbm, -EAGAIN, "activity counter configuration changed",
				cmp_pmu_counter_addr[0] + i * 4, c.mask[0]);
			return -EAGAIN;
		}
		delta = (c.count - previous->count) & CMP_PMU_COUNT_MASK;
		if (delta > hbm->delta[i])
			hbm->delta[i] = delta;
		if (delta > hbm->events)
			hbm->events = delta;
		if (!delta)
			continue;
		/* Under the floor this movement is not work, it is a resident process
		 * touching memory that belongs to it and no request: 7 to 54 events
		 * about once a second held a card at the busy clock for as long as
		 * anybody watched it, while the same counters reported thousands on a
		 * card that was loading weights and millions on one that was serving.
		 *
		 * The baseline is deliberately not advanced here.  Advancing it would
		 * spend events the card really generated on a movement the governor
		 * chose to ignore, and a workload whose first sample is small would
		 * have to generate the whole floor again; left where it is, the events
		 * accumulate against the last accepted reading and the sample that
		 * crosses the floor sees all of them. */
		if (delta < wake_events)
			continue;
		hbm->active_counters |= 1u << i;
		*previous = c;
		moved = true;
	}
	return moved;
}

/* The positive evidence that nobody is using the card -- which the counters
 * cannot give, and on this chip no register can either.
 *
 * The ampere headers this driver ships contain no engine-status register at all:
 * the answer to "is the graphics engine busy" lives inside GSP-RM, and the only
 * traffic the CPU can see for itself is the borrowed counters above, measured
 * reading zero for 8.4 s on a card drawing 178 W.  So the question gets asked the
 * way it can be asked -- from userspace, of RM, once a second -- and held here.
 *
 * Every way of not answering counts as an answer of "busy": never written, stale,
 * malformed.  Only a fresh `quiet` opens the door to a stop request.  Nothing on
 * the way out needs it, because being late to wake costs a watt and being late to
 * notice work costs a machine. */
static int lease_allows(struct cmp_hbm_worker *worker, u64 now)
{
	struct cmp_hbm *hbm = &worker->hbm;
	u64 written = READ_ONCE(worker->lease_written);
	u64 since = READ_ONCE(worker->lease_quiet_since);

	if (!lease_ms)
		return 0;	/* Waived by the operator; the status file says so. */
	if (!written)
		return problem(hbm, -EAGAIN,
			       "idle lease absent: no keeper has answered for this card",
			       0, worker->lease_writes);
	if (now > written && now - written > (u64)lease_ms * 1000000)
		return problem(hbm, -EAGAIN, "idle lease stale", 0,
			       (u32)((now - written) / 1000000));
	if (!READ_ONCE(worker->lease_quiet))
		return problem(hbm, -EAGAIN, "idle lease refused: the keeper reports work on the card",
			       0, worker->lease_writes);
	/* An operator who wrote `force` by hand has said the thing the streak and the
	 * window exist to establish, once, on purpose, while looking at the machine.
	 * Requiring them to have written it twice a second ago as well would only be
	 * a test of how fast somebody types.  It is still a lease, so it still ages
	 * out: forgetting about a `force` costs a card on the low clock until the
	 * next write, not until the next reboot. */
	if (READ_ONCE(worker->lease_forced))
		return 0;
	/* One sample is not evidence.  The keeper asks RM once a second, so a
	 * request that began just after the last question is not in the answer this
	 * card is holding, and the only thing that catches it is the next answer
	 * agreeing.  Two reports is the minimum that can disagree. */
	if (worker->lease_streak < CMP_HBM_LEASE_MIN_REPORTS)
		return problem(hbm, -EAGAIN, "idle lease is too young to be believed", 0,
			       worker->lease_streak);
	/* And the agreement has to cover the window the counters were asked to
	 * vouch for, rather than merely having arrived at the end of it. */
	if (!since || now < since ||
	    now - since < (u64)idle_ms * 1000000)
		return problem(hbm, -EAGAIN, "idle lease has not held quiet for the whole idle window",
			       0, since ? (u32)((now - since) / 1000000) : 0);
	return 0;
}

/* A transition this module did not like is not retried on the next tick.  The
 * failure this has to be designed against is the one it was caught doing:
 * releasing a framebuffer that would not be released, attempted every 20 ms for
 * five minutes -- 24814 attempts, with one rate-limited log line per 30 s
 * standing in front of all of them. */
static void cmp_hbm_backoff(struct cmp_hbm *hbm, u64 now)
{
	hbm->backoff_ms = hbm->backoff_ms ?
		min(hbm->backoff_ms * 2U, (unsigned int)CMP_HBM_MAX_BACKOFF_MS) :
		CMP_HBM_DEFAULT_POLL_MS;
	hbm->retry_at = now + (u64)hbm->backoff_ms * 1000000;
}

static int sample_state(struct cmp_hbm_worker *worker, u64 now)
{
	struct cmp_hbm *hbm = &worker->hbm;
	void __iomem *bar = worker->bar;
	bool busy = false;
	int ret;

	hbm->reason = NULL;
	hbm->active_counters = 0;
	hbm->events = 0;
	/* Off limits: the card could not be handed back, so it is not touched
	 * again.  Everything below this line is an MMIO access. */
	if (hbm->halted)
		return -EIO;
	if (!firmware_booted(bar)) {
		u32 gfw = readl(bar + CMP_GFW_BOOT_OFFSET);

		reset_state(hbm);
		if (!readable(gfw) || (gfw & 0xff) != 0xff)
			return problem(hbm, -EAGAIN, "waiting for GFW boot",
				       CMP_GFW_BOOT_OFFSET, gfw);
		return problem(hbm, -EAGAIN, "waiting for GFW/GSP boot",
			       CMP_GSP_RISCV_STATUS_OFFSET,
			       readl(bar + CMP_GSP_RISCV_STATUS_OFFSET));
	}
	if (!hbm->profile) {
		ret = capture_profile(hbm, bar);
		if (ret)
			return ret;
	}
	if (mode == CMP_HBM_PIN_BUSY)
		return go_busy(hbm, bar);
	if (mode == CMP_HBM_PIN_IDLE) {
		if (!hbm->baseline) {
			ret = go_busy(hbm, bar);
			if (ret)
				return ret;
		}
		if (hbm->idle)
			return 0;
		/* Even an operator-specified idle clock goes through the lease:
		 * pinning is how the watts are measured, and it should not be
		 * possible to do that by accident on a card somebody is using.
		 * `force` in the lease is the way to say the pin is meant.  A waived
		 * lease does not count as an answer, because waiving the lease is a
		 * statement about the counters, not about this card. */
		if (!lease_ms)
			return problem(hbm, -EPERM,
				       "idle clock pinned with no lease to ask: write `force` to /sys/kernel/cmp_hbm_pm/lease, or unset lease_ms=0",
				       0, 0);
		ret = lease_allows(worker, now);
		if (ret)
			return ret;
		ret = go_idle(hbm, bar);
		hbm->baseline = !ret;
		return ret;
	}
	if (!hbm->baseline) {
		ret = go_busy(hbm, bar);
		if (ret)
			return ret;
		if (!discover_counters(hbm, bar))
			return -EAGAIN;
		hbm->baseline = true;
		hbm->sampled = hbm->quiet_since = now;
		return 0;
	}
	if (now < hbm->sampled || now - hbm->sampled > CMP_HBM_MAX_SAMPLE_NS) {
		problem(hbm, -EAGAIN, "activity sample gap", 0, 0);
		goto unknown;
	}
	ret = counters_moved(hbm, bar);
	if (ret < 0)
		goto unknown;
	busy = ret > 0;
	hbm->sampled = now;
	if (busy) {
		if (hbm->idle) {
			u64 stayed = now - hbm->idle_since;
			u64 earned = (u64)idle_ms * 1000000;
			u64 worked = hbm->busy_since && hbm->idle_since > hbm->busy_since ?
				     hbm->idle_since - hbm->busy_since : 0;
			/* Whether the reading that ended this stay was work, or the keeper
			 * asking the card whether it was idle.  Three NVML calls a round is
			 * three RPCs into GSP-RM, and a resident card was measured coming
			 * back up on `active 0x2 events 517` to `events 520` while the same
			 * counter reported 249983, 473659 and 2785189 the moment a request
			 * was served.  With no floor in force there is no reading too small
			 * to be work, and the guard below is the one every watt in the
			 * documentation was measured under. */
			bool woken_by_work = !wake_events ||
				(u64)hbm->events >=
				(u64)wake_events * CMP_HBM_FLAP_WORK_FACTOR;

			/* Being woken is not a reason to regret putting a card down.  Not
			 * staying down is, and the first version of this asked only whether
			 * a stay was shorter than the quiet period that earned it -- a
			 * comparison that goes blind the moment a card's stays clear that
			 * bar by a hair.  The resident sweep is the shape it misses: stays
			 * of about 1.2 s against `idle_ms` of 1000, the card up for half a
			 * second at a time, and 38 % of its life spent climbing back to the
			 * busy clock.  The answer to that card is the counter floor, not a
			 * pause (the dwell sweep is in docs/HBM-PM.md), and this second
			 * test is not there to catch it either: it is there for the case
			 * a short `idle_ms` opens, where a card clears a threshold nobody
			 * would call long, is up far more than it is down, and reads as
			 * working as often as it is asked.  So a stay is measured against
			 * the interval that preceded it as well as against the quiet
			 * period, and a card that is up more than it is down stops being
			 * asked for a while.
			 *
			 * A card woken by something that was plainly work is exempt from
			 * that second test, because a card that served a request and came
			 * back down ten seconds later is the feature working, and the only
			 * thing a pause would delay is the next watt. */
			if (stayed < earned || (!woken_by_work && stayed < worked)) {
				unsigned int pause;

				hbm->flaps++;
				pause = idle_ms << min_t(unsigned int, hbm->flaps,
							 CMP_HBM_FLAP_SHIFT);
				if (!pause || pause > CMP_HBM_MAX_FLAP_PAUSE_MS)
					pause = CMP_HBM_MAX_FLAP_PAUSE_MS;
				hbm->calm_at = now + (u64)pause * 1000000;
			} else {
				hbm->flaps = 0;
			}
			hbm->busy_since = now;
		}
		/* Traffic resets the retry clock too: a card that is working has
		 * proven that whatever went wrong before did not take the card with
		 * it, and the backoff is about not hammering a stuck transition,
		 * not about never switching again. */
		hbm->quiet_since = now;
		ret = go_busy(hbm, bar);
		if (!ret) {
			hbm->backoff_ms = 0;
			hbm->retry_at = 0;
		}
		return ret;
	}
	if (hbm->idle) {
		/* The card is down and the counters say nothing, which is the one
		 * reading they are not entitled to give: a card serving requests at
		 * 178 W showed `active 0x0` for 8.4 s, and a card whose keeper has
		 * been stopped shows it indefinitely.  Being idle is therefore not a
		 * state this module is allowed to have earned once; the permission
		 * has to be current, so the lease is asked again on every sample
		 * that would otherwise have left the card here.  Nothing about the
		 * way in is loosened by this -- it is the same question, and a
		 * keeper that stopped answering, crashed, or changed its mind about
		 * a card it had just vouched for now brings it back up within
		 * `poll_ms` rather than at the mercy of a counter that may never
		 * move. */
		ret = lease_allows(worker, now);
		if (ret)
			return go_busy(hbm, bar);
		return 0;
	}
	if (!hbm->idle && now - hbm->quiet_since >= (u64)idle_ms * 1000000) {
		if (now < hbm->retry_at)
			return problem(hbm, -EAGAIN,
				       "idle transition waiting out the backoff after a failed one",
				       0, hbm->backoff_ms);
		/* The other kind of not-now: the transition below worked, and the card
		 * was back up before the quiet period it had just been granted was
		 * over.  Nothing failed, which is why the failed-transition backoff
		 * above does not cover it, and nothing has to be re-armed either: the
		 * counters still veto, the lease still has to be current, and all this
		 * gate does is stop a card that is not actually idle from being asked
		 * once a second forever. */
		if (now < hbm->calm_at)
			return problem(hbm, -EAGAIN,
				       "idle transition waiting out the pause after a stay too short to count as idle",
				       0, hbm->flaps);
		ret = lease_allows(worker, now);
		if (ret)
			return ret;
		ret = go_idle(hbm, bar);
		if (ret) {
			cmp_hbm_backoff(hbm, now);
		} else {
			rebase_counters(hbm, bar);
			hbm->idle_since = now;
		}
		return ret;
	}
	return 0;
unknown:
	hbm->baseline = false;
	ret = go_busy(hbm, bar);
	return ret ? ret : -EAGAIN;
}

static bool held_by_nvidia(struct pci_dev *pdev)
{
	return pdev->driver && !strcmp(pdev->driver->name, "nvidia");
}

static void reschedule(struct cmp_hbm_worker *worker, unsigned long delay)
{
	if (!READ_ONCE(worker->bound) || !READ_ONCE(enable) ||
	    READ_ONCE(worker->paused) || READ_ONCE(worker->stopping))
		return;
	mod_delayed_work(system_freezable_wq, &worker->work, delay);
}

/* The caller holds the PCI device lock.  Never wake a runtime-suspended GPU:
 * the point of this feature is to leave power unspent, not to measure it.
 *
 * foreign_ok lets the busy restore run while the card is on its way out of the
 * nvidia driver: at unbind we still owe it the clock we lowered, and nobody else
 * can hand it back.  Sampling never sets it, because once another driver owns the
 * card its clocks are its own business. */
static int access_get(struct cmp_hbm_worker *worker, bool foreign_ok)
{
	struct pci_dev *pdev = worker->pdev;
	u16 command;
	int pm;

	if ((!foreign_ok && !held_by_nvidia(pdev)) || pdev->current_state != PCI_D0)
		return -ENODEV;
#ifdef CMP_HBM_PM_ACTIVE_HAS_IGNORE
	pm = pm_runtime_get_if_active(&pdev->dev, true);
#else
	pm = pm_runtime_get_if_active(&pdev->dev);
#endif
	if (!pm)
		return -EAGAIN;
	if (pci_read_config_word(pdev, PCI_COMMAND, &command) ||
	    command == 0xffff || !(command & PCI_COMMAND_MEMORY)) {
		if (pm > 0)
			pm_runtime_put(&pdev->dev);
		return -EAGAIN;
	}
	/* A disabled runtime-PM implementation needs no usage reference. */
	return pm > 0;
}

static void access_put(struct cmp_hbm_worker *worker, int pm)
{
	if (pm > 0)
		pm_runtime_put(&worker->pdev->dev);
}

static const char *mode_name(void)
{
	static const char * const names[] = { "auto", "pinned-busy", "pinned-idle" };

	return names[mode <= CMP_HBM_PIN_IDLE ? mode : CMP_HBM_AUTO];
}

/* -EAGAIN is the normal answer early in boot and while a card is asleep, and it
 * is also the answer a card gives when the driver never opened the two masks this
 * needs, so the difference between "not yet" and "never" has to be readable in
 * the log without dyndbg: at most one line per reason, then one per 30 s.
 *
 * The reason a line has to be remembered as *printed*, separately from the reason
 * the last poll ended with: a card whose client is resident alternates a poll that
 * had nothing to do, which returns 0, with one the lease refuses, and comparing the
 * two makes every refusal a new saying.  That is not a hypothetical shape -- on a
 * card holding two tensor-parallel workers it printed one line every two to six
 * seconds, 71 of them in one boot, with the reason never once changing.
 * The decline counter does want the previous return value, because a refusal that
 * follows the card being seen working is a decision the governor really did have to
 * take again, so the two pairs of fields stay separate. */
static void report_result(struct cmp_hbm_worker *worker, int ret, u64 now)
{
	struct cmp_hbm *hbm = &worker->hbm;
	bool newsaying = ret != worker->last_error || hbm->reason != worker->last_reason;
	bool unprinted = ret != worker->logged_error || hbm->reason != worker->logged_reason;

	if (!ret) {
		if (worker->last_error)
			pci_dbg(worker->pdev, "cmp_hbm_pm: activity sampling active, counters=%#x\n",
				hbm->counters);
	} else if (unprinted || now - worker->last_warn >= 30000000000ull) {
		pci_warn(worker->pdev,
			 "cmp_hbm_pm: %s (%d) reg=%#x value=%#x busy_coeff=%#x coverage=%#x/%#x/%#x idle=%u fb_stopped=%u\n",
			 hbm->reason ? hbm->reason : "hardware unavailable", ret,
			 hbm->reg, hbm->value, hbm->busy_coeff,
			 hbm->coverage[0], hbm->coverage[1], hbm->coverage[2],
			 hbm->idle, hbm->fb_stopped);
		worker->last_warn = now;
		worker->logged_error = ret;
		worker->logged_reason = hbm->reason;
	} else if (ret == -EAGAIN) {
		pci_dbg(worker->pdev, "cmp_hbm_pm: %s (%d) reg=%#x value=%#x\n",
			hbm->reason ? hbm->reason : "hardware unavailable", ret,
			hbm->reg, hbm->value);
	}
	if (ret) {
		/* A card that says "not yet", or "not on a driver that never opened
		 * the masks", is not a card that failed.  On a plain install every
		 * poll of every card lands on -EAGAIN, which counted as 50 errors a
		 * second against a governor doing exactly what it should; only a
		 * refusal the hardware did not explain with one of the -EAGAIN
		 * reasons is worth calling an error.
		 *
		 * `declines` counts sayings, not polls.  The lease gate answers
		 * -EAGAIN on every poll for as long as a card stays eligible and
		 * unleased, which is forty increments a second at poll_ms=20 and a
		 * number that cannot tell a long refusal from a stutter; the first
		 * reading of it on hardware counted 1222 declines in the thirty
		 * seconds after a card went quiet, with nothing happening at all.
		 * `errors` stays per-attempt on purpose: it is the field that is
		 * supposed to sit at zero, and a number climbing there is the alarm
		 * whether it arrives one at a time or forty. */
		if (ret == -EAGAIN) {
			if (newsaying)
				WRITE_ONCE(worker->stats.declines, worker->stats.declines + 1);
		} else {
			WRITE_ONCE(worker->stats.errors, worker->stats.errors + 1);
		}
	}
	WRITE_ONCE(worker->stats.last_error, ret);
	WRITE_ONCE(worker->stats.reason, hbm->reason ? hbm->reason : "ok");
	worker->last_error = ret;
	worker->last_reason = hbm->reason;
}

static void govern(struct cmp_hbm_worker *worker)
{
	struct cmp_hbm *hbm = &worker->hbm;
	bool idle = hbm->idle, profile = hbm->profile;
	u64 start = ktime_get_ns(), elapsed;
	int ret;

	if (firmware_booted(worker->bar)) {
		if (!worker->boot_seen)
			pci_info(worker->pdev, "cmp_hbm_pm: firmware booted gfw=%#x gsp=%#x\n",
				 readl(worker->bar + CMP_GFW_BOOT_OFFSET),
				 readl(worker->bar + CMP_GSP_RISCV_STATUS_OFFSET));
		worker->boot_seen = true;
	} else {
		worker->boot_seen = false;
	}
	ret = sample_state(worker, start);
	elapsed = ktime_get_ns() - start;
	if (!worker->halt_logged && hbm->halted) {
		/* One line, never rate limited, at the priority that survives a ring
		 * buffer full of driver assertions.  This is the state in which the
		 * module has stopped touching the card, and the two registers that
		 * say whether the framebuffer is still this module's to give back
		 * have to be readable in the log rather than reconstructed from a
		 * second boot. */
		worker->halt_logged = true;
		pci_emerg(worker->pdev,
			  "cmp_hbm_pm: HALTED after %s (%d): the framebuffer was not given back, this card will not be touched again until the module is reloaded. fb_req=%#x fb_ack=%#x idle=%u fb_stopped=%u\n",
			  hbm->halt_reason ? hbm->halt_reason : "failed release",
			  hbm->halt_error, hbm->fb_req, hbm->fb_ack, hbm->idle,
			  hbm->fb_stopped);
	}
	if (!profile && hbm->profile)
		pci_info(worker->pdev,
			 "cmp_hbm_pm: busy profile %u MHz coeff=%#x, idle target %u MHz, fbpas=%#x\n",
			 coeff_mhz(hbm->busy_coeff), hbm->busy_coeff,
			 coeff_mhz(hbm->idle_coeff), hbm->fbpas);
	if (hbm->counters_live && !worker->counters_printed) {
		unsigned int i;

		/* Once per attach rather than once per baseline: these words only
		 * change when firmware re-arms the counters, which is a driver
		 * reload away, and a policy write reprinting them would bury the
		 * switches this module is actually being asked about.  What each
		 * borrowed slot watches is on the line because `active` says only
		 * which slot moved, and every one of them now has a veto over going
		 * idle -- so which engine answers to which slot is what a reader
		 * needs in order to know what that veto has been covering for. */
		worker->counters_printed = true;
		for (i = 0; i < CMP_HBM_COUNTERS; i++) {
			struct cmp_hbm_counter *c = &hbm->counter[i];

			if (!(hbm->counters & (1u << i)))
				continue;
			pci_info(worker->pdev,
				 "cmp_hbm_pm: counter %u watches %#x %#x %#x, control=%u threshold=%#x, vetoes idle\n",
				 i, c->mask[0], c->mask[1], c->mask[2], c->control,
				 c->threshold);
		}
	}
	if (idle != hbm->idle) {
		WRITE_ONCE(worker->stats.transitions, worker->stats.transitions + 1);
		if (log_transitions)
			pci_info(worker->pdev,
				 /* `idle` is the state before the sample and `hbm->idle` the one
				  * after it, so the two words read left to right as the
				  * switch itself: a card that has just gone quiet says
				  * "busy -> idle", not "idle -> idle".  `events` is the largest
				  * single-sample count the counters reported on the way there,
				  * and the handshake registers are printed because the
				  * transition that went wrong was diagnosable only from them,
				  * and they were not in the log at the time.
				  *
				  * `because` carries the answer that moved the clock when the
				  * counters did not: a card pulled back up by a lease that went
				  * stale or was withdrawn arrives here with `active 0x0 events
				  * 0`, which without the reason reads like a card wobbling for
				  * nothing -- and reading it that way is what makes an operator
				  * turn the governor off. */
				 "cmp_hbm_pm: HBM %s -> %s at %u MHz after %llu ms quiet, counters active %#x events %u fb %#x/%#x%s%s, switch took %llu us\n",
				 idle ? "idle" : "busy", hbm->idle ? "idle" : "busy",
				 coeff_mhz(readl(worker->bar + hbm->first_fbpa +
						 CMP_PLL_COEFF)),
				 (unsigned long long)((hbm->baseline ?
							start - hbm->quiet_since : 0) /
						       1000000),
				 hbm->active_counters,
				 hbm->events, hbm->fb_req, hbm->fb_ack,
				 hbm->reason && !hbm->idle ? ", because " : "",
				 hbm->reason && !hbm->idle ? hbm->reason : "",
				 (unsigned long long)(elapsed / 1000));
	}
	snapshot_stats(worker);
	report_result(worker, ret, start);
}

static void sample_work(struct work_struct *work)
{
	struct cmp_hbm_worker *worker = container_of(work, struct cmp_hbm_worker, work.work);
	int pm;

	if (READ_ONCE(worker->paused) || READ_ONCE(worker->stopping) ||
	    !READ_ONCE(worker->bound) || !READ_ONCE(enable))
		return;
	/* The card is off limits, so the loop ends here instead of rescheduling
	 * itself around it.  This is the difference between giving up on a card and
	 * retrying a release of a dead card every 20 ms for five minutes, which is
	 * what `errors 24814` was: the rate limiter was limiting the printing, not
	 * the attempting. */
	if (READ_ONCE(worker->hbm.halted))
		return;
	/* Only ever trylock: the bus notifier runs with this lock held and
	 * cancels this work, so a blocking lock here would deadlock unbind. */
	if (device_trylock(&worker->pdev->dev)) {
		pm = access_get(worker, false);
		if (pm >= 0) {
			if (worker->last_access)
				pci_dbg(worker->pdev, "cmp_hbm_pm: PCI access resumed\n");
			govern(worker);
			access_put(worker, pm);
		} else {
			if (pm != worker->last_access)
				pci_dbg(worker->pdev, "cmp_hbm_pm: waiting for an active PCI device (%d) state=%d\n",
					pm, worker->pdev->current_state);
			reset_state(&worker->hbm);
		}
		worker->last_access = pm < 0 ? pm : 0;
		device_unlock(&worker->pdev->dev);
	}
	reschedule(worker, msecs_to_jiffies(poll_ms ?: CMP_HBM_DEFAULT_POLL_MS));
}

/* Back to the busy clock and forget what we knew, so the next sample starts from
 * the card's own state rather than from ours.  Every caller here moves the clock
 * for a reason that has nothing to do with the activity counters, so the reason
 * belongs in the line: a pin to the busy clock through `mode` is a switch like
 * any other, and unless it is counted too, the status file says a card that a
 * reader can see drawing full power never left the profile the governor chose. */
static void restore_locked(struct cmp_hbm_worker *worker, const char *why)
{
	/* A card we left low-clock owes us the restore even once nvidia has let
	 * go of it, so the unbind path is allowed past the driver check.  A card
	 * that has been halted is not asked at all: the point of the halt is that
	 * the next MMIO to it is not this module's to make, and the emergency line
	 * in the log already says what the card is owed. */
	int pm = worker->hbm.halted ? -EBUSY :
		access_get(worker, worker->hbm.idle || worker->hbm.fb_stopped);

	if (pm >= 0) {
		u64 start = ktime_get_ns(), elapsed;
		bool idle = worker->hbm.idle;
		int ret = go_busy(&worker->hbm, worker->bar);

		elapsed = ktime_get_ns() - start;
		if (ret)
			pci_warn(worker->pdev,
				 "cmp_hbm_pm: busy restore deferred (%d) %s reg=%#x value=%#x\n",
				 ret,
				 worker->hbm.reason ? worker->hbm.reason : "hardware unavailable",
				 worker->hbm.reg, worker->hbm.value);
		else if (idle) {
			WRITE_ONCE(worker->stats.transitions,
				   worker->stats.transitions + 1);
			if (log_transitions)
				/* pll_wait() has already checked every FBPA's coefficient
				 * against this word, so the profile asked for is the profile
				 * verified, and there is nothing to gain from reading the
				 * register a second time here. */
				pci_info(worker->pdev,
					 "cmp_hbm_pm: HBM idle -> busy at %u MHz, restored for %s, switch took %llu us\n",
					 coeff_mhz(worker->hbm.busy_coeff), why,
					 (unsigned long long)(elapsed / 1000));
			else
				pci_info(worker->pdev,
					 "cmp_hbm_pm: busy profile restored for %s\n", why);
		}
		access_put(worker, pm);
	} else if (worker->hbm.halted) {
		pci_warn(worker->pdev,
			 "cmp_hbm_pm: busy restore skipped, card halted after %s (%d)\n",
			 worker->hbm.halt_reason ? worker->hbm.halt_reason : "failed release",
			 worker->hbm.halt_error);
	} else if (worker->hbm.idle) {
		pci_warn(worker->pdev,
			 "cmp_hbm_pm: busy restore deferred: PCI device inactive (%d)\n", pm);
	}
	reset_state(&worker->hbm);
	worker->boot_seen = false;
	snapshot_stats(worker);
}

static int pci_notify(struct notifier_block *nb, unsigned long event, void *data)
{
	struct cmp_hbm_worker *worker = container_of(nb, struct cmp_hbm_worker, pci_nb);

	if (data != &worker->pdev->dev)
		return NOTIFY_DONE;
	/* Bus notifications run with the PCI device lock held, which is why the
	 * work below only ever tries it: cancelling that work here cannot wait on
	 * the lock the notifier is standing on, and taking it again would be
	 * taking it twice. */
	if (event == BUS_NOTIFY_BOUND_DRIVER && held_by_nvidia(worker->pdev)) {
		WRITE_ONCE(worker->bound, true);
		reschedule(worker, 0);
	} else if (event == BUS_NOTIFY_UNBIND_DRIVER) {
		WRITE_ONCE(worker->bound, false);
		cancel_delayed_work_sync(&worker->work);
		restore_locked(worker, "driver unbind");
		pci_info(worker->pdev, "cmp_hbm_pm: governor stopped for driver unbind\n");
	}
	return NOTIFY_DONE;
}

static int pm_notify(struct notifier_block *nb, unsigned long event, void *unused)
{
	struct cmp_hbm_worker *worker = container_of(nb, struct cmp_hbm_worker, pm_nb);

	switch (event) {
	case PM_SUSPEND_PREPARE:
	case PM_HIBERNATION_PREPARE:
	case PM_RESTORE_PREPARE:
		WRITE_ONCE(worker->paused, true);
		cancel_delayed_work_sync(&worker->work);
		pci_dev_lock(worker->pdev);
		restore_locked(worker, "sleep");
		pci_dev_unlock(worker->pdev);
		pci_dbg(worker->pdev, "cmp_hbm_pm: governor paused for sleep\n");
		break;
	case PM_POST_SUSPEND:
	case PM_POST_HIBERNATION:
	case PM_POST_RESTORE:
		pci_dev_lock(worker->pdev);
		reset_state(&worker->hbm);
		worker->boot_seen = false;
		pci_dev_unlock(worker->pdev);
		WRITE_ONCE(worker->paused, false);
		reschedule(worker, 0);
		pci_dbg(worker->pdev, "cmp_hbm_pm: governor waiting for firmware after sleep\n");
		break;
	}
	return NOTIFY_DONE;
}

/* Takes its own reference to pdev, and gives it back when it is stopped. */
static struct cmp_hbm_worker *worker_start(struct pci_dev *pdev)
{
	struct cmp_hbm_worker *worker;
	int ret;

	worker = kzalloc(sizeof(*worker), GFP_KERNEL);
	if (!worker)
		return ERR_PTR(-ENOMEM);
	worker->pdev = pci_dev_get(pdev);
	INIT_DELAYED_WORK(&worker->work, sample_work);
	worker->pci_nb.notifier_call = pci_notify;
	worker->pm_nb.notifier_call = pm_notify;
	/* Quiesce at the head of Linux's sleep notification chain, so the busy
	 * restore runs before anything decides to power the card off.  This
	 * assumes nothing about NVIDIA's own notifier priority. */
	worker->pm_nb.priority = INT_MAX;
	if (pci_resource_len(pdev, 0) < CMP_HBM_BAR_SIZE ||
	    !(worker->bar = pci_iomap_range(pdev, 0, 0, CMP_HBM_BAR_SIZE))) {
		ret = -ENODEV;
		goto fail;
	}
	ret = bus_register_notifier(&pci_bus_type, &worker->pci_nb);
	if (ret)
		goto fail;
	worker->pci_registered = true;
	ret = register_pm_notifier(&worker->pm_nb);
	if (ret)
		goto fail;
	worker->pm_registered = true;
	pci_dev_lock(pdev);
	WRITE_ONCE(worker->bound, held_by_nvidia(pdev));
	pci_dev_unlock(pdev);
	mutex_lock(&workers_lock);
	list_add_tail(&worker->node, &workers);
	mutex_unlock(&workers_lock);
	pci_info(pdev,
		 "cmp_hbm_pm: governor registered mode=%s idle_ms=%u poll_ms=%u lease_ms=%u idle_ndiv=%u idle_pldiv=%u idle_refresh=%u wake_events=%u transition=fb-stop/bypass\n",
		 mode_name(), idle_ms, poll_ms, lease_ms, idle_ndiv, idle_pldiv,
		 idle_refresh, wake_events);
	reschedule(worker, 0);
	return worker;
fail:
	worker->stopping = true;
	if (worker->pm_registered)
		unregister_pm_notifier(&worker->pm_nb);
	if (worker->pci_registered)
		bus_unregister_notifier(&pci_bus_type, &worker->pci_nb);
	cancel_delayed_work_sync(&worker->work);
	if (worker->bar)
		pci_iounmap(pdev, worker->bar);
	pci_dev_put(worker->pdev);
	kfree(worker);
	return ERR_PTR(ret);
}

static void worker_stop(struct cmp_hbm_worker *worker)
{
	WRITE_ONCE(worker->stopping, true);
	if (worker->pm_registered)
		unregister_pm_notifier(&worker->pm_nb);
	if (worker->pci_registered)
		bus_unregister_notifier(&pci_bus_type, &worker->pci_nb);
	cancel_delayed_work_sync(&worker->work);
	if (worker->bar) {
		pci_dev_lock(worker->pdev);
		restore_locked(worker, "governor removed");
		pci_dev_unlock(worker->pdev);
		pci_iounmap(worker->pdev, worker->bar);
	}
	/* Under the lock the status file and the lease writer walk this list by:
	 * nothing else can be holding it here (the hardware work above is done
	 * without it, on purpose), and without it a reader that happened to be
	 * printing the status while the module unloaded followed the node into kfree. */
	mutex_lock(&workers_lock);
	list_del(&worker->node);
	mutex_unlock(&workers_lock);
	pci_dev_put(worker->pdev);
	kfree(worker);
}

/* The device lock, without waiting on a driver that may be tearing a dead card
 * down.  This is not a theoretical hazard: a policy write through cmp_hbm_resync()
 * blocked here for as long as the nvidia driver held the lock unwinding the card
 * this module had stopped, which is exactly the moment an operator needs the
 * write to go through.  Asking politely for 200 ms and then saying so is the
 * most a policy change is worth. */
static bool pci_dev_lock_bounded(struct cmp_hbm_worker *worker)
{
	unsigned int waited;

	for (waited = 0; waited <= CMP_HBM_RESYNC_WAIT_MS; waited += 20) {
		if (pci_dev_trylock(worker->pdev))
			return true;
		msleep(20);
	}
	return false;
}

/* A policy change takes effect from the next sample: back to busy, forget the
 * profile, and let the card be measured again from its own registers.  The lock
 * is dropped around the hardware work, and no worker leaves the list until the
 * module unloads, so walking it stays valid. */
static void cmp_hbm_resync(void)
{
	struct cmp_hbm_worker *worker;

	mutex_lock(&workers_lock);
	list_for_each_entry(worker, &workers, node) {
		bool got;

		if (READ_ONCE(worker->stopping))
			continue;
		mutex_unlock(&workers_lock);
		got = pci_dev_lock_bounded(worker);
		if (got) {
			/* The only caller that moves the clock because the policy asked it
			 * to, which is the one case where a reader of `mode` expects to see
			 * the switch they caused. */
			restore_locked(worker, "policy change");
			pci_dev_unlock(worker->pdev);
		} else {
			pci_warn(worker->pdev,
				 "cmp_hbm_pm: policy change could not reach this card: its driver has held the device lock for %u ms; the clock it is on is unchanged and shown by the status file\n",
				 CMP_HBM_RESYNC_WAIT_MS);
			reschedule(worker, msecs_to_jiffies(poll_ms ?: CMP_HBM_DEFAULT_POLL_MS));
		}
		mutex_lock(&workers_lock);
	}
	mutex_unlock(&workers_lock);
	cmp_hbm_requeue(0);
}

static void cmp_hbm_requeue(unsigned long delay)
{
	struct cmp_hbm_worker *worker;

	mutex_lock(&workers_lock);
	list_for_each_entry(worker, &workers, node) {
		if (READ_ONCE(enable))
			reschedule(worker, delay);
		else
			cancel_delayed_work(&worker->work);
	}
	mutex_unlock(&workers_lock);
}

/* ------------------------------------------------------------------------
 * What the VBIOS performance table actually offers.
 *
 * The whole reason this module exists is that the card's table carries one
 * performance level, and a claim about a table ought to be read from the table
 * rather than inferred from what nvidia-smi chose to print: it prints one memory
 * frequency because the table has one, and it prints P0 because the table has
 * only P0, but neither of those readings says whether a row could be added.
 *
 * The PROM window in BAR0 carries the complete VBIOS container, and the walk
 * below is the one gpu-unlock 3.0.0-beta uses to reach the 'P' data image and
 * the perf table inside it
 * (https://github.com/Zagitta/gpu-unlock/tree/3.0.0-beta, kernel/gpu_unlock/vbios.c,
 * gpu_unlock_vbios_p8_overrides(), GPL-2.0-only).  It is copied rather than
 * re-derived because a decoder that gets the walk wrong reports a missing row
 * where the image has one, and that is the one conclusion here anyone would act
 * on.
 *
 * Nothing in this section writes: the image is a copy taken with
 * memcpy_fromio() and the only output is text.  That is the property the test
 * suite holds this code to, so it is stated where the code can be checked
 * against it rather than in a comment the code could drift away from.
 */

/* The VBIOS container, not a register: BAR0 offset, and the window is 1 MiB at
 * most, which is what the PROM address lines decode.  Read in chunks because a
 * million MMIO reads taken as one block is a quarter second of the card's
 * configuration bus spent on a diagnostic. */
#define CMP_HBM_PROM_OFFSET	0x00300000u
#define CMP_HBM_PROM_SIZE	(1024 * 1024)
#define CMP_HBM_PROM_CHUNK	(64 * 1024)

/* The GA100 performance-level table, by the layout upstream verified before
 * editing it: header of CMP_PERF_HEADER bytes, rows of 65, one u32 of limits per
 * domain with the floor in the low 14 bits. */
#define CMP_PERF_FORMAT		0x60
#define CMP_PERF_HEADER		10
#define CMP_PERF_DOMAIN_COUNT	5
#define CMP_PERF_ROW_SIZE	65
#define CMP_PERF_DOMAINS	10
#define CMP_PERF_MIN_MASK	0x3fffu
#define CMP_PERF_ROW_EMPTY	0xff
#define CMP_PERF_ID_P0		15
#define CMP_PERF_ID_P8		7
#define CMP_PERF_MAX_ROWS	16

struct cmp_perf_row {
	u8 id, policy[4];
	u32 limits[CMP_PERF_DOMAINS];
};

struct cmp_perf_table {
	bool found;
	u32 image, adjust, bit, data, table;
	u8 version[5];
	u8 format, header, count;
	/* Rows that are not the empty marker, and how many of them are: a table whose
	 * only free row is not the first one is a table upstream will not edit. */
	unsigned int rows, states, free_slots;
	int p0, p8;
	struct cmp_perf_row row[CMP_PERF_MAX_ROWS];
	/* Why a generated P8 is not possible on this image, in the terms upstream
	 * uses for it: 0 means the insert the unlock performs would work here. */
	int refusal;
};

static u16 cmp_le16(const u8 *p)
{
	return (u16)p[0] | ((u16)p[1] << 8);
}

static u32 cmp_le32(const u8 *p)
{
	return (u32)p[0] | ((u32)p[1] << 8) | ((u32)p[2] << 16) | ((u32)p[3] << 24);
}

static bool cmp_in_range(size_t length, u64 off, u64 bytes)
{
	return off > length || bytes > length - off;
}

/* Expansion ROM image signatures, and the data-block signatures that follow
 * them: the same two sets kgspExtractVbiosFromRom_TU102 accepts. */
static bool cmp_rom_signature(u16 sig)
{
	return sig == 0xaa55 || sig == 0x4e56 || sig == 0xbb77;
}

static bool cmp_data_signature(u32 sig)
{
	return sig == 0x52494350 || sig == 0x5344504e || sig == 0x53494752;
}

static int cmp_pci_image(const u8 *rom, size_t length, u32 *offset)
{
	u32 image = 0;

	/* The container starts with an NVGI header, not with 0xaa55; the flash
	 * descriptor directory says where the first PCI image lives. */
	if (length >= 12 && cmp_le32(rom) == 0x4947564eu) {
		u32 total, flash;
		u64 dir;

		if (((cmp_le32(rom + 4) >> 8) & 0xff) != 3)
			return -EOPNOTSUPP;
		total = cmp_le32(rom + 8) & 0xfffffu;
		if (cmp_in_range(length, total, 4))
			return -EINVAL;
		flash = cmp_le32(rom + total);
		dir = (u64)flash + 0x1000u;
		if (cmp_in_range(length, dir, 12) || cmp_le32(rom + dir) != 0x44524652u)
			return -EINVAL;
		image = cmp_le32(rom + dir + 8);
	}
	if (!image || (image & 3) || cmp_in_range(length, image, 2) ||
	    cmp_le16(rom + image) != 0xaa55)
		return -EINVAL;
	*offset = image;
	return 0;
}

/* Walk the expansion ROM chain to find the legacy image and the NPDS extension,
 * whose distance apart is what the BIT table's pointers have to be adjusted by. */
static int cmp_expansion(const u8 *rom, size_t length, u32 *adjust)
{
	u32 pos = 0, base = 0, ext = 0;
	unsigned int images = 0;

	if (cmp_in_range(length, 0, 2) || !cmp_rom_signature(cmp_le16(rom)))
		return -EINVAL;
	for (;;) {
		u64 pcir, npde;
		u32 bytes, last, sig;
		u16 ext_rev, ext_len;

		if (++images > 8 || cmp_in_range(length, pos, 0x1a) ||
		    !cmp_rom_signature(cmp_le16(rom + pos)))
			return -EINVAL;
		pcir = (u64)pos + cmp_le16(rom + pos + 0x18);
		if (cmp_in_range(length, pcir, 0x16))
			return -EINVAL;
		sig = cmp_le32(rom + pcir);
		if (!cmp_data_signature(sig))
			return -EINVAL;
		bytes = (u32)cmp_le16(rom + pcir + 0x10) * 512;
		last = rom[pcir + 0x15] & 0x80;
		npde = (pcir + cmp_le16(rom + pcir + 0x0a) + 15) & ~(u64)15;
		if (!cmp_in_range(length, npde, 4) && cmp_le32(rom + npde) == 0x4544504e) {
			if (cmp_in_range(length, npde, 10))
				return -EINVAL;
			ext_rev = cmp_le16(rom + npde + 4);
			ext_len = cmp_le16(rom + npde + 6);
			if (ext_rev == 0x100 || ext_rev == 0x101) {
				u32 sub = (u32)cmp_le16(rom + npde + 8) * 512;

				if (ext_len < 10 || cmp_in_range(length, npde, ext_len))
					return -EINVAL;
				if (ext_len > 10)
					last = rom[npde + 10] & 0x80;
				else if (sub < bytes)
					last = 0;
				bytes = sub;
			}
		}
		if (!bytes || cmp_in_range(length, pos, bytes))
			return -EINVAL;
		if (!base && rom[pcir + 0x14] == 0)
			base = bytes;
		if (!ext && rom[pcir + 0x14] == 0xe0)
			ext = pos;
		if (last)
			break;
		pos += bytes;
	}
	if (ext && base && ext < base)
		return -EINVAL;
	*adjust = ext && base ? ext - base : 0;
	return 0;
}

/* The BIT table inside the legacy image, and the 'B' and 'P' tokens it holds:
 * 'B' is the BIOS information block whose four bytes are the VBIOS version, 'P'
 * the data image whose first word points at the perf table.  The BIT header
 * checksums over itself, so a misaligned find is caught rather than believed.
 * Everything it learns on the way is recorded in `out`, because the offsets are
 * what a reader checks when the walk finds something unexpected. */
static int cmp_perf_image(const u8 *rom, size_t length, struct cmp_perf_table *out)
{
	u32 image, image_base, adjust, pcir, bit, bit_found = 0, legacy, bios = 0, in = 0;
	bool found = false;
	int ret;

	if (!rom || length > CMP_HBM_PROM_SIZE)
		return -EINVAL;
	ret = cmp_pci_image(rom, length, &image);
	if (ret)
		return ret;
	/* Every offset below is relative to the PCI image, which is where the
	 * container's own header stopped being interesting. */
	image_base = image;
	rom += image;
	length -= image;
	ret = cmp_expansion(rom, length, &adjust);
	if (ret || cmp_in_range(length, 0, 0x1a))
		return ret ? ret : -EINVAL;
	pcir = cmp_le16(rom + 0x18);
	if (cmp_in_range(length, pcir, 0x16) || cmp_le16(rom) != 0xaa55 ||
	    cmp_le32(rom + pcir) != 0x52494350 || cmp_le16(rom + pcir + 4) != 0x10de ||
	    rom[pcir + 0x14] != 0)
		return -EOPNOTSUPP;
	if (!cmp_le16(rom + pcir + 0x10) ||
	    cmp_in_range(length, 0, (size_t)cmp_le16(rom + pcir + 0x10) * 512))
		return -EINVAL;
	legacy = (u32)cmp_le16(rom + pcir + 0x10) * 512;
	for (bit = 0; bit + 12 <= legacy; bit++) {
		u32 header, stride, count, sum = 0, i, b = 0, d = 0;

		if (cmp_le16(rom + bit) != 0xb8ff || cmp_le32(rom + bit + 2) != 0x00544942)
			continue;
		header = rom[bit + 8];
		stride = rom[bit + 9];
		count = rom[bit + 10];
		if (cmp_le16(rom + bit + 6) != 0x100 || header < 12 ||
		    (stride != 6 && stride != 8) || cmp_in_range(legacy, bit, header) ||
		    cmp_in_range(legacy, (u64)bit + header, (u64)count * stride))
			return -EBADMSG;
		for (i = 0; i < header; i++)
			sum += rom[bit + i];
		if (sum & 0xff)
			return -EBADMSG;
		for (i = 0; i < count; i++) {
			/* One entry of the BIT table: a two-character tag, the size of
			 * the block it points at, and that pointer.  Only 'B' and 'P'
			 * matter here, and an entry that is neither is somebody else's. */
			const u8 *tag = rom + bit + header + i * stride;
			u32 ptr = stride == 8 ? cmp_le32(tag + 4) : cmp_le16(tag + 4);

			if (tag[0] != 'B' && tag[0] != 'P')
				continue;
			if (tag[1] != 2 || cmp_in_range(legacy, ptr, cmp_le16(tag + 2)) ||
			    !ptr)
				return -EOPNOTSUPP;
			if (tag[0] == 'B') {
				if (b || cmp_le16(tag + 2) < 5)
					return -EBADMSG;
				b = ptr;
			} else {
				/* A 'P' image shorter than its own header cannot point
				 * at a perf table, which is what the 4 is. */
				if (d || cmp_le16(tag + 2) < 4)
					return -EBADMSG;
				d = ptr;
			}
		}
		if (!b || !d || found)
			return -EBADMSG;
		bios = b;
		in = d;
		bit_found = bit;
		found = true;
	}
	if (!found)
		return -ENOENT;
	/* Zero is a wildcard in NVIDIA's version guards, so an image whose version
	 * is zero is not an image whose version can be trusted. */
	if (!cmp_le32(rom + bios) || !rom[bios + 4])
		return -EOPNOTSUPP;
	out->version[0] = rom[bios + 3];
	out->version[1] = rom[bios + 2];
	out->version[2] = rom[bios + 1];
	out->version[3] = rom[bios];
	out->version[4] = rom[bios + 4];
	/* Kept unadjusted on purpose: the table pointer is read from the data image
	 * where it sits, and `adjust` belongs to the offset it points at. */
	out->image = image_base;
	out->bit = bit_found;
	out->data = in;
	out->adjust = adjust;
	return 0;
}

static const char * const cmp_perf_domain_names[CMP_PERF_DOMAINS] = {
	"GPC", "XBAR", "d2", "SYS", "d4", "NVD", "d6", "d7", "d8", "HOST",
};

static const char *cmp_perf_id(u8 id)
{
	switch (id) {
	case CMP_PERF_ID_P0: return "P0";
	case CMP_PERF_ID_P8: return "P8";
	case CMP_PERF_ROW_EMPTY: return "free";
	default: return "other";
	}
}

static int cmp_perf_table_read(const u8 *rom, size_t length, struct cmp_perf_table *out)
{
	u32 offset, entry, i, j;
	int ret;

	memset(out, 0, sizeof(*out));
	out->p0 = out->p8 = -1;
	ret = cmp_perf_image(rom, length, out);
	if (ret)
		return ret;
	/* Every offset cmp_perf_image() recorded is relative to the PCI image, the
	 * same base it used while walking: move to it before trusting any of them. */
	rom += out->image;
	length -= out->image;
	/* The first word of the data image points at the table.  That pointer is
	 * relative to the legacy image, and `adjust`, the distance to the NPDS
	 * extension, belongs to where it points and not to where the pointer is read
	 * from: getting that backwards is how a decoder ends up describing bytes that
	 * are not a table at all. */
	if (cmp_in_range(length, out->data, 4))
		return -EINVAL;
	offset = cmp_le32(rom + out->data);
	if (!offset || offset > ~(u32)0 - out->adjust)
		return -EINVAL;
	offset += out->adjust;
	if (cmp_in_range(length, offset, CMP_PERF_HEADER))
		return -EINVAL;
	out->table = offset;
	out->format = rom[offset];
	out->header = rom[offset + 1];
	out->count = rom[offset + 3];
	/* The five header bytes upstream refuses to edit a table without: a table
	 * that is not this layout is a table whose rows are not 65 bytes, and
	 * reading rows of the wrong width produces numbers that look real. */
	if (rom[offset] != CMP_PERF_FORMAT || rom[offset + 1] != CMP_PERF_HEADER ||
	    rom[offset + 2] != CMP_PERF_DOMAIN_COUNT || !rom[offset + 3] ||
	    rom[offset + 4] != 6 || rom[offset + 5] != 10)
		return -EOPNOTSUPP;
	if (rom[offset + 3] > CMP_PERF_MAX_ROWS)
		return -E2BIG;
	if (cmp_in_range(length, (u64)offset + CMP_PERF_HEADER,
			 (u64)rom[offset + 3] * CMP_PERF_ROW_SIZE))
		return -EINVAL;
	out->rows = rom[offset + 3];
	out->found = true;
	for (i = 0; i < out->rows; i++) {
		struct cmp_perf_row *row = &out->row[i];

		entry = offset + CMP_PERF_HEADER + i * CMP_PERF_ROW_SIZE;
		row->id = rom[entry];
		memcpy(row->policy, rom + entry + 1, sizeof(row->policy));
		for (j = 0; j < CMP_PERF_DOMAINS; j++)
			row->limits[j] = cmp_le32(rom + entry + 5 + j * 6 + 2);
		if (row->id == CMP_PERF_ROW_EMPTY) {
			out->free_slots++;
			/* Upstream will only put a generated P8 in the first row, because
			 * RM's own idle limit is inserted at level zero and a row after an
			 * existing state would reorder the factory entries. */
			if (out->free_slots == 1 && i == 0 && out->p0 < 0)
				out->refusal = -EOPNOTSUPP;
		} else {
			out->states++;
		}
		if (row->id == CMP_PERF_ID_P0 && out->p0 < 0)
			out->p0 = i;
		else if (row->id == CMP_PERF_ID_P0)
			out->refusal = -EBADMSG;
		else if (row->id == CMP_PERF_ID_P8 && out->p8 < 0)
			out->p8 = i;
	}
	if (out->p8 >= 0)
		out->refusal = -EALREADY;
	else if (!out->p0 && out->refusal != -EOPNOTSUPP)
		out->refusal = -EOPNOTSUPP;
	else if (out->p0 >= 0 && memcmp(out->row[out->p0].policy, "\x08\x03\x00\x00", 4) &&
		 memcmp(out->row[out->p0].policy, "\x08\x03\xff\x00", 4))
		out->refusal = -EOPNOTSUPP;
	return 0;
}

static int cmp_perf_table_format(const struct cmp_perf_table *t, const char *bdf,
				 u32 memory_mhz, char *buf, size_t size)
{
	static const u8 wake_domains[CMP_PERF_DOMAIN_COUNT] = { 0, 1, 3, 5, 9 };
	unsigned int i, j;
	int len = 0;

	if (!t->found)
		return scnprintf(buf, size, "%s perf table: not found in the PROM image\n", bdf);

	len += scnprintf(buf + len, size - len,
			 "%s perf table 0x%02x: %u rows, %u states, %u free, vbios %02x.%02x.%02x.%02x.%02x\n",
			 bdf, t->format, t->rows, t->states, t->free_slots,
			 t->version[0], t->version[1], t->version[2], t->version[3],
			 t->version[4]);
	for (i = 0; i < t->rows; i++) {
		const struct cmp_perf_row *row = &t->row[i];

		if (row->id == CMP_PERF_ROW_EMPTY)
			continue;
		len += scnprintf(buf + len, size - len,
				 "  %s id=%#02x policy=%02x %02x %02x %02x floor MHz:",
				 cmp_perf_id(row->id), row->id, row->policy[0],
				 row->policy[1], row->policy[2], row->policy[3]);
		for (j = 0; j < CMP_PERF_DOMAINS; j++) {
			u32 floor = row->limits[j] & CMP_PERF_MIN_MASK;
			const char *name = cmp_perf_domain_names[j];

			/* The one domain whose floor is the memory clock the governor
			 * read back off the PLL is the HBM tuple, which is the row
			 * upstream leaves alone: naming it from a match rather than from
			 * an offset is the difference between a label and a guess. */
			if (memory_mhz && floor == memory_mhz)
				name = "HBM?";
			len += scnprintf(buf + len, size - len, " %s>=%u", name, floor);
		}
		len += scnprintf(buf + len, size - len, "\n");
	}
	/* The five domains whose floors a generated P8 would lower, named so that a
	 * reader can see whether any of them sits above the floor the card already
	 * runs at: that gap is the whole of what P8 could be worth. */
	len += scnprintf(buf + len, size - len,
			 "  a generated P8 would lower:");
	for (j = 0; j < CMP_PERF_DOMAIN_COUNT; j++)
		len += scnprintf(buf + len, size - len, " %s",
				 cmp_perf_domain_names[wake_domains[j]]);
	len += scnprintf(buf + len, size - len, " to 135 MHz, leaving the rest of the P0 row alone\n");
	switch (t->refusal) {
	case 0:
		len += scnprintf(buf + len, size - len,
				 "  P8 insert: possible on this image (first row free, one P0, known policy)\n");
		break;
	case -EALREADY:
		len += scnprintf(buf + len, size - len,
				 "  P8 insert: the board already provides a P8 row\n");
		break;
	case -EBADMSG:
		len += scnprintf(buf + len, size - len,
				 "  P8 insert: more than one P0 row, which no edit may reorder\n");
		break;
	default:
		len += scnprintf(buf + len, size - len,
				 "  P8 insert: not possible on this image (%d): needs a free first row, exactly one P0, and the policy bytes 08 03 00 00\n",
				 t->refusal);
		break;
	}
	return len;
}

static int cmp_perf_table_fill(struct cmp_hbm_worker *worker, struct cmp_perf_table *out)
{
	resource_size_t len = pci_resource_len(worker->pdev, 0);
	size_t size;
	u8 *rom;
	size_t off;
	int ret;

	if (len <= CMP_HBM_PROM_OFFSET)
		return -EIO;
	size = min_t(size_t, CMP_HBM_PROM_SIZE, (size_t)(len - CMP_HBM_PROM_OFFSET));
	rom = vmalloc(size);
	if (!rom)
		return -ENOMEM;
	/* Chunked, and yielding between chunks: the reads themselves are harmless,
	 * but a million of them without a break is a quarter second held here. */
	for (off = 0; off < size; off += CMP_HBM_PROM_CHUNK) {
		memcpy_fromio(rom + off, worker->bar + CMP_HBM_PROM_OFFSET + off,
			      min_t(size_t, CMP_HBM_PROM_CHUNK, size - off));
		cond_resched();
	}
	ret = cmp_perf_table_read(rom, size, out);
	vfree(rom);
	return ret;
}

static ssize_t perf_table_show(struct kobject *kobj, struct kobj_attribute *attr, char *buf)
{
	struct cmp_hbm_worker *worker;
	struct cmp_perf_table table;
	int len = 0;

	/* Held across the PROM copy, which is not what it is usually for: the list
	 * is what has to stay stable, and the poll work does not take this lock, so
	 * a read here costs a policy write a moment and costs the card nothing.  It
	 * is also the longest anything holds the list, which is what the status file's
	 * bound is sized against. */
	if (!workers_lock_get("perf table"))
		return scnprintf(buf, PAGE_SIZE,
				 "perf table unavailable: the worker list is being held by another read or a policy change; read it again\n");
	list_for_each_entry(worker, &workers, node) {
		int ret = cmp_perf_table_fill(worker, &table);

		if (ret)
			len += scnprintf(buf + len, PAGE_SIZE - len,
					 "%s perf table: unreadable (%d)\n",
					 pci_name(worker->pdev), ret);
		else
			len += cmp_perf_table_format(&table, pci_name(worker->pdev),
						     READ_ONCE(worker->stats.busy_mhz),
						     buf + len, PAGE_SIZE - len);
	}
	mutex_unlock(&workers_lock);
	return len;
}

static struct kobj_attribute perf_table_attr = __ATTR_RO(perf_table);

static ssize_t status_show(struct kobject *kobj, struct kobj_attribute *attr, char *buf)
{
	struct cmp_hbm_worker *worker;
	struct cmp_hbm_stats *s;
	u64 now = ktime_get_ns();
	int len = 0;

	if (!workers_lock_get("status"))
		return scnprintf(buf, PAGE_SIZE,
				 "status unavailable: the worker list is being held by a perf table read or a policy change; read it again\n");
	len += scnprintf(buf + len, PAGE_SIZE - len,
			 "mode=%s enable=%u idle_ms=%u poll_ms=%u lease_ms=%u idle_ndiv=%u idle_pldiv=%u idle_refresh=%u wake_events=%u\n",
			 mode_name(), enable, idle_ms, poll_ms, lease_ms, idle_ndiv,
			 idle_pldiv, idle_refresh, wake_events);
	list_for_each_entry(worker, &workers, node) {
		struct cmp_hbm *hbm = &worker->hbm;
		char deltas[40] = "", lease[96] = "", note[50] = "", flaps[16] = "";
		u64 written = READ_ONCE(worker->lease_written);
		unsigned int i, n = 0;

		/* Per worker, and the reason it is written here rather than at the top
		 * is the incident: the rewrite that added the counter deltas dropped
		 * this line, every use of `s` is a READ_ONCE() (which only ever takes an
		 * address, so the compiler has nothing to complain about), and the first
		 * read of this file on hardware spent the values of an uninitialised
		 * stack slot as pointers while holding the worker list. */
		s = &worker->stats;

		/* Every counter that was borrowed, with the largest movement it has
		 * ever reported in one sample.  These are the numbers that were
		 * missing when it mattered: they are what tells a reader that one of
		 * the two counters a card can borrow sees a workload and the other
		 * one does not. */
		for (i = 0; i < CMP_HBM_COUNTERS && n < sizeof(deltas) - 8; i++)
			if (hbm->counters & (1u << i))
				n += scnprintf(deltas + n, sizeof(deltas) - n, "%s%u",
					       n ? "/" : "", READ_ONCE(s->delta[i]));
		if (worker->lease_note[0])
			scnprintf(note, sizeof(note), " %s", worker->lease_note);
		if (!lease_ms)
			scnprintf(lease, sizeof(lease), "waived%s", note);
		else if (!written)
			scnprintf(lease, sizeof(lease), "none%s", note);
		else
			scnprintf(lease, sizeof(lease), "%llu ms %s%s%s%s",
				  (now > written ? now - written : 0) / 1000000,
				  READ_ONCE(worker->lease_forced) ? "forced " : "",
				  READ_ONCE(worker->lease_quiet) ? "quiet" : "busy",
				  now - written > (u64)lease_ms * 1000000 ? " stale" : "",
				  note);
		/* Only when it is not zero: a card that has never been put down and
		 * picked up again should read the way it always did, and a card that has
		 * should say how many times, because that is the difference between a
		 * feature that saves nothing and a card that is not idle. */
		if (READ_ONCE(s->flaps))
			scnprintf(flaps, sizeof(flaps), " flaps %u", READ_ONCE(s->flaps));
		len += scnprintf(buf + len, PAGE_SIZE - len,
				 "%s %u MHz busy, %u MHz idle target, %s%s%s%s, quiet %llu ms, counters %#x active %#x deltas %s, switches %llu declines %llu errors %llu%s, lease %s, fb %#x/%#x, %s\n",
				 pci_name(worker->pdev),
				 READ_ONCE(s->busy_mhz), READ_ONCE(s->idle_mhz),
				 READ_ONCE(s->pinned) ? "pinned" :
					READ_ONCE(s->idle) ? "idle" : "busy",
				 READ_ONCE(s->fb_stopped) ? " fb-stopped" : "",
				 READ_ONCE(s->halted) ? " halted" : "",
				 READ_ONCE(s->profile) && !READ_ONCE(s->baseline) ?
						" resyncing" : "",
				 READ_ONCE(s->quiet_ms), hbm->counters,
				 READ_ONCE(s->active_counters), deltas,
				 READ_ONCE(s->transitions),
				 READ_ONCE(s->declines), READ_ONCE(s->errors), flaps,
				 lease,
				 READ_ONCE(s->fb_req), READ_ONCE(s->fb_ack),
				 READ_ONCE(s->halted) && READ_ONCE(s->halt_reason) ?
						READ_ONCE(s->halt_reason) :
						READ_ONCE(s->reason) ?
							READ_ONCE(s->reason) : "not sampled");
	}
	mutex_unlock(&workers_lock);
	return len;
}

static struct kobj_attribute status_attr = __ATTR_RO(status);

/* One card's answer, written by whoever is allowed to know: `quiet` says nobody
 * is using this card, `busy` says somebody is, `force` says the operator has
 * taken the judgement away from the keeper.  The verb is deliberately tiny,
 * because everything this module cannot hear is a failure in the safe direction:
 * a keeper that is not running, that has crashed, that writes a card this module
 * has never heard of, or that writes a word it was not told to accept, all leave
 * the card on its busy clock.
 *
 * The note is what the keeper saw, echoed back into the status file.  It is not
 * checked, and nothing decides anything from it -- which is exactly why it can be
 * free text, and why the lease itself could not be. */
/* nvidia-smi and NVML's modern busId field spell the domain with eight digits
 * (`00000000:05:00.0`); pci_name() spells it with four.  Leading zeros in the
 * domain are therefore not part of the comparison, because refusing the other
 * correct spelling is a -ENODEV whose reason nobody can guess from a terminal --
 * and it is not hypothetical: the keeper shipped reading the padded form, every
 * write was refused, it exited, and the cards sat at the busy clock looking
 * exactly like a governor that had decided not to idle.  A domain the kernel
 * could not print is still nobody's card, and so is a tail that does not match.
 */
static bool lease_bdf_matches(const char *name, const char *bdf)
{
	const char *a = name, *b = bdf;

	while (a[0] == '0' && a[1] != '\0')
		a++;
	while (b[0] == '0' && b[1] != '\0')
		b++;
	if (!strcasecmp(a, b))
		return true;
	/* the domainless tail an operator may type by hand: `05:00.0` */
	return strlen(b) <= strlen(name) &&
	       !strcasecmp(name + strlen(name) - strlen(b), b);
}

static ssize_t lease_store(struct kobject *kobj, struct kobj_attribute *attr,
			   const char *buf, size_t count)
{
	char bdf[32], verdict[16], note[48];
	struct cmp_hbm_worker *worker;
	bool quiet = false, forced = false;
	int matched = 0, fields;

	fields = sscanf(buf, "%31s %15s %47[^\n]", bdf, verdict, note);
	if (fields < 2)
		return -EINVAL;
	if (fields < 3)
		note[0] = '\0';
	if (!strcmp(verdict, "quiet")) {
		quiet = true;
	} else if (!strcmp(verdict, "force")) {
		quiet = forced = true;
	} else if (strcmp(verdict, "busy")) {
		return -EINVAL;
	}
	/* -EBUSY rather than a wait: the keeper runs again in a second and a half, and
	 * an answer it writes late is still an answer, while a task of the keeper's
	 * stuck here would stop every card being fed. */
	if (!workers_lock_get("lease"))
		return -EBUSY;
	list_for_each_entry(worker, &workers, node) {
		const char *name = pci_name(worker->pdev);
		u64 stamp = ktime_get_ns();

		if (strcmp(bdf, "all") && !lease_bdf_matches(name, bdf))
			continue;
		/* Counting how long the same answer has been given is what makes an
		 * answer mean something: see lease_allows(). */
		if (READ_ONCE(worker->lease_quiet) == quiet) {
			if (worker->lease_streak < UINT_MAX)
				worker->lease_streak++;
		} else {
			worker->lease_streak = 1;
			WRITE_ONCE(worker->lease_quiet_since, quiet ? stamp : 0);
		}
		if (!quiet)
			WRITE_ONCE(worker->lease_quiet_since, 0);
		/* Content first, timestamp last, published with release semantics:
		 * a reader that sees the new timestamp has to see the new verdict
		 * with it, and a reader that does not has to treat the lease as the
		 * stale thing it still is. */
		WRITE_ONCE(worker->lease_quiet, quiet);
		WRITE_ONCE(worker->lease_forced, forced);
		scnprintf(worker->lease_note, sizeof(worker->lease_note), "%s", note);
		smp_store_release(&worker->lease_written, stamp);
		WRITE_ONCE(worker->lease_writes, worker->lease_writes + 1);
		matched++;
	}
	mutex_unlock(&workers_lock);
	if (!matched)
		return -ENODEV;
	return count;
}

static struct kobj_attribute lease_attr = __ATTR_WO(lease);

static bool supported(u16 device)
{
	return device == 0x20c2 || device == 0x2082;
}

static int __init cmp_hbm_pm_init(void)
{
	struct pci_dev *pdev, *next = NULL;
	unsigned int found = 0, failed = 0;

	if (!idle_ms || !poll_ms || !idle_ndiv || idle_ndiv > 0xff ||
	    !idle_pldiv || idle_pldiv > 0x3f || !idle_refresh || idle_refresh > 0xff ||
	    wake_events > CMP_HBM_MAX_WAKE_EVENTS)
		return -EINVAL;
	/* Loud, once, at load: with the lease waived the only thing left between a
	 * card and a lost framebuffer is a pair of counters that have been measured
	 * failing at it.  Legal for a bench, and not a way to run a machine. */
	if (!lease_ms)
		pr_warn("cmp_hbm_pm: lease_ms=0 waives the evidence that nobody is using the card: the borrowed counters are all that is left, and on a card serving requests they read zero for seconds while it drew 178 W\n");

	for (pdev = pci_get_device(PCI_VENDOR_ID_NVIDIA, PCI_ANY_ID, NULL);
	     pdev; pdev = next) {
		/* pci_get_device() releases the reference it was handed and takes
		 * one on what it returns, so this call is where the current
		 * iteration's reference is paid back: the loop's reference is
		 * always the one in pdev, and worker_start() takes its own. */
		next = pci_get_device(PCI_VENDOR_ID_NVIDIA, PCI_ANY_ID, pdev);
		if (supported(pdev->device)) {
			struct cmp_hbm_worker *worker = worker_start(pdev);

			if (IS_ERR(worker)) {
				pci_warn(pdev, "cmp_hbm_pm: governor registration failed (%ld)\n",
					 PTR_ERR(worker));
				failed++;
			} else {
				found++;
			}
		}
	}
	if (!found) {
		pr_info("cmp_hbm_pm: no CMP 170HX to govern%s\n",
			failed ? "; every supported card refused the governor" : "");
		return -ENODEV;
	}
	status_dir = kobject_create_and_add("cmp_hbm_pm", kernel_kobj);
	if (!status_dir) {
		pr_warn("cmp_hbm_pm: status file unavailable\n");
	} else {
		status_file = !sysfs_create_file(status_dir, &status_attr.attr);
		if (!status_file)
			pr_warn("cmp_hbm_pm: cannot create status file\n");
		/* The perf table is a second file rather than more lines in the first:
		 * reading it copies a megabyte of the PROM window out of the card, and
		 * nobody should pay that to ask what clock a card is at. */
		perf_table_file = !sysfs_create_file(status_dir, &perf_table_attr.attr);
		if (!perf_table_file)
			pr_warn("cmp_hbm_pm: perf table file unavailable\n");
		/* Write-only on purpose: what the keeper said is printed by the status
		 * file, where it sits next to the state the governor reached with it.
		 * A second file holding the same words could disagree with the first. */
		lease_file = !sysfs_create_file(status_dir, &lease_attr.attr);
		if (!lease_file)
			pr_warn("cmp_hbm_pm: lease file unavailable, no card will go idle\n");
	}
	return 0;
}

static void __exit cmp_hbm_pm_exit(void)
{
	struct cmp_hbm_worker *worker, *next;

	if (status_dir) {
		if (perf_table_file)
			sysfs_remove_file(status_dir, &perf_table_attr.attr);
		if (lease_file)
			sysfs_remove_file(status_dir, &lease_attr.attr);
		if (status_file)
			sysfs_remove_file(status_dir, &status_attr.attr);
		kobject_put(status_dir);
		status_dir = NULL;
	}
	list_for_each_entry_safe(worker, next, &workers, node)
		worker_stop(worker);
}

module_init(cmp_hbm_pm_init);
module_exit(cmp_hbm_pm_exit);
MODULE_DESCRIPTION("HBM idle power governor for unlocked NVIDIA CMP 170HX");
MODULE_LICENSE("GPL");
