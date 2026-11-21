# CMP 170HX 台架熔丝实测与 80 GiB 配方（2026-11-17 已验证）

状态：**09 卡 80 GiB 端到端通过**（`Total 81920 / Reserved 726 MiB / Free 81194 MiB`，
单次 78 GiB 分配写满并读回干净，Xid 0）。05/06 两张保持 `65536 / 626 / 64911`，
其间 BAR1 P2P（`cuCtxEnablePeerAccess` + 64 MiB 对端拷贝）仍正常。

验证用的产物：`nvidia.ko` md5 `df6109d8e75df9f5bbd624be0622ad44`，
`cmp-dynamic.patch` md5 `fb4a100e06cad606ee659207de203424`（同一策略，现为 `driver/dynfb/cmp_dynamic.h`），内核 `6.18.18.c1107-trim`，
驱动 610.57.04。完整日志与补丁存于台架 `/root/evidence-80g/`。

## 熔丝实测（只读采集）

`ffprobe.ko` 读 BAR0 + 熔丝控制器 CMD_READ：

| BDF | UUID | SKU | `fbp_disable` | `fbp_defective` | 可用 FBP | 能力 |
|---|---|---|---|---|---|---|
| 05:00.0 | 38bbb4b3 | 8gb | `0x852` | `0x840` | 8/12 | 64 GiB（可烧到 80） |
| 06:00.0 | b6b9f653 | 8gb | `0x2D` | `0x2D` | 8/12 | 64 GiB（物理封顶） |
| 09:00.0 | 85b20be7 | 8gb | **`0x840`** | `0x840` | **10/12** | **80 GiB（已烧好）** |

- 8 GB SKU = 每 FBP 8 GiB，die 共 12 个；`0x852 → 0x840` 清掉 bits 1/4 得 10 个 = 80 GiB。
- 09 烧后 `fbpa_disable = fbio_disable = rop_l2_disable =` 各自 `_defective`，
  `status_fb_config = status_half_fbpa = status_half_ltc = 0` ⇒ 这 10 个 FBP 是**合法**的，
  不存在"块没放开"。
- 09 同时清了 PCIe 组（`disable_gen3_speed_bit1/9 = 0`，驱动侧 `CAP2 = 0x0180001e`）。

## 寄存器语义（本仓库 `common/constants.yaml`）

`0x00100ce0` = `NV_PFB_PRI_MMU_LOCAL_MEMORY_RANGE`（下称 LMR，硬件自报显存总量）：

| 字段 | 含义 |
|---|---|
| `[3:0]` | `LOWER_SCALE` |
| `[9:4]` | `LOWER_MAG`，总量 = `mag << (scale + 20)` |
| `[8:7]` | FBP 分区数编码：`0→8`、`1→10`、`2→12`、`3→14` |

| 值 | 解码 | 出现的卡 |
|---|---|---|
| `0x00000208` | 8 FBP × 1 GiB = 8 GiB | 未烧卡的原生值（`_kgspCmpDynGeomEval` 打印的 native） |
| `0x00000288` | 10 FBP × 1 GiB | 09 烧完熔丝、驱动未介入时 |
| `0x0000020b` | 8 FBP × 8 GiB = 64 GiB | 64 GiB 软解锁（05/06） |
| `0x0000028b` | 10 FBP × 8 GiB = 80 GiB | **80 GiB，正是 A100-80G 的原生字** |

`0x009a0204` = FBPA `CFG1`，`[23:16]` 是两个半字节（每 FBP 大小）：`0x44`=1 GiB、
`0x66`=4 GiB、`0x77`=8 GiB。80 GiB 用 `0x02779000`，64 GiB 用 `0x02449000`。

