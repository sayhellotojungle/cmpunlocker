# GPU-to-GPU P2P

This ports the P2P implementation from the supplied `cmpunlocker-p2p` into
the `cmpunlocker-0.4` patch-based build. The existing memory profiles, per-device
geometry, PCIe Gen2 and passthrough implementation remain in `0.4`.

That port is one of two routes peer traffic can take, and it is the one that needs
a big BAR1. The other, `--p2p=mailbox`, moves the bytes through the `NV_P2P` write
aperture instead and is what a card whose framebuffer outgrows every BAR1 it can be
given is left with. They are described separately below, and a build carries one or
the other, never both.

## Enable or disable

```bash
sudo ./install.sh --p2p=bar1        # the BAR1 route, asked for by name
sudo ./install.sh --p2p=mailbox     # or --p2p, which means this
# Existing flags can be combined with the choice:
sudo ./install.sh --p2p=mailbox --profile=10gb --no-passthrough
```

The installer passes the choice to `driver/build.sh` as `CMPUNLOCKER_ENABLE_P2P`.
Direct builds use the same interface:

```bash
sudo env CMPUNLOCKER_ENABLE_P2P=mailbox ./driver/build.sh
```

Accepted values are `off` (also `0`, the empty string, and unset), `mailbox` (also `1`,
which says yes without naming a route, and the default route is the mailbox) and `bar1`.
Anything else is rejected before anything is built. The installer uses its command-line
flag as the authority: without `--p2p`, P2P is disabled even if the calling shell exports
the environment variable.

`mailbox` is the default route because it is the one that asks nothing of the host's MMIO
map, and the only one a card on the wide layout can use: it needs 64 KiB per peer and no
BAR1 mapping of the framebuffer, and a card on the wide
layout has no BAR1 that covers its own memory. It is also the route that has been run on
the two 80 GiB cards here, at Gen4, with the bytes checked
(`tools/p2p-content-check.py` passing on both ordered pairs). `bar1` is the older route
and the one that needs a large BAR1 and enough Above-4G MMIO from the host; it is still
what GPUDirect Storage's native p2pdma mode needs, so it is asked for by name. Where a
card's trap cannot be armed, that card reports P2P unsupported on this route rather than
moving garbage, which is the one way in which the default route is the more conservative
of the two. Read "The mailbox route" before choosing it, and read "Verify real transfers"
before believing either one.

## The BAR1 route (`--p2p=bar1`)

For this build, `/etc/modprobe.d/cmp-pcie-gen2.conf` contains one combined
`NVreg_RegistryDwords` value:

```text
RmForceEnableGen2=1;RMPcieLinkSpeed=0xF;RmForceDisableIomapWC=1;RMForceStaticBar1=1;RMPcieP2PType=1;CMPEnable80G=1
```

`RMPcieLinkSpeed` is a maximum-speed allow-mask, and `0xF` is what a default install
writes: it lets a card ask for what its own fuses allow, and the Gen3 -> E1 DevInit
replay -> Gen4 sequence in `driver/dynfb/hooks/` is what actually initializes the PHY,
per card, from that card's own PCIe speed fuses. Leaving the key unset is not equivalent —
RM then fails `SET_PCIE_SPEED` with `NV_ERR_NOT_SUPPORTED` — and `0xF` on its own would
let RM ask for a speed the link cannot train, which is why the mask and the per-card
bring-up travel together. `--no-gen4` writes `0x1` instead, the value the GDS/P2PDMA
baseline was validated with: RM is then not allowed above Gen2 on any card. The module
also receives `NVreg_EnableResizableBar=1`, and `cmp_pcie_gen=0` unless `--no-gen4` left
it out. Automatic fuse-based PCIe bypass was removed after a regression. The wide layout
is on by default, so a card that takes the 80 GiB layout has `RMForceStaticBar1`
downgraded to AUTO by the driver, and with it BAR1 P2P; the reason and the numbers are in
"BAR1 size and the 80 GiB layout" below. Cards on the 64 GiB layout keep static
BAR1, and `--no-80g` keeps every card there.

