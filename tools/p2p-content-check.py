#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Check the bytes a peer copy moved, not the code it returned.

tools/test-p2p.py answers "does a peer transfer run at speed and keep its
content"; this answers the narrower question a mailbox P2P build has to be
asked, because that path completes a copy without ever having been programmed.
The NV_P2P aperture registers carry the page each write chunk lands on, so on a
card whose PRI trap could not be armed the target's window never moves off its
reset value and everything past the first 64 KiB of the copy is stale memory
behind a CUDA_OK.  Measured on a g250 CMP 170HX: a 32 MiB cuMemcpyPeer returned
success and 33423360 of its 33554432 bytes came back wrong.

What a return code cannot see, a destination that starts out wrong can, so every
size below is copied over a destination preloaded with the complement of the
expected data, at the exact sizes where a window that stops moving first shows:
the mailbox window itself (64 KiB) and the half-megabyte pages it describes.
Every ordered pair is checked, and the first wrong byte offset is reported,
because "33423360 bad bytes, starting at 0x20000" names the mechanism and
"33423360 bad bytes" does not.

Only libcuda is needed -- no CUDA toolkit, no cuda-python:

    python3 tools/p2p-content-check.py [gpu_id gpu_id ...]   # default: all GPUs
    python3 tools/p2p-content-check.py --bw [--size=256m] [--seconds=5] 0 1

