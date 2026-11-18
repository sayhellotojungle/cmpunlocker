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
                         "p2p-caps", "p2p-resizable-bar", "p2p-bar1dma"}


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


@pytest.mark.parametrize("version", VERSIONS)
def test_patches_apply(version):
    assert shutil.which("patch"), "GNU patch is not installed"
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["tar", "-xzf", str(tarball(version)), "-C", tmp], check=True)
        src = pathlib.Path(tmp, "open-gpu-kernel-modules-" + version)
        assert src.is_dir(), os.listdir(tmp)
        # The optional set is applied on every supported release, with no version
        # list to hide behind: the three sites that used to reject on 615.x are
        # anchors now, so the only reason one of these can miss is that the driver
        # changed the line we mean.
        for name in ORDER + P2P_ORDER:
            r = apply_patch(src, name)
            assert r.returncode == 0, "%s on %s:\n%s%s" % (name, version, r.stdout, r.stderr)

        # ...and the anchor-placed policy hooks, which is how build.sh continues.
        r = subprocess.run([sys.executable, str(DYNFB), "--p2p=1", str(src)],
                           capture_output=True, text=True)
        assert r.returncode == 0, "dynfb on %s:\n%s%s" % (version, r.stdout, r.stderr)
        assert "FAIL" not in r.stdout

        # Report by anchor is not enough: check each payload reached the file its
        # hook names, so a patch that stops applying cannot hide a missing hook.
        dynfb = repo.dynfb()
        assert (src / dynfb.HEADER_DST).is_file(), "policy header not copied"
        for _, key, _, _, payload_name in dynfb.HOOKS:
            rel = dynfb.FILES[key]
            payload = (DYNFB.parent / payload_name).read_text()
            landed = (src / rel).read_text()
            assert payload.strip() in landed, "%s missing from %s on %s" % (
                payload_name, rel, version)

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


def test_p2p_hooks_are_off_unless_the_feature_is_on():
    """Without --p2p the feature has to be absent, not merely inert.

    The three P2P hooks are the only part of the feature that reaches outside
    driver/patches/p2p/, so they are the whole of what "optional" has to mean: each
    one reports itself off, and the files they would edit come out byte for byte as
    upstream shipped them.  This matters more than it looks: one of the three turns
    resizable BAR on for every GPU the module drives, because a registry default
    cannot be gated by device ID, and a plain build must not acquire that.
    """
    version = VERSIONS[0]
    dynfb = repo.dynfb()
    gated = {"p2p-caps": "gpu", "p2p-resizable-bar": "nvreg", "p2p-bar1dma": "nvoc"}
    payloads = {name: payload for name, _, _, _, payload in dynfb.HOOKS}
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["tar", "-xzf", str(tarball(version)), "-C", tmp], check=True)
        src = pathlib.Path(tmp, "open-gpu-kernel-modules-" + version)
        for name in ORDER:                      # the base set, as a plain build does
            r = apply_patch(src, name)
            assert r.returncode == 0, "%s on %s:\n%s%s" % (name, version, r.stdout, r.stderr)

        r = subprocess.run([sys.executable, str(DYNFB), "--p2p=0", str(src)],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr
        reported = sorted(line.split()[0] for line in r.stdout.splitlines()
                          if " off " in line)
        assert reported == sorted(gated), r.stdout

        for name, key in gated.items():
            rel = dynfb.FILES[key]
            text = (src / rel).read_text()
            assert payloads[name] not in text, "%s landed with --p2p off" % name
            assert text == stock_file(version, rel), \
                "%s is not the file upstream shipped, and no P2P patch was applied" % rel
