                    /*
                     * cmpunlocker: a burned card can outgrow the BAR1 the resize
                     * path can hand it (64 GiB of BAR1 in front of an 80 GiB
                     * framebuffer).  Above, "BAR1 cannot map the framebuffer" is a
                     * fatal registry error and GSP-RM never finishes init, which
                     * takes the card down even though the framebuffer is fine.
                     * The AUTO branch reports the same condition as unsupported,
                     * so report it the same way: static BAR1 stays off, together
                     * with the BAR1 P2P that needs a BAR1 at least as large as the
                     * framebuffer, and everything else boots.  Kernel-RM half; the
                     * closed GSP-RM half is _kgspCmpDynAdjustRegistry().
                     *
                     * 0x20C2 is the 8 GB SKU with a hidden FBP pair and 0x2082 the
                     * 10 GB SKU; see docs/CMP170HX-FUSE-DATA.zh-CN.md.
                     */
                    if (((pGpu->idInfo.PCIDeviceID >> 16) == 0x20C2U) ||
                        ((pGpu->idInfo.PCIDeviceID >> 16) == 0x2082U))
                    {
                        NV_PRINTF(LEVEL_ERROR,
                                  "CMP-BAR1 GPU%u: FB 0x%llx will not fit in BAR1 "
                                  "0x%llx, skipping forced static BAR1\n",
                                  pGpu->gpuInstance, bar1MapSize, bar1VASizeAligned);
                        return NV_ERR_NOT_SUPPORTED;
                    }
