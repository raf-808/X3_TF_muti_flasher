# SPEC.md — TF 卡批量写入工具（tf_flasher）：已知问题清单

> 已知问题从 AGENTS.md 迁出（2026-11-23），使用方法/编译打包/测试台见 [AGENTS.md](AGENTS.md)。
> 清单为 2026-11-23 审查，并已逐条对照 `tf_flasher.py` 复核（各条标注「核对」结论与代码位置）。
> 按影响排序。修任何一条前，先在 AGENTS.md §7 的 VHD 测试台补对应 E2E；修掉后请同步更新本清单状态。

## 状态图例

- **现行缺陷**：代码路径真实存在，当前可触发（或满足所述条件即触发）。
- **潜伏缺陷**：代码路径真实存在，但常规输入（如扇区对齐的整机镜像）不会踩到。
- **加固建议**：现行代码未找到可触发路径，属对未来改动的防御性要求。

## 1. 尾部补零计入对账 【潜伏缺陷】

`flash_one` 末尾不足一扇区时补零写入，但补零同时计入了写入字节数与 sha256：
`tail += b"\x00" * pad` 后 `sha.update(tail)`、`n += len(tail)`（tf_flasher.py:546-554），
而对账按不含补零的 `img_size`/`img_sha256` 比对（tf_flasher.py:563-567）——非扇区对齐镜像
写完整卡后必报「写入字节数不符」或「写入流 sha256 不符」，读回校验同样失败。
仅非对齐镜像触发；整机 MBR 镜像通常扇区对齐，故至今未暴露。

**修复方向**：补零只写；对账哈希只覆盖 `img_size`；读回校验也只读 `img_size` 字节。

## 2. 无镜像内容校验 【现行缺陷】

`analyze_image`（tf_flasher.py:360-391）非 `.gz` 一律按裸 img 处理，文件自身哈希即「预期
哈希」，无 gzip 魔数、最小尺寸、MBR/分区表签名检查；GUI 文件对话框还允许 `*.*`
（tf_flasher.py:779）。选错文件（`.zip`/文本/0 字节）会写满卡且「校验通过」——哈希对的是
错文件自身。

**修复方向**：加 gzip 魔数检查、最小尺寸阈值、MBR/分区表签名检查（裸 img 必须以
`0x55AA` + 合理分区表开头）。

## 3. 未排除镜像/工具所在盘 【现行缺陷】

`selectable()`（tf_flasher.py:281-289）与「全选可写」（tf_flasher.py:759-763）只判可移除/
非系统盘/≤64 GiB：镜像所在 U 盘若满足条件会被选中，写到一半源被擦；`start()` 的容量检查
（tf_flasher.py:847）只能拦「卡容量 < 镜像」这种情况。

**修复方向**：排除镜像路径所在物理盘与进程（exe/脚本）所在物理盘，禁止其成为写卡目标。

## 4. 卷锁定/弹出失败仍继续写 【现行缺陷】

`lock_volumes` 对弹出失败的卷只 warn「继续原样写入」（tf_flasher.py:311-313），
`flash_one` 收到 warn 后照写（tf_flasher.py:505-508）。卷仍挂载时 Windows 脏元数据可能在
写入后回写覆盖新 MBR/分区表，而读回校验此时已经结束，抓不到。
另外卷映射只按盘符（`_volume_map`，tf_flasher.py:206-237），挂载到目录（无盘符）的卷
不会被锁定。

**修复方向**：锁定/弹出失败即判该目标失败（或加显式 `--force` 才继续）；卷枚举改走卷
管理器（FindFirstVolume/挂载点遍历）以覆盖无盘符卷。

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

## 7. 运行期「读回校验」复选框未禁用 【现行缺陷】

`_set_running`（tf_flasher.py:905-914）运行期禁用了刷新/全选/浏览/各盘复选框，唯独没禁
`var_verify` 复选框。各 worker 线程取启动瞬间的值执行（tf_flasher.py:897），而台账与完成
摘要取结束时的当前值（tf_flasher.py:996-997、:1002）→ 中途切换后记录与实际执行不一致，
可能错报「校验通过」。

**修复方向**：运行期禁用该复选框；或每盘结果自带 `verify` 字段，摘要按各盘实际执行记录。

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
