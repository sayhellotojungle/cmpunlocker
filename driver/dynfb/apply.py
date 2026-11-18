#!/usr/bin/env python3
"""Places the dynamic-capacity hooks into an extracted driver tree.

Why a script instead of one more patch file: every insertion below is found by an
anchor, not by a context window, so a release that moves code around changes a
line number and nothing else.  A missing or ambiguous anchor is a hard error,
which is the failure mode a build wants; a patch that applies "with fuzz" is the
failure mode it does not want, because a fuzzy hunk lands somewhere plausible in
the wrong function.  Most anchors are lines our own earlier patches insert, which
makes them independent of the release by construction.

The policy itself is cmp_dynamic.h, copied next to the other GSP headers; the
payloads in hooks/ are what gets spliced in at each anchor, and each of them is
one call plus, in a few places, a short block that has to touch locals of the
enclosing function.  Two payloads replace their anchor instead of being spliced at
it: single-line edits whose neighbouring line is somebody else's to change, which is
exactly what a context diff cannot survive (see REPLACES below).

Three hooks belong to an optional feature and are placed only when build.sh says the
feature is on, so a tree built without --p2p is byte-for-byte the tree from before
P2P.

  apply.py [--check] [--p2p=0|1] <tree>    report / rewrite the tree

Run from driver/build.sh after the patch set, before the profile rewrite.
"""
import io
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

HEADER_SRC = "cmp_dynamic.h"
HEADER_DST = "src/nvidia/inc/kernel/gpu/gsp/cmp_dynamic.h"

FILES = {
    "gsp": "src/nvidia/src/kernel/gpu/gsp/kernel_gsp.c",
    "bus": "src/nvidia/src/kernel/gpu/bus/arch/turing/kern_bus_tu102.c",
    "nv": "kernel-open/nvidia/nv.c",
    # The three below are the P2P sites that a context diff cannot express: a block
    # that goes at the end of a statement list (the line after it differs between
    # releases, and a context diff must match three contiguous lines on both sides),
    # and two single-line edits, one of them in nvoc's generated func table, where
    # a new GPU name in the comment above is enough to reject the hunk.
    "gpu": "src/nvidia/src/kernel/gpu/gpu.c",
    "nvreg": "kernel-open/nvidia/nv-reg.h",
    "nvoc": "src/nvidia/generated/g_kern_bus_nvoc.c",
}

# (name, file, text before the payload, text after the payload, payload file).
# The pair must occur exactly once in the file.
HOOKS = [
    # The policy, above every user of it, and below the two things it reads: the
    # registry strings, and the WPR meta accessor our own sec2 patch defines.  Both
    # are stock-include-ordered, so the anchor is the end of that patch's helper:
    # our own text, which no driver release moves, and which guarantees everything
    # the header needs has been declared already.
    ("include", "gsp",
     "            devId == SEC2_POSTBL_TIMING_CMP_170HX_10GB_PCI_DEVICE_ID);\n}\n",
     "", "hooks/include.c"),

    # The one decision, before the WPR is placed against the framebuffer.
    ("early-geom", "gsp", "",
     "    // Populate WPR meta structure (requires knowing FB size on dGPU, "
     "which depends on GFW_BOOT)\n",
     "hooks/early_geom.c"),

    # The layout table, overridden for a burned card before it is written.
    ("table-override", "gsp",
     "                lmrValue  = 0x0000028AU;\n            }\n", "",
     "hooks/table_override.c"),

    # What the card ended up with, after the post-write register dump.
    ("decision", "gsp",
     "                      GPU_REG_RD32(pGpu, 0x00100ce0U),\n"
     "                      devId);\n", "",
     "hooks/decision.c"),

    # The size the booter is told about.
    ("wpr-meta", "gsp", "",
     '        NV_PRINTF(LEVEL_ERROR,\n'
     '                  "SEC2_DEBUG: WPR meta updated fbSize=',
     "hooks/wpr_meta.c"),

    # Ask for the replay after a layout the booter could not boot.
    ("retry", "gsp", "",
     "        // Increment the bootAttempt counter only on failure to boot GSP\n",
     "hooks/retry.c"),

    # What GSP-RM itself is told about the framebuffer.
    ("static-info", "gsp", "",
     '            NV_PRINTF(LEVEL_ERROR,\n'
     '                      "SEC2_DEBUG: static-info BEFORE: fb_length=',
     "hooks/static_info.c"),

    # Static BAR1 as seen by the firmware: after the table is packed for GSP-RM.
    ("registry", "gsp",
     "        pKernelGsp->pRegTable = NULL;\n        return status;\n    }\n\n",
     "    return status;\n}",
     "hooks/registry.c"),

    # A framebuffer larger than BAR1 is unsupported, not fatal.
    ("bar1", "bus", "",
     "                    return NV_ERR_INVALID_REGISTRY_KEY;\n",
     "hooks/bar1.c"),

    # Gen4, in the order the driver runs it.  Both nv.c anchors are lines our own
    # pcie-gen2-probe-retrain.patch inserts, so they hold across releases; the
    # call site is placed first because the function below also contains that
    # call (as its fallback) and would otherwise make the anchor ambiguous.
    ("gen4-call", "nv", "    nv_cmp170hx_retrain_gen2(nv);\n", "",
     "hooks/gen4_call.c"),

    ("gen4-func", "nv",
     '                  "CMP Gen2: PCIe capability access failed (%d); '
     'skipping link retrain\\n", ret);\n}\n', "",
     "hooks/nv_gen4.c"),

    # The Gen3 -> E1 -> Gen4 sequence runs once, from nv.c post-init, and it has to
    # stay that way.  A second copy here was tried: RM's own PCIe init rewrites
    # LINK_CONFIG back to Gen2 after it, so nv.c entered with CYA_0 already holding
    # the E1 value and the pass that mattered replayed nothing.  The verified
    # bring-up runs one pass from the VBIOS state.  What a second pass costs exactly
    # is hard to separate from a partner that caps the link below the target; what it
    # cannot buy is a faster link, and the hook now keeps Gen3 either way.

    # P2P, opt-in with --p2p.  These were context-diff hunks (p2p/0007 and the first
    # two hunks of p2p/0011) and are here because a context diff cannot express them:
    # each wants to land where the line after it (or, for the generated file, the
    # comment above it) is something a different release edits.
    ("p2p-caps", "gpu",
     "    pGpu->pcieP2PReadCaps = p2pCapsParams.p2pReadCapsStatus;\n"
     "    pGpu->pcieP2PWriteCaps = p2pCapsParams.p2pWriteCapsStatus;\n", "",
     "hooks/p2p_caps.c"),

    ("p2p-resizable-bar", "nvreg",
     "NV_DEFINE_REG_ENTRY_GLOBAL(__NV_ENABLE_RESIZABLE_BAR, 0);\n", "",
     "hooks/p2p_resizable_bar.c"),

    # In nvoc's generated table, so the ChipHal list comment above it is somebody
    # else's business: a new Blackwell name there is enough to reject a patch whose
    # own line never changed.
    ("p2p-bar1dma", "nvoc",
     "        pThis->__kbusGetBar1P2PDmaInfo__ = &kbusGetBar1P2PDmaInfo_395e98;\n",
     "", "hooks/p2p_bar1dma.c"),
]