死路一条，别再试：`0x001183a4`（`NV_USABLE_FB_SIZE_IN_MB`）在这个时点读回
`0xbadf5040`（`NV_PPRIV_SYS_PRI_ERROR_CODE_FECS_PRI_CLIENT_ERR`），FECS 未放行；
整个 MMU 块线性扫（`0x00100c00..0x00100cfc`、`0x009a0100..`、`0x001fa7c0..`）在
64 GiB 与 80 GiB 两种启动之间**没有任何 64 GiB 上限字段**。也就是说 64 GiB 这道墙
从来不在硬件里。

## 80 GiB 最终配方（09 卡实测通过）

| # | 位置 | 动作 |
|---|---|---|
| 1 | `_kgspBootGspRm` 内、**第一次** `kgspPopulateWprMeta_HAL` 之前 | 写 `CFG1=0x02779000`、`LMR=0x0000028b` |
| 2 | `_kgspInitGpuProperties` static-info | `fb_length = 0x1400000000`（region 表**不要手改**，GSP 会按 LMR 自己生成） |
| 3 | `_kgspPrepareRegistry` 内第二次 `osPackageRegistry` 之后 | 把送给 GSP-RM 的 `RMForceStaticBar1` 由 `1(ENABLE)` 改成 `2(AUTO)` |
| 4 | `kern_bus_tu102.c` 强制静态 BAR1 分支 | FB 装不进 BAR1 时对 CMP 返回 `NV_ERR_NOT_SUPPORTED` 而非 `NV_ERR_INVALID_REGISTRY_KEY` |

判据（09 卡本轮日志）：

```
CMP-BAR1 GPU2 pre-init CFG1=0x02449000 LMR=0x00000288 burned=1 stage=0
CMP-BAR1 GPU2: GSP registry entry #39 RMForceStaticBar1=1 -> 2 (AUTO), ...
CMP-BAR1 GPU2: packed 0x698 bytes / 46 registry entries for GSP-RM, 1 forced-static-BAR1 entry rewritten
CMP-DYN GEOM GPU2 EARLY stage=0 want CFG1=0x02779000 LMR=0x0000028b readback CFG1=0x02779000 LMR=0x0000028b
CMP-DYN WPR: populate fbSize=0x1400000000 wprStart=0x13f7100000 wprEnd=0x13fff00000 heapSize=0x7000000
kgspBootstrap_TU102: SEC2_DEBUG: normal BooterLoad status=0x0
_kgspInitGpuProperties: CMP-DYN FB: burned card -> fb_length 0x1400000000 (table 0x1000000000, 10 FBPA x 8 GiB)
_kgspInitGpuProperties: SEC2_DEBUG: static-info AFTER: fb_length=0x1400000000 last_limit=0x13ffffffff
RmInitAdapter: SEC2_DEBUG: late PMA extension status=0x0
```

`Reserved = fbLength - wprStart`（这条等式两边逐位吻合：81920 − 64909 = 17011 是旧值），
所以 WPR 必须坐在 FB 顶部才有几百 MiB 的保留量 —— 这也是配方 1 必须提前的原因。

## 更正一：LMR 可以改，之前失败纯粹是"写入时机"

先前结论"不能把 LMR 写成 `0x28b`，否则 SEC2 booter 够不到高位 WPR（`0x65` 超时）"
**是错的**。真正的变量是时机：几何写在第一次 `kgspPopulateWprMeta_HAL` 之后，booter
用的仍是旧布局，于是 WPR 与 booter 认知不一致 → `kflcnWaitForHalt_TU102` 超时。
把同一组值提前到第一次 populate 之前写入，booter 在 79.85 GiB 顶部的 WPR 上
`status=0x0` 一次通过。这正是原作者说的 "small modification"。

自愈阶梯现在只在 `NV_ERR_TIMEOUT`（`0x65`，falcon 超时）时退回几何；其他状态码
保持不动——booter 过了以后再改 CFG1/LMR，卡会整个会话起不来（实测过）。

## 更正二：region 表不是"保持原样"，而是"别手改"

