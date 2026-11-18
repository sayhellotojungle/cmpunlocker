/* cmpunlocker: dynamic CMP 170HX capacity and static-BAR1 policy.
 *
 * The framebuffer layout is read off the card (LMR partition count, CFG1
 * partition size) instead of a card table, and a framebuffer larger than BAR1 is
 * kept from taking the card down.  build.sh drops this file into
 * src/nvidia/inc/kernel/gpu/gsp/ and driver/dynfb/apply.py adds the hooks that
 * call it; see docs/CMP170HX-FUSE-DATA.zh-CN.md for the measurements behind the
 * fuse words.
 */
#ifndef __CMP_DYNAMIC_H__
#define __CMP_DYNAMIC_H__

/* CMP-DYN: per-card capacity policy, read from the card instead of a card table.
 *
 * NV_PFB_PRI_MMU_LOCAL_MEMORY_RANGE (LMR) is the fuse derived framebuffer layout
 * word, and it is readable before this driver writes it: [8:7] counts the FBP
 * partitions (0 -> 8, 1 -> 10, 2 -> 12, 3 -> 14).  CFG1 sizes one partition: the
 * [19:16] nibble is 4 for 1 GiB, 6 for 4 GiB and 7 for 8 GiB.  An untouched 8 GB
 * SKU reports eight partitions of one GiB; burning the framebuffer floorsweep
 * fuses reveals the hidden pair, and CFG1 0x02779000 with LMR 0x28b then declares
 * exactly what a real A100 80 GB declares, ten partitions of eight GiB.
 *
 * Two things are deliberately not the burn signal.  PCIe capability: a card can
 * have that fuse group cleared while the framebuffer group is untouched.  The
 * partition count on its own: a 10 GB SKU reports ten partitions with nothing
 * left to unlock, so the wide layout is offered to the 8 GB SKU only.  See
 * docs/CMP170HX-FUSE-DATA.zh-CN.md for the measurements behind this.
 */
#define CMP_DYN_8GB_DEV_ID       0x20C2U   /* the SKU with a hidden FBP pair      */
#define CMP_DYN_10GB_DEV_ID      0x2082U   /* ten partitions, nothing to unlock   */
#define CMP_DYN_CFG1_ADDR        0x009a0204U
#define CMP_DYN_LMR_ADDR         0x00100ce0U
#define CMP_DYN_LMR_PART_SHIFT   7U
#define CMP_DYN_LMR_PART_MASK    0x3U
#define CMP_DYN_CFG1_SIZE_SHIFT  16U
#define CMP_DYN_CFG1_SIZE_MIN    4U        /* one GiB per partition               */
#define CMP_DYN_CFG1_SIZE_MAX    7U        /* eight GiB per partition             */
#define CMP_DYN_80G_CFG1         0x02779000U  /* eight GiB per partition          */
#define CMP_DYN_64G_LMR          0x0000020BU  /* eight partitions of eight GiB    */
#define CMP_DYN_80G_LMR          0x0000028BU  /* ten partitions of eight GiB      */
#define CMP_DYN_MAX_GPU          32U

// Compiled in by default, asked for per card: build.sh rewrites this switch from
// CMPUNLOCKER_EXPERIMENTAL_80G and only then adds CMPEnable80G to RegistryDwords.
#define CMP_DYN_ENABLE_80G       1
#define CMP_DYN_REG_ENABLE_80G   "CMPEnable80G"
#define CMP_DYN_REG_STAGE        "CMPDynStage"
#define CMP_DYN_REG_DEBUG        "CMPDynDebug"

// Widest layout first; every step back is a layout some card is known to boot.
#define CMP_DYN_STAGE_80G        0U
#define CMP_DYN_STAGE_64G        1U
#define CMP_DYN_STAGE_NATIVE     2U
#define CMP_DYN_STAGE_LAST       CMP_DYN_STAGE_NATIVE

