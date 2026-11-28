# 610 台架启动回归：恢复与证据边界

## 取证更新：已确认 payload 越界写

移除显卡后从局域网上的另一台机器取证。journal 无持久记录，但 EFI pstore 已归档到
`/var/lib/systemd/pstore/`。`179489835/dmesg.txt` 的首次 Oops：

- `nv_open_q` 在线程内调用 `kgspSec2PostblTimingRefillPayload`。
- RIP 为 `_kgspSec2PostblTimingFillPayload+0x47`，写指令为 `[rdi+0xf76c]`。
- R08=0x1000；填充循环结束位置 RSI=0x1004。说明传入4KiB签名，随后执行了约62KiB处的固定偏移写。
- `RebuildStockSignature` 把最初0xf800的payload描述符替换为原厂4KiB签名；失败重试重新进入postbl，原代码没有长度检查。

因此已在postbl重入、refill映射前和底层填充函数加入尺寸检查。重试遇到短签名应返回
初始化错误，而不是继续写宿主内存。`179489866/dmesg.txt`还记录了vmap链表损坏，
与内存越界相容，但尚不能证明每一处损坏均来自同一写入。

GPU2此前还报Falcon超时0x65和Xid140；WPR日志已显示80GiB。
这说明80GiB数值写入了启动元数据，不能由此证明布局可用，也不能把Xid直接当作物理坏显存。
本修复针对已确认的越界；GPU2最初的启动失败仍须独立定位。

## 已确认

- 首版 PCIe 门控在三卡上均跳过 Gen2 强制；05/06 实际 Gen1，09 实际 Gen2且 GSP 初始化失败。
- 修订版恢复强制后，用户在交换机看到三卡 Gen2，但主机无法进入系统。没有本次串口/控制台日志，不能断言是内核挂死、GPU 初始化阻塞或用户态服务失败。
- 之前运行时 `/proc/driver/nvidia/params` 已确认 `RMPcieLinkSpeed=0xF` 生效。
- Booter 的错误码不能直接当作 RM SET_PCIE_SPEED 的返回码解释。
- `0x280000000` = 10GiB；`0x1400000000` = 80GiB。此前交接中的容量换算错误已撤回。

## 本地恢复版

默认开启自动 80GiB 提升：一张卡是否走宽布局不来自设备 ID 表，而是从卡自己的 `LMR`
分区数与 `CFG1` 分区大小推导，只有 `0x20C2` 且分区数超过 8 的卡会被授予；booter 超时
才逐级退回。没烧过熔丝的卡拿不到这条路径，仍按 profile 容量表的 CFG1/LMR 配方启动。
要在一台机器上退回去，装的时候加 `--no-80g`，或者把 `/etc/modprobe.d/cmp-pcie-gen2.conf`
里的 `CMPEnable80G=1` 删掉（前提是当初的构建没有把它编译掉，见下）。
无条件保留原有 Gen2 强制路径。`CMPUNLOCKER_EXPERIMENTAL_80G` 默认 1，
`0`（等同 `--no-80g`）把这条路径整个编译掉并计入构建缓存。10-FBPA 配方只保留作研究，
不能宣称已修复硬破解卡。

链路重训不再由启动服务承担，`gen2.service` 已从包中移除。Gen1→Gen2 由驱动在 nv.c 的
init 路径完成（`nv_cmp170hx_retrain_gen2()`：先写 BAR0 的 `CYA_0` 与 `LINK_CONFIG_0`，
再修改两端 `LNKCTL2` 并触发重训、轮询）。BAR0 是驱动的映射，用户态工具无法写入。
2026-11-18 在该 unit 停用后冷启动实测：两张软解卡 `LnkSta=0x1102`，烧录卡 `0x1104`，
journal 中不存在用户态写链路的记录。`/usr/local/sbin/gen2-hammer` 仍作为手动工具安装：
最多 20 次尝试，判据见「开机链路重训服务的作用范围与移除依据」一节，Gen3/Gen4 目标需以
`CMP170HX_GEN2_TARGET` 显式指定。
该机制不防止内核驱动卡死，也不替代冷启动实测。

## 无法启动时的人工恢复入口