配方 2 生效（LMR 提前写对）后，GSP 自己就生成了覆盖 80 GiB 的划分，无需也不应手工拉伸：

```
REGION[0] 0x0        - 0x140fffff     reserved=0x14100000 perf=0
REGION[1] 0x14100000 - 0x13f400ffff   reserved=0x0         perf=20
REGION[2] 0x13f4010000-0x13f6f0ffff   reserved=0x2f00000   perf=20
REGION[3] 0x13f6f10000-0x13f6ffffff   reserved=0xf0000     perf=0
REGION[4] 0x13f7000000-0x13ffffffff   reserved=0x9000000   perf=0
```

旧的"凭空造 16 GiB 尾部区 → GSP 拒表"是被 64 GiB 的 `perf=16` 划分绑住时的现象；
现在连 `fbAddrSpaceSizeMb >= fbTotalMemSizeMb @ mem_mgr_gsp_client.c:122` 那条断言
也不再出现（地址空间与容量一致了）。

## 真正的墙：BAR1 与 `RMForceStaticBar1`

`NV_ERR_INVALID_REGISTRY_KEY`(0x3E) 在全树只有两个产出点，都在
`kern_bus_tu102.c: kbusIsStaticBar1Supported_TU102`：

- `case FORCE_STATIC_BAR1_ENABLE`：`BAR1(对齐后) < 客户端可见 FB` → `return NV_ERR_INVALID_REGISTRY_KEY`；
- `default:`：键值不在 0/1/2 内 → 同上。

`AUTO`（键缺失时的默认）走另一条路：装不下只是 `NV_ERR_NOT_SUPPORTED`，
调用方（`kern_bus_gh100.c` / `kern_bus_gm107.c`）把静态 BAR1 关掉继续启动。

而这台机器的 `RMForceStaticBar1=1` 是**本项目构建脚本**为了 BAR1 P2P 写进去的
（`driver/build.sh` P2P 分支：`RMForceStaticBar1=1;RMPcieP2PType=1`）。所以：

| 现象 | 根因 |
|---|---|
| 旧结果 `81920 / reserved 17011` 能起来 | WPR 在 63.4 GiB ⇒ 可见 FB ≈63.4 GiB 刚好塞进 64 GiB BAR1 |
| WPR 移到顶部后 `RmInit` 回 `0x3E` | 可见 FB 79.2 GiB > 64 GiB BAR1，强制静态 BAR1 判死 |

三张卡 BAR1 都是 64 GiB（rebar 上限，`CAP=0x001ffc00`），FB 80 GiB 时
"BAR1 ≥ FB" 不可能成立 ⇒ 09 只能放弃静态 BAR1（等价于原生 A100-80G 的行为）。

### 两侧都要处理

1. **kernel-RM 侧**：源码在树里，直接在 `FORCE_STATIC_BAR1_ENABLE` 分支里对 CMP
   （device `0x20C2`/`0x2082`）把致命返回降级成 `NOT_SUPPORTED`。
   日志：`CMP-BAR1 GPU2: FB 0x13e6a00000 will not fit in BAR1 0xfffe00000, skipping forced static BAR1`。
2. **GSP-RM 固件侧**：`gsp_ga100.bin` 不是从这棵树编的，改它的源码没有用；
   固件读的键只来自 `_kgspPrepareRegistry` 打包的 `PACKED_REGISTRY_TABLE`。

表结构（`g_os_nvoc.h`，由 `kernel-open` 的 `RmPackageRegistry()` 填充，逐 GPU 一份）：

```c
typedef struct PACKED_REGISTRY_ENTRY { NvU32 nameOffset; NvU8 type; NvU32 data; NvU32 length; } PACKED_REGISTRY_ENTRY;
typedef struct PACKED_REGISTRY_TABLE { NvU32 size; NvU32 numEntries; PACKED_REGISTRY_ENTRY entries[0]; } PACKED_REGISTRY_TABLE;
```