typedef struct
{
    NvU32 bSeen;         // the policy below has been evaluated for this GPU
    NvU32 bBurned;       // the card may be given the wide layout
    NvU32 stage;         // CMP_DYN_STAGE_* currently programmed
    NvU32 bTableDumped;  // the GSP registry table has been logged once
    NvU32 nativeCfg1;
    NvU32 nativeLmr;
} CmpDynGeomState;

static CmpDynGeomState _cmpDynGeom[CMP_DYN_MAX_GPU];

// Follows CMPDynDebug.  The dumps are diagnostic only and they read offsets that
// answer with a PPRIV error code, so they never run by default.
static NvU32 _cmpDynDebug = 0U;

static CmpDynGeomState *
_kgspCmpDynGeomState(OBJGPU *pGpu)
{
    if (pGpu->gpuInstance >= CMP_DYN_MAX_GPU)
    {
        return NULL;
    }

    return &_cmpDynGeom[pGpu->gpuInstance];
}

// Partition count from a layout word; zero when the word is not reachable yet.
static NvU32
_kgspCmpDynFbpCountWord(NvU32 lmr)
{
    if (lmr == 0xffffffffU)
    {
        return 0U;
    }

    return 8U + 2U * ((lmr >> CMP_DYN_LMR_PART_SHIFT) & CMP_DYN_LMR_PART_MASK);
}

static NvU32
_kgspCmpDynFbpCount(OBJGPU *pGpu)
{
    return _kgspCmpDynFbpCountWord(GPU_REG_RD32(pGpu, CMP_DYN_LMR_ADDR));
}

// One partition, from the word that is programmed right now rather than from a
// constant: the same helper then describes the native layout (one GiB per
// partition), the 64 GiB layout and the wide layout without knowing the stage.
static NvU32
_kgspCmpDynPartShiftWord(NvU32 cfg1)
{
    NvU32 size = (cfg1 >> CMP_DYN_CFG1_SIZE_SHIFT) & 0xfU;

    if ((size < CMP_DYN_CFG1_SIZE_MIN) || (size > CMP_DYN_CFG1_SIZE_MAX))
    {
        return 0U;
    }

    return size + 26U;  // 4 -> one GiB .. 7 -> eight GiB
}

static NvU64
_kgspCmpDynFbBytesPair(NvU32 cfg1, NvU32 lmr)
{
    NvU32 count = _kgspCmpDynFbpCountWord(lmr);
    NvU32 shift = _kgspCmpDynPartShiftWord(cfg1);

    if ((count == 0U) || (shift == 0U))
    {
        return 0ULL;
    }

    return (NvU64)count << shift;
}

static NvU64
_kgspCmpDynFbBytes(OBJGPU *pGpu)
{
    return _kgspCmpDynFbBytesPair(GPU_REG_RD32(pGpu, CMP_DYN_CFG1_ADDR),
                                  GPU_REG_RD32(pGpu, CMP_DYN_LMR_ADDR));
}

static const char *
_kgspCmpDynStageName(NvU32 stage)
{
    switch (stage)
    {
        case CMP_DYN_STAGE_80G:
            return "wide";
        case CMP_DYN_STAGE_64G:
            return "64g";
        default:
            return "native";
    }
}

static NvBool
_kgspCmpDynIsBurned(OBJGPU *pGpu)
{
#if CMP_DYN_ENABLE_80G
    // The devId test matters: the ten partitions of a 10 GB SKU look exactly like
    // the ten partitions of a burned 8 GB SKU, and only one of the two is spare.
    if ((pGpu->idInfo.PCIDeviceID >> 16) != CMP_DYN_8GB_DEV_ID)
    {
        return NV_FALSE;
    }

    return (_kgspCmpDynFbpCount(pGpu) > 8U) ? NV_TRUE : NV_FALSE;
#else
    (void)pGpu;
    return NV_FALSE;
#endif
}

