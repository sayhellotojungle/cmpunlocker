# P2P 合并说明

以 `cmpunlocker-0.4` 为基础，已融合附件中 `cmpunlocker-p2p` 的 `--p2p`
参数及其驱动实现。本仓库根目录即合并后的主项目。

P2P 现在是两条路线而不是一个开关：`--p2p=bar1` 是上面那次合并进来的 BAR1 路径
（等价于旧写法 `--p2p`、`CMPUNLOCKER_ENABLE_P2P=1`），`--p2p=mailbox` 走
`NV_P2P` 写邮箱孔径，不需要大 BAR1，是给 80 GiB 布局那张卡留的路。一个构建只带其中
一条：RM 每对 peer 只承载一种 PCIe P2P 协议（`kbusIsPcieBar1P2PMappingSupported_GH100`
的第 4 道门会因 peer 已被另一条占用而拒绝），混着打补丁只会得到先到那条，另一条被
静默拒绝，因此 `tests/test_patches.py` 直接断言两个补丁清单不相交。

## 使用

在项目根目录运行：

```bash
sudo ./install.sh --p2p=bar1      # 同 --p2p
sudo ./install.sh --p2p=mailbox
```

可以与原有 `--profile=8gb|10gb`、`--no-iommu`、`--no-passthrough` 一起使用
（`--no-gen2-service` 仍被接受，但已无意义：开机不再有重训服务）。不传 `--p2p` 时，
全部可选 P2P 驱动补丁关闭。安装完成后需彻底关机再开机。

直接构建也支持：

```bash
sudo env CMPUNLOCKER_ENABLE_P2P=mailbox ./driver/build.sh
```

取值是 `off|bar1|mailbox`（`0`、空、`none` 等同 `off`；`1` 仍等同 `bar1`），
其他值在动手构建之前就被拒绝。重新运行不带 `--p2p` 的安装命令可关闭 P2P，随后冷启动。
升级内核后，仍按 `0.4` 的方式手动重新安装；需要 P2P 时务必继续传入同一个 `--p2p=` 选择。

## 两种写法的分工

| | `--p2p=bar1` | `--p2p=mailbox` |
| --- | --- | --- |
| 搬运方式 | 静态 BAR1 映射整块显存，拷贝引擎直接 DMA 进 peer | 逐块写 peer 的 `NV_P2P` 写邮箱窗口 |
| 需要的 BAR1 | ≥ 显存容量（本代卡能力表上限 64 GiB） | 每 peer 64 KiB，出厂尺寸够用 |
| 80 GiB 布局 | 不能参与（见 [P2P.md](P2P.md) "BAR1 size and the 80 GiB layout"） | 唯一可选路线 |
| registry | `RMForceStaticBar1=1;RMPcieP2PType=1` | 只用自有键 `CMPEnableP2PMailbox=1`；`RMPcieP2PType` 保持默认 MAILBOX，static BAR1 保持 AUTO |
| 卡侧前提 | resizable-BAR 能力表 | PRI 译码陷阱 31 的权限掩码 `0x0012277c` 能开成全 1 |
| 实测状态 | 已实测：2/2 有向配对，每端 16 MiB，两端回读校验，无 Xid | 已实测：80 GiB 布局的两张卡，2/2 有向配对 20 项内容校验全过（65535 B 到 32 MiB），Gen4 x16 计时拷贝 64 MiB 到 4 GiB 逐字节校验通过，单向约 20~25 GB/s，事后无 Xid。fork 的 6.65 GB/s 是 Gen2 链路下的数字 |
| 不支持时 | BAR1 抬不到需要尺寸就不工作，BAR 停在出厂值 | 由 `hooks/p2p_caps_mailbox.c` 守卫：掩码不是全 1 就保留 GSP 原判定，报 not supported |

`ForceP2P` 两条路都不设。RM 自己的该覆盖会把能力判定直接短路成 OK，正好绕掉 mailbox
唯一那道知道卡不行的守卫；fork 那份带 `ForceP2P=0x11` 的配置在 g250 上的实测结果是
32 MiB 的 `cuMemcpyPeer` 返回成功而 33554432 字节里错 33423360（只有头 128 KiB 对）。

## 实现范围

