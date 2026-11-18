#!/usr/bin/env python3
"""Check what the SMs and the framebuffer really are, without nvcc.

The CMP 170HX locks both its memory geometry and its SM count in fuses, and a
driver-side unlock can make the *reporting* of either one look good without the
silicon following. This tool therefore measures, through the CUDA driver API
only (libcuda, no toolkit, no cuda-python):

  sm    per device: SM count from the driver, %nsmid from the chip itself,
        the SM ids the hardware actually schedules blocks onto, and a
        deterministic per-thread compute check over every thread.
  load  sustained FP32 FMA throughput, so the SM count can be checked against
        the throughput it implies (an enumerated-but-dead SM does not compute).
  fb    fill the whole allocation with an index-dependent pattern and re-derive
        and compare every 32-bit word on the device, then prove the checker is
        live by injecting a known 4 KiB corruption.

    sudo python3 tools/sm-probe.py sm
    sudo python3 tools/sm-probe.py load [--bus 09:00.0]
    sudo python3 tools/sm-probe.py fb [--bus 09:00.0] [--gib 78]
"""
import argparse
import ctypes
import sys
import time

GB = 1 << 30
cu = ctypes.CDLL("libcuda.so.1")

CU_DEVICE_ATTRIBUTE_MAX_THREADS_PER_BLOCK = 1
CU_DEVICE_ATTRIBUTE_SHARED_MEMORY_PER_BLOCK = 8
CU_DEVICE_ATTRIBUTE_REGISTERS_PER_BLOCK = 12
CU_DEVICE_ATTRIBUTE_CLOCK_RATE = 13
CU_DEVICE_ATTRIBUTE_MULTIPROCESSOR_COUNT = 16
CU_DEVICE_ATTRIBUTE_MEMORY_CLOCK_RATE = 36
CU_DEVICE_ATTRIBUTE_GLOBAL_MEMORY_BUS_WIDTH = 37
CU_DEVICE_ATTRIBUTE_L2_CACHE_SIZE = 38
CU_DEVICE_ATTRIBUTE_MAX_THREADS_PER_MULTIPROCESSOR = 39

# FMAs per thread per iteration; the chains are independent so the loop is
# throughput bound, and the seeds come from %tid/%ctaid so ptxas cannot fold it.
FMA_CHAINS = 6
FMA_ITERS = 2000000
HASH_MIX_A = 0x9E3779B1
HASH_MIX_B = 0x85EBCA77

PTX_HEAD = ".version 8.0\n.target sm_80\n.address_size 64\n\n"
PTX_HEAD85 = ".version 8.5\n.target sm_80\n.address_size 64\n\n"

# Every block spins on its SM for a while and then records the SM it is on.
PTX_PROBE = PTX_HEAD + """
.visible .entry sm_probe(
    .param .u64 p_out,
    .param .u64 p_spin
)
{
    .reg .pred   %p<2>;
    .reg .b32    %r<5>;
    .reg .b64    %rd<8>;

    ld.param.u64      %rd1, [p_out];
    ld.param.u64      %rd2, [p_spin];
    mov.u32           %r1, %tid.x;
    setp.ne.u32       %p1, %r1, 0;
    @%p1 bra          L_DONE;
    mov.u32           %r2, %smid;
    mov.u64           %rd3, %clock64;
L_SPIN:
    mov.u64           %rd4, %clock64;
    sub.u64           %rd5, %rd4, %rd3;
    setp.lt.u64       %p1, %rd5, %rd2;
    @%p1 bra          L_SPIN;
    mov.u32           %r3, %ctaid.x;
    mul.wide.u32      %rd6, %r3, 4;
    add.u64           %rd7, %rd1, %rd6;
    st.global.u32     [%rd7], %r2;
L_DONE:
    ret;
}
"""

# %nsmid needs a newer PTX ISA, so it lives in its own module and its absence
# cannot take the probe down with it.
PTX_NSMID = PTX_HEAD85 + """
.visible .entry nsmid_probe(
    .param .u64 p_out
)
{
    .reg .pred   %p<2>;
    .reg .b32    %r<3>;
    .reg .b64    %rd<2>;

    ld.param.u64      %rd1, [p_out];
    mov.u32           %r1, %tid.x;
    setp.ne.u32       %p1, %r1, 0;
    @%p1 bra          L_DONE;
    mov.u32           %r2, %nsmid;
    st.global.u32     [%rd1], %r2;
L_DONE:
    ret;
}
"""