static CmpDynGeomState *
_kgspCmpDynGeomEval(OBJGPU *pGpu)
{
    CmpDynGeomState *pState = _kgspCmpDynGeomState(pGpu);
    NvU32 value;

    if (pState == NULL)
    {
        return NULL;
    }

    if (pState->bSeen == 0U)
    {
        pState->bSeen = 1U;
        pState->stage = CMP_DYN_STAGE_80G;

        // Opt-in twice over: RegistryDwords asks for the wide layout, and a layout
        // an earlier attempt already gave up on stays given up on, so that a card
        // that cannot boot wide does not try again on every init.  Without the key
        // the card keeps the geometry it is known to boot from, which makes the
        // experiment a modprobe-config edit rather than a driver rebuild.
        if ((osReadRegistryDword(pGpu, CMP_DYN_REG_ENABLE_80G, &value) != NV_OK) ||
            (value == 0U))
        {
            pState->stage = CMP_DYN_STAGE_64G;
        }
        else if ((osReadRegistryDword(pGpu, CMP_DYN_REG_STAGE, &value) == NV_OK) &&
                 (value > pState->stage) && (value <= CMP_DYN_STAGE_LAST))
        {
            pState->stage = value;
        }

        _cmpDynDebug = ((osReadRegistryDword(pGpu, CMP_DYN_REG_DEBUG, &value) == NV_OK) &&
                        (value != 0U)) ? 1U : 0U;

        NV_PRINTF(LEVEL_ERROR,
                  "CMP-DYN POLICY GPU%u stage=%u(%s) debug=%u\n",
                  pGpu->gpuInstance, pState->stage,
                  _kgspCmpDynStageName(pState->stage), _cmpDynDebug);
    }

    // The fuse words are only reachable once BAR0 is live, and the call sites run
    // at different points of the boot, so keep taking them until one reads back
    // something, and never discard what was captured already.
    if (pState->bBurned == 0U)
    {
        pState->nativeCfg1 = GPU_REG_RD32(pGpu, CMP_DYN_CFG1_ADDR);
        pState->nativeLmr  = GPU_REG_RD32(pGpu, CMP_DYN_LMR_ADDR);

        if (_kgspCmpDynIsBurned(pGpu) != NV_FALSE)
        {
            pState->bBurned = 1U;
            NV_PRINTF(LEVEL_ERROR,
                      "CMP-DYN FUSES GPU%u CFG1=0x%08x LMR=0x%08x fb=0x%llx "
                      "partitions=%u\n",
                      pGpu->gpuInstance, pState->nativeCfg1, pState->nativeLmr,
                      (unsigned long long)_kgspCmpDynFbBytes(pGpu),
                      _kgspCmpDynFbpCount(pGpu));
        }
    }

    return pState;
}

static void
_kgspCmpDynGeomPair(const CmpDynGeomState *pState, NvU32 *pCfg1, NvU32 *pLmr)
{
    switch (pState->stage)
    {
        case CMP_DYN_STAGE_64G:
            *pCfg1 = CMP_DYN_80G_CFG1;
            *pLmr  = CMP_DYN_64G_LMR;
            break;
        case CMP_DYN_STAGE_NATIVE:
            *pCfg1 = pState->nativeCfg1;
            *pLmr  = pState->nativeLmr;
            break;
        default:
            *pCfg1 = CMP_DYN_80G_CFG1;
            *pLmr  = CMP_DYN_80G_LMR;
            break;
    }
}

static void
_kgspCmpDynGeomWrite(OBJGPU *pGpu, const CmpDynGeomState *pState, const char *pWhere)
{
    NvU32 cfg1Value;
    NvU32 lmrValue;

    _kgspCmpDynGeomPair(pState, &cfg1Value, &lmrValue);
    GPU_REG_WR32(pGpu, CMP_DYN_CFG1_ADDR, cfg1Value);
    GPU_REG_WR32(pGpu, CMP_DYN_LMR_ADDR, lmrValue);

    NV_PRINTF(LEVEL_ERROR,
              "CMP-DYN GEOM GPU%u %s stage=%u(%s) CFG1=0x%08x LMR=0x%08x fb=0x%llx\n",
              pGpu->gpuInstance, pWhere, pState->stage,
              _kgspCmpDynStageName(pState->stage), cfg1Value, lmrValue,
              (unsigned long long)_kgspCmpDynFbBytesPair(cfg1Value, lmrValue));
}