A `mailbox` build writes the opposite keys — `RMPcieP2PType` stays at its default
(`NV_REG_STR_RM_PCIEP2P_TYPE_DEFAULT` is `MAILBOX`) and `RMForceStaticBar1` stays
unset, since this route maps nothing and needs 64 KiB per peer rather than a BAR1 as
large as the framebuffer. Its only own key is `CMPEnableP2PMailbox=1`, which is what
opens the register mask in patch `0021` and what the caps hook reads before claiming
support; deleting it from that line turns the feature off with no rebuild.

This file is written before the build script regenerates initramfs. The installer does not overwrite it afterwards. Do not add a second competing
`NVreg_RegistryDwords` option in another modprobe file. The source branch also
reports hangs with an explicit `ForceP2P=0x11` registry option; the installer
does not add that option, on either route. RM's own `ForceP2P` override also
short-circuits the capability check to OK, which on the mailbox route would
silence the one guard that knows the trap could not be armed.

P2P builds, and the first build disabling an installed P2P configuration,
skip the live NVIDIA module reload. Shut down completely and power on again.
If initramfs generation reports a failure, correct it and regenerate initramfs
before rebooting; a stale image may still contain the previous modules/options.

To disable P2P, reinstall without `--p2p` and cold boot. The source cache is
invalidated and the optional patches of whichever route was built are removed by
extracting a fresh driver tree. The combined config is rewritten with only the
original Gen2 settings. `remove.sh --yes` removes that config and the installed metadata with
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

That last sentence was checked rather than assumed, on both of the registers that could
be meant by it. A resizable-BAR capability keeps two of them: the table, and a separate
selected-size field that is what actually programs the aperture. The kernel will only ever
ask for a size in the table (`nv_resize_pcie_bars()` takes `fls(sizes)-1` and
`pci_rebar_set_size()` refuses anything outside it), so the selected-size field is the one
door that has not been tried by a driver — and unlike the table, it is writable.

```text
NVRM: cmpcap: BAR1 entry at config 0xbbc (supported 0x001ffc00, control 0x00001001)
NVRM: cmpcap: supported = 64M 128M 256M 512M 1G 2G 4G 8G 16G 32G 64G
NVRM: cmpcap: kernel's possible=0x0001ffc0 -> largest 65536 MiB at index 16; control selects index 16
NVRM: cmpcap: MMIO write of 0x003ffc00 (one notch above the top) -> 0x001ffc00 REFUSED
NVRM: cmpcap: asking for index 10 (1024 MiB):  control 0x00001001 -> 0x00000a01, ACCEPTED, BAR1 decodes 1024 MiB
NVRM: cmpcap: asking for index 17 (131072 MiB): control 0x00001001 -> 0x00001101, REFUSED
NVRM: cmpcap: asking for index 20 (1048576 MiB): control 0x00001001 -> 0x00001401, REFUSED
NVRM: cmpcap: BAR1 now decodes 256 MiB (index 8) <-- still the old aperture
NVRM: cmpcap: restored control=0x00001001 (ok), command=0x0002 (ok), BAR1 decodes 65536 MiB
```

The table is read-only through both write paths, config space and the XVE mirror behind
BAR0. The selected-size field is not: asked for 64 MiB or 1 GiB it takes the value and
the BAR decodes that, so the write path is live and the answer to 128 GiB is the
device's own. Asked for index 17, or for index 20, it comes back as index 8 — the
256 MiB the card has at power on, which is what a rejected value leaves, not the size
requested. The device decodes no BAR1 larger than 64 GiB whatever the host is willing
to allocate for it. `tools/cmpcap.c` is the probe that says this, and it refuses to
touch the field while a driver owns the card, because between the write and the
restore the aperture does not match what the host allocated.

The consequence for the 80 GiB layout is unchanged and now has a mechanism behind it: the
card will decode no BAR1 larger than 64 GiB at any size the host is willing to give it.

