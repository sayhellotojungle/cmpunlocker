# Installation

Here are the steps to install cmpunlocker on your system.

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

## What a plain install turns on

Everything that decides something about a card is on, because every one of those
decisions is read off the card itself and an untouched card is never asked for more than
it already reports:

| On by default | What it does | To back it out |
| --- | --- | --- |
| Memory unlock | 64 GB on an 8 GB card, 40 GB on a 10 GB card, from the profile table | `--profile=` |
| Wide (80 GiB) layout | A card whose framebuffer fuses were burned reports the hidden FBP pair and is sized from its own `CFG1`/`LMR`. A card that cannot boot it steps back one layout. | `--no-80g` |
| Fused PCIe speed | Each card is asked for the speed its own speed fuses allow, so a burned card trains Gen4 and a soft-unlocked card stays at Gen2 | `--no-gen4` |
| ECC (DRAM and SRAM), full SM count | Part of the base patch set | nothing to turn off |
| VM passthrough setup | The unlock survives being handed to `vfio-pci` | `--no-passthrough` |
| IOMMU kernel parameters | `intel_iommu=on`/`amd_iommu=on` plus `iommu=pt` | `--no-iommu` |

GPU-to-GPU P2P is the one feature still behind a flag, and the only one that asks you to
choose a route: it needs host setup on top of the driver, and a capability report on a
machine where nothing actually moves looks exactly like success.

## Optional P2P

```bash
sudo ./install.sh --p2p              # the mailbox route, the default route of the two
sudo ./install.sh --p2p=bar1         # the BAR1 route, asked for by name
sudo ./install.sh --p2p=mailbox      # spelled out
```

This can be combined with the existing profile, IOMMU, Gen2-service and
passthrough options. `--p2p=bar1` is the route that needs the host's large BAR1 set up
first, and a card on the wide layout cannot take it at all; `--p2p=mailbox` needs no
large BAR1 but depends on one fused register per card, and is the route that was verified
on this machine's two 80 GiB cards, at Gen4 x16. Both are described, with how to tell
them apart, in [P2P setup and real-transfer verification](P2P.md).

P2P is off by default. Reinstall without `--p2p` to disable it and cold boot.
Re-run with the same `--p2p=` choice after a kernel upgrade to retain it. Switching
between the two routes is a rebuild, not a reconfiguration: the driver carries one
PCIe P2P protocol per peer pair.

## Uninstall

To uninstall cmpunlocker, run the following command:

```bash
sudo ./remove.sh --yes
```

Then perform a cold reboot (full power off, then boot).