// Leftovers of the hunt for a 64 GiB cap: a set of candidate registers and a
// linear window over the MMU, FBPA and WPR blocks.  The answer was that there is
// no such field, so this only runs when someone asks for it.
static void
_kgspCmpDynDump(OBJGPU *pGpu, const char *pTag)
{
    static const NvU32 addrs[] = {
        0x001183a4U, 0x00823804U,
        0x009a0148U, 0x009a0200U, 0x009a0204U, 0x009a0208U, 0x009a020cU,
        0x001fa7c0U, 0x001fa7c4U, 0x001fa7c8U, 0x001fa7ccU, 0x001fa804U,
        0x001fa80cU, 0x001fa81cU, 0x001fa820U, 0x001fa824U, 0x001fa828U,
        0x001fa82cU, 0x001fa830U
    };
    NvU32 i;

    for (i = 0; i < (sizeof(addrs) / sizeof(addrs[0])); i++)
    {
        NV_PRINTF(LEVEL_ERROR, "CMP-DYN DIAG GPU%u %s [%08x]=0x%08x\n",
                  pGpu->gpuInstance, pTag, addrs[i], GPU_REG_RD32(pGpu, addrs[i]));
    }
}

static void
_kgspCmpDynScan(OBJGPU *pGpu, NvU32 base, NvU32 dwords, const char *pTag)
{
    NvU32 off;

    for (off = 0; off < dwords * 4U; off += 4U)
    {
        NvU32 value = GPU_REG_RD32(pGpu, base + off);

        if ((value != 0U) && (value != 0xffffffffU))
        {
            NV_PRINTF(LEVEL_ERROR, "CMP-DYN SCAN GPU%u %s [%08x]=0x%08x\n",
                      pGpu->gpuInstance, pTag, base + off, value);
        }
    }
}

static void
_kgspCmpDynDumps(OBJGPU *pGpu, const char *pTag)
{
    if (_cmpDynDebug == 0U)
    {
        return;
    }

    _kgspCmpDynDump(pGpu, pTag);
    _kgspCmpDynScan(pGpu, 0x00100c00U, 64U, pTag);
    _kgspCmpDynScan(pGpu, 0x009a0100U, 128U, pTag);
    _kgspCmpDynScan(pGpu, 0x001fa7c0U, 32U, pTag);
}

static void
_kgspCmpDynNoteFailure(OBJGPU *pGpu, NV_STATUS status, NvU8 *pbRetry)
{
    CmpDynGeomState *pState = _kgspCmpDynGeomState(pGpu);

    if ((pState == NULL) || (pState->bBurned == 0U) ||
        (pState->stage >= CMP_DYN_STAGE_LAST))
    {
        return;
    }

    //
    // Only a falcon timeout says anything about the geometry: that is the
    // signature of a booter that cannot reach the top of a wide framebuffer.  Any
    // other failure (a policy check inside GSP-RM, for instance) fails the same
    // way at every geometry, and rewriting CFG1/LMR between attempts while an
    // attempt already raised the WPR leaves the GPU unbootable for the rest of the
    // session, so keep the geometry for anything else.
    //
    if (status != NV_ERR_TIMEOUT)
    {
        return;
    }

    pState->stage++;

    //
    // The caller only replays a failed boot when it has an ECC reason to, which a
    // healthy card never has, so ask for the replay here.  Recording the stage in
    // the registry keeps a later init from trying the wide layout again.
    //
    NV_PRINTF(LEVEL_ERROR,
              "CMP-DYN STAGE GPU%u booter timed out (caller replay: %s), stepping "
              "back to stage=%u(%s)\n",
              pGpu->gpuInstance, (*pbRetry != 0U) ? "already asked" : "forced now",
              pState->stage, _kgspCmpDynStageName(pState->stage));

    *pbRetry = NV_TRUE;
    osWriteRegistryDword(pGpu, CMP_DYN_REG_STAGE, pState->stage);
}