Verification on that machine, after a cold boot and with the serving workload running:
the two cards on the 64 GiB layout passed 2/2 directed peer pairs at 16 MiB per
endpoint, checked at both endpoints, with no Xid afterwards. Peer transfers on the card
using the 80 GiB layout are not claimed, and cannot be claimed while the card provides at
most a 64 GiB BAR1.

## The mailbox route (`--p2p=mailbox`)

The BAR1 route needs an aperture at least as large as the framebuffer, and the card
will not offer one past 64 GiB. The mailbox route moves the same bytes through
RM's other PCIe P2P protocol, which addresses the peer through the `NV_P2P` block
rather than through a mapping of its memory: `PCIE_P2P_WRITE_MAILBOX_SIZE` is 64 KiB
per peer and `P2P_MAX_NUM_PEERS` is 8, so the whole thing lives in the first 512 KiB
of BAR1 regardless of how much memory the card has. It is therefore the route that
is left for a card on the 80 GiB layout. It is not the slower one on this hardware:
the figure the source branch publishes, about 6.65 GB/s per direction, is a Gen2 x16
link, and the same path on two 80 GiB cards at Gen4 x16 measured the best of a run of
sustained copies at:

| copy size | 0 → 1 | 1 → 0 | content |
| --- | --- | --- | --- |
| 64 MiB | 20.2 GB/s | 23.2 GB/s | checked, correct |
| 256 MiB | 25.2 GB/s | 23.3 GB/s | checked, correct |
| 1 GiB | 25.2 GB/s | 23.3 GB/s | checked, correct |
| 4 GiB | 20.3 GB/s | 23.3 GB/s | checked, correct |

That is the link: a Gen4 x16 port carries about 25 GB/s of payload, so on this
platform the two routes differ in which cards may use them and not in how fast the
bytes go. The numbers come from `tools/p2p-content-check.py --bw`, which times a loop
of `cuMemcpyPeer` and then copies the same size once more against a destination
preloaded with the complement, so the figure is only printed for bytes that arrived.

What the closed GSP-RM will not do by itself is touch the `NV_P2P` block
(`0x00139000`-`0x0013afff`). Peer setup ends with the host writing
`NV_P2P_WREQMB_L/H` on the remote side (`kbusWriteP2PWmbTag_GM200`), and with the
peer's window pinned at whatever its reset value was, every write lands in the same
64 KiB. Three pieces make those accesses reach the registers:

| Site | Implementation |
| --- | --- |
| `patches/p2p/0021-mailbox-trap31-plm.patch` | Writes `0xffffffff` to the trap's own `PRIV_LEVEL_MASK` (`0x0012277c`) through the SEC2 post-BL replay, by adding a thirteenth row to the table our own `sec2-postbl-plm-ss-cfg.patch` already programs. That replay is the only context that can write this mask: at level 0, the privilege level the mask itself is checked against, nothing can. Gated on `CMPEnableP2PMailbox`, so the twelve other rows and every other build are untouched. |
| `patches/p2p/0020-mailbox-trap31.patch` | Arms PRI decode trap 31 around the peer-link path on both GPUs and restores it afterwards: `MATCH` `0x0012247c` = `0x00139000` (the first `NV_P2P` register), `MASK` `0x001224fc` = `0xfc000fff` (bits 31:26 and 11:0 compared), `DATA1` `0x0012257c` = `0xc0000000`, `DATA2` `0x001225fc`, `ACTION` `0x0012267c` = `0x100000` (re-stamp privilege, do not deny), `CFG` `0x001226fc` = `0x1c010`. Armed means any PRI access inside the `NV_P2P` block is served at elevated privilege, which is what lets the GSP's mailbox-tag writes through. Restoring writes `ACTION` back to 0 first, so no window can open over a half-restored payload. |
| `dynfb/hooks/p2p_caps_mailbox.c` | The capability override, with a guard: it asks for support only if `0x0012277c` actually reads back `0xffffffff`. |

