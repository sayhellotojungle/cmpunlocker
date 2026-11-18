# GPU-to-GPU BAR1 P2P

This ports the P2P implementation from the supplied `cmpunlocker-p2p` into
the `cmpunlocker-0.4` patch-based build. The existing memory profiles, per-device
geometry, PCIe Gen2 and passthrough implementation remain in `0.4`.

## Enable or disable

```bash
sudo ./install.sh --p2p
# Existing flags can be combined with --p2p:
sudo ./install.sh --p2p --profile=10gb --no-passthrough
```

The installer passes `CMPUNLOCKER_ENABLE_P2P=1` to `driver/build.sh`. Direct
builds use the same interface:

```bash
sudo env CMPUNLOCKER_ENABLE_P2P=1 ./driver/build.sh
```

Unset, empty or `0` disables P2P; `1` enables it. Other values are rejected.
The installer uses its command-line flag as the authority: without `--p2p`,
P2P is disabled even if the calling shell exports the environment variable.

For a P2P build, `/etc/modprobe.d/cmp-pcie-gen2.conf` contains one combined
`NVreg_RegistryDwords` value:

```text
RmForceEnableGen2=1;RMPcieLinkSpeed=0x1;RmForceDisableIomapWC=1;RMForceStaticBar1=1;RMPcieP2PType=1
```

`RMPcieLinkSpeed=0x1` is the GDS/P2PDMA-validated baseline: RM is not allowed to
request link speeds above Gen2. Leaving the key unset is not equivalent — RM
then fails `SET_PCIE_SPEED` with `NV_ERR_NOT_SUPPORTED`. `CMPUNLOCKER_EXPERIMENTAL_GEN4=1`
raises it to `0xF` and adds the module parameter `cmp_pcie_gen=0`, which is the
only case in which the value is raised: the Gen3 -> E1 DevInit replay -> Gen4
sequence in `driver/dynfb/hooks/` is what actually initializes the PHY, per card,
from that card's own PCIe speed fuses, and `0xF` on its own would let RM ask for
a speed the link cannot train. The module also receives
`NVreg_EnableResizableBar=1`. Automatic fuse-based PCIe bypass was removed
after a regression, and experimental 80 GiB geometry is off by default. A card
that takes the 80 GiB layout has `RMForceStaticBar1` downgraded to AUTO by the
driver, and with it BAR1 P2P; the reason and the numbers are in
"BAR1 size and the 80 GiB layout" below. Cards on the 64 GiB layout keep static
BAR1.

This file is written before the build script regenerates initramfs. The installer does not overwrite it afterwards. Do not add a second competing
`NVreg_RegistryDwords` option in another modprobe file. The source branch also
reports hangs with an explicit `ForceP2P=0x11` registry option; the installer
does not add that option.

P2P builds, and the first build disabling an installed P2P configuration,
skip the live NVIDIA module reload. Shut down completely and power on again.
If initramfs generation reports a failure, correct it and regenerate initramfs
before rebooting; a stale image may still contain the previous modules/options.

To disable P2P, reinstall without `--p2p` and cold boot. The source cache is
invalidated and all four optional patches are removed by extracting a fresh
driver tree. The combined config is rewritten with only the original Gen2
settings. `remove.sh --yes` removes that config and the installed metadata with
the modules. Keep the same install flags after a kernel upgrade; this merge
retains `0.4`'s manual rebuild workflow.

## BAR1 size and the 80 GiB layout

Forced static BAR1 requires BAR1 to cover the framebuffer. The size BAR1 can take is
limited by the card's own resizable-BAR capability table, which is part of the card's
BIOS. On these cards that table ends at 64 GiB:

```text
Capabilities: [bb0 v1] Physical Resizable BAR
        BAR 1: current size: 64GB, supported: 64MB 128MB 256MB 512MB 1GB 2GB 4GB 8GB 16GB 32GB 64GB
```

64 GiB is the maximum BAR1 the GPU provides as it stands. The limit applies to a
hardware-unlocked card as well as a soft-unlocked one, so it is not a consequence of how
the memory unlock was performed, and it is not a shortage of host address space. 64 GiB is
also the size the 64 GiB memory unlock requires, which is why every card is at that size.

