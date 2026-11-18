"""Compile the actual patched payload writer and exercise retry-size buffers."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PostblBounds(unittest.TestCase):
    @unittest.skipUnless(shutil.which("cc"), "requires a C compiler")
    def test_stock_signature_is_never_overwritten(self):
        patch = (ROOT / "driver/patches/sec2-postbl-plm-ss-cfg.patch").read_text()
        added = "\n".join(line[1:] for line in patch.splitlines()
                          if line.startswith("+") and not line.startswith("+++"))
        start = added.index("static void\n_kgspSec2PostblTimingPutU32")
        end = added.index("\nNV_STATUS\nkgspSec2PostblTimingRefillPayload", start)
        source = r'''
#include <stdint.h>
#include <string.h>
#include <assert.h>
typedef uint8_t NvU8;
typedef uint32_t NvU32;
typedef uint64_t NvU64;
#define SEC2_POSTBL_TIMING_SIGNATURE_SIZE 0xf800ULL
#define SEC2_POSTBL_TIMING_FILL_DWORD 0x4a7U
#define NV_PRINTF(...) ((void)0)
'''+ added[start:end] + r'''
int main(void) {
    unsigned char buffer[0x10000];
    const unsigned sizes[] = {0, 0x1000, 0xf7ff};
    for (unsigned j = 0; j < sizeof(sizes)/sizeof(sizes[0]); ++j) {
        memset(buffer, 0xa5, sizeof(buffer));
        _kgspSec2PostblTimingFillPayload(buffer, sizes[j], 0x1fa7cc, 0xfffff0ff);
        for (unsigned i = 0; i < sizeof(buffer); ++i) assert(buffer[i] == 0xa5);
    }
    _kgspSec2PostblTimingFillPayload(0, 0xf800, 0, 0);
    memset(buffer, 0xa5, sizeof(buffer));
    _kgspSec2PostblTimingFillPayload(buffer, 0xf800, 0x1fa7cc, 0xfffff0ff);
    assert(buffer[0xf76c] == 0xcc && buffer[0xf76d] == 0xa7);
    assert(buffer[0xf754] == 0xff && buffer[0xf755] == 0xf0);
    for (unsigned i = 0xf800; i < sizeof(buffer); ++i) assert(buffer[i] == 0xa5);
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            (path / "test.c").write_text(source)
            subprocess.run(["cc", "-Wall", "-Werror", str(path / "test.c"),
                            "-o", str(path / "test")], check=True, capture_output=True)
            subprocess.run([str(path / "test")], check=True, timeout=10)


if __name__ == "__main__":
    unittest.main()
