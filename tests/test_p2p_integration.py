"""Offline P2P integration checks; never builds/loads a real kernel module.

System-facing shell scripts run from a temporary copy with every system path
redirected under that directory and privileged external commands replaced by
test doubles. Patch composition uses reconstructed context fixtures, not a
complete NVIDIA release; it cannot establish release build compatibility.
"""
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest

import repo

ROOT = repo.ROOT
# Counted, not typed: a patch added to PATCH_ORDER changes how many times
# build.sh calls patch(1), and this file should follow it instead of drifting.
BASE_PATCHES = len(repo.patch_order())
# The two P2P paths are separate builds, so a build of one applies only its own
# list.  Both are named here rather than read, and
# test_the_patch_lists_are_the_ones_build.sh_selects is what keeps the two
# representations from drifting apart.
BAR1_NAMES = ("0011-p2p-bar1.patch", "0013-skip-mailbox-peer-preinit.patch",
              "0015-bar1p2p-readcap-override.patch")
MAILBOX_NAMES = ("0020-mailbox-trap31.patch", "0021-mailbox-trap31-plm.patch")
BAR1_PATCHES = BASE_PATCHES + len(BAR1_NAMES)
ALL_PATCHES = BAR1_PATCHES + len(MAILBOX_NAMES)


def hunks(path):
    """Read and validate unified-diff counts, returning each old/new context."""
    lines = path.read_text().splitlines(keepends=True)
    target = None
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("+++ "):
            target = line.split()[1].split("/", 1)[1]
        match = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
        if not match:
            i += 1
            continue
        old_count, new_count = int(match[2] or 1), int(match[4] or 1)
        old, new = [], []
        i += 1
        while len(old) < old_count or len(new) < new_count:
            line = lines[i]
            # GNU patch also accepts an empty context line without its space.
            prefix, content = (" ", "\n") if line == "\n" else (line[0], line[1:])
            if prefix in " -":
                old.append(content)
            if prefix in " +":
                new.append(content)
            if prefix not in " +-\\":
                raise AssertionError(f"Malformed hunk in {path}: {line!r}")
            i += 1
        if len(old) != old_count or len(new) != new_count:
            raise AssertionError(f"Bad hunk counts in {path}")
        yield target, int(match[1]), old, new


MOCK = r'''import hashlib, json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
root = pathlib.Path(os.environ["TEST_ROOT"])
conf = root / "system/etc/modprobe.d/cmp-pcie-gen2.conf"
record = {"cmd": name, "args": sys.argv[1:],
          "config": conf.read_text() if conf.exists() else None}
payload = ""
if name == "patch":
    payload = sys.stdin.read()
    record["sha256"] = hashlib.sha256(payload.encode()).hexdigest()
with (root / "events.jsonl").open("a") as stream:
    stream.write(json.dumps(record) + "\n")
if name == "uname":
    print("p2p-test-kernel")
elif name == "patch":
    if os.environ.get("FAIL_PATCH") and os.environ["FAIL_PATCH"] in payload:
        sys.exit(1)
elif name == "make":
    out = pathlib.Path.cwd() / "kernel-open/nvidia.ko"
    out.parent.mkdir(parents=True, exist_ok=True)
    # The feature strings have to be in it: build.sh checks for them before it
    # claims a build-only run proved anything, and a mock that cannot hold them
    # would make that check pass for the wrong reason.
    out.write_text("mock module; not loadable\n"
                   "CMP-DYN CMP-BAR1 cmp_pcie_gen\n")
elif name == "update-initramfs" and os.environ.get("FAIL_INITRAMFS"):
    sys.exit(1)
elif name == "modprobe" and "-n" in sys.argv:
    print("insmod " + str(root / "system/lib/modules/p2p-test-kernel/updates/cmpunlocker/nvidia.ko"))
elif name == "modinfo" and "srcversion" in sys.argv:
    print("MOCK")
elif name == "nvidia-smi":
    if "--query-gpu=driver_version" in sys.argv:
        print(os.environ["TEST_VERSION"])
    else:
        print("00000000:01:00.0, 8192\n00000000:02:00.0, 10240")
elif name == "lspci":
    print("01:00.0 3D controller: NVIDIA CMP [10de:20c2]")
    print("02:00.0 3D controller: NVIDIA CMP [10de:2082]")
elif name == "curl":
    sys.exit("Offline test unexpectedly attempted a download")
'''


