import os
import pathlib
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

import pytest

import repo

URL = "https://github.com/NVIDIA/open-gpu-kernel-modules/archive/refs/tags/%s.tar.gz"
CACHE = pathlib.Path(os.environ.get("CMPUNLOCKER_BUILD_DIR", repo.ROOT / "driver" / ".build"))
PATCHES = repo.ROOT / "driver" / "patches"
ORDER = repo.patch_order()
VERSIONS = repo.versions()
assert VERSIONS, "driver/VERSION lists no versions"


def tarball(version):
    path = CACHE / ("open-gpu-kernel-modules-%s.tar.gz" % version)
    if not path.is_file():
        CACHE.mkdir(parents=True, exist_ok=True)
        partial = path.with_name(path.name + ".partial")
        with urllib.request.urlopen(URL % version, timeout=60) as resp, open(partial, "wb") as out:
            shutil.copyfileobj(resp, out)
        partial.replace(path)
    return path


def test_patch_order_covers_patch_dir():
    assert sorted(ORDER) == sorted(p.name for p in PATCHES.glob("*.patch"))


# driver/build.sh applies these without fuzz, and so does this test: a hunk that
# only lands because patch ignored part of its context is a hunk that lands in the
# wrong function the next time the driver moves.  Everything in a subdirectory is
# optional and therefore held to that rule.
DYNFB = repo.ROOT / "driver" / "dynfb" / "apply.py"
P2P_ORDER = repo.p2p_patch_order()
BAR1_ORDER = repo.p2p_patch_order("bar1")
MAILBOX_ORDER = repo.p2p_patch_order("mailbox")

# The hooks that are allowed to anchor on a line the driver itself wrote, because
# the behaviour being changed is that line's.  Every other hook anchors on text our
# own patches insert, which no release can move.  This is the same split the README
# documents, and test_anchor_provenance_is_what_the_readme_claims is what keeps it
# from drifting: a hook moving from one column to the other changes how much a new
# driver release costs, which is a claim worth checking instead of asserting.
#
# The three P2P hooks are here for the same reason the P2P set has no version list:
# they are the sites a context diff used to carry, and a context diff failed there
# precisely because it depended on driver lines that were not the point.
DRIVER_NATIVE_ANCHORS = {"early-geom", "retry", "registry", "bar1",
                         "p2p-caps", "p2p-caps-mailbox",
                         "p2p-resizable-bar", "p2p-bar1dma"}


def test_the_two_p2p_paths_share_no_patch():
    """Neither build may ask for both sets, because RM will not run both.

    gate 4 of kbusIsPcieBar1P2PMappingSupported_GH100 refuses the BAR1 route over
    a peer number the mailbox route already claimed, so a shared patch would make
    --p2p mean "whichever of the two the peer pair reached first".  The split is
    what makes the option answerable.
    """
    assert BAR1_ORDER and MAILBOX_ORDER, "a path lost its patches"
    assert not set(BAR1_ORDER) & set(MAILBOX_ORDER), (BAR1_ORDER, MAILBOX_ORDER)
    assert sorted(P2P_ORDER) == sorted(set(BAR1_ORDER) | set(MAILBOX_ORDER))


def stock_file(version, rel):
    """One file from the release as upstream shipped it, without a second extract."""
    with tarfile.open(tarball(version)) as archive:
        with archive.extractfile("open-gpu-kernel-modules-%s/%s" % (version, rel)) as member:
            return member.read().decode("utf-8", "replace")


def test_anchor_provenance_is_what_the_readme_claims():
    version = VERSIONS[0]
    dynfb = repo.dynfb()
    for name, key, before, after, _ in dynfb.HOOKS:
        anchor = before + after
        found = anchor in stock_file(version, dynfb.FILES[key])
        ours = name not in DRIVER_NATIVE_ANCHORS
        assert found != ours, (
            "%s: anchor %s in the %s release as shipped, so it %s; expected %s. "
            "Update DRIVER_NATIVE_ANCHORS and the README table together." % (
                name, "found" if found else "absent", version,
                "is the driver's own line and is release-sensitive" if found
                else "is our patch text and cannot move",
                "ours" if ours else "the driver's own"))


def test_replace_mode_payloads_are_unique_before_we_place_them():
    """apply.py reports "already placed" by looking for the payload text.

    That is only a real answer while the payload cannot be confused with something the
    driver already ships, which bites the two hooks that replace the line they change:
    nvoc's table points a second chip at the same GH100 implementation, so the
    replacement carries a trailing comment of ours and would report "missing" as
    "placed" otherwise.  Measured against the release as shipped, because what is under
    test is the file before we touch it.
    """
    version = VERSIONS[0]
    dynfb = repo.dynfb()
    payloads = {name: payload for name, _, _, _, payload in dynfb.HOOKS}
    assert dynfb.REPLACES <= set(payloads), "REPLACES names a hook that does not exist"
    for name, key, before, after, _ in dynfb.HOOKS:
        if name not in dynfb.REPLACES:
            continue
        rel = dynfb.FILES[key]
        stock = stock_file(version, rel)
        assert stock.count(before + after) == 1, \
            "%s: anchor found %d times in %s as shipped" % (
                name, stock.count(before + after), rel)
        assert (DYNFB.parent / payloads[name]).read_text() not in stock, \
            "%s: payload is already in %s, so a missed hook would read as placed" % (
                name, rel)


