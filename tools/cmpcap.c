/* SPDX-License-Identifier: GPL-2.0 */
/*
 * cmpcap: what actually decides the size of BAR1 on a CMP 170HX.
 *
 * The card advertises sizes up to 64 GiB and we want to know whether that top notch
 * is a strap we can lift or a wall.  Two separate registers are involved, and the
 * difference matters:
 *
 *   supported (config pos+0x0c, XVE BAR0+0x88bbc)  a bitmap of allowed sizes, bit b
 *       meaning 2^(b+16) bytes.  The kernel's pci_rebar_get_possible_sizes() reads it
 *       (shifted right by four) and nv_resize_pcie_bars() then asks for fls(sizes)-1,
 *       so this is what limits a resize done the honest way.  Writes here have always
 *       been refused, from both config space and MMIO.
 *
 *   control   (config pos+0x10, XVE BAR0+0x88bc0)  the selected size in bits 12:8, as
 *       an index meaning 2^(i+20) bytes, plus the BAR index it belongs to in bits 2:0.
 *       Nothing writes this except pci_rebar_set_size(), which refuses any value the
 *       bitmap above does not advertise.  So the selected size has never been asked
 *       for on its own -- and the device decodes more than the bitmap lists, which is
 *       the reason to ask.
 *
 * Programming a selected size larger than the host believes the BAR is leaves the
 * device decoding an aperture the kernel has not allocated, so that pass insists no
 * driver is attached; run it with the cards unbound.  It saves and restores the BAR
 * address around the probe the way the kernel's own sizing code does.
 *
 * Nothing here survives a reload: init runs once, prints, puts everything back and
 * returns -EBUSY so the module unloads itself with its answers in dmesg.
 *
 *     insmod cmpcap.ko                              # what does the card advertise
 *     insmod cmpcap.ko bdf=0000:05:00.0 select_index=17   # 17 == 128 GiB
 */
#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/init.h>
#include <linux/pci.h>
#include <linux/io.h>
#include <linux/sizes.h>

#define XVE_BASE	0x88000u
#define XVE_CYA_UNLOCK	0x724u		/* the unlock our bar1-resize patch writes */
#define XVE_CFG		0xDCCu
#define XVE_REBAR_SUP	0xbbcu		/* == config pos+0x0c for BAR1 */
#define XVE_REBAR_CTL	0xbc0u		/* == config pos+0x10 for BAR1 */

/* One Resizable BAR entry.  The spec and this card disagree about how the entries are
 * numbered -- the kernel's own accessor asks for BAR n at pos+0x04/+0x08+8n and that is
 * what produces the values the driver actually uses -- so the entry is found by asking
 * the device which BAR each one names and confirming it against the kernel's decode,
 * rather than by arithmetic. */
#define CTL_BAR_IDX	0x00000007u
#define  CTL_SIZE_SHIFT	8
#define  CTL_SIZE_MASK	0x00001F00u

/*
 * Where this card keeps BAR `bar_nr`.  Returns the config offset of its supported
 * dword, or 0 if no entry claims that BAR.  An entry is a (supported, control) pair;
 * the control word names the BAR it belongs to, and the supported word is the one the
 * kernel reports back through pci_rebar_get_possible_sizes(), so both halves of the
 * pair are checked before anything is written to them.
 */
static u32 find_entry(struct pci_dev *pdev, u32 pos, int bar_nr, const char *how)
{
	u32 want = pci_rebar_get_possible_sizes(pdev, bar_nr);
	int i;

	for (i = 0; i <= 0x20; i += 4) {
		u32 sup = 0, ctl = 0;

		pci_read_config_dword(pdev, pos + i, &sup);
		pci_read_config_dword(pdev, pos + i + 4, &ctl);
		if (want && (sup & 0xFFFFFFF0u) >> 4 != want)
			continue;
		if ((ctl & CTL_BAR_IDX) != (u32)bar_nr)
			continue;
		if ((ctl & CTL_SIZE_MASK) >> CTL_SIZE_SHIFT == 0)
			continue;                       /* nobody selected a size here */
		pr_info("cmpcap: BAR%d entry at config 0x%x (supported 0x%08x, control 0x%08x) via %s\n",
			bar_nr, pos + i, sup, ctl, how);
		return pos + i;
	}
	pr_err("cmpcap: no Resizable BAR entry claims BAR%d (kernel possible=0x%08x)\n",
	       bar_nr, want);
	return 0;
}

static char *bdf = "0000:05:00.0";
module_param(bdf, charp, 0444);
MODULE_PARM_DESC(bdf, "PCI address of the card to probe");

/* -1 only reads and probes the advertised list; otherwise a size index, 16 == 64 GiB. */
static int select_index = -1;
module_param(select_index, int, 0444);
MODULE_PARM_DESC(select_index, "program the control register's size index and re-probe the BAR");

static int bar = 1;
module_param(bar, int, 0444);
MODULE_PARM_DESC(bar, "which BAR to probe (default 1)");