static void
_kgspCmpDynCopyName(const NvU8 *pBytes, NvU32 size, NvU32 offset, char *pDst, NvU32 dstLen)
{
    NvU32 i = 0;

    while (((i + 1U) < dstLen) && ((offset + i) < size) && (pBytes[offset + i] != '\0'))
    {
        pDst[i] = (char)pBytes[offset + i];
        i++;
    }

    pDst[i] = '\0';
}

static NvBool
_kgspCmpDynNameIs(const char *pA, const char *pB)
{
    NvU32 k;

    for (k = 0; k < 32U; k++)
    {
        if (pA[k] != pB[k])
        {
            return NV_FALSE;
        }

        if (pA[k] == '\0')
        {
            return NV_TRUE;
        }
    }

    return NV_FALSE;
}

// The GSP-RM firmware blob is not rebuilt from this tree, so the keys it acts on
// are only the ones handed to it in the table _kgspPrepareRegistry packs for this
// GPU, which makes that table the only lever that reaches the static BAR1 policy
// inside the firmware.
//
// The P2P configuration ships RMForceStaticBar1=ENABLE, and under that setting
// kbusIsStaticBar1Supported_TU102 turns "BAR1 cannot map the whole framebuffer"
// into NV_ERR_INVALID_REGISTRY_KEY, which GSP-RM reports as the result of RmInit
// and the card never comes up.  A burned card whose framebuffer now spans 80 GiB
// while BAR1 tops out at 64 GiB is exactly that case.  AUTO is what RM uses when
// the key is absent: the same check then answers NV_ERR_NOT_SUPPORTED and the
// caller leaves static BAR1 off, which is the state an FB larger than BAR1 has
// always booted with.  Only a burned card that asked for the wide layout is
// touched, so cards that keep the 64 GiB layout keep static BAR1 and P2P.
static void
_kgspCmpDynAdjustRegistry(OBJGPU *pGpu, KernelGsp *pKernelGsp)
{
    CmpDynGeomState *pState = NULL;
    PACKED_REGISTRY_TABLE *pTable;
    PACKED_REGISTRY_ENTRY *pEntry;
    NvU8 *pBytes;
    NvU32 devId = pGpu->idInfo.PCIDeviceID >> 16;
    NvU32 entries;
    NvU32 hits = 0;
    NvU32 i;
    NvU32 bDump;
    char name[32];

    if ((devId != CMP_DYN_8GB_DEV_ID) && (devId != CMP_DYN_10GB_DEV_ID))
    {
        return;
    }

    //
    // _kgspPrepareRegistry runs before the first geometry write, so the burn
    // signal has to be taken here as well: evaluating it is idempotent and only
    // ever fills in what is still missing.
    //
    pState = _kgspCmpDynGeomEval(pGpu);

    NV_PRINTF(LEVEL_ERROR,
              "CMP-BAR1 GPU%u pre-init CFG1=0x%08x LMR=0x%08x burned=%u stage=%u(%s)\n",
              pGpu->gpuInstance, GPU_REG_RD32(pGpu, CMP_DYN_CFG1_ADDR),
              GPU_REG_RD32(pGpu, CMP_DYN_LMR_ADDR),
              (pState != NULL) ? pState->bBurned : 0xffffffffU,
              (pState != NULL) ? pState->stage : 0xffffffffU,
              (pState != NULL) ? _kgspCmpDynStageName(pState->stage) : "none");

    if ((pState == NULL) || (pState->bBurned == 0U) ||
        (pState->stage != CMP_DYN_STAGE_80G))
    {
        return;
    }

    pTable = pKernelGsp->pRegTable;
    if ((pTable == NULL) || (pTable->size == 0U))
    {
        return;
    }

    pBytes  = (NvU8 *)pTable;
    entries = pTable->numEntries;
    bDump   = (pState->bTableDumped == 0U) ? 1U : 0U;

    if (bDump != 0U)
    {
        pState->bTableDumped = 1U;
    }

    for (i = 0; i < entries; i++)
    {
        pEntry = &pTable->entries[i];

        if (pEntry->nameOffset >= pTable->size)
        {
            continue;
        }

        _kgspCmpDynCopyName(pBytes, pTable->size, pEntry->nameOffset, name, sizeof(name));

        if (pEntry->type != REGISTRY_TABLE_ENTRY_TYPE_DWORD)
        {
            continue;
        }

        // What the firmware is told, once: the only way to see which of the
        // build-time keys reached it at all.
        if ((_cmpDynDebug != 0U) && (bDump != 0U))
        {
            NV_PRINTF(LEVEL_ERROR, "CMP-REG GPU%u entry #%u %s=0x%08x\n",
                      pGpu->gpuInstance, i, name, pEntry->data);
        }

        if ((_kgspCmpDynNameIs(name, NV_REG_STR_RM_FORCE_STATIC_BAR1) == NV_FALSE) ||
            (pEntry->data == NV_REG_STR_RM_FORCE_STATIC_BAR1_AUTO))
        {
            continue;
        }

        NV_PRINTF(LEVEL_ERROR,
                  "CMP-BAR1 GPU%u: GSP registry entry #%u %s=%u -> %u (AUTO), the "
                  "burned framebuffer no longer fits in BAR1\n",
                  pGpu->gpuInstance, i, name, pEntry->data,
                  NV_REG_STR_RM_FORCE_STATIC_BAR1_AUTO);
        pEntry->data = NV_REG_STR_RM_FORCE_STATIC_BAR1_AUTO;
        hits++;
    }

    // hits == 0 means the key never reached the firmware and something else is
    // holding the card down, so keep the line even when the rewrite succeeded.
    NV_PRINTF(LEVEL_ERROR,
              "CMP-BAR1 GPU%u: packed 0x%x bytes / %u registry entries for GSP-RM, "
              "%u forced-static-BAR1 entr%s rewritten\n",
              pGpu->gpuInstance, pTable->size, entries, hits,
              (hits == 1U) ? "y" : "ies");
}

