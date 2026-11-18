# P2P 合并说明

以 `cmpunlocker-0.4` 为基础，已融合附件中 `cmpunlocker-p2p` 的 `--p2p`
参数及其驱动实现。本仓库根目录即合并后的主项目。

## 使用

在项目根目录运行：

```bash
sudo ./install.sh --p2p
```

可以与原有 `--profile=8gb|10gb`、`--no-iommu`、`--no-passthrough` 一起使用
（`--no-gen2-service` 仍被接受，但已无意义：开机不再有重训服务）。不传 `--p2p` 时，
全部可选 P2P 驱动补丁关闭。安装完成后需彻底关机再开机。

直接构建也支持：

```bash
sudo env CMPUNLOCKER_ENABLE_P2P=1 ./driver/build.sh
```

重新运行不带 `--p2p` 的安装命令可关闭 P2P，随后冷启动。升级内核后，
仍按 `0.4` 的方式手动重新安装；需要 P2P 时务必继续传入该参数。

## 实现范围

| 内容 | 合并方式 |
| --- | --- |
| GSP 的 P2P 读写能力覆盖 | 将 `cmpUnlockForceP2PCaps()` 的逻辑转成 `0.4` 原生补丁，保留两个 CMP 设备 ID 的判断。 |
| BAR1 P2P 完整路径 | 迁入 HAL 选择、映射创建/删除、BAR1 总线地址传递、PTE/物理地址改写、UVM 和 IOVAS 相关改动。 |
| mailbox 预注册修正 | BAR1 模式下跳过 mailbox peer 预注册，避免已有 peer mask 阻断 BAR1。 |
| 主机读能力修正 | 迁入 CMP 读能力覆盖，并去掉原补丁对已禁用 `0014` 调试补丁的上下文依赖。 |
| BAR1 resize | 保留 `0.4` 已有的等效实现，不重复应用同一功能的补丁。 |
| 安装配置 | 将 `RmForceEnableGen2=1` 与 `RMForceStaticBar1=1;RMPcieP2PType=1` 写入同一条 `NVreg_RegistryDwords`，在生成 initramfs 前完成。 |
| 按卡动态解锁 | `driver/dynfb/`：`cmp_dynamic.h` 是策略本体，`apply.py` 在打完补丁后按**锚点**把钩子植入驱动（不用上下文窗口；锚点大多是我们自己补丁里的代码，因此与驱动版本无关）。判据是卡自己的 `LMR` 分区数与 `CFG1` 分区大小，只有 `0x20C2` 且分区数超过 8 的卡吃 80GiB（PCIe 能力不作判据）；默认关闭（`CMPUNLOCKER_EXPERIMENTAL_80G=0`）；只有 booter 超时才逐级退回并写入 registry。无条件 Gen2 初始化保留；Gen4 走 Gen3→E1→Gen4（`dynfb/hooks/nv_gen4.c`），目标代际读每张卡自己的 `LNKCAP2` 熔丝，软解卡自动留在 Gen2；没到目标时按 `LnkSta` 实际训练到的代际收口（桥只到 Gen3 就留在 Gen3），连 Gen3 都没站住才调 Gen2 重训收回；开关是 `CMPUNLOCKER_EXPERIMENTAL_GEN4=1`。每个决定打印 `CMP-DYN` 内核日志，详见 `BOOT-RECOVERY.zh-CN.md`。 |
| 关闭与重建 | P2P 开关进入缓存标记；切换开关会重新解压驱动源码。关闭时同时清除配置中的 P2P 参数。 |
| 验证 | 记录 `p2p_enabled`，增加能力矩阵提示、真实 CUDA 读写校验程序和离线回归测试。 |

三个 P2P 驱动补丁位于 `driver/patches/p2p/`，另外三处改动改由 `dynfb/apply.py`
按锚点植入（`hooks/p2p_caps.c`、`hooks/p2p_resizable_bar.c`、
`hooks/p2p_bar1dma.c`）。没有整体替换 `0.4`
的解锁实现，也没有迁入与 P2P 无关的超频、时序调整和软件包持久化系统。
原有 8GB/10GB/混合显存识别、Gen2、直通实现继续保留。

## 目标主机验证

每张参与 P2P 的 GPU 都需要足够大的 BAR1，且主机 PCIe 路径必须支持真实
peer 读写。源项目使用每卡 64 GiB BAR1。BAR1 扩容由驱动在加载时通过 REBAR
完成，前提是 BIOS 开启 Above-4G Decoding 并提供足够大的 MMIO High 地址
空间；BIOS 不分配时驱动无法凭空获得地址空间，BAR1 保持出厂尺寸，P2P 不可用。

```bash
sudo ./verify.sh
nvidia-smi topo -p2p r
nvidia-smi topo -p2p w
nvcc -O2 -arch=sm_80 -o /tmp/cmpunlocker-test-p2p tools/test-p2p.cu
timeout 120s /tmp/cmpunlocker-test-p2p 0 1
```

能力覆盖会让查询结果显示支持，不能据此认定传输成功。CUDA 程序用不同
数据初始化两张卡，分别执行 peer 读和写，并校验目标及源数据，以检测
错误的本地显存别名映射。不指定设备编号时测试所有可见 CUDA GPU 的有向配对。

## 本次验证与限制

- Shell、Python 和 P2P 补丁的语法检查通过（`tests/test_lint.py`：shellcheck 等）。
- 离线回归测试覆盖参数传递、默认关闭、混合显存配置、开关和补丁变化引起的缓存失效、
  补丁失败中止、initramfs 配置顺序及失败上报、以及钩子植入发生在补丁之后、编译之前。
  不带 `--p2p` 时三个 P2P 钩子自报 off，它们要改的三个文件与上游发布逐字节相同。
- P2P 补丁集在 `driver/VERSION` 的每个版本上都实测过：三个补丁加三个钩子，六个版本
  全部干净应用（`tests/test_patches.py` 逐个版本解包验证）。补丁集不含版本白名单。
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
- 使用重建的补丁上下文验证了 P2P 补丁组合可以在不引入已禁用调试补丁的情况下应用。
- 将迁入的能力覆盖 C 代码块独立编译执行，验证两个 CMP ID 生效、其他 ID 的状态保持不变。

运行离线测试：

```bash
python3 -m pytest tests -q
```


完整英文实现说明见 [P2P.md](P2P.md)。
