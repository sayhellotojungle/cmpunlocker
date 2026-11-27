
/* ----------------------------------------------------------------------- *
 * Gen4 bring-up, for a card whose PCIe speed fuses have been cleared.
 *
 * The CMP 170HX VBIOS suppresses the Gen4 PHY-init block that the A100 DevInit
 * runs, so programming LINK_CONFIG alone leaves the PHY uninitialised and the
 * retrain lands back on Gen2; and a link sitting at 5 GT/s cannot be retrained
 * straight to 16 GT/s (measured LnkSta 0x1102).  So this replays the A100 flow
 * at driver post-init, over BAR0 and PCIe config space only:
 *
 *   Phase A  Gen3 step  - rate field to Gen3, LTSSM through Recovery, so the
 *                         PHY is running and the link is above 5 GT/s.
 *   Phase B  E1 replay  - the PHY-init block the VBIOS skipped: CYA, CE0/CE4
 *                         equalisation presets, PL link rate, rate field to the
 *                         target, then the "perform equalization" request on
 *                         both ends of the link.
 *   Phase C  retrain    - LnkCtl2 target speed on both ends plus Retrain Link,
 *                         through config space, because RM reports
 *                         NV_ERR_NOT_SUPPORTED for a speed its SKU says the GPU
 *                         cannot do.
 *
 * Encoding gotcha: LINK_CONFIG_0[19:18] is REVERSE encoded, 0=Gen4 1=Gen3
 * 2=Gen2 3=Gen1, while LNKCTL2 target speed and PL_LINK_RATE[19:16] are FORWARD
 * encoded, 1=Gen1 .. 4=Gen4.  Writing 2 into the LINK_CONFIG field to "ask for
 * Gen4" therefore pins the link at Gen2, which is what an A100 register dump
 * looks like it says at a glance.
 *
 * Run once per boot, from driver post-init, with the link where the VBIOS left it.
 * A copy of this sequence at GSP bootstrap was tried: RM's own PCIe init rewrites
 * LINK_CONFIG_0 back to Gen2 afterwards, so nv.c entered with CYA_0 already holding
 * the E1 value (0x060711b2) and the rate field back at Gen2, and the pass that
 * mattered replayed nothing.  The verified bring-up logs its one pass starting from
 * the VBIOS state (CYA 0x060731b2) and ending at LnkSta 0x1104.
 * The Gen2 clamp the booter applies (XVE_OVR=0x6) is not in the way: this overwrites
 * it with 0x8 on the way through.
 *
 * The target is decided per card from the card's own PCIe speed fuses, which is
 * what LNKCAP2's Supported Link Speeds Vector reports, so a soft-unlocked card in
 * the same box stays on the Gen2 path that pcie-gen2.patch set up.  Measured on the
 * bench, live read of CAP_EXP+0x2c after the driver is up: the two soft-unlocked
 * cards report 0x00000006 (Gen1/Gen2 only), the burned card reports 0x0180001e
 * (Gen1-Gen4), and the slot above them advertises 16 GT/s (0x01801f1e), so the gate
 * separates the two kinds of card without a card table.  BAR0 0x000880a4 is the same
 * word and was not read directly: sysfs refuses to map resource0 while the driver
 * owns the BAR, and the config-space value is the one the rest of the link sees
 * anyway.
 *
 * A card that does not reach that target keeps whatever the link did train, down to
 * but not below Gen3, and only a link that cannot hold Gen3 goes back to the Gen2
 * recipe.  The target is the card's own ceiling, not the link's: on this bench the
 * burned card's fuses allow Gen4 while the bridge above it is a Gen3 link upstream,
 * so Gen3 is the steady state to aim the link at even when Phase C reports a miss.
 * Dragging a trained 8 GT/s link back to 5 GT/s because it is not 16 GT/s throws
 * away bandwidth for no safety.
 *
 * Every rung of that ladder is now asked of both ends and confirmed on both ends,
 * and that is the part a one-sided version gets away with on some boards and not on
 * others.  Measured on an EPYC server (card 41:00.0 on AMD root port 40:03.1, driver
 * 615.71.09): a pass that raised only the card's own rate fields left the link at
 * 5 GT/s, because nv_cmp170hx_retrain_gen2() has just written the port's LNKCTL2
 * target speed to Gen2 and an AMD root port honours what it is told; the E1 replay
 * then produced no equalisation edge, the direct 5 -> 16 GT/s request missed, and
 * the card stayed on Gen2 for the whole uptime.  What fixed it there was a companion
 * unit that reloaded the driver, and the one property that bought was not the reload
 * itself: the second pass started from a link already settled at 8 GT/s.  A
 * PEX880xx switch downstream port, the topology this bring-up was verified on, hides
 * the same one-sided step, which is why the miss never showed up before.
 *
 * So the ladder is walked as a ladder: ask both ends for Gen3, confirm 8 GT/s at
 * both ends, replay E1 over a PHY that is really running at 8 GT/s, then ask both
 * ends for the target.  A board that needs the first rung to be re-taken gets
 * cmp_pcie_retries more walks of the same sequence, with the equalisation edge
 * re-armed, which is what a second pass otherwise lacks.
 * ----------------------------------------------------------------------- */