The values in that table are empirical. They come from the `cmpunlocker-p2p-nobar1`
branch (commit `14b38f72dbe0ee03dfd9377880a0c83302f703d3`), whose trap idea traces
back to asm64-hooligan; no published NVIDIA header names trap 31 or lists them. The
arm/restore order around the peer-link call is also theirs and is deliberate: the
traps have to be in place while the mailbox tags are written. The one hook the two
routes share, `p2p_resizable_bar.c`, comes along for the same reason it is in that
branch: RM allocates the mailbox window out of BAR1 and the resize is what makes
RM's idea of the aperture match the bus address the pair ends up using.

The guard in the caps hook exists because of what the unguarded version did on a
g250 CMP 170HX. Its `0x0012277c` came out of the booter as `0xFFFFFF8F`, so the trap
could not cover the whole block; a 32 MiB `cuMemcpyPeer` returned `CUDA_OK` and
33423360 of its 33554432 bytes came back wrong, with only the first 128 KiB correct.
A capability report that says OK there is worse than one that says NotSupported, so
on a card whose mask did not open, the hook leaves the GSP's own answer alone and the
pair shows up as unsupported.

That failure mode is why the mailbox route is verified by content and not by return
codes — see "Verify real transfers" and `tools/p2p-content-check.py`. The one register
the whole route turns on is readable from BAR0, which is worth ten seconds before a
build if the card is unbound (`echo 0000:05:00.0 > /sys/bus/pci/drivers/nvidia/unbind`);
while a driver owns BAR0 the kernel refuses the access — `pread` on
`/sys/bus/pci/devices/.../resource0` answers `EIO` — and the number has to come from
the driver itself:

```bash
start=$(sed -n '1p' /sys/bus/pci/devices/0000:05:00.0/resource | awk '{print $1}')
sudo busybox devmem $((start + 0x12277c)) 32       # the trap's PRIV_LEVEL_MASK, unbound
sudo dmesg | grep CMP-P2P                           # the same number, from the driver
```

`0xffffffff` means the trap can cover the block. A partial value such as the
`0xffffff8f` that g250 shipped with means the guard will refuse the caps and the pair
will stay unsupported. Either number is the state after the booter has replayed its
table, so it says the mask is open now, not that it opened by itself; the line that
shows the replay and the answer the guard drew from it are both in `dmesg`:

```text
NVRM: GPU0 _kgspBootGspRm: SEC2_DEBUG: PLM[12] TRAP31_PLM(0x12277c) attempt=0 status=0xffff reg=0xffffffff
NVRM: GPU0 _gpuInitPcieP2PCapability: CMP-P2P GPU0: mailbox caps forced to OK (devId 0x20c2, trap31 PLM 0xffffffff)
NVRM: _cmpP2PTrapArm_GM200: CMP-P2P GPU0 peer0: trap31 armed on NV_P2P (PLM 0xffffffff)
```

The first line is the booter opening the mask, the second is the caps guard deciding
support from what it read, and the third only appears when a peer link is actually
being set up — four of them, two per card, are what a working pair looks like. The
`status=0xffff` of the first line is not a failure code: every row of the table the
booter replays prints that, including the twelve that have always worked.

This route has been run on the two 80 GiB cards of the bench described throughout
this file: both cards' mask read all ones after the booter's replay, the guard reported
support for both, the trap armed four times (once per GPU per direction of the pair)
when the first peer copy set the link up, and `tools/p2p-content-check.py` passed all
twenty content checks in both ordered pairs, from 65535 bytes to 32 MiB. The timed
copies at 64 MiB, 256 MiB, 1 GiB and 4 GiB came back content-correct as well. Nothing
in `dmesg` complained afterwards. What has *not* been tried is eight cards, or this
route under `--profile=10gb`, or a VM.