由现场操作者在 GRUB 临时编辑内核参数，保留现有参数并追加：

```
module_blacklist=nvidia,nvidia_drm,nvidia_modeset,nvidia_uvm,nvidia_peermem,nvidia_fs systemd.unit=multi-user.target
```

这是阻止 NVIDIA 自动加载的恢复启动，不是卸载驱动。保持 `cmp_rebar_shim` 配置
及 initramfs 模块顺序，不要在驱动已绑定时临时运行 shim commit。
若该入口仍不能启动，需要控制台/串口最后日志或救援系统；不能继续猜测。

进入后先保存 `journalctl -k -b -1`（若有持久日志）、当前启动日志、
`systemctl --failed`、`lspci -Dnnk` 和 `/proc/cmdline`。不要先运行 nvidia-smi
反复触发失败 GPU 初始化。确认 `lsmod` 无 NVIDIA 模块后再准备恢复安装。

安装前把当前 `updates/cmpunlocker/`、两份 cmp modprobe 配置及 initrd
复制到带时间戳的恢复目录。旧 `/root/cmpunlocker.pre-dynamic.tgz` 仅凭文件名
不能认定含可用旧模块。安装器曾移除 DKMS，不能靠改名模块目录假装完成回退。

恢复构建保持宿主用户态匹配的 610.57.04 和原内核，并且不带宽布局（`--no-80g`，等价的
旧写法是 `CMPUNLOCKER_EXPERIMENTAL_80G=0`）。恢复构建一般也不带 P2P：`--p2p` 会把
mailbox 路线装回来，而 `--p2p=bar1` 是另一条路线，两者都不该在排查启动问题时顺手加进来：

```
sudo env CMPUNLOCKER_DRIVER_VERSION=610.57.04 ./install.sh --no-80g --no-gen4 --no-iommu --no-passthrough
```

核对 `modinfo -n nvidia` 与 initramfs 中的 shim/配置。旧安装留下的 `gen2.service`
会被新版 `install.sh` 停用并删除（连同 `/etc/cmpunlocker/gen2.env`）；
`/etc/cmpunlocker/gen-policy` 保留，它现在只是手动工具的目标表，开机没人读。需要
停机/物理断电时由用户安排；热重启不保证复位 PHY。

## Gen4

参考踩坑文档 §5：LINK_CONFIG_0[19:18] 为反向编码 0/1/2/3=Gen4/3/2/1，而
`LNKCTL2` 与 `PL_LINK_RATE[19:16]` 是正向 1..4；已记录的成功流程是先 Gen3，再
E1，最后 Gen4，不能从 Gen2 直接重训 Gen4。禁止用户态 mmap 写 XP 寄存器。
文档 §5.3 说 RM(4) 在 0xF 下可成功，§5.4 又说 RM(4) 永远 0x56，记录冲突，
所以链路状态一律只看 `LnkSta` 加实测带宽；`nvidia-smi` 的 `Max=3` 是 GSP 对
CMP SKU 的身份认知，驱动改 `LnkCap` 无效。ECC 计数被补丁清零，不能用它验证
显存健康。

实现只有一处：`driver/dynfb/hooks/nv_gen4.c`（Phase A Gen3 台阶 → Phase B E1
重放 → Phase C 配置空间重训），挂在 driver post-init。目标按卡取：读自己的
`LNKCAP2` Supported Link Speeds Vector，只软解的卡报 Gen2 因而完全不进这条
路径；GSP 期那条 Gen2 钳制照旧写，本序列会用 `XVE_OVR=0x8` 盖掉它。

**每一级台阶都向链路两端索取，并在两端确认。** `nv_cmp170hx_retrain_gen2()` 在
本序列前两行把上游口 `LNKCTL2` 目标速率写成 Gen2，本卡单方面抬到 Gen3 并不等于链路
抬到 Gen3：Phase A 之后会用一次「两端同时抬到 Gen3」的重训把这一台阶踩实，双端
`LnkSta` 都读到 8 GT/s 才继续，并留 250ms 让链路静置，再把 E1 块写进一个真的在
8 GT/s 跑的 PHY。E1 块本身可能把链路踢下 Recovery，所以写完还会再确认一次，掉了就
重新补这一台阶，再抬目标代际。上游口自己的速度向量也读出来打进日志：卡能请求多高与
板子给多高是两件事，日志要能区分是哪一端封顶。

