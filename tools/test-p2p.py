#!/usr/bin/env python3
"""Verify real peer reads/writes without nvcc, via the CUDA driver API.

Same semantics as tools/test-p2p.cu: every ordered GPU pair, both endpoints
read back, so an aliased local mapping cannot silently pass. Requires only
the NVIDIA driver (libcuda); no CUDA toolkit needed:

    pip install cuda-python numpy
    python3 tools/test-p2p.py [gpu_id gpu_id ...]     # default: all GPUs
"""
import sys

import numpy as np

try:
    from cuda.bindings import driver as cu
except ImportError:
    from cuda import cuda as cu

CUDA_OK = 0
CUDA_ERROR_PEER_ACCESS_ALREADY_ENABLED = 704
COUNT = 1 << 22          # 4M uint32 = 16 MiB per endpoint
BYTES = COUNT * 4
SEED_A = 0x13579BDF
SEED_B = 0xECA86420


def ck(ret, what):
    err = ret[0] if isinstance(ret, tuple) else ret
    err = int(err)
    if err != CUDA_OK:
        try:
            name = cu.cuGetErrorString(cu.CUresult(err))[1]
        except Exception:
            name = b""
        raise RuntimeError(f"{what}: CUDA error {err} ({name!r})")
    if isinstance(ret, tuple) and len(ret) > 1:
        return ret[1]
    return None


def status(ret):
    """The status code of a call whose only output is its CUresult.

    The bindings return a one-tuple for those in some versions and a bare status
    in others, and both are truthy, so normalising with `or 0` cannot work: on the
    bench (cuda-bindings as shipped in its serving container) the tuple reached
    int() and the peer test died on a TypeError before allocating anything, which
    is not the same failure as peer access not working.
    """
    return int(ret[0] if isinstance(ret, tuple) else ret)


def pattern(seed):
    return (np.arange(COUNT, dtype=np.uint32) * np.uint32(2654435761)
            ^ np.uint32(seed))


def to_device(ptr, arr):
    ck(cu.cuMemcpyHtoD(ptr, arr.ctypes.data, BYTES), "HtoD")


def read_back(ptr):
    buf = np.empty(COUNT, dtype=np.uint32)
    ck(cu.cuMemcpyDtoH(buf.ctypes.data, ptr, BYTES), "DtoH")
    return buf


def pair_test(exec_id, remote_id):
    if not ck(cu.cuDeviceCanAccessPeer(exec_id, remote_id), "canAccessPeer"):
        print(f"GPU {exec_id} -> {remote_id}: peer access unavailable")
        return False

    ctx_e = ck(cu.cuDevicePrimaryCtxRetain(exec_id), "retain ctx")
    ctx_r = ck(cu.cuDevicePrimaryCtxRetain(remote_id), "retain ctx")
    local = peer = None
    try:
        ck(cu.cuCtxSetCurrent(ctx_e), "set current")
        err = status(cu.cuCtxEnablePeerAccess(ctx_r, 0))
        if err not in (CUDA_OK, CUDA_ERROR_PEER_ACCESS_ALREADY_ENABLED):
            ck(err, "enablePeerAccess")

        ck(cu.cuCtxSetCurrent(ctx_e), "set current")
        local = ck(cu.cuMemAlloc(BYTES), "cuMemAlloc local")
        ck(cu.cuCtxSetCurrent(ctx_r), "set current")
        peer = ck(cu.cuMemAlloc(BYTES), "cuMemAlloc peer")

        pat_a, pat_b = pattern(SEED_A), pattern(SEED_B)
        passed = True
        for direction in ("read", "write"):
            print(f"GPU {exec_id} -> {remote_id}: peer {direction}...",
                  flush=True)
            ck(cu.cuCtxSetCurrent(ctx_e), "set current")
            to_device(local, pat_a)
            ck(cu.cuCtxSetCurrent(ctx_r), "set current")
            to_device(peer, pat_b)
            ck(cu.cuCtxSynchronize(), "sync")

            if direction == "read":       # executor pulls peer -> local
                ck(cu.cuCtxSetCurrent(ctx_e), "set current")
                ck(cu.cuMemcpyPeer(local, ctx_e, peer, ctx_r, BYTES),
                   "peer read")
                changed = ("result", local, ctx_e, pat_b)
                source = ("source", peer, ctx_r, pat_b)
            else:                         # executor pushes local -> peer
                ck(cu.cuCtxSetCurrent(ctx_r), "set current")
                ck(cu.cuMemcpyPeer(peer, ctx_r, local, ctx_e, BYTES),
                   "peer write")
                changed = ("result", peer, ctx_r, pat_a)
                source = ("source", local, ctx_e, pat_a)

            ck(cu.cuCtxSynchronize(), "sync")
            for label, ptr, ctx, want in (changed, source):
                ck(cu.cuCtxSetCurrent(ctx), "set current")
                if not np.array_equal(read_back(ptr), want):
                    print(f"GPU {exec_id} -> {remote_id}: peer {direction} "
                          f"corrupted {label}")
                    passed = False
        return passed
    finally:
        # Guarded because the allocations can fail: freeing None would replace the
        # real error with an UnboundLocalError and hide which call actually failed.
        if local is not None:
            ck(cu.cuCtxSetCurrent(ctx_e), "set current")
            ck(cu.cuMemFree(local), "free local")
        if peer is not None:
            ck(cu.cuCtxSetCurrent(ctx_r), "set current")
            ck(cu.cuMemFree(peer), "free peer")
        cu.cuDevicePrimaryCtxRelease(exec_id)
        cu.cuDevicePrimaryCtxRelease(remote_id)


def main():
    ck(cu.cuInit(0), "cuInit")
    count = ck(cu.cuDeviceGetCount(), "deviceGetCount")
    devices = list(range(count)) if len(sys.argv) < 2 else [int(a) for a in sys.argv[1:]]
    if len(devices) < 2 or max(devices) >= count:
        sys.exit(f"usage: {sys.argv[0]} [gpu_id x2+] ({count} GPUs visible)")
    for dev in devices:
        try:
            # The bindings hand back the padded name buffer itself, as bytes.
            name = ck(cu.cuDeviceGetName(64, dev), "name")
            name = (name.split(b"\x00")[0].decode("ascii", "replace")
                    if isinstance(name, bytes) else str(name))
            print(f"GPU {dev}: {name}")
        except Exception:
            print(f"GPU {dev}")

    failures = pairs = 0
    for executor in devices:
        for remote in devices:
            if executor == remote:
                continue
            pairs += 1
            if not pair_test(executor, remote):
                failures += 1
    print(f"{pairs - failures}/{pairs} directed pairs passed real peer "
          f"reads and writes.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