DWORD 键的 `data` 就是值本身，`nameOffset` 指向表尾 blob 里的 NUL 结尾键名。
所以在第二次 `osPackageRegistry()` 之后按名字把 `RMForceStaticBar1` 改成 `2` 即可，
只影响被烧卡（判据是 LMR 的额外 FBP 位 + 当前 stage 为 80 GiB），05/06 完全不受影响。

**踩过的坑**：`_kgspPrepareRegistry` 由 `kgspInitRm_IMPL` 调用，位置早于
`_kgspBootGspRm`（几何写入点）。若钩子的门槛条件依赖 `bBurned` 这种"要到 boot 路径里
才被填上"的缓存状态，第一次（也是唯一有用的一次）尝试就会被静默跳过 —— 表已经发出去了，
再改第二次重试时卡已被 `0x3E` 打瘫。钩子必须自己调用一次幂等的 eval，并把原始
`CFG1/LMR` 一起打印出来。

`RegistryDwordsPerDevice="pci=DDDD:BB:DD.F;..."` 也是可用的逐设备入口（`nvp->pRegistry`
会一起打包），但 05/06/09 三张卡 device id 相同，只有 BDF 可分，故本项目选择了代码内改写。

## `bBurned` 的判据本身有竞态（2026-11-21 观测）

`_kgspCmpDynGeomEval()` 只在 `bBurned == 0` 时抓一次快照，而它读的是 `CFG1`/`LMR` ——
这两个恰恰是**我们自己会写**的活寄存器，`_kgspCmpDynIsBurned()` 的判据又只是"当前 LMR
的 FBP 数是否大于 8"。于是这个位取决于钩子落在几何写之前还是之后，而不是取决于熔丝：

| 快照时机 | 读到的 LMR | 判定 |
| --- | --- | --- |
| 几何写之前 | 熔丝原值（05/06 出厂即 `0x00000288`，10 分区） | `burned=1`，描述的是卡 |
| 80 GiB stage 写之后 | `0x00000288`（我们的宽几何，与熔丝值同号） | `burned=1`，结论对，但 `CMP-DYN FUSES` 打印的是我们写的值 |
| 64 GiB stage 写之后 | `0x0000020b`（我们的合成 8 分区） | `burned=0`，一张确有富余分区的卡被认成无解 |

实测：2026-11-21 一次不带 `CMPEnable80G=1` 的启动（stage=64g），同一批卡、同一份熔丝，
GPU0 报 `burned=1`，GPU1 报 `LMR=0x0000020b burned=0` —— 差别只在两张卡各自第一次
eval 的时机。那一次 stage 本来就是 64g，`burned` 没参与决策，症状只是 dmesg 把我们的
几何当成熔丝内容报了出来；隐患在别处：

- `_kgspCmpDynNoteFailure()` 在 `bBurned == 0` 时直接返回，也就是说 **booter 超时后的
  自动降级台阶是以这个可能出错的位为前提的**：被判成未烧写的卡没有退路，只有那一次
  geometry 可试。当天卡死的那次是 booter `0xffff`，不是 falcon 超时，本来就不走台阶，
  所以这不是当天的病因，而是这条安全网的可信度问题。
- `_kgspCmpDynAdjustRegistry()` 把静态 BAR1 降级也挂在同一位上（外加 `stage == 80G`），
  判错时该降的没降，退化为 GSP-RM 自己报的硬失败。

要修得把判据与活寄存器解耦：从熔丝控制器的 `CMD_READ` 取真值（本文"只读采集"那节用的
`ffprobe` 已经证明读得到 `0x00000288`，不依赖 BAR0 几何），或把快照钉在一个保证早于
任何几何写的位置，并在日志里标明这个值究竟来自熔丝还是寄存器回读。

## 运行时开关（无需重编即可回退）

`build.sh` 在 `CMPUNLOCKER_EXPERIMENTAL_80G=1` 时往 `RegistryDwords` 里加 `CMPEnable80G=1`：

