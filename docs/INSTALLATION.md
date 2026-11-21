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

## Optional P2P

```bash
sudo ./install.sh --p2p=bar1        # same as --p2p
sudo ./install.sh --p2p=mailbox
```

This can be combined with the existing profile, IOMMU, Gen2-service and
passthrough options. `--p2p=bar1` is the route that needs the host's large BAR1 set up
first; `--p2p=mailbox` needs no large BAR1 but depends on one fused register per card,
and is the route that was verified on this machine's two 80 GiB cards, at Gen4 x16.
Both are described, with how to tell them apart, in
[P2P setup and real-transfer verification](P2P.md).

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
