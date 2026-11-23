# AGENTS.md — TF 卡批量写入工具（tf_flasher）

> 面向下一个开发者/Agent 的项目指南：定位、目录结构、上手、写入流程、编译打包、测试台与平台坑点。已知问题清单单独维护在 [SPEC.md](SPEC.md)。
> 本工具原在「地平线 X3」仓库（提交 `66bddd5` 引入），2026-11-23 迁出为独立项目（X3 侧提交 `d83db3d`），本仓库为唯一维护地。

## 1. 项目概述

把整机/裸盘镜像（`.img.gz` / `.img`）**流式**写入插在本机的多个 TF/SD 读卡器，tkinter 简易 GUI + 无 GUI 命令行，**零第三方运行依赖**。

- 目标平台：Windows 10/11（物理盘写入只支持 Windows；镜像解析/gzip 部分跨平台）。
- Python：3.9+，实测 3.12 / 3.13；GUI 需带 tkinter，纯 CLI 不需要。
- 典型用途：X3 开发板整机镜像（如 `X320261117OK.img.gz`，解压 15,931,539,456 B）批量灌卡。
- 注意：GUI 标题、docstring、台账字段里仍保留 `X3` 字样（迁出前的现场产物）；若要做通用工具，改 `App.__init__` 的 `root.title(...)` 与文件头 docstring 即可，不影响功能。

## 2. 目录结构

```
TF卡批量写入/
├── tf_flasher.py              # 全部实现：Win32 原生层 + 镜像解析 + 写入 worker + GUI + CLI
├── tf_flasher.bat             # 双击启动器：自动挑一个带 tkinter 的解释器（机器相关路径，见文件头注释）
├── .gitignore                 # 缓存/台账/exe/测试产物/本地镜像不入库
├── flasher_build/             # 写入 E2E 测试台（VHD 当"物理盘"，安全无风险）
│   ├── _test_write_e2e.py     # 单盘：造 16MiB 确定性镜像 → 真 flash_one 写+fsync+读回校验
│   ├── _test_write_multi.py   # 多路：对所有测试 VHD 并发 flash_one（3 路已验证）
│   ├── mk_vhd_script.py       # 生成 vhd.txt（diskpart：建 64MB 固定 VHD 并挂载）
│   ├── mk_vhd_more.py         # 生成 vhd3.txt（再建/挂 2 个测试 VHD）
│   ├── mk_vhd_letter.py       # 生成 vhd2.txt（给测试 VHD 分区并分配盘符 X:）
│   └── detach_vhd.py          # 生成 vhd_detach.txt（卸载 3 个测试 VHD）
├── flash_cache/               # 本地：镜像预扫描缓存（按 gz 的 sha256 内容寻址，git 忽略）
├── flash_logs/                # 本地：每次写入运行的 JSON 台账（git 忽略）
└── flasher_dist/              # 本地：PyInstaller 输出（git 忽略）
```

运行时路径规则（`tf_flasher.py` 开头常量）：脚本运行时缓存/台账落在**脚本旁**；PyInstaller onefile 下 `__file__` 指向 `%TEMP%` 解包目录，因此 frozen 时改用 `sys.executable` 所在目录。

## 3. 快速上手

GUI：

```powershell
.\tf_flasher.bat            # 或 python tf_flasher.py（带 tkinter 的解释器）
```

流程：插卡 → 刷新列表 → 勾选目标（仅可移除介质可选）→ 浏览镜像 → 开始写入。非管理员启动会弹 UAC 提权重启。

CLI：

```powershell
python tf_flasher.py --list                       # 枚举物理磁盘（只读，安全）
python tf_flasher.py --image D:\img\x.img.gz      # 解析镜像：压缩/解压大小 + sha256（带缓存）
python tf_flasher.py --flash D:\img\x.img.gz --drives 2,3
python tf_flasher.py --flash D:\img\x.img.gz --drives 2,3 --no-verify --yes   # 谨慎：跳过确认与读回校验
```

写物理盘必须管理员；CLI 非管理员会自动 `ShellExecute runas` 提权重开（frozen 下该路径失效，见 SPEC.md #5）。

## 4. 写入流程与安全机制

每个目标盘一个线程，**边解压 gzip 边直写物理盘**，不落临时 `.img`：

1. `lock_volumes()`：对该盘所有本地卷 `FSCTL_LOCK_VOLUME` + `FSCTL_DISMOUNT_VOLUME`，句柄保持打开维持锁定（失败只告警，见 SPEC.md #4）。
2. 打开 `\\.\PhysicalDriveN`（`GENERIC_READ|WRITE`，share 必须 READ|WRITE），用 `_RawDiskFile` **直接走 Win32 `WriteFile`/`ReadFile`**（不经 CRT fd 层）。
3. 8 MiB 块流式写入并同步累计 sha256；末尾不足一扇区时补零（补零计入对账是已知缺陷，见 SPEC.md #1）。
4. `FlushFileBuffers` 落盘。
5. 与后台预扫描结果对账：字节数 + sha256（缓存命中则秒出）。
6. 可选读回校验：整段再读一遍算 sha256，比对镜像预期哈希（抓坏块/扩容假卡）。

内置防护：

| 机制 | 阈值/行为 |
|---|---|
| 目标判定 | 可移除介质（属性位 / 几何 MediaType / SD/MMC 总线三选一）+ 非系统盘 + 容量 ≤ 64 GiB |
| 大磁盘保护 | > 64 GiB 的盘不枚举为写卡目标（防误选大硬盘） |
| 二次确认 | 明列每个目标的盘号/容量/总线/型号/盘符 |
| 容量保护 | 已知镜像大小时，卡过小直接拒绝；未知时写入中途报错兜底 |
| 台账 | 每次运行落 `flash_logs/flash_<时间戳>.json`（目标、哈希、速度、结果） |

