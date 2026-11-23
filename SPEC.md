# SPEC.md — TF 卡批量写入工具（tf_flasher）：已知问题清单

> 已知问题从 AGENTS.md 迁出（2026-11-23），使用方法/编译打包/测试台见 [AGENTS.md](AGENTS.md)。
> 清单为 2026-11-23 审查，并已逐条对照 `tf_flasher.py` 复核（各条标注「核对」结论与代码位置）。
> 2026-11-23：完成 S1–S6 的 P0/P1 修复与专用 VHD 验收；#1/#2/#3/#4/#7 已修复并附证据，#5/#6/#8/#9 仍待后续包。
> 按影响排序。修任何一条前，先在 AGENTS.md §7 的 VHD 测试台补对应 E2E；修掉后请同步更新本清单状态。

## 状态图例

- **现行缺陷**：代码路径真实存在，当前可触发（或满足所述条件即触发）。
- **潜伏缺陷**：代码路径真实存在，但常规输入（如扇区对齐的整机镜像）不会踩到。
- **加固建议**：现行代码未找到可触发路径，属对未来改动的防御性要求。
- **已修复**：本轮已修复，并附模拟测试/专用 VHD E2E 证据。

## 1. 尾部补零计入对账 【已修复 2026-11-23】

原问题：`flash_one` 末尾不足一扇区时补零写入，但补零同时计入了写入字节数与 sha256，
导致非扇区对齐镜像写完整卡后必报「写入字节数不符」或「写入流 sha256 不符」。

**修复**：`flash_one` 分离 `logical_n`（镜像原始字节，用于对账/结果 `bytes`）与
`written_n`（设备实际字节，含补零，结果 `written_bytes`）；补零只写设备，sha256/对账
只覆盖原始内容；读回按扇区对齐读取，但只对 `img_size` 字节计哈希（物理盘 `ReadFile`
要求扇区对齐长度）。

**验证**：`tests/test_tail_accounting.py`（12 项，含 512B/4KiB 扇区、对齐读回模拟）；
专用 VHD `--tail` 16MiB+1 镜像写入+读回成功，结果 bytes=16,777,217。

## 2. 无镜像内容校验 【已修复 2026-11-23】

原问题：`analyze_image` 对非 `.gz` 一律按裸 img 处理，无 gzip 魔数、最小尺寸、
MBR/分区表签名检查，任意文件可被当作镜像写入且“哈希对的是错文件自身”。
（核对纠正：并非“任意文件都会写满卡后校验通过”——零字节/非对齐等会先在对账处失败，
但错误文件确实可能被写入。）

**修复**：新增完整整盘镜像门禁：gzip 魔数/CRC/截断校验；空文件、过小文件、
仅 0x55AA、文本/zip、MBR 分区越界、GPT 头/分区表 CRC 错误、保护 MBR 无 GPT 头
一律拒绝；仅接受结构有效的整盘 MBR/GPT 镜像（按用户裁决不支持单分区/无分区表镜像）。
GUI 改为“完整预检通过后才启动写线程”，预检期间可取消、失败零写入；CLI `--image`
与 `--flash` 对非法镜像返回非 0。旧缓存缺少 `layout_ok` 时只补读头部校验，不能绕过门禁。

**验证**：`tests/test_image_preflight.py`（25 项）；`python tf_flasher.py --image SPEC.md`
退出码 2 并提示“缺少 MBR 引导签名 0x55AA”。

## 3. 未排除镜像/工具所在盘 【已修复 2026-11-23】

原问题：`selectable()` / 全选 / CLI 只判可移除、非系统盘、容量，镜像所在 U 盘或
程序所在盘可能被选中并在写入中途被擦。

**修复**：新增“路径 → 卷 GUID → 物理盘号”源盘解析（复用完整卷枚举，覆盖目录挂载、
多 extent；UNC/网络映射盘不属于本地物理盘）；镜像路径与程序（脚本/exe）所在盘进入
禁止集合；映射失败或卷拓扑不可靠时 fail closed。GUI 列表禁用并标注，`start()` 与
CLI `--flash` 共用 `partition_targets()` 做最终准入，不能靠关闭 verify 绕过。

**验证**：`tests/test_source_guard.py`（14 项，含 GUI/CLI 拒绝、UNC、映射失败）。

## 4. 卷锁定/弹出失败仍继续写 【已修复 2026-11-23】

原问题：`lock_volumes` 对弹出失败只 warn 后继续；卷映射只按盘符，漏掉无盘符/
目录挂载卷。