```
options nvidia NVreg_EnableResizableBar=1 \
  NVreg_RegistryDwords="...;CMPEnable80G=1;RMForceStaticBar1=1;RMPcieP2PType=1"
```

驱动里 `_kgspCmpDynGeomEval()` 读不到这个键就直接把 stage 定为 `CMP_DYN_STAGE_64G`
（即已验证的 64 GiB 布局，`CFG1=0x02779000 LMR=0x0000020b`，静态 BAR1 与 P2P 保留）。
删掉该键 + `update-initramfs -u` + 重启即可回到 64 GiB，不必重新编译驱动。

## 验收

```
$ nvidia-smi --query-gpu=index,pci.bus_id,memory.total,memory.reserved,memory.free --format=csv,noheader
0, 00000000:05:00.0, 65536 MiB, 626 MiB, 64911 MiB
1, 00000000:06:00.0, 65536 MiB, 626 MiB, 64911 MiB
2, 00000000:09:00.0, 81920 MiB, 726 MiB, 81194 MiB
```

- 单次 `cuMemAlloc(78 GiB)` 成功，`free` 78.88 → 0.88 GiB；整块 `cuMemsetD8(0xA5)` 后
  在 `0x0`、`0x9c0000000`、`0x137ff00000` 三点各读回 1 MiB 全对；顶部 2 GiB 改写 `0x5A`
  后三点读回全对 ⇒ 新放开的高位 16 GiB（烧进来的那对 FBP）读写无误。
- 再要 1 GiB 干净地返回 `CUDA_ERROR_OUT_OF_MEMORY`(2)，释放后 `free` 回到 78.88 GiB。
- ECC `Enabled`，volatile/aggregate 全部 0，`Remapped Rows: N/A`，整个 boot 无 Xid。
- `cuCtxEnablePeerAccess(05 -> 06)` + 64 MiB 对端拷贝校验通过（P2P 未退化）。

## SM 与算力（09 随熔丝一起解到 108）

`nvidia-smi -q -d compute` 在这个驱动上只打印 `Compute Mode`，看不到 SM 数，所以
`tools/sm-probe.py` 用 CUDA driver API + 手写 PTX（机器上没有 nvcc 也能跑）从四个互相
独立的角度交叉验证：

| 卡 | 解锁路径 | SM（CUDA 属性） | `%nsmid`（芯片自报） | 实际被调度的不同 SM | SM 编号 | L2 | 位宽 | 持续 FP32 | 每 SM |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 09 | FF 熔丝 + 本项目 80 GiB | 108 | 108 | 108 | 0–107 连续 | 40 MiB | 5120 | 18.94 TFLOPS | 175.4 GFLOPS |
| 06 | 纯软解 64 GiB | 74 | 74 | 74 | 0–73 连续 | 32 MiB | 4096 | 12.91 | 174.4 |
| 05 | 纯软解 64 GiB | 74 | 74 | 74 | 0–73 连续 | 32 MiB | 4096 | 13.44 | 181.6 |

- 探针是 `grid = SM × 8`、256 线程的自旋 kernel，每个 block 回报自己所在的 `%smid`。
  09 上 864 个 block 落在 108 个不同编号上、`0..107` 无空洞 ⇒ 驱动不是照着熔丝表虚报，
  那 34 个额外 SM 是真在干活的硅片。
- 吞吐比值 `18.94 / 12.91 = 1.47`，SM 比值 `108 / 74 = 1.46`；每 SM 吞吐三张卡一致（±2%）；
  持续 8 秒满载期间 SM 时钟稳在 1410 MHz、`clocks_event_reasons.active=0x0`（不降频），
  09 功耗 140 W 对 06 的 107 W —— 多出的 SM 按约 1 W/SM 线性增加功耗。
