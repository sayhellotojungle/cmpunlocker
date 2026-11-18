"""The anchor-placed dynamic-capacity hooks (driver/dynfb).

The policy is a source file the build drops into the driver tree, not a patch, so
these checks are about the mechanism: the payloads have to be self-contained, and
apply.py has to refuse a tree it cannot place them in.
"""
import glob
import io
import os
import subprocess
import sys
import unittest

import repo

DYNFB = repo.ROOT / "driver" / "dynfb"


def read(path):
    with io.open(str(path), encoding="utf-8", newline="") as handle:
        return handle.read()


def masked(text):
    """Comments and string bodies blanked out, offsets preserved."""
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        two = text[i:i + 2]
        if two == "/*":
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out[i:j] = [c if c == "\n" else " " for c in text[i:j]]
            i = j
        elif two == "//":
            j = text.find("\n", i)
            j = n if j < 0 else j
            out[i:j] = [" "] * (j - i)
            i = j
        elif text[i] in '"\'':
            quote, j = text[i], i + 1
            while j < n and text[j] != quote:
                j += 2 if text[j] == "\\" else 1
            out[i + 1:j] = ["x"] * (j - i - 1)
            i = j + 1
        else:
            i += 1
    return "".join(out)


class DynfbFiles(unittest.TestCase):
    def test_policy_header_is_self_contained(self):
        text = read(DYNFB / "cmp_dynamic.h")
        for guard in ("#ifndef __CMP_DYNAMIC_H__", "#define __CMP_DYNAMIC_H__",
                      "#endif"):
            self.assertIn(guard, text)
        clean = masked(text)
        self.assertEqual(clean.count("{"), clean.count("}"))
        self.assertEqual(clean.count("("), clean.count(")"))
        self.assertIn("_kgspCmpDynGeomEval(OBJGPU *pGpu)", text)
        self.assertNotIn("GeomEval(pGpu, &", text)

    def test_every_hook_payload_is_balanced_on_its_own(self):
        """A payload that unbalances braces cannot compile inside any function."""
        listed = sorted(p for _, _, _, _, p in repo.dynfb().HOOKS)
        on_disk = sorted("hooks/" + os.path.basename(p) for p in
                         glob.glob(str(DYNFB / "hooks" / "*.c")))
        self.assertEqual(listed, on_disk, "exactly one payload per hook, none unused")
        self.assertGreaterEqual(len(listed), 14)
        for rel in listed:
            clean = masked(read(DYNFB / rel))
            self.assertEqual(clean.count("{"), clean.count("}"), rel)
            self.assertEqual(clean.count("("), clean.count(")"), rel)

    def test_hooks_are_one_call_or_a_local_block(self):
        """Only the sites that need function locals may carry real code."""
        for name in ("include", "early_geom", "table_override", "decision",
                     "wpr_meta", "retry", "registry", "gen4_call"):
            body = [line for line in
                    read(DYNFB / "hooks" / ("%s.c" % name)).split("\n") if line.strip()]
            self.assertEqual(len(body), 1, name)

    def test_gen4_is_gated_on_the_card_and_falls_back(self):
        """Mixed boxes: a soft-unlocked card must never be asked for Gen4."""
        nv = read(DYNFB / "hooks" / "nv_gen4.c")
        # The target comes from what the card's own fuses report, not from a build
        # choice.
        self.assertIn("PCI_EXP_LNKCAP2", nv)
        self.assertIn("cmp_pcie_gen = 2", nv)      # default: what every card does today
        self.assertIn("if (target <= 2)", nv)   # fuses say Gen2: change nothing
        # and a card that cannot hold anything above Gen2 ends up on the Gen2 recipe.
        self.assertIn("nv_cmp170hx_retrain_gen2(nv);", nv)

    def test_a_link_that_trains_below_its_target_keeps_its_speed(self):
        """Gen4 is the card's ceiling, not the link's: a trained Gen3 stays trained.

        Measured on the bench: the burned card's fuses allow Gen4, the bridge above
        it is a Gen3 link upstream, and the retrain landed on LnkSta 0x1103.  The
        first version of this hook answered that by calling the Gen2 recipe, which
        spent a stable 8 GT/s link to be strict about a speed the slot cannot carry.
        The fallback may only be reachable from a link that failed to train above
        Gen2, and the ordering of the payload is the only thing that says so here,
        so it is checked rather than asserted.
        """
        body = masked(read(DYNFB / "hooks" / "nv_gen4.c"))
        # Definition, the ask at the fused target, and the retrain aimed at whatever
        # the link reached: two passes at most, and the second only after a partial.
        self.assertEqual(body.count("nv_cmp170hx_link_retrain("), 3)
        guard = body.find("if (trained >= 3)")
        fallback = body.find("fallback:")
        hammer = body.find("nv_cmp170hx_retrain_gen2(nv);")
        self.assertGreater(guard, 0, "the trained speed has to be looked at")
        self.assertGreater(fallback, guard, "the Gen2 path comes after that check")
        self.assertGreater(hammer, fallback, "the Gen2 recipe is the last resort")
        # The accepted-partial branch has to leave the function: falling through it
        # would train Gen3 and then hammer it back down two lines later.
        self.assertIn("goto done", body[guard:fallback])
        # A link left at Gen3 must not still be told to chase Gen4 on every future
        # retrain: the card's own rate fields are rewritten to the trained speed.
        self.assertIn("0x8c1c0", body[guard:fallback])
        self.assertIn("0x8c040", body[guard:fallback])

    def test_the_phy_init_block_is_replayed_exactly_once(self):
        """Two copies of the E1 block cannot add up to a faster link.

        RM rewrites LINK_CONFIG_0 during its own PCIe init, so an earlier pass
        leaves CYA_0 at the E1 value while the rate field is back at Gen2, and the
        later pass writes back what it read instead of producing an equalisation
        edge.  Nothing here can express "run this once" except there being exactly
        one copy of it.
        """
        listed = [p for _, _, _, _, p in repo.dynfb().HOOKS]
        with_block = [rel for rel in listed
                      if "0x060711b2" in read(DYNFB / rel)]
        self.assertEqual(with_block, ["hooks/nv_gen4.c"],
                         "the E1 replay must live in exactly one payload")

    def test_apply_reports_without_a_tree(self):
        r = subprocess.run([sys.executable, str(DYNFB / "apply.py")],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("usage", r.stdout)
        r = subprocess.run([sys.executable, str(DYNFB / "apply.py"),
                            "--header-dest"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(),
                         "src/nvidia/inc/kernel/gpu/gsp/cmp_dynamic.h")

    def test_apply_refuses_a_tree_it_cannot_place_in(self):
        import shutil
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            for rel in sorted(repo.dynfb().FILES.values()):
                os.makedirs(os.path.join(tmp, os.path.dirname(rel)), exist_ok=True)
                with open(os.path.join(tmp, rel), "w", newline="\n") as handle:
                    handle.write("/* an unrelated tree */\n")
            r = subprocess.run([sys.executable, str(DYNFB / "apply.py"),
                                "--check", "--p2p=1", tmp], capture_output=True, text=True)
            self.assertEqual(r.returncode, 1, r.stdout)
            self.assertIn("did not land", r.stdout)
            self.assertNotIn("skip", r.stdout)
            # Every hook has to be the one that reports the miss: a hook that stays
            # quiet in a tree it cannot place in is a hook that silently did nothing.
            self.assertEqual(r.stdout.count("FAIL"), len(repo.dynfb().HOOKS), r.stdout)
            # --check must not touch the tree
            self.assertEqual(read(os.path.join(tmp, "src/nvidia/src/kernel/gpu/gsp/"
                                               "kernel_gsp.c")),
                             "/* an unrelated tree */\n")
            self.assertFalse(os.path.exists(os.path.join(tmp, "src/nvidia/inc")))
            shutil.rmtree(tmp, ignore_errors=True)

    def test_build_script_wires_dynfb_in_before_the_profile_rewrite(self):
        script = (repo.ROOT / "driver" / "build.sh").read_text()
        self.assertIn("dynfb/apply.py", script)
        self.assertLess(script.index("dynfb/apply.py"),
                        script.index("Applying memory profile"),
                        "the profile rewrite rewrites the table the hooks anchor on")
        self.assertIn("cmp_dynamic.h", script)      # in the cache key
        self.assertNotIn("cmp-dynamic.patch", script)


if __name__ == "__main__":
    unittest.main()