static int unlock = 1;
module_param(unlock, int, 0444);
MODULE_PARM_DESC(unlock, "write the XVE unlock pair before touching the control register");

static void print_sizes(const char *tag, u32 bits)
{
	char buf[256];
	int n = 0, i;

	for (i = 0; i < 32; i++) {
		u64 bytes = 1ULL << (i + 16);          /* the supported dword's own bit */

		if (!(bits & BIT(i)))
			continue;
		if (bytes >= SZ_1G)
			n += scnprintf(buf + n, sizeof(buf) - n, "%lluG ", div_u64(bytes, SZ_1G));
		else
			n += scnprintf(buf + n, sizeof(buf) - n, "%lluM ", div_u64(bytes, SZ_1M));
	}
	pr_info("cmpcap: %s = %s\n", tag, n ? buf : "(none)");
}

/*
 * What the device decodes right now, read the way the kernel reads it at enumeration:
 * all-ones into the BAR, and back comes the mask.  Only safe with memory decoding off
 * and no driver attached.
 */
static u64 bar_decoded_size(struct pci_dev *pdev, int bar_nr)
{
	int at = 0x10 + 4 * bar_nr;
	u32 lo, lo_mask = 0, hi = 0, hi_mask = 0;
	u64 full, size;

	if (bar_nr < 1 || bar_nr > 5)
		return 0;

	pci_read_config_dword(pdev, at, &lo);
	pci_write_config_dword(pdev, at, ~0u);
	pci_read_config_dword(pdev, at, &lo_mask);
	pci_write_config_dword(pdev, at, lo);

	if ((lo & 0x6) == 0x4 && bar_nr < 5) {         /* 64-bit: the next BAR is its high half */
		pci_read_config_dword(pdev, at + 4, &hi);
		pci_write_config_dword(pdev, at + 4, ~0u);
		pci_read_config_dword(pdev, at + 4, &hi_mask);
		pci_write_config_dword(pdev, at + 4, hi);
	}

	full = ((u64)hi_mask << 32) | (lo_mask & 0xFFFFFFF0u);
	size = ~full + 1;
	return size >= SZ_1M ? size & ~(SZ_1M - 1) : 0;
}