The host provides more address space than the card can use. The 80 GiB layout needs a
contiguous BAR1 of `0x13e6a00000` (79.6 GiB), no size the card offers covers it, and the
driver therefore downgrades `RMForceStaticBar1` to AUTO for that card and reports it. That
is why the card on the 80 GiB layout is not a static-BAR1 card and does not take part in
BAR1 P2P:

```text
NVRM: GPU2 kbusIsStaticBar1Supported_TU102: CMP-BAR1 GPU2: FB 0x13e6a00000 will not fit
in BAR1 0xfffe00000, skipping forced static BAR1
```

The host allocation is visible in the kernel resource map. The region reserved for the
bridge above card `09:00.0` is 128 GiB, because BAR1 (64 GiB) and BAR3 (32 MiB) together
exceed 64 GiB and the allocation is aligned to the next power of two. The host therefore
has address space that the card cannot use:

```text
5000000000-6fffffffff : PCI Bus 0000:08          # 128 GiB
  5000000000-5fffffffff : 0000:09:00.0           # BAR1, 64 GiB
  6000000000-6001ffffff : 0000:09:00.0           # BAR3, 32 MiB
```

The consequence is per card and follows from the card's BIOS. A card on the 80 GiB layout
has 80 GiB of memory and no BAR1 P2P. A card on the 64 GiB layout has 64 GiB of memory and
working BAR1 P2P. Having 80 GiB of memory and BAR1 P2P on the same card requires a BAR1 of
128 GiB, which requires a card BIOS whose resizable-BAR table includes that size; on this
hardware that means an A100-class BIOS update. No driver-side change adds a size the card's
table does not list.

Verification on that machine, after a cold boot and with the serving workload running:
the two cards on the 64 GiB layout passed 2/2 directed peer pairs at 16 MiB per
endpoint, checked at both endpoints, with no Xid afterwards. Peer transfers on the card
using the 80 GiB layout are not claimed, and cannot be claimed while the card provides at
most a 64 GiB BAR1.

## Host requirements

- Every participating CMP GPU needs BAR1 large enough to cover its unlocked
  framebuffer. On the tested platform that limit is 64 GiB and it comes from the
  BIOS, so a card using the 80 GiB layout cannot take part in P2P; see
  "BAR1 size and the 80 GiB layout" above. The 40 GiB framebuffer of device `2082`
  is inside the 64 GiB BAR1.
- Firmware must provide enough address space for all GPU BARs and bridge
  windows. BAR1 expansion is done by the driver at probe time and depends on
  the BIOS allocating sufficient MMIO (Above-4G Decoding, large enough MMIO-H
  window). If the firmware does not provide that space, no driver-side change
  can create it; the BAR stays at its stock size and P2P will not work.
- The actual PCIe topology, ACS routing and IOMMU configuration must carry
  peer reads and writes. `0.4` retains its default IOMMU passthrough setup;
  `--no-iommu` leaves that configuration to the operator.
- A stock guest driver does not inherit these P2P source patches merely
  because an unlocked card was passed through. Guest P2P needs its own patched
  driver and working PCIe mappings in the guest/host configuration.

The source project's throughput numbers and kernel version are records of
its host, not verification of this merged tree on another machine.

## Verify real transfers

After a cold boot, check every CMP GPU, then inspect capability reports:

```bash
sudo ./verify.sh
lspci -tv
for bdf in $(lspci -Dn | awk '/10de:20c2|10de:2082/{print $1}'); do
    sudo lspci -vv -s "$bdf" | sed -n '/Region 1:/p'
done
nvidia-smi topo -p2p r
nvidia-smi topo -p2p w
```

The override makes capability reports say P2P is available. That alone cannot
show that data moves. Build the included CUDA test with a CUDA toolkit:

```bash
nvcc -O2 -arch=sm_80 -o /tmp/cmpunlocker-test-p2p tools/test-p2p.cu
timeout 120s /tmp/cmpunlocker-test-p2p
# Or select CUDA device indices (including two devices across switches):
timeout 120s /tmp/cmpunlocker-test-p2p 0 1
```

`tools/test-p2p.py` performs the same checks through the CUDA driver API and
requires no CUDA toolkit, only `libcuda` with `numpy` and `cuda.bindings`
(`pip install cuda-python numpy`). It takes the same device index arguments.
In a container, resolve the indices with `cuDeviceGetPCIBusId` before choosing
a pair; enumeration order inside a container is not the host's order.