#ifndef PCI_EXP_LNKCTL2_TLS
#define PCI_EXP_LNKCTL2_TLS         0x000f
#endif

/* The delta between the VBIOS state and the state A100's DevInit leaves: CYA_0
 * reads 0x060731b2 out of the VBIOS and 0x060711b2 after the PHY-init block ran.
 * Setting this back before a repeated Phase B is what gives that replay an edge to
 * produce; without it the second pass writes back the word it just read, which is
 * exactly how the GSP-bootstrap copy managed to do nothing. */
#define CMP_CYA0_PHY_INIT_EDGE    (1U << 13)

int cmp_pcie_gen = 2;
module_param(cmp_pcie_gen, int, 0644);
MODULE_PARM_DESC(cmp_pcie_gen,
                 "CMP 170HX target PCIe generation: 2 (default, as shipped), "
                 "0 = per card from its own fuses, 3 or 4 to ask for more");

int cmp_pcie_e1 = 1;
module_param(cmp_pcie_e1, int, 0644);
MODULE_PARM_DESC(cmp_pcie_e1,
                 "Replay the suppressed A100 Gen4 PHY-init block (E1) before retraining");

int cmp_pcie_rate = 0;
module_param(cmp_pcie_rate, int, 0644);
MODULE_PARM_DESC(cmp_pcie_rate,
                 "Override the LINK_CONFIG_0[19:18] rate field (0=Gen4 1=Gen3 2=Gen2 3=Gen1)");

int cmp_pcie_retries = 1;
module_param(cmp_pcie_retries, int, 0644);
MODULE_PARM_DESC(cmp_pcie_retries,
                 "Extra walks of the Gen3 -> E1 -> Gen4 ladder per driver init: hosts "
                 "whose root port needs the Gen3 rung re-taken use the default 1, "
                 "0 is the single pass this bring-up was first verified with");

/* Highest generation a device's own LNKCAP2 Supported Link Speeds Vector reports
 * (bits [7:1], one bit per generation from Gen1), 0 if it cannot be read.  On the
 * card this is what the PCIe speed fuses drive; on the port above it it is what the
 * board advertises, which is a separate ceiling and not one to trust blindly: a
 * BIOS that pins a port low states an intention, not a limit the hardware has. */
static int
nv_cmp170hx_advertised_gen(struct pci_dev *dev)
{
    NvU32 cap2 = 0;
    NvU32 vector;
    int gen;

    if (pcie_capability_read_dword(dev, PCI_EXP_LNKCAP2, &cap2) != 0)
    {
        return 0;
    }

    vector = (cap2 >> 1) & 0xfU;
    for (gen = 0; (gen < 4) && ((vector & (1U << gen)) != 0); gen++)
    {
    }

    return gen;
}

/* What the link runs at right now: the slower of the two ends' LnkSta speed fields,
 * because a link is exactly as fast as its slowest port.  0 if either end cannot be
 * read, -1 if the GPU's own config space is gone. *status and *partner_status carry
 * the raw words for the log. */
static int
nv_cmp170hx_link_speed(struct pci_dev *gpu, struct pci_dev *upstream,
                       u16 *status, u16 *partner_status)
{
    u16 speed;

    *status = 0;
    *partner_status = 0;

    if (pcie_capability_read_word(gpu, PCI_EXP_LNKSTA, status) != 0)
    {
        *status = 0;
        return -1;
    }
    speed = *status & PCI_EXP_LNKSTA_CLS;

    if (pcie_capability_read_word(upstream, PCI_EXP_LNKSTA, partner_status) == 0)
    {
        if ((*partner_status & PCI_EXP_LNKSTA_CLS) < speed)
        {
            speed = *partner_status & PCI_EXP_LNKSTA_CLS;
        }
    }
    else
    {
        // A port that will not answer its own status word has not made the link
        // slower, and the GPU's answer is still the speed traffic runs at.
        *partner_status = *status;
    }

    return speed;
}