The content pass is what decides whether P2P works. `--bw` is a separate question
asked of the same two buffers: one sustained copy per direction, timed, reported as
the best of the samples. On the mailbox route that number is the cost of the route,
and the only figure published for it is a Gen2 link.
"""
import ctypes as ct
import os
import sys
import time

SIZES = (64 * 1024 - 1, 64 * 1024, 64 * 1024 + 1,
         128 * 1024 - 1, 128 * 1024, 128 * 1024 + 1,
         512 * 1024, 1024 * 1024, 8 * 1024 * 1024, 32 * 1024 * 1024)

COMPLEMENT = bytes(b ^ 0xFF for b in range(256))


class CUDA:
    def __init__(self):
        self.lib = ct.CDLL("libcuda.so.1")
        ptr, devptr, size = ct.c_void_p, ct.c_uint64, ct.c_size_t
        signatures = {
            "cuInit": [ct.c_uint],
            "cuDeviceGetCount": [ct.POINTER(ct.c_int)],
            "cuDeviceGet": [ct.POINTER(ct.c_int), ct.c_int],
            "cuDeviceGetName": [ct.c_void_p, ct.c_int, ct.c_int],
            "cuDeviceCanAccessPeer": [ct.POINTER(ct.c_int), ct.c_int, ct.c_int],
            "cuCtxCreate_v2": [ct.POINTER(ptr), ct.c_uint, ct.c_int],
            "cuCtxDestroy_v2": [ptr],
            "cuCtxSetCurrent": [ptr],
            "cuCtxEnablePeerAccess": [ptr, ct.c_uint],
            "cuCtxSynchronize": [],
            "cuMemAlloc_v2": [ct.POINTER(devptr), size],
            "cuMemcpyHtoD_v2": [devptr, ptr, size],
            "cuMemcpyDtoH_v2": [ptr, devptr, size],
            "cuMemcpyPeer": [devptr, ptr, devptr, ptr, size],
        }
        for name, args in signatures.items():
            fn = getattr(self.lib, name)
            fn.argtypes, fn.restype = args, ct.c_int
        self.call("cuInit", 0)

    def call(self, name, *args):
        status = getattr(self.lib, name)(*args)
        if status:
            raise RuntimeError("%s: CUDA error %d" % (name, status))

    def devices(self):
        count = ct.c_int()
        self.call("cuDeviceGetCount", ct.byref(count))
        result = []
        for ordinal in range(count.value):
            device = ct.c_int()
            self.call("cuDeviceGet", ct.byref(device), ordinal)
            result.append(device.value)
        return result

    def name(self, device):
        buf = ct.create_string_buffer(96)
        # Best effort: a name is for the log, and a driver that will not give one
        # should not turn a check into a failure.
        self.lib.cuDeviceGetName(buf, len(buf), device)
        return buf.value.decode("utf-8", "replace")

    def create(self, device):
        context = ct.c_void_p()
        self.call("cuCtxCreate_v2", ct.byref(context), 0, device)
        return context

    def current(self, context):
        self.call("cuCtxSetCurrent", context)

    def enable(self, device, peer, peer_context):
        supported = ct.c_int()
        self.call("cuDeviceCanAccessPeer", ct.byref(supported), device, peer)
        if not supported.value:
            raise RuntimeError("peer access unsupported: %d->%d "
                               "(this is what the guard in hooks/"
                               "p2p_caps_mailbox.c is for)" % (device, peer))
        self.call("cuCtxEnablePeerAccess", peer_context, 0)

    def allocate(self, size):
        pointer = ct.c_uint64()
        self.call("cuMemAlloc_v2", ct.byref(pointer), size)
        return pointer

    def upload(self, pointer, data):
        host = ct.create_string_buffer(data, len(data))
        self.call("cuMemcpyHtoD_v2", pointer, host, len(data))

    def download(self, pointer, size):
        host = ct.create_string_buffer(size)
        self.call("cuMemcpyDtoH_v2", host, pointer, size)
        return host.raw

    def copy(self, dest, dest_context, source, source_context, size):
        self.call("cuMemcpyPeer", dest, dest_context, source, source_context, size)
        self.call("cuCtxSynchronize")

    def destroy(self, context):
        # Destroying the private context also frees its allocations and peer maps.
        self.call("cuCtxDestroy_v2", context)


def _mismatch(actual, expected):
    """How wrong a destination came back, as (bad_bytes, offset_of_first_or_None).

    Chunked rather than one pass over the whole buffer: the timed mode checks at
    gigabyte sizes, where building a Python-level XOR of everything would dominate
    the run.  Equal chunks are settled by a bytes comparison, which is what a
    correct copy is made of.
    """
    bad, first, chunk = 0, None, 1 << 20
    for start in range(0, len(actual), chunk):
        left, right = actual[start:start + chunk], expected[start:start + chunk]
        if left == right:
            continue
        diff = bytes(a ^ b for a, b in zip(left, right))
        bad += len(diff) - diff.count(0)
        if first is None:
            first = start + (len(diff) - len(diff.lstrip(b"\0")))
    return bad, first


def check(cuda, wanted=None, sizes=SIZES):
    devices = cuda.devices()
    if wanted is not None:
        devices = [d for d in devices if d in wanted]
    if len(devices) < 2:
        raise RuntimeError("at least two visible CUDA devices are required")
    contexts, pointers = [], []
    try:
        for device in devices:
            context = cuda.create(device)
            contexts.append(context)
            cuda.current(context)
            pointers.append(cuda.allocate(max(sizes)))
        print("%s: %d checks per ordered pair, sizes %s"
              % (" / ".join(cuda.name(d) for d in devices), len(sizes),
                 ", ".join("%dB" % s for s in sizes)), flush=True)
        for source in range(len(devices)):
            for dest in range(len(devices)):
                if source == dest:
                    continue
                cuda.current(contexts[source])
                cuda.enable(devices[source], devices[dest], contexts[dest])
        cases = 0
        failures = 0
        for source in range(len(devices)):
            for dest in range(len(devices)):
                if source == dest:
                    continue
                for size in sizes:
                    expected = os.urandom(size)
                    cuda.current(contexts[source])
                    cuda.upload(pointers[source], expected)
                    cuda.current(contexts[dest])
                    # Every byte starts wrong, the first one included, so a copy
                    # that never left the destination's own memory cannot pass.
                    cuda.upload(pointers[dest], expected.translate(COMPLEMENT))
                    cuda.copy(pointers[dest], contexts[dest], pointers[source],
                              contexts[source], size)
                    actual = cuda.download(pointers[dest], size)
                    if actual != expected:
                        bad, first = _mismatch(actual, expected)
                        print("FAIL %d->%d %d bytes: %d bad, first at 0x%x"
                              % (devices[source], devices[dest], size, bad, first),
                              flush=True)
                        failures += 1
                    else:
                        print("PASS %d->%d %d bytes"
                              % (devices[source], devices[dest], size), flush=True)
                    cases += 1
        print("%s: %d content checks, %d devices"
              % ("FAIL" if failures else "PASS", cases, len(devices)), flush=True)
        return failures
    finally:
        for context in reversed(contexts):
            cuda.destroy(context)


def _available_memory():
    """Host RAM an allocation has to fit in, or None if the box does not say."""
    try:
        with open("/proc/meminfo") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return None


def bandwidth(cuda, wanted, size, seconds):
    """One number per direction, and the bytes behind it, at the size it is quoted at.

    Not a benchmark -- a sustained set of cuMemcpyPeer calls per direction, timed,
    and then one copy of the same size checked byte for byte.  The table of measured
    rates in docs/P2P.md comes from this, which is why it checks the copy it times:
    a figure for bytes that never arrived is the exact failure this route is known
    for, and it is the one a timer cannot see.
    """
    devices = cuda.devices()
    if wanted is not None:
        devices = [d for d in devices if d in wanted]
    if len(devices) != 2:
        raise RuntimeError("the bandwidth mode takes exactly two GPUs")
    # The checked copy needs three host buffers of `size`: the pattern, its
    # complement, and the readback.  Refusing is better than the OOM killer
    # choosing which process on the box dies.
    available = _available_memory()
    if available is not None and size * 3 > available * 3 // 4:
        raise RuntimeError("%d MiB needs more host memory than this box has to give"
                           % (size >> 20))
    contexts, pointers, failures = [], [], 0
    try:
        for device in devices:
            context = cuda.create(device)
            contexts.append(context)
            cuda.current(context)
            pointers.append(cuda.allocate(size))
        for source in range(2):
            for dest in range(2):
                if source != dest:
                    cuda.current(contexts[source])
                    cuda.enable(devices[source], devices[dest], contexts[dest])
        for direction in ((0, 1), (1, 0)):
            source, dest = direction
            cuda.current(contexts[source])
            cuda.upload(pointers[source], os.urandom(min(size, 1 << 20)))
            best = 0.0
            count = 0
            deadline = time.perf_counter() + seconds
            while time.perf_counter() < deadline:
                start = time.perf_counter()
                cuda.copy(pointers[dest], contexts[dest], pointers[source],
                          contexts[source], size)
                elapsed = time.perf_counter() - start
                best = max(best, size / elapsed / 1e9)
                count += 1
            # The best of the samples, not the mean: the first copy of a fresh peer
            # pair pays for the mapping, and a mean would report that as the link.
            #
            # Then the same size once more, checked.  A number measured on a copy
            # whose bytes were never looked at is a number about a DMA engine that
            # might be writing to the wrong window, and at 4 GiB the window is the
            # one thing this route can get wrong silently.
            expected = os.urandom(size)
            cuda.current(contexts[source])
            cuda.upload(pointers[source], expected)
            cuda.current(contexts[dest])
            cuda.upload(pointers[dest], expected.translate(COMPLEMENT))
            cuda.copy(pointers[dest], contexts[dest], pointers[source],
                      contexts[source], size)
            actual = cuda.download(pointers[dest], size)
            verdict = "PASS" if actual == expected else "FAIL"
            extra = ""
            if verdict == "FAIL":
                bad, first = _mismatch(actual, expected)
                extra = " (%d bad, first at 0x%x)" % (bad, first)
                failures += 1
            print("%d->%d  %d MiB  best %.2f GB/s over %d copies  content %s%s"
                  % (devices[source], devices[dest], size >> 20, best, count,
                     verdict, extra), flush=True)
        return failures
    finally:
        for context in reversed(contexts):
            cuda.destroy(context)


def _bytes(text):
    text = text.strip().lower()
    for suffix, shift in (("g", 30), ("m", 20), ("k", 10)):
        if text.endswith(suffix):
            return int(float(text[:-1]) * (1 << shift))
    return int(text)


def main(argv):
    size, seconds, bw = 256 << 20, 5.0, False
    args = []
    # Both --flag value and --flag=value, because the shell keeps the second form
    # when someone edits a command line that already had an argument after it.
    for item in argv:
        if item.startswith("--") and "=" in item:
            args.extend(item.split("=", 1))
        else:
            args.append(item)
    if "--bw" in args:
        args.remove("--bw")
        bw = True
    while "--size" in args:
        at = args.index("--size")
        if at + 1 == len(args):
            print("--size needs a value", file=sys.stderr)
            return 2
        try:
            size = _bytes(args[at + 1])
        except ValueError:
            print("--size: bad value %r" % args[at + 1], file=sys.stderr)
            return 2
        del args[at:at + 2]
    while "--seconds" in args:
        at = args.index("--seconds")
        if at + 1 == len(args):
            print("--seconds needs a value", file=sys.stderr)
            return 2
        try:
            seconds = float(args[at + 1])
        except ValueError:
            print("--seconds: bad value %r" % args[at + 1], file=sys.stderr)
            return 2
        del args[at:at + 2]
    try:
        wanted = {int(a) for a in args} if args else None
    except ValueError:
        print("usage: p2p-content-check.py [--bw] [--size=256m] [--seconds=5] "
              "[gpu_id ...]", file=sys.stderr)
        return 2
    try:
        cuda = CUDA()
        if bw:
            return bandwidth(cuda, wanted, size, seconds)
        return 1 if check(cuda, wanted) else 0
    except (OSError, AttributeError, RuntimeError) as exc:
        print("FAIL: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
