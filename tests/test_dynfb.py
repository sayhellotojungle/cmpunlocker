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
        # Definition, the Gen3 rung, that rung again if the E1 block dropped the
        # link, the ask at the fused target, and the retrain inside the settle.  Every
        # one of them is a rung of the ladder, not a retry.
        self.assertEqual(body.count("nv_cmp170hx_link_retrain("), 5)
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
        # retrain, so the branch has to go through the settle, and the settle is what
        # rewrites the card's own rate fields to the trained speed.
        self.assertIn("nv_cmp170hx_link_settle(", body[guard:fallback])
        settle = body[body.find("nv_cmp170hx_link_settle(struct pci_dev"):]
        settle = settle[:settle.find("\n}")]
        self.assertIn("0x8c1c0", settle, "PL_LINK_RATE goes to the trained speed")
        self.assertIn("0x8c040", settle, "and so does the LINK_CONFIG_0 rate field")

    def test_a_held_rung_is_not_given_back_by_the_gen2_fallback(self):
        """The floor is the highest rung the walk stood on, not Gen2.

        Reported on a Gen3 board: the card's fuses allow Gen4, so the hook aims Phase C
        at Gen4, the 16 GT/s equalisation is refused and the link comes back at 5 GT/s,
        and the first version of this fell through to the Gen2 recipe.  It had confirmed
        8 GT/s at both ends twice in that same walk.  The board was measured to carry
        Gen3, the refuse says nothing about that rung, and dropping to Gen2 is the same
        bandwidth thrown away for no safety the bench measurement was written against.
        """
        body = masked(read(DYNFB / "hooks" / "nv_gen4.c"))
        rung_held = body.find("held = trained;")
        e1 = body.find("0x060711b2")
        fallback = body.find("fallback:")
        hammer = body.find("nv_cmp170hx_retrain_gen2(nv);")
        # The rung is recorded where it is confirmed, before the PHY-init block, or the
        # fallback has nothing to settle on.
        self.assertGreater(rung_held, 0, "the confirmed rung is recorded")
        self.assertLess(rung_held, e1, "recorded when it is stood on, not after the aim")
        tail = body[fallback:hammer]
        self.assertIn("if (held >= 3)", tail,
                      "the fallback looks at the rung before handing the card to Gen2")
        self.assertIn("nv_cmp170hx_link_settle(", tail,
                      "and it settles the link there rather than merely hoping")
        # A card whose config space stopped answering gets no further retrain, and a
        # link that refused the speed it had just trained is not owed another aim.
        self.assertEqual(body.count("held = 0;") - body.count("int held = 0;"), 2,
                         "both paths that invalidate the rung say so")

    def test_an_all_ones_link_status_is_not_a_trained_speed(self):
        """A card that reads 0xffff did not report Gen15.

        During the few hundred ms a retrain takes, config space reads answer all-ones
        and pcie_capability_read_word() reports success.  0xffff & PCI_EXP_LNKSTA_CLS is
        0xf, which passes every `>= gen` test in the walk, so a read taken mid-Recovery
        was reported as the rung reached: the walk then declared success while the link
        was still at 5 GT/s and wrote a target speed and rate fields the encodings have
        no value for.  The partner's all-ones was harmless, because 15 never wins a
        min(); only the card's own end needed taking back.
        """
        body = masked(read(DYNFB / "hooks" / "nv_gen4.c"))
        speed_fn = body[body.find("nv_cmp170hx_link_speed(struct pci_dev"):]
        speed_fn = speed_fn[:speed_fn.find("\n}")]
        unreadable = speed_fn.find("if (*status == 0xffffU)")
        self.assertGreater(unreadable, 0, "the card's all-ones read is taken back")
        self.assertLess(unreadable, speed_fn.find("PCI_EXP_LNKSTA_CLS"),
                        "before the speed field is read out of it")
        self.assertIn("return 0;", speed_fn[unreadable:],
                      "and it is reported as no speed, not as the slowest one")

    def test_the_gen3_rung_is_asked_of_both_ends_before_the_phy_init_block(self):
        """The first rung has to be a rung the partner is standing on too.

        Measured on an EPYC server (card 41:00.0 on root port 40:03.1): the pass
        that raised only the card's own rate fields left the link at 5 GT/s, because
        the Gen2 retrain two lines earlier had pinned the port's target speed to
        Gen2 and an AMD root port honours that.  The E1 block then replayed onto a
        PHY that was still at 5 GT/s, the 5 -> 16 GT/s request missed, and the card
        was left on Gen2 for the uptime; the companion unit that appeared on that
        host worked because its second pass started from a link settled at 8 GT/s.
        So the Gen3 rung is asked of both ends, confirmed at both ends, and sits
        before the E1 block rather than beside it.
        """
        body = masked(read(DYNFB / "hooks" / "nv_gen4.c"))
        e1 = body.find("0x060711b2")
        rung = body.find("nv_cmp170hx_link_retrain(gpu, upstream, 3,")
        self.assertGreater(rung, 0, "the Gen3 rung is a retrain, not a register write")
        self.assertLess(rung, e1, "8 GT/s first, the PHY-init block second")
        # Both ends: the slower port is what the link reports, so the rung is only
        # taken as held when the port above the card says 8 GT/s too.
        self.assertIn("upstream, PCI_EXP_LNKSTA", body[:rung],
                      "the rung's confirmation reads the partner's link status")
        rung_guard = body.find("if (trained < 3)")
        self.assertGreater(rung_guard, rung, "the rung's own answer is looked at")
        self.assertLess(rung_guard, e1, "and E1 is not replayed onto a link that missed it")
        # A second walk of the ladder has to re-arm the equalisation edge, or it
        # writes back the CYA_0 value the first walk left there.  The GSP-bootstrap
        # copy failed for exactly that reason.
        edge = body.find("writel(v | CMP_CYA0_PHY_INIT_EDGE")
        self.assertGreater(edge, 0, "the retry has to produce an edge, not a restate")
        self.assertLess(edge, e1)
        self.assertIn("cmp_pcie_retries", body, "and the retry count is an operator's knob")
        # The E1 block is programmed while the link is up, so it can be what drops
        # the link; the hook looks again rather than assuming the rung held.
        self.assertIn(
            "nv_cmp170hx_link_speed(gpu, upstream, &link_status, &partner_status) < 3",
            body, "the rung is re-checked after the PHY-init block, not just before it")
        # And a card already at its target is left alone: a warm reboot can train the
        # link without this hook, and retraining a working link is a cost with no
        # buyer.
        self.assertIn("link already there", read(DYNFB / "hooks" / "nv_gen4.c"),
                      "a link at its target does not get walked")

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
        dynfb = repo.dynfb()
        for mode in dynfb.P2P_MODES:
            with tempfile.TemporaryDirectory() as tmp:
                for rel in sorted(dynfb.FILES.values()):
                    os.makedirs(os.path.join(tmp, os.path.dirname(rel)), exist_ok=True)
                    with open(os.path.join(tmp, rel), "w", newline="\n") as handle:
                        handle.write("/* an unrelated tree */\n")
                r = subprocess.run([sys.executable, str(DYNFB / "apply.py"),
                                    "--check", "--p2p=" + mode, tmp],
                                   capture_output=True, text=True)
                self.assertEqual(r.returncode, 1, r.stdout)
                self.assertIn("did not land", r.stdout)
                self.assertNotIn("skip", r.stdout)
                # Every hook that this mode asks for has to be the one that reports
                # the miss: a hook that stays quiet in a tree it cannot place in is a
                # hook that silently did nothing.  The other modes' hooks report
                # themselves off, which is the only honest thing they can do.
                asked = [name for name, key, _, _, _ in dynfb.HOOKS
                         if name not in dynfb.OPTIONAL
                         or mode in dynfb.OPTIONAL[name]]
                off = [name for name, key, _, _, _ in dynfb.HOOKS
                       if name in dynfb.OPTIONAL and mode not in dynfb.OPTIONAL[name]]
                self.assertEqual(r.stdout.count("FAIL"), len(asked), r.stdout)
                self.assertEqual(r.stdout.count("FAIL"), len(dynfb.HOOKS) - len(off),
                                 "%s: a hook neither placed nor reported" % mode)
                for name in off:
                    self.assertIn("%-15s off " % name, r.stdout,
                                  "%s in %s" % (mode, name))
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