PTX_FMA = PTX_HEAD + """
.visible .entry fma_bench(
    .param .u64 p_out,
    .param .u32 p_iters
)
{
    .reg .pred   %p<2>;
    .reg .f32    %f<10>;
    .reg .b32    %r<6>;
    .reg .b64    %rd<2>;

    ld.param.u64      %rd1, [p_out];
    ld.param.u32      %r1, [p_iters];
    mov.u32           %r2, %tid.x;
    mov.u32           %r3, %ctaid.x;
    cvt.rn.f32.u32    %f1, %r2;
    mul.f32           %f1, %f1, 0f30888889;
    add.f32           %f1, %f1, 0f3F800000;
    cvt.rn.f32.u32    %f2, %r3;
    mul.f32           %f2, %f2, 0f31888889;
    add.f32           %f2, %f2, 0f3F800000;
    mov.f32           %f3, 0f3F7FFFFF;
    mov.f32           %f4, 0f3F800000;
    mov.f32           %f5, 0f3F800001;
    mov.f32           %f6, 0f33800000;
    mov.f32           %f7, 0f3F800000;
    mov.f32           %f8, 0f3F800001;
L_LOOP:
    fma.rn.f32        %f1, %f1, %f3, %f6;
    fma.rn.f32        %f2, %f2, %f3, %f6;
    fma.rn.f32        %f4, %f4, %f3, %f6;
    fma.rn.f32        %f5, %f5, %f3, %f6;
    fma.rn.f32        %f7, %f7, %f3, %f6;
    fma.rn.f32        %f8, %f8, %f3, %f6;
    sub.u32           %r1, %r1, 1;
    setp.ne.u32       %p1, %r1, 0;
    @%p1 bra          L_LOOP;
    add.f32           %f1, %f1, %f2;
    add.f32           %f4, %f4, %f5;
    add.f32           %f7, %f7, %f8;
    add.f32           %f1, %f1, %f4;
    add.f32           %f1, %f1, %f7;
    cvt.rzi.u32.f32   %r4, %f1;
    setp.eq.u32       %p1, %r4, 1;
    @!%p1             bra L_END;
    st.global.f32     [%rd1], %f1;
L_END:
    ret;
}
"""

# out[i] = hash(i) for every thread; the host knows the same hash, so a bad SM
# shows up as a mismatching thread rather than as a passing launch.
PTX_VERIFY = PTX_HEAD + """
.visible .entry verify_kernel(
    .param .u64 p_out,
    .param .u32 p_n
)
{
    .reg .pred   %p<2>;
    .reg .b32    %r<6>;
    .reg .b64    %rd<3>;

    ld.param.u64      %rd1, [p_out];
    ld.param.u32      %r1, [p_n];
    mov.u32           %r2, %ctaid.x;
    mov.u32           %r3, %ntid.x;
    mov.u32           %r4, %tid.x;
    mad.lo.s32        %r5, %r2, %r3, %r4;
    setp.ge.u32       %p1, %r5, %r1;
    @%p1 bra          L_END;
    mul.lo.u32        %r2, %r5, 0x9E3779B1;
    add.u32           %r2, %r2, 0x12345;
    mul.lo.u32        %r2, %r2, 0x85EBCA77;
    xor.b32           %r2, %r2, %r5;
    mul.wide.u32      %rd2, %r5, 4;
    add.u64           %rd2, %rd1, %rd2;
    st.global.u32     [%rd2], %r2;
L_END:
    ret;
}
"""