static int __init cmpcap_init(void)
{
	struct pci_dev *pdev;
	void __iomem *pcfg;
	unsigned int dom, bus, dev, func, pos;
	unsigned short cmd, cmd_after;
	u32 sup, ctl, ctl_after, back, entry[5];
	u32 k_sizes, want_ctl, sup_at, ctl_at, xve_sup, xve_ctl;
	int i, top, decoded_index;
	u64 decoded;

	if (sscanf(bdf, "%x:%x:%x.%d", &dom, &bus, &dev, &func) != 4)
		return -EINVAL;
	pdev = pci_get_domain_bus_and_slot(dom, bus, PCI_DEVFN(dev, func));
	if (!pdev) {
		pr_err("cmpcap: %s not present\n", bdf);
		return -ENODEV;
	}

	pos = pci_find_ext_capability(pdev, PCI_EXT_CAP_ID_REBAR);
	if (!pos) {
		pr_err("cmpcap: %s has no Resizable BAR capability\n", bdf);
		pci_dev_put(pdev);
		return -ENODEV;
	}

	if (select_index >= 0 && pdev->driver) {
		pr_err("cmpcap: %s is bound to %s; unbind it before selecting a size\n",
		       bdf, dev_driver_string(&pdev->dev));
		pci_dev_put(pdev);
		return -EBUSY;
	}

	for (i = 0; i < (int)ARRAY_SIZE(entry); i++)
		pci_read_config_dword(pdev, pos + 0x04 + 4 * i, &entry[i]);
	pr_info("cmpcap: %s rebar ecap=0x%x dwords from +0x04: %*phC\n", bdf, pos,
		(int)sizeof(entry), (u8 *)entry);

	k_sizes = pci_rebar_get_possible_sizes(pdev, bar);
	top = fls(k_sizes) - 1;
	sup_at = find_entry(pdev, pos, bar, "BAR_IDX plus the kernel's decode");
	if (!sup_at) {
		pci_dev_put(pdev);
		return -ENODEV;
	}
	ctl_at = sup_at + 4;
	/* The XVE block behind BAR0 mirrors this config range at a fixed displacement,
	 * which is how 0xbbc and BAR0+0x88bbc have always been the same dword. */
	xve_sup = sup_at + XVE_BASE;
	xve_ctl = ctl_at + XVE_BASE;

	pci_read_config_dword(pdev, sup_at, &sup);
	pci_read_config_dword(pdev, ctl_at, &ctl);
	print_sizes("supported", sup);
	pr_info("cmpcap: kernel's possible=0x%08x -> largest %llu MiB at index %d; control selects index %d => %s\n",
		k_sizes, k_sizes ? (u64)(1ULL << (top + 20)) >> 20 : 0ULL, top,
		(int)((ctl & CTL_SIZE_MASK) >> CTL_SIZE_SHIFT),
		top == (int)((ctl & CTL_SIZE_MASK) >> CTL_SIZE_SHIFT) ?
		"the driver's resize is already at the top of the list" : "resize pending");

	if (select_index < 0) {
		/*
		 * The advertised-bitmap question, which has always come back the same:
		 * one notch above the top, through both write paths.
		 */
		void __iomem *xve = pci_iomap(pdev, 0, 0);

		if (xve) {
			u32 before = readl(xve + xve_sup);
			u32 probe = before | BIT(fls(before));

			writel(probe, xve + xve_sup);
			pr_info("cmpcap: MMIO write of 0x%08x (one notch above the top) -> 0x%08x %s\n",
				probe, readl(xve + xve_sup),
				readl(xve + xve_sup) == probe ? "ACCEPTED" : "REFUSED");
			writel(before, xve + xve_sup);
			pci_iounmap(pdev, xve);
		} else {
			pr_err("cmpcap: cannot map BAR0, the MMIO question goes unanswered\n");
		}
		pci_dev_put(pdev);
		return -EBUSY;
	}

	/*
	 * The selected-size question.  Memory decoding goes off first: the aperture is
	 * about to disagree with whatever the host has allocated, and a request from a
	 * half-configured BAR is how a card stops answering the next read.
	 */
	pci_read_config_word(pdev, PCI_COMMAND, &cmd);
	pci_write_config_word(pdev, PCI_COMMAND,
			      cmd & ~(unsigned short)(PCI_COMMAND_MEMORY | PCI_COMMAND_IO));

	pcfg = pci_iomap(pdev, 0, 0);
	if (pcfg && unlock) {
		writel(0x30, pcfg + XVE_BASE + XVE_CYA_UNLOCK);
		writel(0x8000000A, pcfg + XVE_BASE + XVE_CFG);
	}

	want_ctl = (ctl & ~CTL_SIZE_MASK) | ((u32)select_index << CTL_SIZE_SHIFT);
	decoded = bar_decoded_size(pdev, bar);
	pr_info("cmpcap: BAR%d decodes %llu MiB before; asking for index %d (%llu MiB): control 0x%08x -> 0x%08x\n",
		bar, decoded >> 20, select_index, (u64)(1ULL << (select_index + 20)) >> 20,
		ctl, want_ctl);

	/* Config space first: the path the kernel itself would have taken. */
	pci_write_config_dword(pdev, ctl_at, want_ctl);
	pci_read_config_dword(pdev, ctl_at, &ctl_after);
	pr_info("cmpcap: config write -> control reads 0x%08x (idx %d) %s\n", ctl_after,
		(ctl_after & CTL_SIZE_MASK) >> CTL_SIZE_SHIFT,
		ctl_after == want_ctl ? "ACCEPTED" : "REFUSED");

	if (((ctl_after & CTL_SIZE_MASK) >> CTL_SIZE_SHIFT) != select_index && pcfg) {
		writel(want_ctl, pcfg + xve_ctl);
		pci_read_config_dword(pdev, ctl_at, &ctl_after);
		pr_info("cmpcap: MMIO write -> XVE 0x%x reads 0x%08x, config reads 0x%08x (idx %d) %s\n",
			xve_ctl, readl(pcfg + xve_ctl), ctl_after,
			(ctl_after & CTL_SIZE_MASK) >> CTL_SIZE_SHIFT,
			((ctl_after & CTL_SIZE_MASK) >> CTL_SIZE_SHIFT) == select_index ?
			"ACCEPTED" : "REFUSED");
	}

	decoded = bar_decoded_size(pdev, bar);
	decoded_index = decoded ? order_base_2(decoded) - 20 : -1;
	pr_info("cmpcap: BAR%d now decodes %llu MiB (index %d)%s\n", bar, decoded >> 20,
		decoded_index,
		decoded_index == select_index ? " <-- the device decodes it" :
		" <-- still the old aperture");

	/* Put back exactly what was found, in the order that keeps the BAR valid. */
	pci_write_config_dword(pdev, ctl_at, ctl);
	pci_read_config_dword(pdev, ctl_at, &back);
	if (pcfg)
		writel(ctl, pcfg + xve_ctl);
	pci_write_config_word(pdev, PCI_COMMAND, cmd);
	pci_read_config_word(pdev, PCI_COMMAND, &cmd_after);
	pr_info("cmpcap: restored control=0x%08x (%s), command=0x%04x (%s), BAR%d decodes %llu MiB\n",
		back, back == ctl ? "ok" : "MISMATCH", cmd_after,
		cmd_after == cmd ? "ok" : "MISMATCH", bar,
		bar_decoded_size(pdev, bar) >> 20);

	if (pcfg)
		pci_iounmap(pdev, pcfg);
	pci_dev_put(pdev);
	return -EBUSY;		/* ran once; let it unload */
}

static void __exit cmpcap_exit(void)
{
}

module_init(cmpcap_init);
module_exit(cmpcap_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("One-shot probe of the CMP 170HX Resizable BAR supported list and selected size");