The test runs a GPU kernel for both peer reads and peer writes for every
ordered pair. It initializes the endpoints with different patterns, checks
the copied bytes and verifies the source is unchanged. This detects local
memory aliasing that a simple capability check can miss. CUDA errors,
unavailable peer access or data mismatches return a nonzero exit status;
`timeout` bounds a userspace wait, but cannot recover a wedged GPU/host.

The included test is a correctness test, not a bandwidth benchmark. The
source branch's `benchmark/nvidia_bench.cu` is a single-GPU benchmark, so it is
not used to claim P2P success.

## Implementation map

Three patches in `driver/patches/p2p/` and three hooks placed by
`driver/dynfb/apply.py`, all of them applied only with `--p2p`, after the original
`0.4` patch sequence:

| Site | Implementation |
| --- | --- |
| `dynfb/hooks/p2p_caps.c` | Inline port of `cmpUnlockForceP2PCaps`: overrides the GSP read/write caps for PCI device IDs `20c2` and `2082`, spliced after the two `pcieP2P*Caps` assignments in `gpu.c`. No dependency on the other branch's unlock C file. |
| `dynfb/hooks/p2p_resizable_bar.c` | REBAR default on, by replacing the `__NV_ENABLE_RESIZABLE_BAR` registry default rather than carrying context around it. |
| `dynfb/hooks/p2p_bar1dma.c` | The generated func table's default `kbusGetBar1P2PDmaInfo` points at `_GH100`; replaces that one line in `g_kern_bus_nvoc.c`. |
| `0011-p2p-bar1.patch` | Original BAR1 HAL selection, create/remove mapping branches, CMP BIF defaults, BAR1 bus-address propagation, peer PTE/physical address rewrites, UVM coherency handling and IOVAS teardown changes. |
| `0013-skip-mailbox-peer-preinit.patch` | Skip mailbox peer pre-registration when BAR1 P2P is selected, leaving peer masks clear. |
| `0015-bar1p2p-readcap-override.patch` | CMP host-system read-cap override, rebased onto source without debug instrumentation. No dependency on the disabled `0014` patch. |

Three of these are hooks rather than hunks because the surrounding lines differ
between driver releases: 615.x added one field assignment below the caps
assignments, changed an unrelated registry default two lines below the REBAR
default, and regenerated the func table with one additional method between the
entry and the trailing comment. GNU patch requires three contiguous context lines
on each side, so a context diff cannot express an insertion whose neighbouring
lines may change; all three rejections were caused by those unrelated edits rather
than by anything in the P2P changes. As anchors, each hook names only the line or
statement list it modifies, which is why the set now applies to 615.x as well.

`0.4` already contains the functional equivalent of the source branch's
`0009-bar1-resize-unlock.patch`; it is retained, not applied twice.

The P2P feature state and selected patch bytes participate in the build cache
stamp, and so do the hook payloads. The installed state is recorded as
`p2p_enabled` next to `driver_version` and `card_profile`. `common/constants.yaml`
records the optional patch order and `tools/read-constants.py` checks its
consistency.

As in the source branch, some changes inside `0011` and the REBAR hook affect shared
HAL defaults, the UVM coherence path and IOVAS diagnostics, not just CMP device-ID
checks. They are absent from builds without `--p2p`: the hooks report themselves as
off and their three files are left byte for byte as upstream shipped them, which
`tests/test_patches.py` checks.

## Validation limits

The source branch lists NVIDIA `610.43.03` and `610.43.02`. The base project's
version list is retained (`driver/VERSION`), and the P2P set applies to all of it.
P2P patches are applied with full context matching (`--fuzz=0`); a changed
driver API/context stops preparation instead of silently skipping a patch.
Measured against every release in `driver/VERSION`, the three patches and the three
hooks all place, which `tests/test_patches.py` checks by applying the set to each
release. The set carries no version list. The three sites that previously failed on
615.x are anchors, so a release that moves one of them fails the build and names the
hook, rather than that release being excluded in advance. Real CMP transfer tests must
still be run in the target environment.

Offline regression checks are available with:

```bash
python3 -m pytest tests -q
```