PTX_FILL = PTX_HEAD + """
.visible .entry fill_kernel(
    .param .u64 p_out,
    .param .u64 p_n
)
{
    .reg .pred   %p<2>;
    .reg .b32    %r<8>;
    .reg .b64    %rd<8>;

    ld.param.u64      %rd1, [p_out];
    ld.param.u64      %rd2, [p_n];
    mov.u32           %r1, %ntid.x;
    mov.u32           %r2, %nctaid.x;
    mul.lo.u32        %r7, %r1, %r2;
    cvt.u64.u32       %rd4, %r7;
    mov.u32           %r3, %ctaid.x;
    mov.u32           %r4, %tid.x;
    mad.lo.s32        %r5, %r3, %r1, %r4;
    cvt.u64.u32       %rd3, %r5;
L_LOOP:
    setp.ge.u64       %p1, %rd3, %rd2;
    @%p1 bra          L_END;
    shr.u64           %rd5, %rd3, 32;
    cvt.u32.u64       %r2, %rd5;
    mul.lo.u32        %r6, %r5, 0x9E3779B1;
    add.u32           %r6, %r6, %r2;
    add.u32           %r6, %r6, 0x12345;
    mul.lo.u32        %r6, %r6, 0x85EBCA77;
    xor.b32           %r6, %r6, %r5;
    mul.lo.u64        %rd6, %rd3, 4;
    add.u64           %rd6, %rd1, %rd6;
    st.global.u32     [%rd6], %r6;
    add.u64           %rd3, %rd3, %rd4;
    cvt.u32.u64       %r5, %rd3;
    bra               L_LOOP;
L_END:
    ret;
}
"""

PTX_CHECK = PTX_HEAD + """
.visible .entry check_kernel(
    .param .u64 p_out,
    .param .u64 p_n,
    .param .u64 p_ctr
)
{
    .reg .pred   %p<2>;
    .reg .b32    %r<8>;
    .reg .b64    %rd<8>;

    ld.param.u64      %rd1, [p_out];
    ld.param.u64      %rd2, [p_n];
    ld.param.u64      %rd7, [p_ctr];
    mov.u32           %r1, %ntid.x;
    mov.u32           %r2, %nctaid.x;
    mul.lo.u32        %r7, %r1, %r2;
    cvt.u64.u32       %rd4, %r7;
    mov.u32           %r3, %ctaid.x;
    mov.u32           %r4, %tid.x;
    mad.lo.s32        %r5, %r3, %r1, %r4;
    cvt.u64.u32       %rd3, %r5;
L_LOOP:
    setp.ge.u64       %p1, %rd3, %rd2;
    @%p1 bra          L_END;
    shr.u64           %rd5, %rd3, 32;
    cvt.u32.u64       %r2, %rd5;
    mul.lo.u32        %r6, %r5, 0x9E3779B1;
    add.u32           %r6, %r6, %r2;
    add.u32           %r6, %r6, 0x12345;
    mul.lo.u32        %r6, %r6, 0x85EBCA77;
    xor.b32           %r6, %r6, %r5;
    mul.lo.u64        %rd6, %rd3, 4;
    add.u64           %rd6, %rd1, %rd6;
    ld.global.u32     %r7, [%rd6];
    setp.eq.u32       %p1, %r7, %r6;
    @%p1 bra          L_NEXT;
    red.global.add.u64      [%rd7], 1;
    red.global.min.u64      [%rd7+8], %rd3;
L_NEXT:
    add.u64           %rd3, %rd3, %rd4;
    cvt.u32.u64       %r5, %rd3;
    bra               L_LOOP;
L_END:
    ret;
}
"""


def ck(rc, what):
    if rc != 0:
        s = ctypes.c_char_p()
        cu.cuGetErrorString(rc, ctypes.byref(s))
        raise RuntimeError("%s: CUDA error %d (%s)" %
                           (what, rc, s.value.decode() if s.value else "?"))


cu.cuCtxSetCurrent.argtypes = [ctypes.c_void_p]
cu.cuCtxDestroy.argtypes = [ctypes.c_void_p]
cu.cuMemFree_v2.argtypes = [ctypes.c_void_p]
cu.cuMemAlloc_v2.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_size_t]
cu.cuMemsetD8_v2.argtypes = [ctypes.c_void_p, ctypes.c_ubyte, ctypes.c_size_t]
cu.cuMemcpyHtoD_v2.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
cu.cuMemcpyDtoH_v2.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
cu.cuLaunchKernel.argtypes = [ctypes.c_void_p] + [ctypes.c_uint] * 7 + \
    [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p)]