写完提醒：Windows 若弹「使用驱动器前需要格式化」**一律点取消**——那是卡的 ext4 分区，Windows 不认识。

## 5. 命令行参考

| 参数 | 说明 |
|---|---|
| `--list` | 枚举物理磁盘；标注容量/总线/可移除/盘符/是否可写 |
| `--image PATH` | 解析镜像（gz 则算压缩包 sha256 + 解压流大小/sha256；裸 img 则文件哈希即预期） |
| `--flash PATH --drives N,N` | 批量写入；先解析后写（fail-fast） |
| `--no-verify` | 跳过写后读回校验 |
| `--yes` | 跳过 CLI 交互确认（危险，脚本用） |
| `--smoke` | GUI 构建自检（不出窗口），打包后回归用 |

退出码：全部成功 `0`，任一失败/取消 `1`，参数或平台错误 `2`。

## 6. 编译打包（单文件 exe）

运行只需 Python 标准库；打包需要 PyInstaller（建议独立 venv，避免污染全局）：

```powershell
cd 'E:\devworkspace\TF卡批量写入'
uv venv .venv --python 3.12              # 或 python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install pyinstaller pillow  # Pillow 仅用于生成图标，应用运行不依赖
python assets\build_icon.py   # 仅需在重新生成图标时运行
pyinstaller --onefile --windowed --uac-admin --clean --icon assets\tf_flasher.ico --add-data "assets\tf_flasher.ico;assets" --name "X3批量写卡" tf_flasher.py
```

要点：

- `--uac-admin`：exe 启动即管理员，不走代码里的“提权重启”兜底（frozen 下该兜底本身有缺陷，见 SPEC.md #5）。
- 图标位于 `assets/tf_flasher.ico`（Windows EXE / 窗口）及 `assets/tf_flasher.png`（预览）；也可直接运行 `pyinstaller X3批量写卡.spec`，图标和资源绑定已写入 spec。
- `--windowed`：无控制台；CLI 子命令在 windowed exe 里看不到输出，回归请用 `--smoke/--list/--image` 看窗口/退出码。
- onefile 产物在 `dist/`（已被 `.gitignore` 忽略）；缓存/台账落 **exe 旁**（`sys.executable` 目录）。
- 打包后回归：`dist\X3批量写卡.exe --smoke`、`--list`、`--image <镜像>`（缓存命中应 2s 内出结果）。
- 构建 venv 建议 3.12（`uv` 的 Windows cpython 自带 tkinter）。

## 7. 测试台（VHD 模拟物理盘）

用 diskpart 固定 VHD 当“物理盘”，不会碰真卡。全程需管理员 PowerShell：

```powershell
# 1) 建 1 个 64MB 固定 VHD 并挂载（生成 vhd.txt）
python flasher_build\mk_vhd_script.py
diskpart /s flasher_build\vhd.txt

# 2) 单盘 E2E：自动找 bus=15 且 ≤128MiB 的盘（也可显式传 PhysicalDriveN 的 N）
python flasher_build\_test_write_e2e.py

# 3) 多路并发：再建 2 个测试 VHD 后跑
python flasher_build\mk_vhd_more.py
diskpart /s flasher_build\vhd3.txt
python flasher_build\_test_write_multi.py

# 4) 卸载
python flasher_build\detach_vhd.py
diskpart /s flasher_build\vhd_detach.txt
```

注意：`mk_vhd_letter.py` 里 `select disk 3` 是当时机器的盘号，换机器要按 `--list` 结果改；VHD 默认落在 `%TEMP%`（`x3wtest*.vhd`），残留会在 `--list` 里以 `虚拟`/bus=15 出现。

## 8. Windows 平台坑点

- **写物理盘必须管理员**；非管理员 `enumerate_disks()` 只能看到“因权限无法访问”的计数。
- **卷必须锁+弹出**：打开卷要带 `FILE_SHARE_READ|WRITE`（share=0 对挂载卷必 err=32）；独占靠 `FSCTL_LOCK_VOLUME`，不是靠 share 标志。
- **不要用 CRT fd 层**（`open_osfhandle`/`fdopen`）：windowed exe 里会偶发 `Errno 9 EBADF`，已改为 `WriteFile`/`ReadFile` 直写。
- **`use_last_error=True` 的 WinDLL**：错误码必须 `ctypes.get_last_error()`，不要用 `GetLastError` 直接调。
- **网络盘会让枚举卡 20s**：`_local_volume_letters()` 先用 `QueryDosDeviceW` 过滤成 `\Device\Harddisk*` 才打开。
- **换行**：`.py` 建议 LF（本仓库未配 `.gitattributes`，Windows 上检出为 CRLF 也能跑）；`.bat` 必须 CRLF。
- **中文路径**：本工具自身只需普通路径；在 Git Bash 里传 `/` 开头参数要加 `MSYS_NO_PATHCONV=1`。

## 9. 提交前检查

1. `python -m py_compile tf_flasher.py flasher_build\*.py` 全过；
2. 改写入路径/对账逻辑后跑一遍 §7 的单盘 + 多路 E2E；
3. `git status` 确认没有镜像（`*.img`/`*.img.gz`）、VHD、`flash_cache/`、`flash_logs/`、exe 被误加；
4. 文案/标题若改成通用版，同步本文件 §1/§6 的命令与产物名；
5. 修掉 SPEC.md 里的条目后，同步更新 SPEC.md 的状态标注。
