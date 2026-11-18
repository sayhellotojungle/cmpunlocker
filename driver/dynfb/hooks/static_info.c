            // A burned 8 GB SKU has one more partition pair than its SKU word
            // declares, and the layout this boot is going to program is the one it
            // can reach, so the capacity comes from the policy instead of a table.
            // Note that a later step back cannot take this word back: the static
            // config info is packed once, before the first boot attempt, which is
            // why the stage is also recorded in the registry for the next init.
            {
                CmpDynGeomState *pStaticState = NULL;
                NvU64 dynFbBytes;

                pStaticState = _kgspCmpDynGeomEval(pGpu);
                if ((pStaticState != NULL) && (pStaticState->bBurned != 0U))
                {
                    NvU32 wideCfg1;
                    NvU32 wideLmr;

                    _kgspCmpDynGeomPair(pStaticState, &wideCfg1, &wideLmr);
                    dynFbBytes = _kgspCmpDynFbBytesPair(wideCfg1, wideLmr);
                    if (dynFbBytes > targetFbBytes)
                    {
                        NV_PRINTF(LEVEL_ERROR,
                                  "CMP-DYN FB GPU%u: wide layout -> fb_length 0x%llx "
                                  "(table 0x%llx, %u partitions)\n",
                                  pGpu->gpuInstance, (unsigned long long)dynFbBytes,
                                  (unsigned long long)targetFbBytes,
                                  _kgspCmpDynFbpCountWord(wideLmr));
                        targetFbBytes = dynFbBytes;
                    }
                }
            }

            // GSP builds the regions from the layout on its own, so these are only
            // logged to prove that it covers the whole framebuffer.
            if ((_cmpDynDebug != 0U) &&
                (numRegions <= NV2080_CTRL_CMD_FB_GET_FB_REGION_INFO_MAX_ENTRIES))
            {
                NvU32 dynR;

                for (dynR = 0; dynR < numRegions; dynR++)
                {
                    NV2080_CTRL_CMD_FB_GET_FB_REGION_FB_REGION_INFO *pRegion =
                        &pFbRegionInfoParams->fbRegion[dynR];

                    NV_PRINTF(LEVEL_ERROR,
                              "CMP-DYN REGION[%u] base=0x%llx limit=0x%llx "
                              "reserved=0x%llx perf=%u\n",
                              dynR, (unsigned long long)pRegion->base,
                              (unsigned long long)pRegion->limit,
                              (unsigned long long)pRegion->reserved,
                              pRegion->performance);
                }
            }