cu.cuModuleLoadDataEx.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_char_p,
                                  ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
cu.cuModuleGetFunction.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
                                   ctypes.c_char_p]


def open_device(bus):
    """First CUDA device whose BDF contains `bus`, or device number `bus`."""
    ck(cu.cuInit(0), "cuInit")
    for i in range(16):
        d = ctypes.c_int()
        if cu.cuDeviceGet(ctypes.byref(d), i) != 0:
            break
        b = ctypes.create_string_buffer(64)
        cu.cuDeviceGetPCIBusId(b, 64, d)
        bus_id = b.value.decode()
        if bus is None or bus.upper() in bus_id.upper():
            return d.value, bus_id
    raise SystemExit("no CUDA device matching %r" % bus)


def attr(dev, a):
    v = ctypes.c_int(-1)
    ck(cu.cuDeviceGetAttribute(ctypes.byref(v), a, dev), "cuDeviceGetAttribute")
    return v.value


def context(dev):
    ctx = ctypes.c_void_p()
    ck(cu.cuCtxCreate_v2(ctypes.byref(ctx), 0, dev), "cuCtxCreate")
    cu.cuCtxSetCurrent(ctx)
    return ctx


def load(ptx, name):
    mod = ctypes.c_void_p()
    ck(cu.cuModuleLoadDataEx(ctypes.byref(mod), ptx.encode(), 0, None, None),
       "load " + name)
    fn = ctypes.c_void_p()
    ck(cu.cuModuleGetFunction(ctypes.byref(fn), mod, name.encode()), "get " + name)
    return mod, fn


def alloc(nbytes, init=None):
    p = ctypes.c_void_p()
    ck(cu.cuMemAlloc_v2(ctypes.byref(p), ctypes.c_size_t(nbytes)), "cuMemAlloc")
    if init is not None:
        ck(cu.cuMemcpyHtoD_v2(p, init, ctypes.c_size_t(nbytes)), "HtoD")
    return p


def read_back(p, nbytes):
    buf = (ctypes.c_ubyte * nbytes)()
    ck(cu.cuMemcpyDtoH_v2(buf, p, ctypes.c_size_t(nbytes)), "DtoH")
    return buf


def launch(fn, grid, threads, args):
    params = (ctypes.c_void_p * len(args))(*[ctypes.cast(ctypes.byref(a), ctypes.c_void_p)
                                             for a in args])
    ck(cu.cuLaunchKernel(fn, grid, 1, 1, threads, 1, 1, 0, None, params, None), "launch")
    ck(cu.cuCtxSynchronize(), "sync")


def word_hash(i):
    """Host mirror of the in-kernel pattern; the index spans more than 2^32."""
    lo = i & 0xFFFFFFFF
    hi = (i >> 32) & 0xFFFFFFFF
    x = (lo * HASH_MIX_A + hi + 0x12345) & 0xFFFFFFFF
    return ((x * HASH_MIX_B) ^ lo) & 0xFFFFFFFF