| 内容 | 合并方式 |
| --- | --- |
| GSP 的 P2P 读写能力覆盖 | 将 `cmpUnlockForceP2PCaps()` 的逻辑转成 `0.4` 原生补丁，保留两个 CMP 设备 ID 的判断。 |
| BAR1 P2P 完整路径 | 迁入 HAL 选择、映射创建/删除、BAR1 总线地址传递、PTE/物理地址改写、UVM 和 IOVAS 相关改动。 |
| mailbox 预注册修正 | BAR1 模式下跳过 mailbox peer 预注册，避免已有 peer mask 阻断 BAR1。 |
| mailbox P2P 路线 | 新增 `0020-mailbox-trap31.patch`（在两个 peer 邮箱建立调用前后武装/恢复 PRI 译码陷阱 31，覆盖 `NV_P2P` 块，仅 CMP 设备 ID）与 `0021-mailbox-trap31-plm.patch`（把该陷阱的权限掩码 `0x0012277c=0xffffffff` 加进我们自己的 `sec2-postbl-plm-ss-cfg.patch` 的 `plmTable`，12 行变 13 行，并按 `CMPEnableP2PMailbox` 跳过），加 `hooks/p2p_caps_mailbox.c`。寄存器值来自 fork 的实测（commit `14b38f72dbe0ee03dfd9377880a0c83302f703d3`，陷阱思路出自 asm64-hooligan），无任何已发布头文件依据。 |
| 不支持就报不支持 | mailbox 的 caps 钩子先读 `0x0012277c`：不是全 1 就不改 GSP 的判定，让该 peer 对显示 not supported。这条守卫的来源是 fork 无守卫版本在 g250 上的实测：32 MiB 拷贝返回成功而只有头 128 KiB 是对的。 |
| 主机读能力修正 | 迁入 CMP 读能力覆盖，并去掉原补丁对已禁用 `0014` 调试补丁的上下文依赖。 |
| BAR1 resize | 保留 `0.4` 已有的等效实现，不重复应用同一功能的补丁。 |
| 安装配置 | 将 `RmForceEnableGen2=1` 与所选路线的键写入同一条 `NVreg_RegistryDwords`，在生成 initramfs 前完成：bar1 加 `RMForceStaticBar1=1;RMPcieP2PType=1`，mailbox 只加 `CMPEnableP2PMailbox=1`。 |
| 按卡动态解锁 | `driver/dynfb/`：`cmp_dynamic.h` 是策略本体，`apply.py` 在打完补丁后按**锚点**把钩子植入驱动（不用上下文窗口；锚点大多是我们自己补丁里的代码，因此与驱动版本无关）。判据是卡自己的 `LMR` 分区数与 `CFG1` 分区大小，只有 `0x20C2` 且分区数超过 8 的卡吃 80GiB（PCIe 能力不作判据）；默认关闭（`CMPUNLOCKER_EXPERIMENTAL_80G=0`）；只有 booter 超时才逐级退回并写入 registry。无条件 Gen2 初始化保留；Gen4 走 Gen3→E1→Gen4（`dynfb/hooks/nv_gen4.c`），目标代际读每张卡自己的 `LNKCAP2` 熔丝，软解卡自动留在 Gen2；没到目标时按 `LnkSta` 实际训练到的代际收口（桥只到 Gen3 就留在 Gen3），连 Gen3 都没站住才调 Gen2 重训收回；开关是 `CMPUNLOCKER_EXPERIMENTAL_GEN4=1`。每个决定打印 `CMP-DYN` 内核日志，详见 `BOOT-RECOVERY.zh-CN.md`。 |
| 关闭与重建 | 所选路线名进入缓存标记；在 `off`/`bar1`/`mailbox` 之间切换会重新解压驱动源码。关闭时同时清除配置中该路线的 P2P 参数。 |
| 验证 | 记录 `p2p_enabled`（值为路线名），增加能力矩阵提示、真实 CUDA 读写校验程序、逐字节内容校验程序 `tools/p2p-content-check.py` 和离线回归测试。 |

五个 P2P 驱动补丁位于 `driver/patches/p2p/`（bar1 用 `0011/0013/0015`，mailbox 用
`0020/0021`，两个清单不相交），另外四处改动改由 `dynfb/apply.py`
按锚点植入（`hooks/p2p_caps.c`、`hooks/p2p_caps_mailbox.c`、
`hooks/p2p_resizable_bar.c`、`hooks/p2p_bar1dma.c`）。两个 caps 钩子共用
`gpu.c` 里同一个锚点，因此 `apply.py` 对“两个钩子要同一处”直接判失败；
`--p2p=` 决定放哪一组。没有整体替换 `0.4`
的解锁实现，也没有迁入与 P2P 无关的超频、时序调整和软件包持久化系统。
原有 8GB/10GB/混合显存识别、Gen2、直通实现继续保留。

## 目标主机验证

`bar1` 路线要求每张参与的 GPU 有足够大的 BAR1，且主机 PCIe 路径必须支持真实
peer 读写。源项目使用每卡 64 GiB BAR1。BAR1 扩容由驱动在加载时通过 REBAR
完成，前提是 BIOS 开启 Above-4G Decoding 并提供足够大的 MMIO High 地址
空间；BIOS 不分配时驱动无法凭空获得地址空间，BAR1 保持出厂尺寸，该路线不可用。
`mailbox` 路线不需要大 BAR1（每 peer 64 KiB，出厂尺寸够用），但它要求卡侧那个 PRI
陷阱的权限掩码打得开，且必须先确认陷阱真武装了再谈数据。

```bash
sudo ./verify.sh
sudo dmesg | grep CMP-P2P          # mailbox 路线：两张卡都要出现 trap31 armed，否则看拒绝原因
nvidia-smi topo -p2p r
nvidia-smi topo -p2p w
nvcc -O2 -arch=sm_80 -o /tmp/cmpunlocker-test-p2p tools/test-p2p.cu
timeout 120s /tmp/cmpunlocker-test-p2p 0 1
python3 tools/p2p-content-check.py 0 1
```

