// cmpunlocker --p2p: default resizable BAR on, so BAR1 P2P mappings can cover the
// whole framebuffer without waiting for the module parameter.  This is a default in
// the compile-time registry table, so it cannot be gated by PCI device ID: every GPU
// the module drives gets it.  NVreg_EnableResizableBar=1 in the modprobe config sets
// the same value at load time; this line is belt-and-braces for a tree installed
// without that file (docs/P2P.md).
NV_DEFINE_REG_ENTRY_GLOBAL(__NV_ENABLE_RESIZABLE_BAR, 1);