**修复**：改用 `FindFirstVolume/FindNextVolume` 全量枚举卷，并用
`IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS` 建立“卷 ↔ 所有物理盘 extent”映射；
写前现场重新枚举目标盘全部卷，逐个 `FSCTL_LOCK_VOLUME` + `FSCTL_DISMOUNT_VOLUME`，
任一失败抛 `VolumeSafetyError`、释放全部句柄且不打开 PhysicalDrive 写句柄；
枚举失败不被当作“空盘”，真正无卷的空白盘允许写入。写盘时 best-effort 将磁盘置
offline（`IOCTL_DISK_SET_DISK_ATTRIBUTES`）解除 Windows 对已挂载卷 extent 的写保护，
结束后恢复 online（验收中发现固定 VHD+NTFS 场景需要）。

**验证**：`tests/test_volume_safety.py`（14 项）；VHD E2E：空白 VHD 16MiB、
含 NTFS 卷 VHD（锁定/弹出 + offline 路径）、三路并发 3/3 全部成功。

## 5. frozen exe 的提权重启失效 【现行缺陷，被 --uac-admin 掩盖】

`relaunch_elevated`（tf_flasher.py:323-326）用 `Path(__file__)` 拼脚本路径，onefile 下指向
`%TEMP%` 解包目录；且 argv 携带 `-I <假路径>`，重启后的 frozen exe 走 argparse 也会因不认识
的 `-I` 直接报错。目前打包带 `--uac-admin`（启动即管理员），该兜底从未被走到。

**修复方向**：frozen 分支改用 `sys.executable` 且不带 `-I`/脚本路径。

## 6. 台账写入未捕获异常 【现行缺陷】

GUI 侧 `_finish`（tf_flasher.py:996）与 CLI 侧 `cli_flash`（tf_flasher.py:1200）都裸调
`write_run_log`。exe 落在只读目录等场景抛异常时：GUI 的 `_finish` 运行在 `after` 回调里
（`_poll` → `_on_msg`），异常被 tkinter 回调机制静默吞掉——完成弹窗不出现、状态栏不更新
（此时 `running` 已复位、按钮已恢复，界面看似完成却无摘要）；CLI 侧直接 traceback、
退出码异常。

**修复方向**：`write_run_log` 调用包 try/except，失败降级为状态栏提示，完成弹窗照常弹出。

## 7. 运行期「读回校验」复选框未禁用 【已修复 2026-11-23】

原问题：运行期未禁用 `var_verify` 复选框；worker 取启动瞬间值执行，而台账与完成摘要取
结束时的当前值，中途切换会错报「校验通过」。

**修复**：保留校验复选框控件引用，`_set_running(True)` 时禁用；`start()` 冻结
`_pending_verify`（启动快照）；`flash_one` 结果新增 `verify_requested` /
`verify_completed`；`_finish()` 的台账与完成摘要只依据启动快照和逐盘实际结果，
未执行读回校验时只显示「写入完成（未读回校验）」。

**验证**：`tests/test_verify_snapshot.py`（8 项，含 UI 变量与快照相反时仍按快照/逐盘记录）；
旧日志字段 `verify` 含义保持不变（启动时请求值），新增字段可被既有消费者忽略。

## 8. `pending` 计数无兜底 【加固建议：2026-11-23 复核未找到现行触发路径】

原审查表述：worker 若在 `try` 外抛异常，GUI 会永久停在「写入中」，取消无效。
复核结论：`flash_one` 的 try 外仅访问 `enumerate_disks` 保证存在的固定字段
（tf_flasher.py:494-502），try 内异常统一被 `except Exception` 捕获，成功/失败/取消三条
退出路径最终都会 `q.put(("final", ...))`——**当前代码不存在已知触发路径**，属对未来改动的
防御性要求。

**修复方向（加固）**：worker 顶层再包一层兜底，保证任何退出路径都发出 `final` 事件。

## 9. CLI/启动器小问题 【现行缺陷，影响小】

- `tf_flasher.bat` 启动命令未带 `%*`（tf_flasher.bat:23 `"%PY%" -I "%SCRIPT%"`），双击
  启动器无法透传 CLI 参数：`tf_flasher.bat --list` 仍会打开 GUI。
- CLI 校验阶段进度不打印：写入阶段结束时 `printed_mb[idx]` 已接近总字节数，校验阶段
  `done ≤ n`，`done - printed_mb ≥ 1 GiB` 永不满足（tf_flasher.py:1167-1184）→ 校验全程
  无进度行。仅外观问题。

**修复方向**：`.bat` 末尾追加 `%*`；校验阶段单独维护进度基线（或按百分比打印）。