# Hooks that belong to an optional feature: build.sh names the features it enabled,
# and a hook whose feature is off is not placed at all, which is how a build without
# --p2p stays exactly as it was before P2P existed.
OPTIONAL = {
    "p2p-caps": "p2p",
    "p2p-resizable-bar": "p2p",
    "p2p-bar1dma": "p2p",
}

# Hooks whose payload replaces the anchor instead of being spliced in front of the
# text after it.  Their payloads are written to be unique in their file, so a re-run
# can still tell "already placed" from "the driver moved".
REPLACES = {"p2p-resizable-bar", "p2p-bar1dma"}


def read(path):
    with io.open(path, encoding="utf-8", newline="") as handle:
        return handle.read()


def write(path, text):
    with io.open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def main(argv):
    if "--header-dest" in argv:
        # So build.sh never has to duplicate the path.
        print(HEADER_DST)
        return 0
    check = "--check" in argv
    # --p2p=1 turns on the optional hooks; anything they gate is left alone, so a
    # tree built without --p2p is the tree the driver shipped as of before P2P.
    features = {}
    for arg in argv:
        if arg.startswith("--") and "=" in arg:
            features[arg[2:].split("=", 1)[0]] = arg.split("=", 1)[1] == "1"
    args = [arg for arg in argv if not arg.startswith("--")]
    if len(args) != 1:
        print("usage: apply.py [--check] [--p2p=0|1] <driver-tree>")
        return 2
    root = args[0].rstrip("/\\")
    texts = {}
    failed = 0
    placed = 0

    for name, key, before, after, payload_name in HOOKS:
        feature = OPTIONAL.get(name)
        if feature is not None and not features.get(feature, False):
            print("%-15s off  %s (--%s not given)" % (name, FILES[key], feature))
            continue
        rel = FILES[key]
        if rel not in texts:
            texts[rel] = read(os.path.join(root, rel))
        text = texts[rel]
        payload = read(os.path.join(HERE, payload_name))
        anchor = before + after
        if payload.strip() and payload in text:
            print("%-15s skip  already placed" % name)
            placed += 1
            continue
        hits = text.count(anchor)
        if hits != 1:
            print("%-15s FAIL  anchor matched %d times in %s" % (name, hits, rel))
            failed += 1
            continue
        index = text.index(anchor)
        if name in REPLACES:
            # The anchor is the line being changed, so it goes away with the splice.
            texts[rel] = text[:index] + payload + text[index + len(anchor):]
        else:
            # Splice, not replace: keep everything that was around the anchor.
            texts[rel] = text[:index] + before + payload + after + text[index + len(anchor):]
        print("%-15s ok    %s" % (name, rel))
        placed += 1

    header_src = os.path.join(HERE, HEADER_SRC)
    if not os.path.isfile(header_src):
        print("%-15s FAIL  %s missing" % ("header", HEADER_SRC))
        failed += 1

    if failed:
        print("dynfb: %d hook(s) did not land; the driver moved and these anchors "
              "need updating" % failed)
        return 1
    if check:
        print("dynfb: %d hooks place cleanly in %s (%d known, the difference belongs "
              "to features that are off)" % (placed, root, len(HOOKS)))
        return 0

    header = os.path.join(root, HEADER_DST)
    os.makedirs(os.path.dirname(header), exist_ok=True)
    shutil.copyfile(header_src, header)
    print("%-15s ok    %s" % ("header", HEADER_DST))

    for rel, text in texts.items():
        path = os.path.join(root, rel)
        original = read(path)
        # A hook that unbalances braces cannot compile, and the count over the
        # whole file is the one check that needs no compiler.
        for open_char, close_char in (("{", "}"), ("(", ")")):
            delta = (text.count(open_char) - text.count(close_char)) - \
                    (original.count(open_char) - original.count(close_char))
            if delta != 0:
                print("dynfb: %s braces unbalanced by %+d, refusing to write"
                      % (rel, delta))
                return 1
        write(path, text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
