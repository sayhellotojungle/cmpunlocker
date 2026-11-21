## About this project

This project unlocks NVIDIA CMP 170HX / A16 memory beyond the capacity the SKU reports,
brings the PCIe link up to Gen4 on hardware-unlocked cards, and optionally enables
GPU-to-GPU P2P over BAR1 or over RM's write mailbox. It is delivered as a set of patches
applied to a stock `open-gpu-kernel-modules` release, with an installer, a verification
script and an uninstaller.

Implemented functions:

- **GPU-to-GPU P2P.** Optional, off by default, enabled with `--p2p=bar1` or
  `--p2p=mailbox`. The first is the BAR1 path: capability override, resizable BAR and the
  BAR1 DMA path, restricted to CMP device ids, and it needs a BAR1 as large as the
  framebuffer it maps. The second reaches the peer through the `NV_P2P` write mailbox
  instead, 64 KiB per peer and no framebuffer mapping, so it is the route left to a card
  whose framebuffer outgrows every BAR1 it can be given — and on this hardware it is the
  faster of the two where the card cannot take a large BAR1, at the Gen4 link rate.
- **80 GiB memory unlock on hardware-unlocked cards.** The capacity is derived from the
  card's own partition count and partition size, not from a card table. A card that cannot
  boot the wide layout steps back one layout and records the stage. Other cards are unlocked
  to 64 GiB (`10de:20c2`) or 40 GiB (`10de:2082`) from the card profile table.
- **Coexistence of soft-unlocked and hardware-unlocked cards, detected automatically.** Each
  card is classified and sized at boot from its own registers, so one build and one driver
  serve a machine that holds both kinds.
- **PCIe Gen4 on hardware-unlocked cards.** Every supported card is retrained from Gen1 to
  Gen2 at driver init. Above Gen2, a per-card sequence supplies the PHY initialisation the
  VBIOS omits; the target is the card's own speed fuses, so a soft-unlocked card stays at
  Gen2. A link that cannot reach its target keeps the speed it trained, down to but not
  below Gen2.

### Differences from the upstream branch

This tree merges the P2P implementation from
[`bayley/cmpunlocker`](https://github.com/bayley/cmpunlocker) into the patch-based
`cmpunlocker` build. All original memory-geometry, PCIe and passthrough behaviour is
preserved. Beyond the merge itself:

- P2P is a build-time option (`--p2p=bar1|mailbox`), off by default; builds without the flag do not
  contain the P2P changes at all, and a build carries one route rather than both.
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

### Optional GPU-to-GPU P2P

This build ships two GPU-to-GPU P2P routes. Both are off by default, and a build
carries one of them, never both:

```bash
sudo ./install.sh --p2p=bar1      # same as plain --p2p
sudo ./install.sh --p2p=mailbox
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

`off`, `0` and the empty string disable it; `1` is the old spelling of `bar1`. On a
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

GDS needs two things this project does not supply, and neither is the `--p2p` option:
the GPU must publish a p2pdma resource, which on these cards takes a static BAR1 and
therefore a framebuffer no larger than 64 GiB; and the storage driver must register
itself with `nvidia-fs`, which the distribution's `nvme.ko` does not. Both held on the
bench on 2026-11-05, when the cards were at 64 GiB and cufile logged
`cuFile using NVME P2PDMA mode`. Neither holds at the 80 GiB layout, so the bench runs
`--p2p=mailbox` without GDS and stages its storage reads through host memory. See
[GPUDirect Storage](docs/P2P.md#gpudirect-storage) for what was measured.

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
    <td>Optional: --p2p=bar1 (needs a BAR1 as large as the framebuffer) or
    --p2p=mailbox (verified on two 80 GiB cards at Gen4, ~20-25 GB/s per direction,
    no large BAR1 needed); requires host setup and real transfer verification</td>
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
  sitting at 5 GT/s cannot be retrained directly to it. It runs **once**, at driver
  post-init: a second copy at GSP bootstrap left the PHY-init registers pre-set, RM's
  PCIe init rewrote the rate field back to Gen2 in between, and the pass that mattered
  then replayed the value it read without producing an equalisation edge. The target is
  read from the card's own `LNKCAP2` supported-speeds vector, which is what the PCIe
  speed fuses drive: a soft-unlocked card reports Gen2 and is not asked for more, so one
  build serves a box holding both kinds.
  A card that does not reach its target keeps the speed the link did train, down to but
  not below Gen3, and both ends of the link plus the card's own rate fields are set to
  the speed it holds, so a later retrain does not request a speed the slot cannot carry.
  Only a link that cannot hold Gen3 returns to the Gen2 path. The fuse value states what
  a card may request, not what the board it sits in will carry, so the two can differ.
  On the bench, two cold boots with the feature enabled both reached the top tier
  (`CMP Gen4: link trained (LnkSta=0x1104)`, upstream bridge `0x2104`) and the link was
  unchanged when read again with the serving workload running; the Gen3 tier is the
  fallback path and was not exercised on this machine.

`CMPUNLOCKER_EXPERIMENTAL_80G=1` adds `CMPEnable80G=1` to `RegistryDwords` and is
what lets a burned card take the wide layout; it is off by default and part of the
build cache. The key can also be dropped from `/etc/modprobe.d/cmp-pcie-gen2.conf`
to back a single machine out without a rebuild. `CMPDynDebug=1` adds the register
dumps. `CMPUNLOCKER_EXPERIMENTAL_GEN4=1` writes `cmp_pcie_gen=0` into that same
file, which asks each card for the speed its own fuses allow, and raises
`RMPcieLinkSpeed` to `0xF` so RM agrees with the link the driver trains; with the
flag off the built-in `cmp_pcie_gen=2` leaves every card at Gen2, as before.
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