能力覆盖会让查询结果显示支持，不能据此认定传输成功。CUDA 程序用不同
数据初始化两张卡，分别执行 peer 读和写，并校验目标及源数据，以检测
错误的本地显存别名映射。不指定设备编号时测试所有可见 CUDA GPU 的有向配对。

`tools/p2p-content-check.py` 是给 mailbox 路线准备的：它在目的端预置成期望值的按位取反
之后，在 64 KiB±1、128 KiB±1、512 KiB、1 MiB、8 MiB、32 MiB 这些窗口尺寸上拷贝，
并报出错字节数与第一个错字节的偏移——窗口停在原位这种故障只在邮箱窗口尺寸上露头，
而返回码全是成功。只用 `libcuda`，不需要 CUDA toolkit 也不需要 `cuda-python`。

## 本次验证与限制

- Shell、Python 和 P2P 补丁的语法检查通过（`tests/test_lint.py`：shellcheck 等）。
- 离线回归测试覆盖参数传递、默认关闭、混合显存配置、路线名和补丁变化引起的缓存失效、
  补丁失败中止、initramfs 配置顺序及失败上报、以及钩子植入发生在补丁之后、编译之前。
  `--p2p` 取 `off`/`bar1`/`mailbox` 三种值时都检查：本路线要用的钩子自报落地、另一条路线的
  钩子自报 off，且没有被任何钩子触碰的文件与上游发布逐字节相同。
  另断言两条路线的补丁清单不相交、mailbox 构建里不出现 BAR1 路线的 HAL 改指向与
  peer mask 清理（`0013` 会清空 mailbox 需要的那个 mask）。
- P2P 补丁集在 `driver/VERSION` 的每个版本上都实测过：bar1 的三个补丁加它的钩子、
  mailbox 的两个补丁加它的钩子，六个版本全部干净应用（`tests/test_patches.py` 逐个版本
  解包验证，`--fuzz=0`）。补丁集不含版本白名单。
  原先在 615.x 上被 reject 的三处 hunk，需要修改的行本身未变化，变化的是相邻行：
  `patch(1)` 要求两侧各三行连续上下文，无法表达位置固定而邻行可变的插入点，因此这三处
  改为锚点植入。若某个发布版本移走其中一处，构建失败并指明钩子名称，而不是由版本清单
  预先排除。
- 真机验证（2026-11-18，冷启动后，服务负载在运行）：两张 64 GiB 布局的卡通过全部有向
  配对，结果为 `2/2 directed pairs passed`，每端 16 MiB，两端回读校验，之后无 Xid。
  使用 80 GiB 布局的卡不参与 BAR1 P2P：BAR1 可取尺寸由卡的 resizable-BAR 能力表决定，
  本代卡最大项为 64 GiB（硬件破解卡与软解卡相同），而 80 GiB 布局需要 79.6 GiB 连续
  BAR1。主机侧不是限制条件，每张卡上方按对齐要求预留 128 GiB。要 128 GiB BAR1 需升级
  A100 的卡 BIOS。实测数据见 [P2P.md](P2P.md) 的 "BAR1 size and the 80 GiB layout" 一节。
- `--p2p=mailbox` 已在本机实测（2026-11-21，冷启动后，05/06 两张 80 GiB 布局的卡）：
  `dmesg | grep CMP-P2P` 两张卡都报 `trap31 armed on NV_P2P (PLM 0xffffffff)`，SEC2 的
  第 13 行 `PLM[12] TRAP31_PLM(0x12277c) reg=0xffffffff` 确实落地；
  `tools/p2p-content-check.py` 2/2 有向配对、20 项内容校验全过（65535 B 到 32 MiB），
  事后无 Xid。`--bw` 计时在 Gen4 x16 下每向约 20~25 GB/s（64 MiB 到 4 GiB，逐字节校验
  通过），即链路带宽；fork 的 6.65 GB/s 是 Gen2 链路下的数字。这两张卡上 `0x0012277c`
  回读是全 1，从这一行分不出是出厂就开着还是被第 13 行开出来的，所以 `0021` 留着——它挡
  的是 g250 那类只到 `0xFFFFFF8F` 的卡，那种卡上守卫会拒绝报支持。验证时看
  `dmesg | grep CMP-P2P` 与 `tools/p2p-content-check.py` 的字节，不要看返回码，也不要看
  `nvidia-smi topo -p2p`——caps 是覆盖出来的。
- 使用重建的补丁上下文验证了 P2P 补丁组合可以在不引入已禁用调试补丁的情况下应用。
- 将迁入的能力覆盖 C 代码块独立编译执行，验证两个 CMP ID 生效、其他 ID 的状态保持不变。

运行离线测试：

```bash
python3 -m pytest tests -q
```


完整英文实现说明见 [P2P.md](P2P.md)。
