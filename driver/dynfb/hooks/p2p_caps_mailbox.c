
    //
    // cmpunlocker --p2p=mailbox: GSP-RM reports PCIe P2P as unsupported on a CMP,
    // but the GA100 mailbox hardware is there and p2p/0020-mailbox-trap31.patch
    // can get the aperture programmed, so report the caps the mailbox path needs.
    //
    // The claim is withheld where it would be a lie. Arming that trap takes a
    // write to the trap's own privilege mask, and that mask is fused per part:
    // where 0x0012277c does not read back 0xFFFFFFFF nothing programs NV_P2P,
    // and a peer copy then returns success on garbage. That is not hypothetical
    // -- it is what a g250 CMP with PLM 0xFFFFFF8F was measured doing: a 32 MiB
    // cuMemcpyPeer reported success and 33423360 of its bytes came back wrong,
    // only the first 128 KiB intact. A truthful "not supported" is worth more
    // than that, so the caps stay exactly as GSP reported them here.
    //
    // The registry key is the feature switch build.sh writes for --p2p=mailbox;
    // without it neither the trap nor this override is in play. The BAR1 variant
    // of this hook (hooks/p2p_caps.c) has no such guard because that path is
    // verified by a BAR1 that maps the framebuffer, not by this register.
    //
    {
        NvU32 cmpDevId  = pGpu->idInfo.PCIDeviceID >> 16;
        NvU32 mailbox   = 0;

        if (((cmpDevId == 0x20C2U) || (cmpDevId == 0x2082U)) &&
            (osReadRegistryDword(pGpu, "CMPEnableP2PMailbox", &mailbox) == NV_OK) &&
            (mailbox == 1))
        {
            NvU32 trapPlm = GPU_REG_RD32(pGpu, 0x0012277cU);

            if (trapPlm == 0xFFFFFFFFU)
            {
                pGpu->pcieP2PReadCaps  = 0;  // NV0000_P2P_CAPS_STATUS_OK
                pGpu->pcieP2PWriteCaps = 0;  // NV0000_P2P_CAPS_STATUS_OK
                NV_PRINTF(LEVEL_ERROR,
                          "CMP-P2P GPU%u: mailbox caps forced to OK (devId 0x%04x, "
                          "trap31 PLM 0x%08x)\n",
                          gpuGetInstance(pGpu), cmpDevId, trapPlm);
            }
            else
            {
                NV_PRINTF(LEVEL_ERROR,
                          "CMP-P2P GPU%u: mailbox P2P left unsupported, trap31 PLM "
                          "0x%08x cannot be written so NV_P2P stays unprogrammed\n",
                          gpuGetInstance(pGpu), trapPlm);
            }
        }
    }