def apply_patch(src, name):
    cmd = ["patch", "--batch", "--forward", "-p1"]
    if "/" in name:
        cmd.append("--fuzz=0")
    cmd += ["-i", str(PATCHES / name)]
    r = subprocess.run(cmd, cwd=src, stdin=subprocess.DEVNULL,
                       capture_output=True, text=True)
    return r


def extract(version, tmp, patches):
    """The release as NVIDIA shipped it, with these patches applied."""
    subprocess.run(["tar", "-xzf", str(tarball(version)), "-C", tmp], check=True)
    src = pathlib.Path(tmp, "open-gpu-kernel-modules-" + version)
    assert src.is_dir(), os.listdir(tmp)
    for name in patches:
        r = apply_patch(src, name)
        assert r.returncode == 0, "%s on %s:\n%s%s" % (name, version, r.stdout, r.stderr)
    return src


def run_dynfb(src, mode, version):
    """apply.py in the position build.sh puts it in; a missed anchor is an error."""
    r = subprocess.run([sys.executable, str(DYNFB), "--p2p=" + mode, str(src)],
                       capture_output=True, text=True)
    assert r.returncode == 0, \
        "dynfb --p2p=%s on %s:\n%s%s" % (mode, version, r.stdout, r.stderr)
    assert "FAIL" not in r.stdout, r.stdout
    return r


@pytest.mark.parametrize("version", VERSIONS)
def test_patches_apply(version):
    assert shutil.which("patch"), "GNU patch is not installed"
    with tempfile.TemporaryDirectory() as tmp:
        # The optional set is applied on every supported release, with no version
        # list to hide behind: the three sites that used to reject on 615.x are
        # anchors now, so the only reason one of these can miss is that the driver
        # changed the line we mean.
        src = extract(version, tmp, ORDER + P2P_ORDER)

        # ...and the anchor-placed policy hooks, which is how build.sh continues.
        # Both patch sets went into this tree, which is what proves they are
        # orthogonal; the hooks only ever come from the path this build selected, so
        # the mailbox payloads are checked in the mailbox build below and asserted
        # absent here.
        r = run_dynfb(src, "bar1", version)
        dynfb = repo.dynfb()
        assert (src / dynfb.HEADER_DST).is_file(), "policy header not copied"
        payloads = {name: payload for name, _, _, _, payload in dynfb.HOOKS}
        for name, key, _, _, payload_name in dynfb.HOOKS:
            if "mailbox" in (dynfb.OPTIONAL.get(name) or ()):
                continue
            rel = dynfb.FILES[key]
            landed = (src / rel).read_text()
            assert (DYNFB.parent / payload_name).read_text().strip() in landed, \
                "%s missing from %s on %s" % (payload_name, rel, version)
        assert (DYNFB.parent / payloads["p2p-caps-mailbox"]).read_text().strip() \
            not in (src / dynfb.FILES["gpu"]).read_text(), \
            "the mailbox caps override landed in a BAR1 build"

        # And reached it in the right order.  The policy header reads the registry
        # strings and the WPR meta accessor our own sec2 patch defines, so the
        # include has to sit below both; and it is the thing every hook calls, so
        # it has to sit above them all.  An include placed too early compiles
        # everything except the one file that includes it, which is a confusing
        # way to discover the anchor moved.
        gsp = (src / dynfb.FILES["gsp"]).read_text()
        include_at = gsp.index('#include "kernel/gpu/gsp/cmp_dynamic.h"')
        for needed in ('#include "nvrm_registry.h"',
                       "#define SEC2_POSTBL_TIMING_WPR_META"):
            assert gsp.index(needed) < include_at, "%s is used above the include" % needed
        called = set()
        for _, key, _, _, payload_name in dynfb.HOOKS:
            if dynfb.FILES[key] != dynfb.FILES["gsp"]:
                continue
            m = re.match(r"(_kgspCmpDyn\w+)\s*\(",
                         (DYNFB.parent / payload_name).read_text().lstrip())
            if m:
                called.add(m.group(1))
        assert called, "no hook calls the policy, so what was this all for"
        for name in sorted(called):
            assert include_at < gsp.index(name + "("), \
                "%s is called above the include" % name