The two routes are mutually exclusive in one driver: gate 4 of
`kbusIsPcieBar1P2PMappingSupported_GH100` refuses the BAR1 route over a peer number the
mailbox route already claimed, which is why the option selects a patch set instead of
turning both on.

## Host requirements

Both routes need the PCIe path itself to carry peer traffic. The rest differs, and
the difference is the reason the option exists:

- **BAR1 route:** every participating CMP GPU needs a BAR1 large enough to cover its
  unlocked framebuffer. On the tested platform that limit is 64 GiB and it comes from
  the BIOS, so a card using the 80 GiB layout cannot take part; see
  "BAR1 size and the 80 GiB layout" above. The 40 GiB framebuffer of device `2082`
  is inside the 64 GiB BAR1.
- **BAR1 route:** firmware must provide enough address space for all GPU BARs and
  bridge windows. BAR1 expansion is done by the driver at probe time and depends on
  the BIOS allocating sufficient MMIO (Above-4G Decoding, large enough MMIO-H
  window). If the firmware does not provide that space, no driver-side change can
  create it; the BAR stays at its stock size and this route will not work. Behind a
  PCIe switch whose window is too small, the resize fails even when the host has the
  address space (see the rebar-shim note in the README).
- **Mailbox route:** neither of the above. The aperture is 64 KiB per peer at the
  bottom of BAR1, which is inside what the card ships with, so this route is the one
  left when the resize is the thing that fails. What it needs instead is the PRI
  trap's privilege mask being openable on both cards (`0x0012277c` all ones after the
  booter ran), which is a property of the card and not of the host.
- **Both:** the actual PCIe topology, ACS routing and IOMMU configuration must carry
  peer reads and writes. RM's own mailbox path also wants a common parent switch
  between the pair (`bCommonPciSwitchFound`), which the PEX880xx boards above these
  cards satisfy. `0.4` retains its default IOMMU passthrough setup;
  `--no-iommu` leaves that configuration to the operator.
- A stock guest driver does not inherit these P2P source patches merely
  because an unlocked card was passed through. Guest P2P needs its own patched
  driver and working PCIe mappings in the guest/host configuration.

The source project's throughput numbers and kernel version are records of
its host, not verification of this merged tree on another machine.

## GPUDirect Storage

Both routes above move bytes between GPUs. GDS is a third route over the same aperture,
NVMe to framebuffer. It has requirements of its own that this project does not supply,
which is why the bench now runs without GDS, but they are not the same on the two routes
cufile can take, and only one of them is a geometry problem.

### The storage side, which blocks both routes

Something must register with `nvidia-fs`, and registration goes from the filesystem or
block driver to `nvidia-fs` during that driver's own module init. Rebinding the device or
reloading `nvidia-fs` cannot add a driver that never registered. The `nvme.ko` and
`nvme_core.ko` shipped with the bench kernel carry no `nvfsops` references at all, and the
registry lists a single provider:

```text
$ cat /proc/driver/nvidia-fs/modules
wekafsio: 1
```

Every file on that machine's NVMe is therefore served by cufile's POSIX fallback, and
`gdscheck -p` reports `NVMe : compat`. Changing it means installing a driver built with
the GDS hooks (DOCA-Host, or MLNX_OFED with GDS enabled), not editing a configuration
file. On the bench that is also an initramfs-level change to the driver that owns the
namespace holding the model files, so it carries a boot-recovery cost of its own.

### The GPU side, where the two routes differ

- **Native `pci_p2pdma`: blocked by the framebuffer size.** cufile's `use_pci_p2pdma`
  route needs the GPU to publish a p2pdma resource, and in the 610.57.04 sources the only
  publisher is `kernel-open/nvidia-uvm/uvm_devmem.c`, which publishes the whole static BAR1
  window and returns first when there is none: `if (!parent_gpu->static_bar1_size ||
  parent_gpu->static_bar1_write_combined) return;` (`uvm_devmem.c:627`). Creating that
  window requires BAR1 to cover the framebuffer, so on cards whose resizable-BAR table
  stops at 64 GiB this route belongs to the 64 GiB layout. `RMForceStaticBar1` offers
  DISABLE, ENABLE and AUTO and no setting changes the arithmetic; see "BAR1 size and the
  80 GiB layout". Measured, not inferred: cufile reports
  `checkIfAllGPUsSupportP2PDMA(): 0` on the bench today.