退路按 `LnkSta` 实际训练到的代际收口。达到目标即收口；未达目标但链路已稳定在 Gen3 时
保持 Gen3，并同时改写两端 `LNKCTL2` 目标与本卡 `LINK_CONFIG_0`/`PL_LINK_RATE` 速率字段
为 Gen3，使后续重训不再追索该链路已证明无法达到的速率。仅当 Gen3 也无法保持时，调用
`nv_cmp170hx_retrain_gen2()` 退回 Gen2。

这条下限同样约束**回退本身**：本趟在双端确认过的最高一级记在 `held` 里，后面更高的那一代
目标索取失败、把链路一起拖到 Gen2 时，先按记下的那一级重写速率字段并重训收口，而不是把卡
交回 Gen2 配方——板子已被实测能承载低一级，高一级被拒绝说明不了低一级。用户主板是 Gen3、
卡熔丝允许 Gen4 时，落到 Gen2 就是这么来的：Phase A 双端确认过 8 GT/s，Phase C 的
16 GT/s 均衡被拒后链路回到 5 GT/s，旧逻辑便一路回退到 Gen2。放弃这一级只有两种理由：再次
索取时被链路拒绝，或配置空间不再应答（后者不再补任何重训）。

`LnkSta` 读到 `0xffff` 既不算速率也不算拒绝：重训的几百毫秒内配置读会返回全 1，
而 `pcie_capability_read_word()` 报成功；`0xffff` 的速度字段是 `0xf`，「已训练到 Gen15」
能通过与阶梯上所有 `>=` 判据，于是链路还在 Recovery 里就被判定为台阶已达，接着写入编码中
没有对应值的目标速率与速率字段。上游口的全 1 原本就无害（15 在取小之后不会胜出），所以
只有本卡这一端需要往回收。

依据：`LNKCAP2` 表示该卡被熔丝允许的最高代际，不表示该主板链路能够达到的代际，两者可以
不一致，分层回退即针对该情况。达到 Gen4 时收口在 Gen4，链路只支持到 Gen3 时收口在 Gen3，
不将已稳定训练的 8 GT/s 降回 5 GT/s。台架 2026-11-18 两次冷启动均达到最高档
（`link trained (LnkSta=0x1104)`，上游桥 `0x2104`），Gen3 一档为回退路径，本机未触发。
`verify.sh` 的判据为 Gen2 下限、高于下限的代际按实测报告，因此 Gen3 与 Gen4 均判为通过；
熔丝允许 Gen4 而链路低于 Gen4 的卡会额外标 `Gen4 missed`，这是提示不是失败，`dmesg`
里的 `CMP Gen` 行会说明是没被索取过还是索取被拒。

「只有一份」这条不变量的准确含义是**只有一份实现**，不是只走一趟。GSP bootstrap 那次
实验（2026-11-18）证明的是：在另一处再放一份、中间被 RM 自己的 PCIe init 改回 Gen2，
真正那趟就会「写回读到的值」、没有产生均衡边沿。因此 `cmp_pcie_retries`（默认 1，钳制在
0-4）允许在同一处把整条阶梯再走一遍，重走前会先把 `CYA_0` bit13 置回去——`0x060731b2`
（VBIOS）与 `0x060711b2`（A100 DevInit 之后）的实测差值就是这一位，不置回去第二趟就只是
在复述第一趟留下的字。该不变量由 `tests/test_dynfb.py` 检查：E1 的 `CYA_0` 值只允许出现在
一个载荷里，且边沿重新武装必须排在 E1 重放之前。

时序代价要算清楚，因为这段跑在驱动 init 里、持着 RM 的 API 锁：双端已在目标代际直接返回
（热重启或 BIOS 自己训练到位的机器不付任何代价）；需要走阶梯时，等 Gen3 的预算是 600ms
（Gen3 无均衡握手），等目标代际的预算是 2s（16 GT/s 要走完均衡各阶段），中间还有 250ms
静置与 100ms 落位。成功一趟约 0.5 秒，拒绝的链路每趟最多约 3 秒，`cmp_pcie_retries` 的
上限就是把最坏情况钉在这里。踩过某一级又被更高一级拒绝的机器再多付一次收口重训，预算同
Gen3 的 600ms：这 600ms 买回来的是 Gen3 链路，而不是把卡丢回 Gen2。

