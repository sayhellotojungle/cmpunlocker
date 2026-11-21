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

Four hooks belong to an optional feature and are placed only when build.sh says
which flavour of it the build is, so a tree built without --p2p is byte-for-byte
the tree from before P2P. --p2p takes a path, not a switch: `bar1` is the CE-DMA
path that needs a BAR1 at least as large as the framebuffer, `mailbox` is the
NV_P2P aperture path that needs almost none, and the two differ in the caps
override they install, which is why they are two hooks and not one with a
condition in it.

  apply.py [--check] [--p2p=off|bar1|mailbox] <tree>    report / rewrite the tree

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

    # Same anchor as p2p-caps, other path, and only ever one of the two: the
    # mailbox variant withholds the caps on a card whose PRI trap cannot be
    # armed, because a mailbox nobody programmed moves garbage behind a
    # successful copy, and a forced cap there turns "not supported" into that.
    ("p2p-caps-mailbox", "gpu",
     "    pGpu->pcieP2PReadCaps = p2pCapsParams.p2pReadCapsStatus;\n"
     "    pGpu->pcieP2PWriteCaps = p2pCapsParams.p2pWriteCapsStatus;\n", "",
     "hooks/p2p_caps_mailbox.c"),

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

# Hooks that belong to an optional feature: build.sh names the path it enabled, and
# a hook whose path was not chosen is not placed at all, which is how a build
# without --p2p stays exactly as it was before P2P existed.  The value is the set of
# --p2p modes that place the hook; p2p-resizable-bar is in both because a wider BAR1
# is what the modprobe config asks for either way, and leaving it out of one of the
# two would make the two builds differ by more than the P2P path.
OPTIONAL = {
    "p2p-caps": ("bar1",),
    "p2p-caps-mailbox": ("mailbox",),
    "p2p-resizable-bar": ("bar1", "mailbox"),
    "p2p-bar1dma": ("bar1",),
}

# --p2p is a path, not a switch, but it started life as one: build.sh and the
# scripts that call it have spoken 0 and 1 since the feature landed.
P2P_ALIASES = {"": "off", "0": "off", "off": "off", "none": "off",
               "1": "bar1", "bar1": "bar1", "mailbox": "mailbox"}
P2P_MODES = ("off", "bar1", "mailbox")

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
    # --p2p picks which P2P path the build is, which is what the four optional hooks
    # below key off.  Anything it does not name is left alone, so a tree built without
    # --p2p is the tree the driver shipped as of before P2P.
    features = {}
    for arg in argv:
        if arg.startswith("--") and "=" in arg:
            name, value = arg[2:].split("=", 1)
            if name == "p2p":
                value = P2P_ALIASES.get(value)
                if value is None:
                    print("dynfb: --p2p=%s is not one of %s"
                          % (arg.split("=", 1)[1], ", ".join(P2P_MODES)))
                    return 2
            features[name] = value
    args = [arg for arg in argv if not arg.startswith("--")]
    if len(args) != 1:
        print("usage: apply.py [--check] [--p2p=off|bar1|mailbox] <driver-tree>")
        return 2
    root = args[0].rstrip("/\\")
    texts = {}
    failed = 0
    placed = 0
    # Two hooks on one anchor is a configuration error, not a double splice: the
    # pair that forces the PCIe P2P caps differs only in what it believes about the
    # card, and both of them in the same function would mean the build answers to
    # whichever of the two paths the operator did not pick.
    taken = {}

    for name, key, before, after, payload_name in HOOKS:
        modes = OPTIONAL.get(name)
        if modes is not None and features.get("p2p", "off") not in modes:
            print("%-15s off  %s (--p2p=%s)" % (name, FILES[key], "|".join(modes)))
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
        if taken.get((rel, anchor), name) != name:
            print("%-15s FAIL  %s and %s both want the same place in %s"
                  % (name, taken[(rel, anchor)], name, rel))
            failed += 1
            continue
        taken[(rel, anchor)] = name
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