/* Ask for the 16 GT/s equalisation process to run on the next speed change. */
static void
nv_cmp170hx_request_equalization(struct pci_dev *dev)
{
    int pos = pci_find_ext_capability(dev, PCI_EXT_CAP_ID_SECPCI);

    if (pos != 0)
    {
        u32 lc3 = 0;
        pci_read_config_dword(dev, pos + 4, &lc3);
        pci_write_config_dword(dev, pos + 4, lc3 | 0x2U);
    }
}

/* Aim both ends of the link at gen and ask for a retrain, through config space,
 * because RM reports NV_ERR_NOT_SUPPORTED for a speed its SKU says the GPU cannot
 * do.  Returns what the slower of the two ends reports afterwards (1..4), 0 if the
 * link never reported at all, or -1 if config space could not be written at all;
 * *status and *partner_status are the raw LnkSta words either way. */
static int
nv_cmp170hx_link_retrain(struct pci_dev *gpu, struct pci_dev *upstream, NvU32 gen,
                         u16 *status, u16 *partner_status, int *err, int polls)
{
    u16 gpu_ctl2, upstream_ctl2, upstream_ctl;
    int attempt;
    int ret;
    int trained = 0;

    // Never report a speed read on an earlier pass as this one's result.
    trained = nv_cmp170hx_link_speed(gpu, upstream, status, partner_status);
    if (trained < 0)
    {
        *err = -1;
        return -1;
    }

    ret = pcie_capability_read_word(gpu, PCI_EXP_LNKCTL2, &gpu_ctl2);
    if (ret == 0)
    {
        ret = pcie_capability_read_word(upstream, PCI_EXP_LNKCTL2, &upstream_ctl2);
    }
    if (ret == 0)
    {
        // LNKCTL2 target speed is forward encoded, 1=Gen1 .. 4=Gen4, unlike the
        // reverse-encoded LINK_CONFIG_0 rate field.  Both ends, always: a speed the
        // partner is not asking for is a speed the link will not reach, and the port
        // above this card was pinned to Gen2 by the retrain two lines before this
        // sequence starts.
        gpu_ctl2      = (gpu_ctl2      & ~PCI_EXP_LNKCTL2_TLS) | (u16)gen;
        upstream_ctl2 = (upstream_ctl2 & ~PCI_EXP_LNKCTL2_TLS) | (u16)gen;
        ret = pcie_capability_write_word(gpu, PCI_EXP_LNKCTL2, gpu_ctl2);
        if (ret == 0)
        {
            ret = pcie_capability_write_word(upstream, PCI_EXP_LNKCTL2, upstream_ctl2);
        }
    }
    if (ret == 0)
    {
        ret = pcie_capability_read_word(upstream, PCI_EXP_LNKCTL, &upstream_ctl);
    }
    if (ret == 0)
    {
        ret = pcie_capability_write_word(upstream, PCI_EXP_LNKCTL,
                                         upstream_ctl | PCI_EXP_LNKCTL_RL);
    }
    if (ret != 0)
    {
        *err = ret;
        return -1;
    }

    for (attempt = 0; attempt < polls; attempt++)
    {
        msleep(50);
        trained = nv_cmp170hx_link_speed(gpu, upstream, status, partner_status);
        if (trained < 0)
        {
            continue;
        }
        if ((NvU32)trained >= gen)
        {
            break;
        }
    }

    return trained;
}

/* Phase A: the Gen3 step.  Nothing above Gen2 trains from 5 GT/s, so the card's own
 * rate fields go to Gen3 and the LTSSM is taken through Recovery; by the end of it
 * the PHY is running and the E1 block has something to work on. */