### 为什么台阶必须两端同时抬（AMD root port）

台架的卡挂在 PEX880xx 交换芯片下游口，那种口对「只有卡单方面改速率字段」的重训不敏感，
所以单边版本的 Phase A 在台架两次冷启动都能收口在 Gen4。EPYC 服务器（卡 `41:00.0`，
上游 `40:03.1` AMD root port，驱动 615.71.09）不掩盖这件事：root port 遵守写进它
`LNKCTL2` 的目标速率，而前一步 Gen2 重训刚把它钉在 Gen2，于是链路停在 5 GT/s，E1 重放
打在还没离开 5 GT/s 的 PHY 上，Phase C 的 5→16 GT/s 直跳按已知规律失败，本次开机被
`nv_cmp170hx_retrain_gen2()` 钉回 Gen2。当时现场是用一个 companion unit 补救：先带
`cmp_pcie_gen=3` 与每卡 `RMPcieLinkSpeed=0x5` 重载一次驱动，双端确认 8 GT/s 后再按原
配置重载一次，40 秒左右双端 16 GT/s。它真正提供的是「Gen4 那一趟从已收口的 8 GT/s
起步」，而不是重载本身——两端同时抬的 Gen3 台阶就是把这一件事搬回驱动里，companion
unit 不再必需（留着也无害：卡已在 Gen4 时它会直接退出）。台架侧的行为未变，EPYC 侧的
效果需在真机以 `dmesg | grep 'CMP Gen'` 与双端 `LnkSta` 复核。

总开关默认打开（写 `cmp_pcie_gen=0` 并把 `RMPcieLinkSpeed` 抬到 `0xF`），
`--no-gen4`（等同 `CMPUNLOCKER_EXPERIMENTAL_GEN4=0`）才是把每张卡钉在 Gen2，与本特性
加入前一致。上机要看的是 `dmesg` 里的 `CMP Gen4: link trained (LnkSta=0x1104)`、
`CMP Gen3: settled (LnkSta=0x1103)` 与 `falling back to Gen2`，以及 `lspci -vv` 的
`LnkSta`：`nvidia-smi` 的 `pcie.link.gen.max` 只是 GSP 的身份认知（抬到 `0xF` 后它
确实会随着变成 4，但判链路仍以 `LnkSta`/sysfs 为准）。

现场报「主板是 Gen3、卡最后停在 Gen2」时，这几行就是分诊表：没有 `CMP Gen` 行而只有
`CMP: staying on Gen2`，是卡自己的熔丝只到 Gen2（软解卡），板子给多高都无关；
`Gen3 rung not held ... port fuses Gen2` 是紧邻上游口被 BIOS 钉在 Gen2；
`Gen3 was held at both ends before Gen4 was refused; settling on Gen3` 后跟
`settled`，是本轮修的那条路——Gen3 拿到并留在 Gen3；仍打印 `falling back to Gen2`，
才说明这块板子连 8 GT/s 都没接住。收口这条分支台架未触发，需在那台 EPYC 上复核。

## 开机链路重训服务的作用范围与移除依据

`gen2.service` 的职责是在启动阶段把链路从 Gen1 重训到 Gen2。同一份工作由
`nv_cmp170hx_retrain_gen2()` 在驱动 init 路径完成，且驱动可访问 BAR0，用户态工具不可。
2026-11-18 冷启动实测的时序（`journalctl -b -k` 与 `journalctl -b -u gen2.service`）：

```
14:24:42  GPU2 CMP-DYN DECISION ... decision=wide-layout        # 80 GiB 布局成立
14:24:45  kernel: CMP Gen4: XP programmed CYA=0x060711b2
14:24:45  kernel: CMP Gen4: link trained (LnkSta=0x1104)         # 驱动将 09 训练到 Gen4
14:24:45  systemd: Starting gen2.service ...
14:24:56  gen2-hammer: 0000:09:00.0: no Gen2 window caught after 20 attempts; final Gen2
```