/* ------------------------------------------------------------------------- *
 * Hook wrappers.  Every place the feature touches the driver is one call into
 * this file, so the driver side of the change is a handful of one-line hooks
 * that a build-time script can place by anchor; see dynfb/apply.py.
 * ------------------------------------------------------------------------- */

/* Before anything is sized from the framebuffer: the first booter pass reads the
 * layout to size the framebuffer aperture, so widening it after that pass leaves
 * the aperture at 64 GiB and the next pass cannot DMA firmware into the top of
 * the framebuffer. */
static void
_kgspCmpDynEarlyGeom(OBJGPU *pGpu)
{
    CmpDynGeomState *pState = _kgspCmpDynGeomEval(pGpu);

    if ((pState != NULL) && (pState->bBurned != 0U))
    {
        _kgspCmpDynDumps(pGpu, "PRE-GEOM");
        _kgspCmpDynGeomWrite(pGpu, pState, "early");
    }
}

/* The profile table is what an unburned card boots from; a burned card gets that
 * same recipe one partition pair larger, which is what its own fuse words
 * declare.  The 64 GiB stage asks for precisely what the table says. */
static void
_kgspCmpDynTableOverride(OBJGPU *pGpu, NvU32 *pCfg1, NvU32 *pLmr)
{
    CmpDynGeomState *pState = _kgspCmpDynGeomEval(pGpu);

    if ((pState != NULL) && (pState->bBurned != 0U))
    {
        _kgspCmpDynGeomPair(pState, pCfg1, pLmr);
    }
}