@pytest.mark.parametrize("version", VERSIONS)
def test_mailbox_p2p_applies_on_every_release(version):
    """The second P2P path is held to the same rule as the first.

    --p2p=mailbox builds a different driver, not a reconfigured one: neither BAR1
    patch is in it, two patches of its own are, and gpu.c gets a different caps
    override, one that will not claim support on a card whose PRI trap is fused
    shut.  None of that gets a version list either, and the one place the two paths
    meet is the anchor they share in gpu.c, which apply.py refuses to double-fill.
    """
    assert shutil.which("patch"), "GNU patch is not installed"
    gm200 = "src/nvidia/src/kernel/gpu/bus/arch/maxwell/kern_bus_gm200.c"
    with tempfile.TemporaryDirectory() as tmp:
        src = extract(version, tmp, ORDER + MAILBOX_ORDER)
        run_dynfb(src, "mailbox", version)
        dynfb = repo.dynfb()
        payloads = {name: (DYNFB.parent / payload).read_text()
                    for name, _, _, _, payload in dynfb.HOOKS}

        gpu_c = (src / dynfb.FILES["gpu"]).read_text()
        assert payloads["p2p-caps-mailbox"].strip() in gpu_c, "no mailbox caps override"
        assert payloads["p2p-caps"].strip() not in gpu_c, \
            "the unconditional BAR1 caps override landed in a mailbox build"
        # The BAR1 route reaches the peer through this HAL; a mailbox build that
        # repointed it would be running both protocols out of one driver.
        assert "kbusGetBar1P2PDmaInfo_GH100; // cmpunlocker" not in \
            (src / dynfb.FILES["nvoc"]).read_text()
        assert "skipping mailbox peer pre-registration" not in \
            (src / "src/nvidia/src/kernel/gpu/bus/arch/maxwell/kern_bus_gm107.c").read_text(), \
            "0013 cleared the peer mask a mailbox build needs populated"

        trap = (src / gm200).read_text()
        assert "_cmpP2PTrapArm_GM200(pGpu0, &localTrap" in trap
        assert "_cmpP2PTrapArm_GM200(pGpu1, &remoteTrap" in trap
        # Three restores, and which three matters: one after each of the two RM calls
        # that may block, and one on the arm path's own readback failure.  Two would
        # mean a peer setup that returned early left the trap armed; zero would mean
        # the patch trusts the mailbox block to stay privileged.
        assert trap.count("_cmpP2PTrapRestore_GM200(pGpu") == 3, \
            [line for line in trap.splitlines()
             if "_cmpP2PTrapRestore_GM200(pGpu" in line]

        gsp = (src / dynfb.FILES["gsp"]).read_text()
        assert '{ 0x0012277cU, 0xffffffffU, "TRAP31_PLM" }' in gsp
        assert "plmIdx < 13" in gsp, "the table grew but the loop still walks twelve rows"
        assert '"CMPEnableP2PMailbox"' in gsp, \
            "the extra mask is not gated, so a plain build would open it too"


def test_p2p_hooks_are_off_unless_their_path_is_the_one_built():
    """A P2P path that was not built has to be absent, not merely inert.

    The gated hooks are the only part of either path that reaches outside
    driver/patches/p2p/, so they are the whole of what "optional" has to mean: each
    one reports itself off, and every file none of the placed hooks touches comes
    out byte for byte as upstream shipped it.  That matters more than it looks: one
    of them turns resizable BAR on for every GPU the module drives, because a
    registry default cannot be gated by device ID, and a plain build must not
    acquire that.  It is checked for all three modes rather than two, because the
    two paths differ only in which of these hooks they place, and both of them
    anchor on the same two lines of gpu.c.
    """
    version = VERSIONS[0]
    dynfb = repo.dynfb()
    payloads = {name: (DYNFB.parent / payload).read_text()
                for name, _, _, _, payload in dynfb.HOOKS}
    gated = {name: (dynfb.FILES[key], set(dynfb.OPTIONAL[name]))
             for name, key, _, _, _ in dynfb.HOOKS if name in dynfb.OPTIONAL}
    assert len({payloads[n] for n in gated}) == len(gated), \
        "two gated hooks share a payload, so its absence would prove nothing"

    for mode in ("off", "bar1", "mailbox"):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["tar", "-xzf", str(tarball(version)), "-C", tmp], check=True)
            src = pathlib.Path(tmp, "open-gpu-kernel-modules-" + version)
            for name in ORDER:                      # the base set, as a plain build does
                r = apply_patch(src, name)
                assert r.returncode == 0, "%s on %s:\n%s%s" % (
                    name, version, r.stdout, r.stderr)

            r = run_dynfb(src, mode, version)
            reported = sorted(line.split()[0] for line in r.stdout.splitlines()
                              if " off " in line)
            assert reported == sorted(n for n, (_, modes) in gated.items()
                                      if mode not in modes), r.stdout

            placed = {n for n, (_, modes) in gated.items() if mode in modes}
            for name, (rel, _) in gated.items():
                text = (src / rel).read_text()
                assert (payloads[name].strip() in text) == (name in placed), \
                    "%s %s in a --p2p=%s build" % (
                        name, "landed" if name not in placed else "is missing", mode)
                if name not in placed and not any(gated[n][0] == rel for n in placed):
                    assert text == stock_file(version, rel), \
                        "%s is not the file upstream shipped in a --p2p=%s build" % (
                            rel, mode)