该服务在链路已被驱动训练到 Gen4 之后将其重训回 Gen2。它执行重训的原因是它判定链路未达
目标；该判定失效的原因是 `tools/hammer.sh` 对 `LnkSta` 的位定义有误：bit15 被当作
`Data Link Layer Active`（`LINKSTA_ACTIVE_MASK=0x8000`），bit11/bit12 被当作事件位。按
PCIe 基规范，事件位是 **bit14 与 bit15**（写 1 清除），`DL Link Active` 是 **bit13**，
bit11 是 `Link Training`，bit12 是 `Slot Clock Configuration`。本机实测：一张承载流量的
Gen2 x16 链路在 GPU 侧读得 `0x1102`（bit12，槽位时钟已配置），在其上游桥读得 `0x2102`
（bit13，链路激活）。两端实现的可选状态位不同，bit15 在两端均为 0。因此「链路已达目标」
在该平台上恒为假：三张卡在每次启动时都被重训，无一被报告为成功，而被驱动训练到 Gen4 的
那张卡被重训回 Gen2。

修正后 `link_is_up_at` 的判据为可观测的三项：速率不低于目标、链路宽度非零、`Link
Training` 已清除；事件位按 bit14/bit15 清除。目标是下限而非上限，因此已处于 Gen4 的链路
保持 Gen4。判据表（含本机实测的 `0x1102` 与驱动训练得到的 `0x1104`）由
`tests/test_hammer.py` 通过 source `tools/hammer.sh` 调用实际函数核对。

修正后当场执行的结果：三张卡均报告 `already Gen2; no retrain needed (target Gen2)`，
退出码 0。这是该工具在该机器上首次以 0 退出。

随后将该 unit 从 `multi-user.target.wants` 移除，再次冷启动，以确认驱动单独承担该工作时
的结果（`journalctl -b -u gen2.service` 无记录，即本次启动不存在用户态链路写入）：

```
05:00.0  GPU LnkSta=0x1102  bridge 04:00.0 LnkSta=0x2102  5.0 GT/s x16
06:00.0  GPU LnkSta=0x1102  bridge 04:10.0 LnkSta=0x2102  5.0 GT/s x16
09:00.0  GPU LnkSta=0x1104  bridge 08:00.0 LnkSta=0x2104  16.0 GT/s x16
```

结论：驱动单独即可完成该工作。Gen1→Gen2 由驱动 init 阶段的重训完成；09 的 Gen4 在负载
建立后（vLLM 两个 worker 各 62 GiB）复读仍为 `0x1104`。据此从包中删除 `gen2.service`，
`install.sh` 只负责停用并删除旧安装遗留的 unit，`/usr/local/sbin/gen2-hammer` 保留为手动
恢复命令（例如 `sudo CMP170HX_GEN2_TARGET=3 gen2-hammer`）。链路健康检查移入
`verify.sh`，判据不变：Gen2 为下限，高于下限的代际按实测报告。

`nvidia-smi` 在该状态下仍报告 Gen2。`pcie.link.gen.current` 与 `pcie.link.gen.max` 是
GSP 对 CMP SKU 的身份标识，不是链路读数。链路状态以 `LnkSta` 与 sysfs 的
`current_link_speed` 为准，即上表前两列。

80 GiB 布局的卡不提供 BAR1 P2P。原因在卡侧：BAR1 可取的大小由卡自己的 resizable-BAR
能力表决定，该表位于卡 BIOS，本代卡最大项为 64 GiB，硬件破解卡与软解卡相同；而 80 GiB
布局需要 79.6 GiB 连续 BAR1，无可用尺寸，因此强制静态 BAR1 被判为不满足并降级为 AUTO。
主机侧不是限制条件：每张卡上方预留 128 GiB（BAR1 64 GiB 加 BAR3 32 MiB 超过 64 GiB，对齐
到下一个 2 的幂）。要同时拥有 80 GiB 显存与 BAR1 P2P，需要 128 GiB BAR1，即升级 A100 的
卡 BIOS 使能力表包含该尺寸。实测数据见 `docs/P2P.md` 的
"BAR1 size and the 80 GiB layout" 一节。