def cmd_sm(args):
    ck(cu.cuInit(0), "cuInit")
    print("%-11s %-8s %-8s %-8s %-6s %-6s %-9s %-9s %-8s %s" % (
        "bus", "SM", "nsmid", "used", "min", "max", "TFLOPS", "MiB", "threads", "check"))
    for i in range(16):
        d = ctypes.c_int()
        if cu.cuDeviceGet(ctypes.byref(d), i) != 0:
            break
        b = ctypes.create_string_buffer(64)
        cu.cuDeviceGetPCIBusId(b, 64, d)
        if args.bus and args.bus.upper() not in b.value.decode().upper():
            continue
        tot = ctypes.c_size_t()
        cu.cuDeviceTotalMem_v2(ctypes.byref(tot), d)
        nsm = attr(d, CU_DEVICE_ATTRIBUTE_MULTIPROCESSOR_COUNT)
        ctx = context(d)

        grid = nsm * 2
        _, probe = load(PTX_PROBE, "sm_probe")
        fb = alloc(4 * grid)
        spin = ctypes.c_ulonglong(4000000)
        launch(probe, grid, 1024, [fb, spin])
        raw = read_back(fb, 4 * grid)
        smids = [int.from_bytes(raw[j * 4:j * 4 + 4], "little") for j in range(grid)]
        cu.cuMemFree_v2(fb)

        nsmid = "-"
        try:
            _, nsp = load(PTX_NSMID, "nsmid_probe")
            fb = alloc(4)
            launch(nsp, 1, 32, [fb])
            nsmid = int.from_bytes(read_back(fb, 4), "little")
            cu.cuMemFree_v2(fb)
        except RuntimeError as e:
            print("  note: %s" % e)

        tf = "-"
        _, fma = load(PTX_FMA, "fma_bench")
        fb = alloc(4)
        it = ctypes.c_uint(FMA_ITERS // 10)
        best = None
        for _ in range(4):
            t0 = time.perf_counter()
            launch(fma, nsm * 8, 256, [fb, it])
            dt = time.perf_counter() - t0
            best = dt if best is None or dt < best else best
        cu.cuMemFree_v2(fb)
        tf = "%.2f" % (nsm * 8 * 256 * FMA_CHAINS * it.value * 2.0 / best / 1e12)

        _, ver = load(PTX_VERIFY, "verify_kernel")
        threads, blocks = 256, nsm * 8
        n = threads * blocks
        fb = alloc(4 * n)
        nn = ctypes.c_uint(n)
        launch(ver, blocks, threads, [fb, nn])
        raw = read_back(fb, 4 * n)
        bad = sum(1 for g in range(n)
                  if int.from_bytes(raw[g * 4:g * 4 + 4], "little") != word_hash(g))
        cu.cuMemFree_v2(fb)
        verdict = "OK" if bad == 0 else "%d BAD of %d" % (bad, n)

        print("%-11s %-8s %-8s %-8s %-6s %-6s %-9s %-9s %-8s %s" % (
            b.value.decode().replace("0000:", ""), nsm, nsmid, len(set(smids)),
            min(smids), max(smids), tf, tot.value >> 20, n, verdict))
        holes = sorted(set(range(max(smids) + 1)) - set(smids))
        if holes:
            print("            SM ids never scheduled in 0..%d: %s  <-- enumerated but dead"
                  % (max(smids), holes))
        print("            l2=%d KiB bus=%d bit threads/SM=%d regs/block=%d sm=%d MHz "
              "mem=%d MHz" % (
                  attr(d, CU_DEVICE_ATTRIBUTE_L2_CACHE_SIZE) >> 10,
                  attr(d, CU_DEVICE_ATTRIBUTE_GLOBAL_MEMORY_BUS_WIDTH),
                  attr(d, CU_DEVICE_ATTRIBUTE_MAX_THREADS_PER_MULTIPROCESSOR),
                  attr(d, CU_DEVICE_ATTRIBUTE_REGISTERS_PER_BLOCK),
                  attr(d, CU_DEVICE_ATTRIBUTE_CLOCK_RATE) // 1000,
                  attr(d, CU_DEVICE_ATTRIBUTE_MEMORY_CLOCK_RATE) // 1000))
        cu.cuCtxDestroy(ctx)


def cmd_load(args):
    dev, bus_id = open_device(args.bus)
    nsm = attr(dev, CU_DEVICE_ATTRIBUTE_MULTIPROCESSOR_COUNT)
    clk = attr(dev, CU_DEVICE_ATTRIBUTE_CLOCK_RATE)
    ctx = context(dev)
    _, fma = load(PTX_FMA, "fma_bench")
    fb = alloc(4)
    threads, launches = 256, 30
    grid = nsm * 8
    it = ctypes.c_uint(FMA_ITERS)
    t0 = time.perf_counter()
    for _ in range(launches):
        launch(fma, grid, threads, [fb, it])
    dt = time.perf_counter() - t0
    flops = grid * threads * FMA_CHAINS * it.value * 2.0
    print("%s SM=%d  sustained FP32 %.2f TFLOPS over %.2f s  (%.1f GFLOPS per SM)" % (
        bus_id, nsm, flops * launches / dt / 1e12, dt, flops * launches / nsm / dt / 1e9))
    print("  peak if every SM issues 128 FP32 FMA/clk at %d MHz: %.2f TFLOPS" % (
        clk // 1000, nsm * 128 * 2 * clk * 1000 / 1e12))
    print("  compare two cards: the ratio of TFLOPS must equal the ratio of SMs,")
    print("  and GFLOPS per SM must be the same on both, or an SM is enumerated but dead.")
    cu.cuMemFree_v2(fb)
    cu.cuCtxDestroy(ctx)


def cmd_fb(args):
    dev, bus_id = open_device(args.bus)
    nsm = attr(dev, CU_DEVICE_ATTRIBUTE_MULTIPROCESSOR_COUNT)
    ctx = context(dev)
    size = None
    for gib in [args.gib] + [g for g in (80, 79, 78, 76, 72, 63, 62, 60, 40, 8)
                            if args.gib is None or g < args.gib]:
        p = ctypes.c_void_p()
        if cu.cuMemAlloc_v2(ctypes.byref(p), ctypes.c_size_t(gib * GB)) == 0:
            size, fb = gib * GB, p
            break
    if size is None:
        raise SystemExit("could not allocate any large buffer on %s" % bus_id)
    nwords = size // 4
    print("%s SM=%d  buffer=%.1f GiB (%d words), grid %dx256" %
          (bus_id, nsm, size / GB, nwords, nsm * 8))

    _, fill = load(PTX_FILL, "fill_kernel")
    _, check = load(PTX_CHECK, "check_kernel")
    nw = ctypes.c_ulonglong(nwords)
    ctr = alloc(16, (ctypes.c_ulonglong * 2)(0, 0xFFFFFFFFFFFFFFFF))
    grid = nsm * 8

    def reset_ctr():
        ck(cu.cuMemcpyHtoD_v2(ctr, (ctypes.c_ulonglong * 2)(0, 0xFFFFFFFFFFFFFFFF), 16),
           "reset counter")

    def run(fn, with_ctr):
        if with_ctr:
            launch(fn, grid, 256, [fb, nw, ctr])
        else:
            launch(fn, grid, 256, [fb, nw])

    t0 = time.perf_counter()
    run(fill, False)
    t_fill = time.perf_counter() - t0
    t0 = time.perf_counter()
    run(check, True)
    t_check = time.perf_counter() - t0
    res = (ctypes.c_ulonglong * 2)()
    ck(cu.cuMemcpyDtoH_v2(res, ctr, 16), "read counter")
    print("  fill  %.2f s -> %.0f GB/s" % (t_fill, size / t_fill / 1e9))
    print("  check %.2f s -> %.0f GB/s   %d of %d words mismatched" %
          (t_check, size / t_check / 1e9, res[0], nwords))
    if res[0]:
        print("  RESULT: FAIL, first bad word at offset 0x%x" % (res[1] * 4))
    else:
        print("  RESULT: every byte of the %.1f GiB was written and compared on the device"
              % (size / GB))

    # a checker that never fires proves nothing: break 4 KiB on purpose
    dirty = size - 4096
    ck(cu.cuMemsetD8_v2(ctypes.c_void_p(fb.value + dirty), ctypes.c_ubyte(0),
                       ctypes.c_size_t(4096)), "inject corruption")
    reset_ctr()
    run(check, True)
    ck(cu.cuMemcpyDtoH_v2(res, ctr, 16), "read counter")
    live = res[0] == 1024 and res[1] * 4 == dirty
    print("  injected 4 KiB at 0x%x -> %d bad words, first at 0x%x: checker %s" %
          (dirty, res[0], res[1] * 4, "live" if live else "SUSPECT"))
    cu.cuMemFree_v2(ctr)
    cu.cuMemFree_v2(fb)
    cu.cuCtxDestroy(ctx)
    return 0 if res[0] == 0 else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=("sm", "load", "fb"))
    ap.add_argument("--bus", default=None, help="BDF fragment, e.g. 09:00.0 (default: sm=all)")
    ap.add_argument("--gib", type=int, default=None, help="buffer size for fb mode")
    args = ap.parse_args()
    if args.mode == "sm":
        cmd_sm(args)
    elif args.mode == "load":
        cmd_load(args)
    else:
        return cmd_fb(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