- **Native `pci_p2pdma`: all-or-nothing over the GPU set.** That same check is a
  conjunction over every GPU cufile may use, so a mixed box cannot take this route for
  its smaller card. Observed on 2026-11-23 with cards `05:00.0` and `06:00.0` on the
  80 GiB layout and `09:00.0` on the 64 GiB layout: `kernel Support: 1`,
  `pci_p2pdma_supported_cuda: 1`, and `checkIfAllGPUsSupportP2PDMA(): 0`.
- **dmabuf/legacy `nvidia-fs`: the destination buffer needs the aperture, not the whole
  framebuffer.** Here RM decides per export, in `RmDmabufGetClientAndDevice`:

  ```c
  *pbStaticPhysAddrs = ((pGpu->getProperty(pGpu, PDB_PROP_GPU_COHERENT_CPU_MAPPING) ||
                         pGpu->pGpuArch->bGpuArchIsZeroFb) &&
                        (mappingType == NV_DMABUF_EXPORT_MAPPING_TYPE_DEFAULT)) ||
                        kbusIsStaticBar1Enabled(pGpu, GPU_GET_KERNEL_BUS(pGpu));
  ```

  With no static window it takes the other branch (`osapi.c`), calls
  `kbusMapFbAperture_HAL(..., BUS_MAP_FB_FLAGS_MAP_UNICAST | BUS_MAP_FB_FLAGS_ALLOW_DISCONTIG, ...)`
  for that buffer and offsets the ranges by `kbusGetGpuFbPhysAddressForRdma`; the comment
  on that branch reads "Dynamic BAR1 requires RMAPI and GPU lock to be held". So a
  framebuffer larger than BAR1 is not automatically fatal on this route: what has to fit
  the aperture is the registered destination buffer, and cufile registers its staging
  buffers once rather than per read. Whether GSP-RM on these cards maps a buffer above the
  64 GiB BAR1 boundary is **not established here** — that logic is inside
  `gsp_ga100.bin`, not in the shipped sources, and it has not been tried on this bench.

### What that adds up to

The bench took the native route once: on 2026-11-05, with the cards on the 64 GiB layout,
cufile logged `cuFile using NVME P2PDMA mode` alongside `checkIfAllGPUsSupportP2PDMA(): 1`.
Today GDS is off on all three cards, for the storage-side reason above. If it is ever
revisited, the legacy route is the one to try, because it does not ask for the 64 GiB
layout back; what it costs is a patched `nvme.ko`, and what it can plausibly save over
reads staged through host memory is one DMA hop, since the read amplification of the
workload is identical on every route.

A key worth knowing before porting an A100 GDS recipe: `ForceP2P=0` is
`NV_REG_STR_CL_FORCE_P2P`, a bit field whose `1:0`, `5:4` and `9:8` fields force P2P
reads, writes and atomics off (`..._DISABLE` 0, `..._ENABLE` 1, `..._DEFAULT` 2), so it
disables peer access rather than selecting a protocol. The protocol selector is
`RMPcieP2PType`, whose values are MAILBOX 0, BAR1 1, AUTO 2 and whose default is MAILBOX.

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

`tools/test-p2p.py` checks 16 MiB per endpoint, which is more than either route can
get wrong in a way that looks like success — except on the mailbox route, where the
failure mode is a window that stopped moving, and that first shows at the sizes the
window is made of. `tools/p2p-content-check.py` is the check for that: no CUDA
toolkit and no `cuda-python`, just `libcuda`, and it copies at 64 KiB ±1, 128 KiB ±1,
512 KiB, 1 MiB, 8 MiB and 32 MiB over a destination preloaded with the complement of
the expected bytes, so a copy that never happened cannot pass either:

```bash
python3 tools/p2p-content-check.py 0 1     # default: every visible GPU
python3 tools/p2p-content-check.py --bw --size=1g --seconds=5 0 1
```

It prints the number of bad bytes and the first bad offset per ordered pair, because
"33423360 bad, first at 0x20000" names the mechanism and "bad bytes" does not.

`--bw` is the same tool asked a different question: instead of the six small sizes,
two buffers of `--size`, one direction at a time, repeated for `--seconds`, reporting
the best copy of the samples. It then copies that size once more and *checks* it, so a
throughput number can never be printed for a transfer whose bytes were never looked
at — which on this route is not a hypothetical, since a window that stopped moving
returns `CUDA_OK`. It refuses a size the host cannot hold three copies of, which is
what the checked copy needs.

## Implementation map

The feature is two patch sets plus four hooks placed by `driver/dynfb/apply.py`,
each applied only for the route that asks for it, after the original `0.4` patch
sequence. `--p2p=bar1` takes the first three patches and three of the hooks;
`--p2p=mailbox` takes the last two patches, the guarded caps hook and the
resizable-BAR default. No build takes both, and `tests/test_patches.py` asserts the
two patch lists share nothing.

| Site | Implementation |
| --- | --- |
| `dynfb/hooks/p2p_caps.c` (bar1) | Inline port of `cmpUnlockForceP2PCaps`: overrides the GSP read/write caps for PCI device IDs `20c2` and `2082`, spliced after the two `pcieP2P*Caps` assignments in `gpu.c`. No dependency on the other branch's unlock C file. |
| `dynfb/hooks/p2p_caps_mailbox.c` (mailbox) | The same splice point, guarded: it reads the trap's privilege mask and leaves the GSP's own answer alone unless that mask really covers the `NV_P2P` block, so a card whose trap cannot be armed reports unsupported instead of OK. Gated on `CMPEnableP2PMailbox`. |
| `dynfb/hooks/p2p_resizable_bar.c` (both) | REBAR default on, by replacing the `__NV_ENABLE_RESIZABLE_BAR` registry default rather than carrying context around it. |
| `dynfb/hooks/p2p_bar1dma.c` (bar1) | The generated func table's default `kbusGetBar1P2PDmaInfo` points at `_GH100`; replaces that one line in `g_kern_bus_nvoc.c`. |
| `0011-p2p-bar1.patch` (bar1) | Original BAR1 HAL selection, create/remove mapping branches, CMP BIF defaults, BAR1 bus-address propagation, peer PTE/physical address rewrites, UVM coherency handling and IOVAS teardown changes. |
| `0013-skip-mailbox-peer-preinit.patch` (bar1) | Skip mailbox peer pre-registration when BAR1 P2P is selected, leaving peer masks clear. |
| `0015-bar1p2p-readcap-override.patch` (bar1) | CMP host-system read-cap override, rebased onto source without debug instrumentation. No dependency on the disabled `0014` patch. |
| `0020-mailbox-trap31.patch` (mailbox) | Arms PRI decode trap 31 over the `NV_P2P` block on both GPUs around the two RM mailbox-setup calls, and restores it afterwards; CMP device ids only. The values are measured, not documented (see "The mailbox route"). |
| `0021-mailbox-trap31-plm.patch` (mailbox) | Adds that trap's privilege mask to the SEC2 post-BL table our own `sec2-postbl-plm-ss-cfg.patch` writes, behind that patch's signature guard, and skips the row unless `CMPEnableP2PMailbox` is set. Without it the booter leaves the mask at `0xFFFFFF8F` and the trap cannot cover the block. |