class P2PIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix=".p2p-test-", dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.tmp = Path(self.temp.name)
        self.project = self.tmp / "project"
        self.project.mkdir()
        for name in ("driver", "common", "tools"):
            shutil.copytree(ROOT / name, self.project / name,
                            ignore=shutil.ignore_patterns(".build", "__pycache__"))
        shutil.copy2(ROOT / "install.sh", self.project / "install.sh")
        for script in (self.project / "install.sh", self.project / "driver/build.sh"):
            text = script.read_text()
            for prefix in ("/lib/modules/", "/lib/firmware/", "/etc/", "/usr/local/",
                           "/proc/", "/sys/", "/boot/"):
                text = text.replace(prefix, str(self.tmp / "system") + prefix)
            # Only the isolated copy runs as simulated root. All paths and
            # system commands it can touch have already been redirected.
            text = text.replace('${EUID}', '${CMPUNLOCKER_TEST_EUID}')
            # Some restricted runners have no /dev/fd support. These fixture
            # producers always emit at least one line, so here-strings are
            # equivalent for their mapfile calls; production scripts stay intact.
            text = re.sub(r"mapfile -t (\w+) < <\((.*?)\)\n",
                          lambda m: 'mapfile -t ' + m[1] + ' <<< "$(' + m[2] + ')"\n',
                          text, flags=re.S)
            text = text.replace('exec > >(tee -a "${LOG_FILE}") 2>&1',
                                '# Output captured by the test subprocess.')
            # Anchor placement needs a real, already-patched release, and this
            # sandbox doubles patch(1), so it doubles the placement step too.  What
            # gets doubled is the call, not the file: the argv build.sh passes is what
            # decides whether the optional P2P hooks are placed, and a double of
            # apply.py itself would not have to answer for it.
            if script.name == "build.sh":
                assert '"--p2p=${P2P_MODE}"' in text, \
                    "build.sh no longer tells the hooks which P2P path this build is"
                double = (
                    '# Test double for the dynfb placement step; see setUp.\n'
                    'python3() {\n'
                    '    if [[ "$1" == *dynfb/apply.py && "$2" != --header-dest ]]; then\n'
                    '        printf \'{"cmd": "dynfb", "argv": "%s"}\\n\' "$*" \\\n'
                    '            >> "${TEST_ROOT}/events.jsonl"\n'
                    '        mkdir -p "${SRC_DIR}/$(dirname "${DYNFB_HEADER_DST}")" &&\n'
                    '            cp "${DYNFB_DIR}/cmp_dynamic.h" "${SRC_DIR}/${DYNFB_HEADER_DST}"\n'
                    '        return 0\n'
                    '    fi\n'
                    '    command python3 "$@"\n'
                    '}\n')
                head, _, rest = text.partition("\n")
                text = head + "\n" + double + rest
            script.write_text(text)
        self.moddir = self.tmp / "system/lib/modules/p2p-test-kernel/updates/cmpunlocker"
        (self.moddir.parents[1] / "build").mkdir(parents=True)
        (self.tmp / "system/proc").mkdir(parents=True)
        (self.tmp / "system/proc/modules").write_text("")
        self.conf = self.tmp / "system/etc/modprobe.d/cmp-pcie-gen2.conf"
        bindir = self.tmp / "bin"
        bindir.mkdir()
        for cmd in ("uname", "patch", "make", "modprobe", "modinfo", "systemctl",
                    "depmod", "update-initramfs", "lspci", "nvidia-smi", "dkms", "curl"):
            path = bindir / cmd
            path.write_text("#!" + sys.executable + "\n" + MOCK)
            path.chmod(0o755)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("CMPUNLOCKER_")}
        # Any supported release will do now that the P2P set carries no version list;
        # the first line is the one build.sh would pick on its own, and the one a
        # branch bump changes first, so that is what this covers.
        self.version = repo.versions()[0]
        self.env.update(PATH=str(bindir) + os.pathsep + os.environ["PATH"],
                        TEST_ROOT=str(self.tmp), TEST_VERSION=self.version,
                        CMPUNLOCKER_TEST_EUID="0", NO_COLOR="1",
                        CMPUNLOCKER_DRIVER_VERSION=self.version)
        self.build_root = self.project / "driver/.build"
        self.build_root.mkdir()
        self.src_name = "open-gpu-kernel-modules-" + self.version
        source = self.tmp / "archive" / self.src_name
        gsp = source / "src/nvidia/src/kernel/gpu/gsp/kernel_gsp.c"
        gsp.parent.mkdir(parents=True)
        # Simulates the result of the original unlock patch sequence. The
        # optional patches themselves are tested with real patch below.
        gsp.write_text("\n".join((
            "SEC2_POSTBL_TIMING_CMP_170HX_8GB_PCI_DEVICE_ID",
            "SEC2_POSTBL_TIMING_CMP_170HX_10GB_PCI_DEVICE_ID",
            "0x02779000U 0x02669000U",
            "0x0000001000000000ULL 0x0000000A00000000ULL")) + "\n")
        with tarfile.open(self.build_root / (self.src_name + ".tar.gz"), "w:gz") as tar:
            tar.add(source, arcname=self.src_name)

    def run_script(self, script="driver/build.sh", args=(), **env):
        merged = dict(self.env, **env)
        return subprocess.run(["bash", str(self.project / script), *args],
                              env=merged, text=True, capture_output=True, timeout=30)

    def events(self, clear=False):
        path = self.tmp / "events.jsonl"
        events = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        if clear:
            path.write_text("")
        return events

    def successful_build(self, value=None, **env):
        if value is not None:
            env["CMPUNLOCKER_ENABLE_P2P"] = value
        result = self.run_script(**env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return self.events(clear=True)

    def test_default_and_empty_disable_all_p2p_patches(self):
        events = self.successful_build()
        self.assertEqual(sum(e["cmd"] == "patch" for e in events), BASE_PATCHES)
        self.assertEqual((self.moddir / "p2p_enabled").read_text(), "off\n")
        self.assertEqual(self.conf.read_text(),
                         'options nvidia NVreg_EnableResizableBar=1 '
                         'NVreg_RegistryDwords="RmForceEnableGen2=1;RMPcieLinkSpeed=0x1;'
                         'RmForceDisableIomapWC=1"\n')
        self.assertFalse(any(e["cmd"] == "patch" for e in self.successful_build("")))

    def test_the_feature_is_passed_to_the_hook_placer(self):
        """--p2p reaches the hooks, because nothing else gates them any more.

        There is no version list to hide a missing feature behind: the P2P sites are
        anchors in the same placer as the dynamic-capacity hooks, so the command line
        build.sh runs is the one place where "this build has no P2P in it" and "this
        build is the mailbox one" get decided.  Logged by the double, which sees the
        argv as build.sh wrote it.  0 and 1 are the spellings that existed before the
        second path, and they still mean what they meant then.
        """
        for value, asked in (("off", "--p2p=off"), ("bar1", "--p2p=bar1"),
                             ("mailbox", "--p2p=mailbox"), ("1", "--p2p=bar1"),
                             ("0", "--p2p=off")):
            events = self.successful_build(value)
            argv = [e["argv"] for e in events if e["cmd"] == "dynfb"]
            self.assertEqual(len(argv), 1, "one placement step per build: %r" % argv)
            self.assertIn(asked, argv[0].split(), "CMPUNLOCKER_ENABLE_P2P=%s" % value)

    def test_build_only_reports_markers_and_installs_nothing(self):
        """CMPUNLOCKER_BUILD_ONLY=1 proves the build and leaves the machine alone."""
        result = self.run_script(CMPUNLOCKER_BUILD_ONLY="1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for marker in ("CMP-DYN", "CMP-BAR1", "cmp_pcie_gen"):
            self.assertIn("nvidia.ko contains " + marker, result.stdout)
        events = self.events(clear=True)
        for cmd in ("depmod", "modprobe", "update-initramfs", "systemctl"):
            self.assertEqual([e for e in events if e["cmd"] == cmd], [],
                             "%s ran in build-only mode" % cmd)
        self.assertFalse(self.conf.exists(), "build-only wrote the modprobe config")
        self.assertFalse((self.moddir / "nvidia.ko").exists(), "build-only installed")

    def test_dynfb_placed_after_patches_before_build(self):
        """The hooks go in once the patch set is complete and before compiling."""
        order = [e["cmd"] for e in self.successful_build()]
        self.assertIn("dynfb", order)
        self.assertLess(max(i for i, cmd in enumerate(order) if cmd == "patch"),
                        order.index("dynfb"))
        self.assertLess(order.index("dynfb"), order.index("make"))
        self.assertTrue((self.tmp / "system/lib/modules/p2p-test-kernel/updates/"
                         "cmpunlocker/nvidia.ko").is_file())

    def patch_tail(self, events, names):
        """The optional patches, by content, as the last patch(1) calls of the run."""
        tail = [e for e in events if e["cmd"] == "patch"][-len(names):]
        for event, name in zip(tail, names):
            data = (self.project / "driver/patches/p2p" / name).read_bytes()
            self.assertEqual(event["sha256"], hashlib.sha256(data).hexdigest(),
                             "%s was not applied from the file on disk" % name)
            self.assertIn("--fuzz=0", event["args"],
                          "%s was applied with fuzz; an optional hunk may not slide"
                          % name)
        return tail

    def test_switching_paths_rebuilds_and_writes_the_right_keys(self):
        """off -> bar1 -> mailbox -> off, with the tree rebuilt for each move.

        The two paths are different drivers, not one driver configured two ways:
        RM carries one PCIe P2P protocol per peer pair, so the only honest meaning
        of --p2p=mailbox is a build with no BAR1 route in it, and the cache stamp
        has to notice that on its own.  The modprobe config is the other half: the
        keys that make BAR1 P2P are the keys the mailbox route exists to avoid.
        """
        self.successful_build("off")
        stamp = self.build_root / self.src_name / ".cmpunlocker-stamp"
        off_stamp = stamp.read_text()
        off_config = self.conf.read_text()

        events = self.successful_build("bar1")
        self.assertEqual(sum(e["cmd"] == "patch" for e in events), BAR1_PATCHES)
        self.patch_tail(events, BAR1_NAMES)
        self.assertNotEqual(stamp.read_text(), off_stamp)
        self.assertEqual((self.moddir / "p2p_enabled").read_text(), "bar1\n")
        init = next(e for e in events if e["cmd"] == "update-initramfs")
        self.assertIn(";RMForceStaticBar1=1;RMPcieP2PType=1", init["config"])
        self.assertNotIn("CMPEnableP2PMailbox", init["config"])
        self.assertFalse(any(e["cmd"] == "modprobe" for e in events))
        self.assertFalse(any(e["cmd"] == "patch" for e in self.successful_build("bar1")),
                         "asking for the path it already had re-extracted the tree")

        events = self.successful_build("mailbox")
        self.assertEqual(sum(e["cmd"] == "patch" for e in events),
                         BASE_PATCHES + len(MAILBOX_NAMES))
        self.patch_tail(events, MAILBOX_NAMES)
        self.assertEqual((self.moddir / "p2p_enabled").read_text(), "mailbox\n")
        init = next(e for e in events if e["cmd"] == "update-initramfs")
        self.assertIn(";CMPEnableP2PMailbox=1", init["config"])
        for key in ("RMForceStaticBar1", "RMPcieP2PType"):
            self.assertNotIn(key, init["config"],
                             "%s belongs to the other path" % key)

        events = self.successful_build("off")
        self.assertEqual(sum(e["cmd"] == "patch" for e in events), BASE_PATCHES)
        self.assertEqual(stamp.read_text(), off_stamp)
        self.assertEqual(self.conf.read_text(), off_config)
        self.assertFalse(any(e["cmd"] == "modprobe" for e in events))

    def test_a_build_from_the_old_switch_still_demands_a_cold_boot(self):
        """p2p_enabled held 0 or 1 before --p2p grew a second path.

        Whether to reload the modules live is decided by what is installed, not by
        what this build wants, so the old spelling of "on" has to count: an upgrade
        from a bar1 install to an off build that reloaded over it would tear peer
        state down underneath a GSP that still has mailboxes mapped.
        """
        self.moddir.mkdir(parents=True, exist_ok=True)
        (self.moddir / "p2p_enabled").write_text("1\n")
        result = self.run_script(CMPUNLOCKER_ENABLE_P2P="off")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("cold reboot", result.stdout)
        self.assertFalse(any(e["cmd"] in ("modprobe",) and "-r" in e["args"]
                             for e in self.events(clear=True)))

    def test_patch_edit_invalidates_cached_build(self):
        self.successful_build("bar1")
        patch = self.project / "driver/patches/p2p/0011-p2p-bar1.patch"
        patch.write_text("Fixture revision\n" + patch.read_text())
        events = self.successful_build("bar1")
        self.assertEqual(sum(e["cmd"] == "patch" for e in events), BAR1_PATCHES)

    def test_failed_patch_never_installs_or_stamps_partial_build(self):
        for mode, failure in (("bar1", "skipping mailbox peer pre-registration"),
                              ("mailbox", "CMP_TRAP31_SET_PRIV")):
            result = self.run_script(CMPUNLOCKER_ENABLE_P2P=mode, FAIL_PATCH=failure)
            self.assertNotEqual(result.returncode, 0,
                                "%s build survived a %s patch failure" % (mode, failure))
            self.assertFalse((self.build_root / self.src_name / ".cmpunlocker-stamp").exists())
            self.assertFalse(self.conf.exists())
            self.assertFalse((self.moddir / "p2p_enabled").exists())
            self.assertFalse(any(e["cmd"] == "make" for e in self.events(clear=True)))
        events = self.successful_build("bar1")
        self.assertEqual(sum(e["cmd"] == "patch" for e in events), BAR1_PATCHES)

    def test_installer_flag_is_authoritative_and_preserves_mixed_inventory(self):
        options = ["--no-iommu", "--no-gen2-service", "--no-passthrough"]
        result = self.run_script("install.sh", options, CMPUNLOCKER_ENABLE_P2P="bar1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.moddir / "p2p_enabled").read_text(), "off\n")
        result = self.run_script("install.sh", options + ["--p2p", "--profile=10gb"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.moddir / "p2p_enabled").read_text(), "bar1\n")
        self.assertEqual((self.moddir / "card_profile").read_text(), "mixed\n")
        self.assertIn("20c2 8gb 65536", (self.moddir / "gpu_inventory").read_text())
        self.assertIn("2082 10gb 40960", (self.moddir / "gpu_inventory").read_text())
        self.assertIn("RMForceStaticBar1=1", self.conf.read_text())
        # The same script, the second path: what the flag selects has to reach the
        # build, and what the build writes has to say mailbox and nothing else.
        result = self.run_script("install.sh", options + ["--p2p=mailbox"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.moddir / "p2p_enabled").read_text(), "mailbox\n")
        self.assertIn("CMPEnableP2PMailbox=1", self.conf.read_text())
        self.assertNotIn("RMForceStaticBar1", self.conf.read_text())
        help_text = self.run_script("install.sh", ["--help"]).stdout
        for advertised in ("--p2p=bar1", "--p2p=mailbox", "--p2p=off"):
            self.assertIn(advertised, help_text)

    def test_an_unknown_p2p_path_is_refused_before_anything_is_built(self):
        result = self.run_script("install.sh", ["--p2p=nvlink"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unknown --p2p value", result.stderr + result.stdout)
        self.assertFalse(self.conf.exists())
        self.assertFalse(any(e["cmd"] in ("patch", "make") for e in self.events()))

    def test_initramfs_failure_reports_incomplete_p2p_install(self):
        result = self.run_script(CMPUNLOCKER_ENABLE_P2P="bar1", FAIL_INITRAMFS="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("initramfs was not rebuilt", result.stderr)
        self.assertNotIn("initramfs rebuilt", result.stdout)
        self.assertTrue(self.conf.exists())
        self.assertEqual((self.moddir / "p2p_enabled").read_text(), "bar1\n")
        self.assertFalse(any(e["cmd"] == "modprobe" for e in self.events(clear=True)))
        self.successful_build("bar1")

    def test_invalid_feature_values_fail_before_build(self):
        for value in ("2", "-1", "false", "on", "nvlink", "BAR1"):
            with self.subTest(value=value):
                result = self.run_script(CMPUNLOCKER_ENABLE_P2P=value)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("must be off, bar1 or mailbox", result.stderr)
        self.assertFalse(self.conf.exists())
        self.assertFalse(any(e["cmd"] in ("patch", "make") for e in self.events()))

    def test_all_profiles_and_missing_optional_patch_manifest(self):
        for profile in ("8gb", "10gb", "mixed"):
            self.successful_build("bar1", CMPUNLOCKER_CARD_PROFILE=profile)
            self.assertEqual((self.moddir / "card_profile").read_text().strip(), profile)
        # Both paths are checked on every build, not the one being built: a tree
        # that lost the mailbox trap today cannot build it tomorrow either, and
        # finding that out at the first --p2p=mailbox is too late a place.
        for name, mode in (("0011-p2p-bar1.patch", "off"),
                          ("0020-mailbox-trap31.patch", "off"),
                          ("0021-mailbox-trap31-plm.patch", "bar1")):
            (self.project / "driver/patches/p2p" / name).unlink()
            result = self.run_script(CMPUNLOCKER_ENABLE_P2P=mode)
            self.assertNotEqual(result.returncode, 0, "%s went unnoticed" % name)
            self.assertIn("optional P2P patch missing", result.stderr)
            (self.project / "driver/patches/p2p" / name).write_bytes(
                (repo.ROOT / "driver/patches/p2p" / name).read_bytes())

    def test_the_patch_lists_are_the_ones_build_sh_selects(self):
        """The names above are typed; the ones build.sh reads are the contract."""
        self.assertEqual(repo.p2p_patch_order("bar1"),
                         ["p2p/" + n for n in BAR1_NAMES])
        self.assertEqual(repo.p2p_patch_order("mailbox"),
                         ["p2p/" + n for n in MAILBOX_NAMES])
        self.assertEqual(sorted(repo.p2p_patch_order()),
                         sorted(["p2p/" + n for n in BAR1_NAMES + MAILBOX_NAMES]))

    @unittest.skipUnless(shutil.which("patch"), "patch required for fixture composition")
    def test_p2p_patches_apply_without_disabled_debug_patches(self):
        directory = ROOT / "driver/patches"
        patches = [directory / "bar0-pramin-clamp.patch"]
        patches += [directory / "p2p" / name for name in BAR1_NAMES]
        contexts = {}
        for patch in patches:
            for target, start, old, _ in hunks(patch):
                positions = contexts.setdefault(target, {})
                for offset, line in enumerate(old, start):
                    if offset in positions:
                        self.assertEqual(positions[offset], line)
                    positions[offset] = line
        fixture = self.tmp / "patch-context-fixture"
        for target, positions in contexts.items():
            file = fixture / target
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text("".join(positions.get(i, f"/* context gap {i} */\n")
                                    for i in range(1, max(positions) + 1)))
        for patch in patches:
            result = subprocess.run(["patch", "--batch", "--forward", "--fuzz=0", "-p1"],
                                    input=patch.read_text(), cwd=fixture,
                                    text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, str(patch) + result.stdout + result.stderr)
        text = (fixture / "src/nvidia/src/kernel/platform/p2p/p2p_caps.c").read_text()
        self.assertIn("forcing read cap OK", text)
        self.assertNotIn("hostsys commonSwitch=", text)

    @unittest.skipUnless(shutil.which("gcc"), "gcc required for isolated C guard check")
    def test_cmp_capability_guards(self):
        # Compile the inserted C blocks with minimal types and exercise the
        # actual device-ID guards, preserving arbitrary stock status values.
        def additions(name):
            lines = (ROOT / "driver/patches/p2p" / name).read_text().splitlines()
            return "\n".join(line[1:] for line in lines
                             if line.startswith("+") and not line.startswith("+++"))
        def payload(rel):
            """A dynfb hook's inserted text, which is the same code from a patch now."""
            return (ROOT / "driver/dynfb" / rel).read_text()

        source = '''#include <assert.h>
#include <string.h>
typedef unsigned NvU32;
typedef struct { struct { NvU32 PCIDeviceID; } idInfo;
                 NvU32 pcieP2PReadCaps, pcieP2PWriteCaps; } OBJGPU;
#define NV0000_P2P_CAPS_STATUS_OK 0
#define NV_PRINTF(...) ((void)0)
void caps(OBJGPU *pGpu) {
''' + payload("hooks/p2p_caps.c") + '''
}
// The mailbox variant, with the two things it asks the driver for stubbed out.  The
// key name is matched rather than ignored, because an override gated on a key the
// build never writes is an override that is always off, and that passes every test
// but the one on the bench.
#define NV_OK 0
static NvU32 g_plm, g_key;
static int osReadRegistryDword(OBJGPU *pGpu, const char *key, NvU32 *out) {
    (void)pGpu;
    if (strcmp(key, "CMPEnableP2PMailbox") != 0)
        return 1;
    *out = g_key;
    return NV_OK;
}
#define gpuGetInstance(p) (0U)
#define GPU_REG_RD32(p, r) (g_plm)
void mailbox_caps(OBJGPU *pGpu) {
''' + payload("hooks/p2p_caps_mailbox.c") + '''
}
void readcap(OBJGPU *pGpu, NvU32 *pP2PReadCapStatus) {
''' + additions("0015-bar1p2p-readcap-override.patch") + '''
}
int main(void) {
    unsigned ids[] = {0x20c2, 0x2082, 0x20b0, 0x1234};
    for (unsigned i = 0; i < 4; ++i) {
        OBJGPU gpu = {{(ids[i] << 16) | 0x10de}, 7, 9};
        NvU32 status = 3;
        caps(&gpu); readcap(&gpu, &status);
        assert(gpu.pcieP2PReadCaps == (i < 2 ? 0 : 7));
        assert(gpu.pcieP2PWriteCaps == (i < 2 ? 0 : 9));
        assert(status == (i < 2 ? 0 : 3));
        status = 0; readcap(&gpu, &status); assert(status == 0);
    }
    // The guard, in all four combinations: only a CMP, only with the key, only
    // with the trap's mask actually open.  The middle case is the whole reason
    // this hook is not the unconditional one above.
    for (unsigned i = 0; i < 4; ++i) {
        NvU32 plms[] = {0xFFFFFFFFU, 0xFFFFFF8FU, 0xFFFFFFFFU};
        NvU32 keys[] = {1, 1, 0};
        for (int t = 0; t < 3; ++t) {
            OBJGPU gpu = {{(ids[i] << 16) | 0x10de}, 7, 9};
            g_plm = plms[t]; g_key = keys[t];
            mailbox_caps(&gpu);
            int armed = (i < 2) && (t == 0);
            assert(gpu.pcieP2PReadCaps == (armed ? 0 : 7));
            assert(gpu.pcieP2PWriteCaps == (armed ? 0 : 9));
        }
    }
    return 0;
}
'''
        file = self.tmp / "guards.c"
        file.write_text(source)
        binary = self.tmp / "guards"
        result = subprocess.run(["gcc", "-std=c99", "-Wall", "-Wextra", "-Werror",
                                 str(file), "-o", str(binary)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        subprocess.run([str(binary)], check=True, timeout=10)


class _FakeCuda(object):
    """Peer copies that actually move bytes, so the checker's own logic is testable.

    Every buffer is host memory and every copy is a slice assignment, which means
    the only thing a subclass can change to fail a test is the failure mode the
    field showed: a destination that stops receiving data partway through.
    """

    def __init__(self, *args, **kwargs):
        self.buffers = {}
        self.copies = []

    def devices(self):
        return [0, 1]

    def name(self, device):
        return "fake %d" % device

    def create(self, device):
        return ("context", device)

    def current(self, context):
        return None

    def enable(self, device, peer, peer_context):
        return None

    def allocate(self, size):
        handle = ("buffer", len(self.buffers))
        self.buffers[handle] = bytearray(size)
        return handle

    def upload(self, pointer, data):
        if len(data) > len(self.buffers[pointer]):
            raise RuntimeError("upload larger than the allocation")
        self.buffers[pointer][:len(data)] = data

    def download(self, pointer, size):
        return bytes(self.buffers[pointer][:size])

    def copy(self, dest, dest_context, source, source_context, size):
        self.copies.append((dest, source, size))
        moved = min(size, len(self.buffers[source]), len(self.buffers[dest]))
        self.buffers[dest][:moved] = self.buffers[source][:moved]

    def destroy(self, context):
        return None


class ContentChecker(unittest.TestCase):
    """The byte-level checker, offline.

    On the mailbox route this script is the difference between "peer copies work"
    and a build that has never been asked the question: an aperture that was never
    programmed returns CUDA_OK and moves garbage, so nothing that looks at return
    codes can see the failure.  That makes the sizes it checks worth pinning down
    -- they have to straddle the window the mailbox is defined in -- and so does
    the one piece of arithmetic it reports, the offset of the first wrong byte,
    which is what names the mechanism.  None of that needs a GPU.
    """

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "p2p_content_check", ROOT / "tools" / "p2p-content-check.py")
        cls.tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.tool)          # imports ctypes, does not load libcuda

    def setUp(self):
        self.out, self.stdout = io.StringIO(), sys.stdout
        sys.stdout = self.out
        self.real_cuda = self.tool.CUDA

    def tearDown(self):
        sys.stdout = self.stdout
        self.tool.CUDA = self.real_cuda

    def test_sizes_bracket_the_window_the_mailbox_is(self):
        # 64 KiB is PCIE_P2P_WRITE_MAILBOX_SIZE and the tags are per 128 KiB page,
        # which is where the measured failure started: 32 MiB copied, the first
        # 0x20000 bytes right.  One byte either side of each boundary, because a
        # check that lands exactly on a size proves nothing about the next chunk.
        for size in (64 * 1024 - 1, 64 * 1024, 64 * 1024 + 1,
                     128 * 1024 - 1, 128 * 1024, 128 * 1024 + 1, 32 * 1024 * 1024):
            self.assertIn(size, self.tool.SIZES)

    def test_destination_starts_out_wrong_everywhere_including_byte_zero(self):
        expected = bytes(range(256)) * 8
        dest = expected.translate(self.tool.COMPLEMENT)
        self.assertEqual(dest, bytes(b ^ 0xFF for b in expected))
        self.assertNotEqual(dest[0], expected[0],
                            "a destination whose first byte is already right can "
                            "hide a copy that never started")

    def test_complement_table_is_its_own_inverse(self):
        data = os.urandom(4096)
        self.assertEqual(data.translate(self.tool.COMPLEMENT).translate(
            self.tool.COMPLEMENT), data)

    def test_mismatch_names_the_offset_that_identifies_the_mechanism(self):
        # The g250 signature: everything up to a 128 KiB page right, nothing after.
        # The pattern has to avoid zero, because the wrong half of the destination
        # is read back as zero and a pattern containing it would undercount.
        expected = bytes((i % 255) + 1 for i in range(256)) * 4096   # 1 MiB
        actual = bytes(expected[:1 << 17]) + b"\0" * (len(expected) - (1 << 17))
        self.assertEqual(self.tool._mismatch(actual, expected),
                         (len(expected) - (1 << 17), 1 << 17))
        self.assertEqual(self.tool._mismatch(expected, expected), (0, None))

    def test_mismatch_is_chunked_because_timed_copies_are_gigabytes(self):
        # The bandwidth mode checks at sizes a host loop could not afford to walk;
        # equality of the fast path has to be a memcmp, not a per-byte build.
        data = b"\xa5" * (8 << 20)
        self.assertEqual(self.tool._mismatch(data, data), (0, None))

    def test_bandwidth_reports_the_best_sample_and_checks_the_same_size(self):
        class Cuda(_FakeCuda):
            def copy(self, dest, dest_context, source, source_context, size):
                time.sleep(0.01)
                return _FakeCuda.copy(self, dest, dest_context, source,
                                      source_context, size)

        self.assertEqual(self.tool.bandwidth(Cuda(), {0, 1}, 1 << 16, 0.05), 0)
        out = self.read_output()
        self.assertIn("best", out)
        self.assertIn("content PASS", out)
        self.assertIn("0->1", out)
        self.assertIn("1->0", out)

    def test_bandwidth_mode_still_catches_a_window_that_stops_moving(self):
        # Timing a copy whose bytes were never looked at would have called the g250
        # failure a 20 GB/s success.
        class StuckWindow(_FakeCuda):
            def copy(self, dest, dest_context, source, source_context, size):
                _FakeCuda.copy(self, dest, dest_context, source, source_context,
                               min(size, 1 << 17))
                return None

        self.assertEqual(self.tool.bandwidth(StuckWindow(), {0, 1}, 1 << 20, 0.0), 2)
        out = self.read_output()
        self.assertEqual(out.count("content FAIL"), 2, out)
        self.assertIn("first at 0x20000", out)

    def test_argument_forms_the_shell_will_actually_hand_us(self):
        for argv in (["--bw", "--size", "64k", "--seconds", "0", "0", "1"],
                     ["--bw", "--size=64k", "--seconds=0", "0", "1"],
                     ["--bw", "--size=64k", "0", "1"]):
            cuda = _FakeCuda()
            self.tool.CUDA = lambda: cuda
            self.assertEqual(self.tool.main(argv), 0, argv)
            self.assertTrue(cuda.copies, argv)
            self.assertIn("content PASS", self.read_output(), argv)
        for argv in (["--bw", "--size"], ["--bw", "--size=potato"],
                     ["--bw", "--seconds=x"]):
            self.assertEqual(self.tool.main(argv), 2, argv)

    def test_bandwidth_refuses_a_size_the_host_cannot_hold(self):
        # The checked copy needs three host buffers of that size; the alternative to
        # refusing is the OOM killer choosing what dies on a serving box.
        real = self.tool._available_memory
        self.tool._available_memory = lambda: 16 << 20
        try:
            with self.assertRaises(RuntimeError) as caught:
                self.tool.bandwidth(_FakeCuda(), {0, 1}, 8 << 20, 0.0)
            self.assertIn("host memory", str(caught.exception))
        finally:
            self.tool._available_memory = real

    def read_output(self):
        self.out.seek(0)
        return self.out.read()


if __name__ == "__main__":
    unittest.main()