static void
nv_cmp170hx_gen3_step(struct pci_dev *gpu, void __iomem *bar0)
{
    u32 v, dlmgr;
    int i;

    v = readl(bar0 + 0x8841c);
    writel((v | ((1U << 11) | (1U << 13))) &
           ~((1U << 12) | (1U << 14) | 0xc0000000U), bar0 + 0x8841c);
    v = readl(bar0 + 0x8c2c0);
    writel(v & ~((1U << 2) | (1U << 3)), bar0 + 0x8c2c0);
    v = readl(bar0 + 0x880a8);
    writel((v & ~0xfU) | 3U, bar0 + 0x880a8);
    v = readl(bar0 + 0x8c1c0);
    writel((v & ~0x000f0000U) | (3U << 16), bar0 + 0x8c1c0);
    v = readl(bar0 + 0x88610);
    writel(v | 0x1000U, bar0 + 0x88610);
    v = readl(bar0 + 0x8c040);
    writel((v & ~0x001c0000U) | (1U << 18) | 0x00400000U, bar0 + 0x8c040);

    // Take the LTSSM through Recovery so the new rate takes effect, then hand
    // it back; DL_MGR and the LTSSM field are read back rather than trusted.
    dlmgr = readl(bar0 + 0x8b8c0);
    writel(dlmgr | 4U, bar0 + 0x8b8c0);
    v = 0xffffffffU;
    for (i = 0; i < 200; i++)
    {
        v = readl(bar0 + 0x8c040);
        if (((v & 0x1fU) == 0) || (v == 0xffffffffU))
        {
            break;
        }
        msleep(1);
    }
    if ((v != 0xffffffffU) && ((v & 0x1fU) == 0))
    {
        writel((v & ~0xfU) | 1U, bar0 + 0x8c040);
        for (i = 0; i < 200; i++)
        {
            v = readl(bar0 + 0x8c040);
            if (((v & 0x1fU) == 0) || (v == 0xffffffffU))
            {
                break;
            }
            msleep(1);
        }
    }
    if (v != 0xffffffffU)
    {
        writel(dlmgr, bar0 + 0x8b8c0);
    }
    if ((v != 0xffffffffU) && ((v & 0x1fU) == 0))
    {
        writel(0x8U, bar0 + 0x8872c);
    }
}