/* One line per card: what the card ended up with, read back from the registers
 * rather than from what we meant to write. */
static void
_kgspCmpDynDecision(OBJGPU *pGpu, NvU32 devId)
{
    CmpDynGeomState *pState = _kgspCmpDynGeomEval(pGpu);

    NV_PRINTF(LEVEL_ERROR,
              "CMP-DYN DECISION devId=0x%x CFG1=0x%08x LMR=0x%08x fb=0x%llx "
              "stage=%u(%s) decision=%s\n",
              devId, GPU_REG_RD32(pGpu, CMP_DYN_CFG1_ADDR),
              GPU_REG_RD32(pGpu, CMP_DYN_LMR_ADDR),
              (unsigned long long)_kgspCmpDynFbBytes(pGpu),
              (pState != NULL) ? pState->stage : 0xffffffffU,
              _kgspCmpDynStageName((pState != NULL) ? pState->stage :
                                               CMP_DYN_STAGE_LAST),
              (_kgspCmpDynIsBurned(pGpu) != NV_FALSE) ? "wide-layout" :
                                                        "table-layout");
}

/* The geometry already describes the enlarged framebuffer, so the populate above
 * placed the WPR against it, at the top, as on the 64 GiB path, and only the size
 * the booter is told about is left behind.  Keep the layout the booter was
 * started with: after a step back the card is a 64 GiB card again and must not be
 * widened here. */
static void
_kgspCmpDynWprMeta(OBJGPU *pGpu, KernelGsp *pKernelGsp)
{
    CmpDynGeomState *pState = _kgspCmpDynGeomEval(pGpu);
    NvU32 wideCfg1;
    NvU32 wideLmr;
    NvU64 wideFbBytes;

    if ((pState == NULL) || (pState->bBurned == 0U) ||
        (pState->stage != CMP_DYN_STAGE_80G))
    {
        return;
    }

    _kgspCmpDynGeomPair(pState, &wideCfg1, &wideLmr);
    wideFbBytes = _kgspCmpDynFbBytesPair(wideCfg1, wideLmr);

    // The accessor is the driver's own member path and its struct tag depends on
    // the release, so go through the macro instead of naming the type.  The
    // populate above leaves it non-NULL, so say so rather than dereference it in
    // a build where that stops being true.
    if (SEC2_POSTBL_TIMING_WPR_META(pKernelGsp) == NULL)
    {
        NV_PRINTF(LEVEL_ERROR,
                  "CMP-DYN WPR GPU%u: no WPR meta to widen to 0x%llx\n",
                  pGpu->gpuInstance, (unsigned long long)wideFbBytes);
    }
    else
    {
        if (SEC2_POSTBL_TIMING_WPR_META(pKernelGsp)->fbSize < wideFbBytes)
        {
            SEC2_POSTBL_TIMING_WPR_META(pKernelGsp)->fbSize = wideFbBytes;
        }

        NV_PRINTF(LEVEL_ERROR,
                  "CMP-DYN WPR fbSize=0x%llx wprStart=0x%llx wprEnd=0x%llx "
                  "heapSize=0x%llx partitions=%u\n",
                  (unsigned long long)SEC2_POSTBL_TIMING_WPR_META(pKernelGsp)->fbSize,
                  (unsigned long long)SEC2_POSTBL_TIMING_WPR_META(pKernelGsp)->gspFwWprStart,
                  (unsigned long long)SEC2_POSTBL_TIMING_WPR_META(pKernelGsp)->gspFwWprEnd,
                  (unsigned long long)SEC2_POSTBL_TIMING_WPR_META(pKernelGsp)->gspFwHeapSize,
                  _kgspCmpDynFbpCount(pGpu));
    }

    _kgspCmpDynDumps(pGpu, "PRE-BOOTER");
}

#endif /* __CMP_DYNAMIC_H__ */