Three of these are hooks rather than hunks because the surrounding lines differ
between driver releases: 615.x added one field assignment below the caps
assignments, changed an unrelated registry default two lines below the REBAR
default, and regenerated the func table with one additional method between the
entry and the trailing comment. GNU patch requires three contiguous context lines
on each side, so a context diff cannot express an insertion whose neighbouring
lines may change; all three rejections were caused by those unrelated edits rather
than by anything in the P2P changes. As anchors, each hook names only the line or
statement list it modifies, which is why the sets now apply to 615.x as well. The
caps override is two hooks on one anchor, one per route, which is also why
`apply.py` treats an anchor two hooks want as a failure: a `gpu.c` carrying both
would answer to whichever route the operator did not pick.

`0.4` already contains the functional equivalent of the source branch's
`0009-bar1-resize-unlock.patch`; it is retained, not applied twice.

The P2P route name (not a yes/no, since `bar1` and `mailbox` are different drivers)
and the selected patch bytes participate in the build cache stamp, and so do the hook
payloads; moving between the two re-extracts the tree. The installed route is recorded
as `p2p_enabled` next to `driver_version` and `card_profile`, and `verify.sh` reads it
back so that it can ask for the check that route needs. `common/constants.yaml`
records both optional patch lists and `tools/read-constants.py` checks both against
`driver/build.sh` on every build, whichever route that build is: a patch that went
missing is a build that cannot be made tomorrow either.

As in the source branch, some changes inside `0011` and the REBAR hook affect shared
HAL defaults, the UVM coherence path and IOVAS diagnostics, not just CMP device-ID
checks. They are absent from builds that do not ask for them: the hooks report
themselves as off and the files they would edit are left byte for byte as upstream
shipped them, which `tests/test_patches.py` checks for all three settings.

## Validation limits

The source branch lists NVIDIA `610.43.03` and `610.43.02`. The base project's
version list is retained (`driver/VERSION`), and both P2P sets apply to all of it.
P2P patches are applied with full context matching (`--fuzz=0`); a changed
driver API/context stops preparation instead of silently skipping a patch.
Measured against every release in `driver/VERSION`, the three BAR1 patches with the
three BAR1 hooks and the two mailbox patches with the mailbox caps hook all place,
which `tests/test_patches.py` checks by applying each set to each release. Neither
set carries a version list. The three sites that previously failed on 615.x are
anchors, so a release that moves one of them fails the build and names the hook,
rather than that release being excluded in advance. Real CMP transfer tests must
still be run in the target environment.

What has and has not been measured on this machine:

| | BAR1 (`--p2p=bar1`) | mailbox (`--p2p=mailbox`) |
| --- | --- | --- |
| Builds and places on every supported release | yes (offline test) | yes (offline test) |
| Peer copies on real CMP cards | yes: 2/2 ordered pairs, 16 MiB per endpoint, verified at both endpoints, no Xid after | yes: 2/2 ordered pairs on two 80 GiB cards, 20 content checks from 65535 B to 32 MiB, plus 64 MiB to 4 GiB timed and checked, no Xid after |
| Throughput | Gen2, link-limited | Gen4 x16, 20 to 25 GB/s per direction, which is the link; the source branch's 6.65 GB/s was Gen2 |
| Depends on a fused register | resizable-BAR table (64 GiB ceiling, both its registers confirmed read-only — see above) | `0x0012277c`, the trap's privilege mask, reading all ones after the booter's table is replayed; the guard is what tells the two bench cards, which read all ones, from the g250, which read `0xFFFFFF8F` |
| Fails honestly when the hardware says no | yes — BAR1 stays stock and the route is refused | yes, on the card that shipped `0xFFFFFF8F`: the guard leaves the GSP's NotSupported alone. The fork's unguarded version reported OK and moved garbage |

On another machine, treat `--p2p=mailbox` as unverified until `dmesg | grep CMP-P2P`
shows the trap armed on both cards and `tools/p2p-content-check.py` passes; the numbers
above are what it looks like when they do. Neither route has been tested in a VM, and a
guest needs its own patched driver either way.

Offline regression checks are available with:

```bash
python3 -m pytest tests -q
```
