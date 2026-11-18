
    // cmpunlocker --p2p: override the GSP restriction on CMP cards only.
    // A capability report is not a substitute for testing real transfers.
    {
        NvU32 cmpDevId = pGpu->idInfo.PCIDeviceID >> 16;
        if (cmpDevId == 0x20C2 || cmpDevId == 0x2082)
        {
            pGpu->pcieP2PReadCaps = 0;  // NV0000_P2P_CAPS_STATUS_OK
            pGpu->pcieP2PWriteCaps = 0; // NV0000_P2P_CAPS_STATUS_OK
            NV_PRINTF(LEVEL_ERROR,
                      "CMPUNLOCK: PCIe P2P caps forced to OK (--p2p)\n");
        }
    }