static void
nv_cmp170hx_gen4_post(nv_state_t *nv)
{
    nv_linux_state_t *nvl = NV_GET_NVL_FROM_NV_STATE(nv);
    struct pci_dev *gpu = nvl->pci_dev;
    struct pci_dev *upstream;
    void __iomem *bar0 = NULL;
    u16 link_status = 0, partner_status = 0;
    u32 v, rate_code, target, fused, partner;
    int ret = 0, attempt, tries, trained;

    if ((gpu == NULL) || (gpu->device != 0x20c2))
    {
        return;
    }

    fused = (u32)nv_cmp170hx_advertised_gen(gpu);
    target = (cmp_pcie_gen > 0) ? (u32)cmp_pcie_gen : fused;
    if (target > 4)
    {
        // LNKCTL2's target-speed field has no encoding above Gen4.
        target = 4;
    }

    if ((fused != 0) && (target > fused))
    {
        NV_DEV_PRINTF(NV_DBG_ERRORS, nv,
                      "CMP Gen%u: fuses only allow Gen%u; asking for Gen%u\n",
                      target, fused, fused);
        target = fused;
    }

    if (target <= 2)
    {
        // The common case, and a case worth one line of its own: a soft-unlocked
        // card, or a card whose speed vector could not be read at all, and this hook
        // changes nothing.  Without this the log is silent about the one reason a
        // card in a Gen4 slot can still be sitting at Gen2, and the reader is left
        // blaming the slot.
        NV_DEV_PRINTF(NV_DBG_INFO, nv,
                      "CMP: staying on Gen2 (fuses report Gen%u, asked for Gen%u)\n",
                      fused, (cmp_pcie_gen > 0) ? (u32)cmp_pcie_gen : 0U);
        return;
    }

    upstream = pci_upstream_bridge(gpu);
    if (upstream == NULL)
    {
        NV_DEV_PRINTF(NV_DBG_ERRORS, nv, "CMP Gen%u: no upstream PCIe bridge\n", target);
        return;
    }

    // What the port above the card advertises for itself.  Not a cap applied here: a
    // BIOS that pins a port states an intention, and the retrain is allowed to prove
    // it wrong once.  It does say whether walking the ladder twice can help, which is
    // the only thing it is used for.
    partner = (u32)nv_cmp170hx_advertised_gen(upstream);

    // LINK_CONFIG_0[19:18], reverse encoded (see above).
    rate_code = (cmp_pcie_rate > 0) ? (u32)cmp_pcie_rate : ((target >= 4) ? 0U : 1U);
    tries = 1 + ((cmp_pcie_retries > 0) ? cmp_pcie_retries : 0);

    bar0 = ioremap(pci_resource_start(gpu, 0), 0x90000);
    if (bar0 == NULL)
    {
        NV_DEV_PRINTF(NV_DBG_ERRORS, nv, "CMP Gen%u: cannot map BAR0\n", target);
        return;
    }

    NV_DEV_PRINTF(NV_DBG_ERRORS, nv,
                  "CMP Gen%u: start fuses=Gen%u port=Gen%u LC0=0x%08x CYA=0x%08x "
                  "PL=0x%08x CE4=0x%08x\n",
                  target, fused, partner, readl(bar0 + 0x8c040), readl(bar0 + 0x8c2c0),
                  readl(bar0 + 0x8c1c0), readl(bar0 + 0x88ce4));

    for (attempt = 1; attempt <= tries; attempt++)
    {
        if ((attempt > 1) && (cmp_pcie_e1 != 0))
        {
            // The previous walk left CYA_0 holding the A100 value, so replaying it
            // would write back the word that is already there.  Put the VBIOS bit
            // back first and the replay below is an edge again, not a restate.
            v = readl(bar0 + 0x8c2c0);
            writel(v | CMP_CYA0_PHY_INIT_EDGE, bar0 + 0x8c2c0);
            NV_DEV_PRINTF(NV_DBG_ERRORS, nv,
                          "CMP Gen%u: walk %d of %d, CYA=0x%08x\n",
                          target, attempt, tries, readl(bar0 + 0x8c2c0));
        }

        // ---- Phase A: the Gen3 step, asked of both ends of the link ----
        nv_cmp170hx_gen3_step(gpu, bar0);

        // Phase 16 GT/s needs "Perform Equalization" requested on both ends, and the
        // Gen3 retrain below may consume the request, so it goes back a second time
        // just before the retrain that needs it.
        nv_cmp170hx_request_equalization(gpu);
        nv_cmp170hx_request_equalization(upstream);
        msleep(50);

        trained = nv_cmp170hx_link_retrain(gpu, upstream, 3, &link_status,
                                           &partner_status, &ret, 20);
        if (trained < 3)
        {
            NV_DEV_PRINTF(NV_DBG_ERRORS, nv,
                          "CMP Gen%u: Gen3 rung not held (LnkSta=0x%04x "
                          "port LnkSta=0x%04x, port fuses Gen%u)\n",
                          target, link_status, partner_status, partner);
            if ((trained < 0) || ((partner != 0) && (partner < 3)))
            {
                // Config space is gone, or the board above the card does not do
                // 8 GT/s.  Neither gets better by asking twice.
                break;
            }
            continue;
        }

        if ((u32)trained >= target)
        {
            // A card asked for, or fused to, Gen3: the rung is the target, and the
            // E1 block below exists to make 16 GT/s trainable, so there is nothing
            // left to do here.
            NV_DEV_PRINTF(NV_DBG_ERRORS, nv,
                          "CMP Gen%u: link trained (LnkSta=0x%04x port LnkSta=0x%04x)\n",
                          target, link_status, partner_status);
            goto done;
        }

        // The link is at 8 GT/s and its partner agrees.  Let it sit there a moment:
        // what the boards that miss this feature need is a first rung that has
        // settled, not merely one that was requested.
        msleep(250);

        // ---- Phase B: the PHY-init block the VBIOS skips, values as the A100
        // DevInit leaves them, verified on the bench.
        if (cmp_pcie_e1 != 0)
        {
            writel(0x00000014U, bar0 + 0x88ce4);           // XP_EQ_CONFIG_4 presets
            v = readl(bar0 + 0x88ce0);
            writel((v & ~0x3fU) | 0x06U, bar0 + 0x88ce0);  // XP_EQ_CONFIG_0 presets
            writel(0x060711b2U, bar0 + 0x8c2c0);           // CYA_0, A100 value
            writel(0x00040036U, bar0 + 0x8c1c0);           // PL link rate, Gen4
            v = readl(bar0 + 0x8c040);
            writel((v & ~0x000c0000U) | (rate_code << 18), bar0 + 0x8c040);
            writel(0x00001001U, bar0 + 0x88610);
        }
        else
        {
            v = readl(bar0 + 0x8c040);
            writel((v & ~0x000c0000U) | (rate_code << 18), bar0 + 0x8c040);
        }

        NV_DEV_PRINTF(NV_DBG_ERRORS, nv,
                      "CMP Gen%u: XP programmed LC0=0x%08x CYA=0x%08x PL=0x%08x\n",
                      target, readl(bar0 + 0x8c040), readl(bar0 + 0x8c2c0),
                      readl(bar0 + 0x8c1c0));
        msleep(100);

        // Programming the PHY can drop the link out of Recovery, and the equalisation
        // the target speed needs only runs from 8 GT/s.  If the block just written
        // took the link back down, take the rung again before aiming above it.
        if (nv_cmp170hx_link_speed(gpu, upstream, &link_status, &partner_status) < 3)
        {
            trained = nv_cmp170hx_link_retrain(gpu, upstream, 3, &link_status,
                                               &partner_status, &ret, 20);
            if (trained < 3)
            {
                NV_DEV_PRINTF(NV_DBG_ERRORS, nv,
                              "CMP Gen%u: Gen3 lost after E1 (LnkSta=0x%04x "
                              "port LnkSta=0x%04x)\n", target, link_status,
                              partner_status);
                break;
            }
        }

        // ---- Phase C: retrain through config space; RM would refuse the speed ----
        nv_cmp170hx_request_equalization(gpu);
        nv_cmp170hx_request_equalization(upstream);
        trained = nv_cmp170hx_link_retrain(gpu, upstream, target, &link_status,
                                           &partner_status, &ret, 40);
        if (trained == (int)target)
        {
            NV_DEV_PRINTF(NV_DBG_ERRORS, nv,
                          "CMP Gen%u: link trained (LnkSta=0x%04x port LnkSta=0x%04x)\n",
                          target, link_status, partner_status);
            goto done;
        }
        if (trained < 0)
        {
            NV_DEV_PRINTF(NV_DBG_ERRORS, nv,
                          "CMP Gen%u: config space access failed (%d)\n", target, ret);
            break;
        }

        if (trained >= 3)
        {
            // The link trains, just not as high as this card's fuses allow, which is
            // the partner's doing and not something a retry can fix: fuses say what a
            // card may do, not what the slot it sits in will carry.  Keep the speed it
            // reached and aim both ends, and the card's own rate fields, at it, so a
            // later retrain does not chase a speed this link has already shown it
            // cannot hold.
            NV_DEV_PRINTF(NV_DBG_ERRORS, nv,
                          "CMP Gen%u: trained Gen%d not Gen%u (LnkSta=0x%04x port"
                          " LnkSta=0x%04x); the link above this card caps it,"
                          " staying on Gen%d\n",
                          target, trained, target, link_status, partner_status,
                          trained);
            v = readl(bar0 + 0x8c1c0);
            writel((v & ~0x000f0000U) | ((u32)trained << 16), bar0 + 0x8c1c0);
            v = readl(bar0 + 0x8c040);
            writel((v & ~0x000c0000U) | (((4U - (u32)trained) << 18) & 0x000c0000U),
                   bar0 + 0x8c040);
            trained = nv_cmp170hx_link_retrain(gpu, upstream, (u32)trained,
                                               &link_status, &partner_status, &ret, 20);
            if (trained >= 3)
            {
                NV_DEV_PRINTF(NV_DBG_ERRORS, nv,
                              "CMP Gen%d: settled (LnkSta=0x%04x port LnkSta=0x%04x)\n",
                              trained, link_status, partner_status);
                goto done;
            }
            // The link will not even hold the speed it just trained; nothing above
            // Gen2 is available on this board.
            break;
        }

        NV_DEV_PRINTF(NV_DBG_ERRORS, nv,
                      "CMP Gen%u: walk %d of %d reached nothing above Gen2"
                      " (LnkSta=0x%04x port LnkSta=0x%04x, port fuses Gen%u)\n",
                      target, attempt, tries, link_status, partner_status, partner);
    }

fallback:
    // Nothing above Gen2 trained, or config space could not be read at all: put the
    // card back on the link every CMP 170HX is known to work at, which is the one
    // our Gen2 recipe trains.
    NV_DEV_PRINTF(NV_DBG_ERRORS, nv,
                  "CMP Gen%u: nothing above Gen2 held (LnkSta=0x%04x), falling back to Gen2\n",
                  target, link_status);
    nv_cmp170hx_retrain_gen2(nv);

done:
    if (bar0 != NULL)
    {
        iounmap(bar0);
    }
}
