## About this project

This project unlocks NVIDIA CMP 170HX / A16 memory beyond the capacity the SKU reports,
brings the PCIe link up to Gen4 on hardware-unlocked cards, and takes the HBM refresh
current off the bill while a card is not working; all three are on in a plain install.
GPU-to-GPU P2P over BAR1 or over RM's write mailbox is the one part still behind
a flag. It is delivered as a set of patches applied to a stock
`open-gpu-kernel-modules` release, with an installer, a verification
script and an uninstaller.

Implemented functions:

- **Idle HBM power management, on by default.** A CMP 170HX carries a single performance level in
  its VBIOS, so nothing ever asks the memory PLL to come down and an idle card pays full HBM2E
  refresh current forever: 45.46 W and 43.20 W on the two cards of the bench machine. A companion
  module, `cmp_hbm_pm.ko`, takes the memory from 1728 MHz to 540 MHz while the card is provably
  not working, using the stop-the-framebuffer sequence the firmware uses for the same job —
  31.70 W and 29.55 W at rest, about 27 W for the pair — and the same size of saving again on the
  case a serving box actually lives in, where a framework holds the card allocated and asks nothing
  of it: 51.2 W and 49.5 W become 37.3 W and 35.9 W. The way back is taken on the first sample that
  sees traffic and costs 1.6 to 2.1 ms, which a client does not notice. The ceiling on what being
  wrong about a card costs was measured rather than argued: a 1500-token request served end to end
  with the memory pinned at the idle clock took 6.238 s against 3.199 s, HTTP 200, ECC unmoved — a
  card at the idle clock has slow memory, not no memory. What authorises the drop is not the
  activity counters, which only ever veto and have been seen report nothing at all for eight
  seconds on a card drawing 178 W: it is a lease written by `tools/hbm-lease.py` from what the
  driver itself says about clients, utilisation and power, refreshed every second, with a busy
  neighbour holding a card busy. `--no-hbm-pm` leaves the feature out of the build entirely, and
  `systemctl stop cmp-hbm-lease.service` returns every card to its busy clock as soon as its lease
  goes stale three seconds later. It is a port and extension of the idle HBM governor from
  [`gpu-unlock 3.0.0-beta`](https://github.com/Zagitta/gpu-unlock/tree/3.0.0-beta); every number
  above is in [Idle HBM power](docs/HBM-PM.md), with the run that produced it.
- **GPU-to-GPU P2P.** Optional, off by default, enabled with `--p2p=bar1` or
  `--p2p=mailbox`; a bare `--p2p` takes the mailbox route, the one that needs no large
  BAR1 and the only one open to a card on the wide layout.
  The BAR1 route is the older path: capability override, resizable BAR and the
  BAR1 DMA path, restricted to CMP device ids, and it needs a BAR1 as large as the
  framebuffer it maps. The mailbox route reaches the peer through the `NV_P2P` write
  mailbox instead, 64 KiB per peer and no framebuffer mapping, so it is the route left to a
  card whose framebuffer outgrows every BAR1 it can be given — and on this hardware it is
  the faster of the two where the card cannot take a large BAR1, at the Gen4 link rate.
- **80 GiB memory unlock on hardware-unlocked cards, on by default.** The capacity is
  derived from the card's own partition count and partition size, not from a card table. A
  card that cannot boot the wide layout steps back one layout and records the stage. Other
  cards are unlocked to 64 GiB (`10de:20c2`) or 40 GiB (`10de:2082`) from the card profile
  table, and `--no-80g` keeps every card on that table.
- **Coexistence of soft-unlocked and hardware-unlocked cards, detected automatically.** Each
  card is classified and sized at boot from its own registers, so one build and one driver
  serve a machine that holds both kinds.
- **PCIe Gen4 on hardware-unlocked cards, on by default.** Every supported card is retrained
  from Gen1 to Gen2 at driver init. Above Gen2, a per-card sequence supplies the PHY
  initialisation the VBIOS omits; the target is the card's own speed fuses, so a
  soft-unlocked card stays at Gen2 and `--no-gen4` leaves every card there. Every rung of
  that sequence is asked of both ends of the link and confirmed at both ends, which is
  what makes it land on an AMD root port as well as behind a PCIe switch. A link that
  cannot reach its target keeps the speed it trained, down to but not below Gen2.

### Differences from the upstream branch

This tree merges the P2P implementation from
[`bayley/cmpunlocker`](https://github.com/bayley/cmpunlocker) into the patch-based
`cmpunlocker` build. All original memory-geometry, PCIe and passthrough behaviour is
preserved. Beyond the merge itself:

- P2P is a build-time option (`--p2p=bar1|mailbox`), off by default; builds without the flag do not
  contain the P2P changes at all, and a build carries one route rather than both. A bare
  `--p2p` names the feature and takes the mailbox route; `bar1` has to be asked for by name.
- The mailbox route is not the fork's version of it. The fork reported capability support
  unconditionally, which on a card whose PRI trap cannot be armed meant P2P "worked" and
  moved garbage; here the override reads back the trap's privilege mask first and leaves
  the card unsupported unless it really is open, and the mask is opened through the SEC2
  post-BL replay, the only context that can write it.
- The P2P set carries no driver-version whitelist. Three patch sites that failed on newer
  releases are anchors rather than context hunks, so a release that moves one of them fails
  the build and names the site.
- Link speeds above Gen2 are supported. Upstream stopped at Gen2; the per-card sequence here
  brings a hardware-unlocked card to its fused maximum, Gen4 on the cards that allow it.
- The 80 GiB capacity is decided at boot from the card's own registers instead of a device
  id table, and a card that cannot boot the wide layout steps back one layout instead of
  failing to come up.
- Forced static BAR1 is downgraded to AUTO where the framebuffer exceeds the BAR1 the card
  provides, so an 80 GiB card boots and reports itself as a non-P2P card instead of failing
  the registry check.
- The 64 GiB ceiling on BAR1 is measured rather than assumed, on both registers behind it
  (`tools/cmpcap.c`): the card's size table takes no write through config space or through
  BAR0's MMIO mirror, and the selected-size field answers anything past 64 GiB with the
  256 MiB the card has at power on. So an 80 GiB card does P2P over the mailbox, not over
  a larger BAR1.
- Size checks were added to the GSP postbl signature refill: a signature shorter than the
  payload it fills returns an initialisation error instead of writing past the buffer into
  host memory.
- The boot-time PCIe retrain service is removed. The driver performs the retrain at init;
  `/usr/local/sbin/gen2-hammer` remains available as a manual tool.
- `verify.sh` reports the negotiated link per card from `LnkSta` and sysfs, and states the
  per-card memory layout the driver decided on.

Usage of the P2P option is in [Optional GPU-to-GPU P2P](#optional-gpu-to-gpu-p2p); merge
details and verification records are in [docs/P2P.md](docs/P2P.md) and
[docs/P2P-MERGE.zh-CN.md](docs/P2P-MERGE.zh-CN.md).

---

## Requirements

- Linux (x86-64)
- Root access
- NVIDIA CMP 170HX
- **nvidia-open 610.xx.xx+ already installed** (libs + firmware)
- Kernel headers matching the running kernel (`linux-headers-$(uname -r)` / `kernel-devel`)
- Secure Boot disabled (patched modules are unsigned)
- Network access on first install (downloads matching stock `open-gpu-kernel-modules` sources)
- Python 3 (used at build time to select 8GB/10GB geometry)

---

## Install

To install cmpunlocker, run the following command:

```bash
sudo ./install.sh
```

To force a certain memory profile, use the `--profile` option:

```bash
sudo ./install.sh --profile=8gb    # 8GB card → 64GB unlock
sudo ./install.sh --profile=10gb   # 10GB card → 40GB unlock
```

Then perform a cold reboot (full power off, then boot).

Everything the installer decides about a card is on for that install, and every one of
those decisions is read off the card itself, so a plain `sudo ./install.sh` is the right
command for a box of mixed cards:

| On by default | Back it out with |
| --- | --- |
| Memory unlock from the profile table (64 GiB on `10de:20c2`, 40 GiB on `10de:2082`) | `--profile=` |
| The wide (80 GiB) layout, for a card whose framebuffer fuses were burned | `--no-80g` |
| The PCIe speed each card's own fuses allow (Gen4 on a burned card, Gen2 on a soft card) | `--no-gen4` |
| An idle card's HBM clock, lowered by `cmp_hbm_pm` and restored the moment the framebuffer is touched | `--no-hbm-pm` |
| ECC on DRAM and SRAM, and the full SM count | nothing to back out |
| VM passthrough preparation | `--no-passthrough` |
| IOMMU kernel parameters (`intel_iommu=on`/`amd_iommu=on` plus `iommu=pt`) | `--no-iommu` |

P2P is the only feature left behind a flag, because it is the only one that needs
something from the host as well as from the driver, and because a capability matrix on a
machine where nothing actually moves is indistinguishable from success.

### Optional GPU-to-GPU P2P

This build ships two GPU-to-GPU P2P routes. Both are off by default, and a build
carries one of them, never both:

```bash
sudo ./install.sh --p2p            # the mailbox route, the default of the two
sudo ./install.sh --p2p=bar1       # the BAR1 route, asked for by name
# Can be combined with the existing options:
sudo ./install.sh --p2p=mailbox --profile=8gb --no-passthrough
```

`--p2p=bar1` enables the BAR1 P2P implementation from `bayley/cmpunlocker`,
including capability overrides, peer mappings and page-table rewrites. It applies to
every release in `driver/VERSION`. Every peer GPU needs a sufficiently large BAR1 and
a host PCIe path that carries peer traffic (BIOS must provide enough Above-4G MMIO
space).

`--p2p=mailbox` moves peer traffic through RM's write-mailbox aperture instead, which
needs 64 KiB per peer and no BAR1 mapping of the framebuffer. That makes it the route
left for a card whose framebuffer is bigger than any BAR1 its own resizable-BAR table
offers (the 80 GiB layout tops out at a 64 GiB BAR1), and for a host that cannot
resize BAR1 at all. It needs a PRI decode trap the closed GSP-RM is denied for those
registers, so it depends on one fused register per card; where that mask will not
open, the driver leaves P2P unsupported instead of reporting copies that move
garbage. It has been run on this machine, on two 80 GiB cards at Gen4 x16: the trap
armed on both, all twenty content checks passed in both directions, and sustained
copies measured 20 to 25 GB/s per direction, which is the link rather than the
mailbox — the 6.65 GB/s the source branch publishes for this path was a Gen2 link.
See [P2P setup and transfer verification](docs/P2P.md).
中文说明：[P2P 合并与验证说明](docs/P2P-MERGE.zh-CN.md)。

> [!IMPORTANT]
> **PCIe-switch hosts: REBAR to a 64 GB BAR1 can fail.** On hosts that hang the
> CMP GPUs behind a PCIe switch, the driver's runtime REBAR resize of BAR1 to
> 64 GB frequently fails because the switch's MMIO window does not expose enough
> Above-4G space for the reallocation. When that happens the BAR stays at its
> stock size and BAR1 P2P (and the full 64 GB BAR1 window) will not work.
>
> - Kernel-level fix: [`bayley/cmpunlocker/kernel-patches`](https://github.com/bayley/cmpunlocker/tree/master/kernel-patches)
>   patches the kernel to reprogram the switch windows and force the BAR1
>   resize. It works but is heavy — a custom kernel to build and maintain.
> - Lightweight alternative: [`cmp170hx-rebar-shim`](https://github.com/sayhellotojungle/cmp170hx-rebar-shim)
>   reaches the same result through a small shim, without rebuilding the kernel.

P2P builds install the modules and module options for the next cold boot.
Reinstall without `--p2p` to disable the optional driver patches and settings.
Keep the same `--p2p=` choice when reinstalling after a kernel upgrade.

The same switch is available for direct driver builds:

```bash
sudo env CMPUNLOCKER_ENABLE_P2P=bar1   ./driver/build.sh
sudo env CMPUNLOCKER_ENABLE_P2P=mailbox ./driver/build.sh
```

`off`, `0` and the empty string disable it. `bar1` is spelled out; `1`, which says yes
without naming a route, means `mailbox`, the same thing a bare `--p2p` means. On a
host where the resize above is the problem rather than the card, `mailbox` is the
route that does not need a large BAR1 at all.

After a cold boot, verify real peer transfers (capability reports alone are not
proof that data moves):

```bash
sudo ./verify.sh
nvidia-smi topo -p2p r
nvidia-smi topo -p2p w
nvcc -O2 -arch=sm_80 -o /tmp/cmpunlocker-test-p2p tools/test-p2p.cu
timeout 120s /tmp/cmpunlocker-test-p2p 0 1
```

On a `mailbox` build, also `sudo dmesg | grep CMP-P2P` — the trap either armed on
both cards or said why not — and check the bytes rather than the return codes with
`python3 tools/p2p-content-check.py 0 1`, which copies over a destination that starts
out wrong at the sizes where an unprogrammed mailbox window first shows. That tool has
a `--bw` mode for throughput; it checks the bytes of the copy it times, since on this
route a window that stopped moving still returns success.

### Optional GPUDirect Storage (GDS)

```bash
sudo ./install.sh --p2p=bar1 --no-iommu
```

`--no-iommu` keeps the installer from adding IOMMU options to the kernel
command line: GDS performs DMA straight between the NVMe device and the GPU's
BAR1, and leaving the IOMMU out of the storage DMA path avoids the address
translation layer in the way.

This is a GDS-specific choice, and so is the `bar1` here: GDS writes into the
framebuffer through the BAR1 mapping, which is the one thing the mailbox route does
not provide. Pure P2P does not need the IOMMU disabled — it
runs fine under the default `iommu=on`/`iommu=pt`; if peer traffic is blocked,
the cause is ACS on the intervening bridges, not the IOMMU itself.

GDS needs more than this project supplies, and none of it is the `--p2p` option. cufile's
native `use_pci_p2pdma` route needs the GPU to publish a p2pdma resource, which takes a
static BAR1 and therefore a framebuffer no larger than 64 GiB — on a card whose fuses were
burned, that means installing with `--no-80g` as well as `--p2p=bar1`; its legacy route
maps each
registered destination buffer into BAR1 instead, so it does not ask for the smaller
layout, but it still needs a storage driver that registers itself with `nvidia-fs`, which
the distribution's `nvme.ko` does not. Native p2pdma did run here on 2026-11-05, when the
cards were at 64 GiB and cufile logged `cuFile using NVME P2PDMA mode`. Today the bench
runs `--p2p=mailbox` with no GDS and stages its storage reads through host memory. See
[GPUDirect Storage](docs/P2P.md#gpudirect-storage) for the two routes and what was
measured on each.

## What Gets Unlocked

<table>
  <tr>
    <th>Feature</th>
    <th>Status</th>
  </tr>
  <tr>
    <td>Full SM compute throughput (SS0/SS1)</td>
    <td>Working ✓</td>
  </tr>
  <tr>
    <td>Memory geometry (64GB on 8GB cards, 40GB on 10GB cards; more when the card's own fuses allow — see <a href="#fuse-burned-cards-dynamic-per-card-unlock">dynamic unlock</a>)</td>
    <td>Working ✓</td>
  </tr>
  <tr>
    <td>PCIe Gen 2 speeds (Gen 3/4 on cards whose speed fuses allow it)</td>
    <td>Working ✓</td>
  </tr>
  <tr>
    <td>Error-Correcting Code (ECC) DRAM and SRAM</td>
    <td>Working ✓ (automatic — enabled by default, no flag)</td>
  </tr>
  <tr>
    <td>Full BAR1 Size (64GB)</td>
    <td>Working ✓</td>
  </tr>
  <tr>
    <td>JTAG (Host2Jtag register access)</td>
    <td>Working ✓</td>
  </tr>
  <tr>
    <td>VFIO-based passthrough</td>
    <td>Working ✓</td>
  </tr>
  <tr>
    <td>GPU profiling</td>
    <td>Working ✓</td>
  </tr>
  <tr>
    <td>GPU-to-GPU P2P</td>
    <td>Optional: --p2p (mailbox, the default route; no large BAR1 needed, verified on two
    80 GiB cards at Gen4, ~20-25 GB/s per direction) or --p2p=bar1 (needs a BAR1 as large
    as the framebuffer); requires host setup and real transfer verification</td>
  </tr>
  <tr>
    <td>Persistence across reboot (patched modules)</td>
    <td>Working ✓</td>
  </tr>
</table>

### ECC and SM count

ECC (DRAM and SRAM) and the additional SMs are part of the default patch set —
they are applied on every install and there is no separate flag. After a reboot,
confirm ECC is reported:

```bash
nvidia-smi -q -d ECC
```

The status may show as pending until after a cold reboot (full power off, then
boot). "more SMs" is covered by the *Full SM compute throughput (SS0/SS1)* row
above.

### Fuse-burned cards: automatic per-card unlock

No slot, bus number or card table is involved: every decision is read from the
card itself, so any slot works and cards can be mixed freely.

- **Memory (8 GB SKU only).** A burned card reports the hidden partition pair
  before the driver writes anything: `LMR` (`NV_PFB_PRI_MMU_LOCAL_MEMORY_RANGE`)
  counts the FBP partitions in bits [8:7], and more than eight of them on device
  id `0x20C2` is the burn signal. The capacity is then derived from the layout
  itself - the `CFG1` [19:16] nibble sizes one partition (4 = 1 GiB, 6 = 4 GiB,
  7 = 8 GiB) - and not from a card table, so the same code describes the native,
  the 64 GiB and the 80 GiB layout. PCIe capability is deliberately *not* the
  signal (a card can have only the PCIe fuse group cleared), and the partition
  count on its own is not either: the 10 GB SKU reports ten partitions with
  nothing left to unlock. Untouched cards keep the profile table (64 GiB).
- **Static BAR1.** Forced static BAR1 requires BAR1 to cover the framebuffer. The sizes
  BAR1 can take come from the card's own resizable-BAR table, which is part of the card's
  BIOS, and on these cards that table ends at 64 GiB. That is the maximum the GPU provides
  as it stands, for a hardware-unlocked card as well as a soft-unlocked one, and it is the
  size the 64 GiB layout needs. With `RMForceStaticBar1=1`, a card that cannot map its own
  framebuffer would fail the registry check, so on a card taking the wide layout the key is
  treated as AUTO on both sides (kernel RM and the registry table handed to GSP-RM), and
  the card boots without static BAR1 and therefore without BAR1 P2P, as a real A100 80 GB
  does. Cards on the 64 GiB layout keep static BAR1, and with it the BAR1 route. The host
  is not the limit: the region reserved above each card is 128 GiB, because BAR1 (64 GiB)
  plus BAR3 (32 MiB) exceeds 64 GiB and is aligned to the next power of two. Having 80 GiB
  of memory and BAR1 P2P on the same card requires a 128 GiB BAR1, and no host-side change
  gets one: a resizable-BAR capability keeps two registers and both were probed
  (`tools/cmpcap.c`). The size table is read-only through config space and through BAR0's
  MMIO mirror; the selected-size field, which is what actually programs the aperture and
  which `pci_rebar_set_size()` will not write outside the table, does take writes — 64 MiB
  and 1 GiB are accepted and decoded — but answers 128 GiB and 1 TiB with the 256 MiB the
  card has at power on. So the limit is the card's, and an 80 GiB card reaches its peers
  over `--p2p=mailbox` instead. Measurements are in [docs/P2P.md](docs/P2P.md).
- **Self-healing.** Only a booter timeout counts as "this layout does not boot":
  the driver then steps one layout back (80 GiB -> 64 GiB -> the card's own fuse
  layout), asks for the boot to be replayed, and records the stage in the
  registry so the next init starts one lower. A card that cannot boot wide
  comes up smaller instead of staying down.
- **PCIe.** Every card is brought from Gen1 to Gen2 by `nv_cmp170hx_retrain_gen2()` in
  the driver's init path, unconditionally, for device ids `0x20C2` and `0x2082`. There
  is no boot-time retrain service: it ran after the driver, could not write BAR0, and on
  a healthy boot its only effect was to retrain links the driver had already trained.
  `gen2-hammer` remains installed as a manual tool with a floor-based target and at most
  20 attempts.
  Above Gen2 the link needs the **Gen3 -> E1 DevInit replay -> Gen4** sequence, because
  the VBIOS leaves out the PHY-init block that makes 16 GT/s trainable and a link
  sitting at 5 GT/s cannot be retrained directly to it. It has **one home**, driver
  post-init: a second copy at GSP bootstrap left the PHY-init registers pre-set, RM's
  PCIe init rewrote the rate field back to Gen2 in between, and the pass that mattered
  then replayed the value it read without producing an equalisation edge. Every rung of
  it is asked of **both ends** of the link and confirmed at both ends, because a speed
  the partner is not asking for is a speed the link will not reach — and the port above
  the card was pinned to Gen2 by the retrain two lines before. `cmp_pcie_retries` walks
  the whole ladder again for a board that needs the Gen3 rung re-taken; a second walk
  re-arms the equalisation edge first, or it replays the value the first walk left
  there, which is the same nothing the GSP-bootstrap copy produced. The target is
  read from the card's own `LNKCAP2` supported-speeds vector, which is what the PCIe
  speed fuses drive: a soft-unlocked card reports Gen2 and is not asked for more, so one
  build serves a box holding both kinds.
  A card that does not reach its target keeps the speed the link did train, down to but
  not below Gen3, and both ends of the link plus the card's own rate fields are set to
  the speed it holds, so a later retrain does not request a speed the slot cannot carry.
  Only a link that cannot hold Gen3 returns to the Gen2 path, and that floor covers the
  **fallback** as much as a partial success: the highest rung a walk confirmed at both
  ends is remembered, so a later aim that is refused and drags the link below it settles
  back on that rung instead of handing the card to the Gen2 recipe. A rung is given up
  only by the link refusing it when it is asked for again, or by config space stopping
  answering. A `LnkSta` of `0xffff` is neither a speed nor a refusal, at either end:
  config reads answer all-ones for the few hundred ms a retrain takes,
  `pcie_capability_read_word()` calls that success, and the speed field of `0xffff` is
  `0xf` - a link "trained at Gen15" satisfies every `>= Gen3` test in the walk and then
  programs a target speed the encoding has no room for. The fuse value states what
  a card may request, not what the board it sits in will carry, so the two can differ,
  and the log names the port's own speed vector alongside the card's so a reader can
  tell the two apart. `verify.sh` prints `GEN3 OK, Gen4 missed` for a card whose fuses
  allow more than the link carries; that is a report, not a failure.
  On the bench, two cold boots with the feature enabled both reached the top tier
  (`CMP Gen4: link trained (LnkSta=0x1104)`, upstream bridge `0x2104`) and the link was
  unchanged when read again with the serving workload running; the Gen3 tier is the
  fallback path and was not exercised on this machine. That bench hangs its cards behind
  a PEX880xx switch, and a switch downstream port is forgiving about a rate change the
  card asks for alone: an EPYC server (card `41:00.0` on root port `40:03.1`) with the
  one-sided version of Phase A never reached Gen4 at boot at all, because the AMD root
  port honours the Gen2 target speed the Gen2 retrain leaves on it, so the E1 block was
  replayed onto a PHY still at 5 GT/s. What worked there was a companion unit reloading
  the driver, whose real contribution was a first pass that left the link settled at
  8 GT/s — the property the both-ended rung now provides without the reload. A card
  already at its target is left alone, which costs nothing; a card that has to walk
  the ladder costs about half a second of init when the walk works and up to about
  three seconds per walk when the link refuses, `cmp_pcie_retries` clamping the walk
  count to 0-4 because this runs with the RM API lock held.  A board that held a rung
  and was refused above it pays one more retrain, the 600 ms settle; that is what buys
  it a Gen3 link instead of a Gen2 one.

Both switches are on for a normal install, and both are part of the build cache, so
`--no-80g` and `--no-gen4` are rebuilds rather than reconfigurations. `--no-80g`, or the
environment spelling `CMPUNLOCKER_EXPERIMENTAL_80G=0`, leaves `CMPEnable80G` out of
`RegistryDwords` and compiles the wide-geometry path out of the driver, which is what
lets a burned card take the wide layout and what takes it away again; on a build that
compiled the path in, dropping the key from
`/etc/modprobe.d/cmp-pcie-gen2.conf` backs a single machine out without a rebuild, and
that is also the cheaper first move when a card turns out not to boot wide.
`CMPDynDebug=1` adds the register
dumps. `--no-gen4`, or `CMPUNLOCKER_EXPERIMENTAL_GEN4=0`, leaves `cmp_pcie_gen` out of
that same file, so the built-in `cmp_pcie_gen=2` pins every card at Gen2 as before, and
lowers `RMPcieLinkSpeed` to `0x1` so RM is not allowed above the link the driver trains;
with the switch on, `cmp_pcie_gen=0` asks each card for the speed its own fuses allow and
`RMPcieLinkSpeed` is `0xF`, so RM agrees with the link the driver actually brings up.
`verify.sh` reports the layout each card ended up with, and
[docs/BOOT-RECOVERY.zh-CN.md](docs/BOOT-RECOVERY.zh-CN.md) covers recovery.

Before trusting a card with the wide layout, check `fbp_disable == 0x840` (ten
usable FBPAs) - see [fuse data and the 80 GiB recipe](docs/CMP170HX-FUSE-DATA.zh-CN.md).

The driver side of this lives in `driver/dynfb/`: `cmp_dynamic.h` (the policy) plus
`apply.py`, which build.sh runs after the patch set. The policy header is copied
into the tree and the hooks that call it are placed **by anchor**, not by a context
window, so a driver release that moves code costs a line number instead of a hunk
that applies "with fuzz" and lands in the wrong function. A hook whose anchor is
missing or ambiguous fails the build and says which one.

### Idle HBM power

Everything in the section above is decided once, while the card is coming up, and never
revisited. Idle power cannot be: the memory clock has to come back up the moment the
framebuffer is touched again, which is a decision only a running system can make, so this
one is a module — and it is a module with a helper, because the one question it cannot ask
from kernel space is whether anything owns the card. The patched driver opens the FBPA
refresh block and the memory PLL during GSP bootstrap — on request, through `CMPEnableHbmPm=1`
in the same `RegistryDwords` the wide layout asks through — and `cmp_hbm_pm.ko` borrows the
activity counters GSP-RM already has armed and moves the memory PLL itself, using the
stop-the-framebuffer sequence the firmware uses for the same job, with the way back taken on
the first sample that sees traffic. Counters only ever veto. Movement in any of them means the
card is working, and silence proves nothing: the borrowed pair has been recorded reporting
`active 0x0` for eight seconds on a card drawing 178 W at 100 % utilisation, and one of them also
ticks while nobody uses it. The permission therefore comes from userspace:
`tools/hbm-lease.py` asks the driver once a second which cards have no client attached, writes the
answer to `/sys/kernel/cmp_hbm_pm/lease`, and a card comes down only while that lease is fresh —
and stays down only while it is re-answered, not on the strength of the answer that brought it
down. A keeper that is not running costs watts, never a card; the status file's `lease none`, or a
verdict marked `stale`, is what distinguishes that from an idle machine.

```bash
cat /sys/kernel/cmp_hbm_pm/status        # per card: busy, idle, or mid-transition
cat /sys/kernel/cmp_hbm_pm/perf_table    # what the card's VBIOS offers: read-only
```

The second one is a decoder rather than a knob: it copies the VBIOS container out of the
PROM window and prints the performance table the card actually carries, which is where the
claim behind the whole feature — one performance level — comes from, and why a generated idle
state is not on the menu here. The memory clock sequence and that table layout both come from
[`gpu-unlock 3.0.0-beta`](https://github.com/Zagitta/gpu-unlock/tree/3.0.0-beta), which this
repository ports and extends; [Idle HBM power](docs/HBM-PM.md) says what was ported, what was
changed, and what was measured after the port.

`--no-hbm-pm` (or `CMPUNLOCKER_HBM_PM=0`) leaves the key out of the build and the module
uninstalled, and either one alone is enough to leave a card alone: the governor reads one
word, reports `refresh access closed`, and the card goes on keeping the clock it has always
kept. What it saves — 13.8 W and 13.7 W on the two cards of the bench card, about 27 W for the
pair, with the keeper's answer being the only thing that authorised the switch, and the same size
of saving again on a card whose serving framework is resident and idle, where it reads 37.3 W and
35.9 W instead of 51.2 W and 49.5 W — what one transition costs, the ten runtime knobs and the
reasons it declines to switch are in [Idle HBM power](docs/HBM-PM.md).

### What is and is not tied to a driver release

Measured by `tests/test_patches.py`, against every release in `driver/VERSION`, on
the stock release and on the patched tree:

| Layer | Form | Cost of a new release |
| --- | --- | --- |
| `dynfb/cmp_dynamic.h`, the policy | copied into the tree whole | none |
| 15 hooks | spliced at anchors | 7 of them anchor on text our own patches insert, so they cannot move; 8 (`early-geom`, `retry`, `registry`, `bar1`, and the P2P caps and resizable-BAR sites) anchor on lines of the driver's own, and a release that moves one of those fails the build and names that hook. `tests/test_patches.py` checks both halves of that split against the release as shipped |
| 14 base patches | context diffs | a release that moves the context rejects the patch, which fails the build and names the patch |
| 3 BAR1 P2P patches, 2 mailbox P2P patches | context diffs, `--fuzz=0` | applies on every supported release, which the same test checks by applying each set to all of them |

Four of the hooks are the P2P feature's, and are placed only for the route that
needs them: `--p2p=bar1` takes the caps override, the BAR1 DMA override and the
resizable-BAR default, `--p2p=mailbox` takes a different caps override — one that
checks the fused privilege mask before claiming support — and the resizable-BAR
default. Without `--p2p` those files are left exactly as upstream shipped them, which
`tests/test_patches.py` checks for all three settings. Two of them
replace the line they change rather than adding to it, which is how a single-line
edit survives a release that edits what sits next to it.
Runtime behaviour is not on that list at all: the layout and the link target come
from the card's own registers, so one module serves mixed cards on any release it
builds on. `driver/VERSION` is the supported-version list; anything outside it is
refused up front rather than attempted.

---

## Uninstall

To uninstall cmpunlocker, run the following command:

```bash
sudo ./remove.sh --yes
```

Then perform a cold reboot (full power off, then boot).

## Contributions

Please read [docs/CONTRIBUTING.md](https://github.com/amoghmunikote/cmpunlocker/blob/master/docs/CONTRIBUTING.md) before opening a PR.

## Support & Community

Having issues? Need help? Join our [Discord community](https://discord.gg/CdHSakKSFv) to discuss with other users and get support.