- 逐线程确定性计算：09 上 221 184 个线程全部算对（`sm` 子命令 `check=OK`）。
- 108 SM / 6912 CUDA / 40 MiB L2 / 5120-bit 正是 A100 PCIe 80 GB 的那一组值，与 09 的
  原生 `LMR=0x00000288`（=10240 MiB，10 GB 版熔丝）、`CAP2=0x0180001e` 同源；05/06 原生
  `LMR=0x00000208`（=8192 MiB）是真正的 8 GB 版，社区结论就是软解天花板 74 SM / 64 GiB。
  **SM 的差别来自熔丝而不是我们的驱动**：驱动只碰 CFG1/LMR、BAR1 与注册表打包表，从不碰
  GPC 掩码，两张纯软解卡停在 74 SM 就是现成的对照组。

### 全量显存完整性（把抽样升级成逐字节）

`sm-probe.py fb` 用"由下标决定的图案"铺满整块分配，再在设备端逐 word 重算比对，最后
故意写坏 4 KiB 确认检查器真的会报（防"永远通过"的检查）：

- 09：78.0 GiB = 20 937 965 568 个 word，**0 处不符**；写 1260 GB/s、读 967 GB/s；
  注入的 4 KiB 精确报回 1024 个坏 word、首地址 `0x137ffff000` ⇒ 10 个 FBP 全部真实可用。
- 05/06：各 63.0 GiB 同样 0 不符 ⇒ 09 的改动没有牵连两张 64 GiB 卡。

## 已知代价与后续

- 09 卡失去静态 BAR1，因而失去 BAR1 P2P；BAR1 硬件上限 64 GiB < FB 80 GiB，
  这条路无法靠"把 BAR1 抬到 128 GiB"解决（`p2p/0011-p2p-bar1.patch` 的 64 GB 上限只对
  FB ≤ 64 GiB 的卡有意义）。05/06 的 P2P 不受影响。
- `_kgspCmpDynDump/_kgspCmpDynScan` 的 PRE-GEOM/PRE-BOOTER 线性扫与 `CMP-REG` 全表打印是
  诊断件，稳定后应删（会产生 `badf5040` 坏读告警与上千行日志）。
- 自愈阶梯保留：万一某批卡 booter 真够不到高位 WPR，会退回 64 GiB 而不是整机不可用。

## 复测命令

```sh
# SM / 算力 / 显存完整性（在装驱动的机器上直接跑，不需要 CUDA toolkit）
python3 tools/sm-probe.py sm                      # 三张卡的 SM 数、%nsmid、实际调度编号、逐线程计算
python3 tools/sm-probe.py load --bus 09:00.0      # 持续 FP32，用来核对"吞吐比 == SM 比"
python3 tools/sm-probe.py fb --bus 09:00.0 --gib 78   # 全量写+设备端逐 word 校验 + 注入坏块自检

# 熔丝侧读数
cd /root/ffcalc
sudo ./scripts/ffscan-nosmi.sh collected-live 1
python3 -m ffcalc report cards/<uuid>.json
```

## 教训

- **PCIe 能力 ≠ 显存容量**。`CAP2` 只能证明 PCIe 那组熔丝被清过；80 GiB 要求
  `fbp_disable == 0x840`。两者必须分开确认。
- **驱动改不了被熔丝关掉的 FBP**。熔丝没清时任何 CFG1/LMR/fb_length 组合都只会崩。
- **"某个值不能写"往往只是"写得太晚"**。同一个 `LMR=0x28b`，晚写让 booter 超时，
  提前写一次通过。凡是涉及 WPR/布局的寄存器，写点必须早于第一次 `populate`。
- **软解锁的墙常在策略层**。64 GiB 不是硬件上限，也不是 MMU 里某个字段，而是我们自己
  为了 P2P 打开的 `RMForceStaticBar1=1`；先找返回码的产生点，再决定改哪一侧。
- **闭源 GSP 固件只能用数据面影响**：注册表打包表（以及 `RegistryDwords*`）是唯一杠杆。
