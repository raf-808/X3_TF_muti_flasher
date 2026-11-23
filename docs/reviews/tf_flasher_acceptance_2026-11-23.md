# TF 批量写卡工具验收报告（2026-11-23）

- 分支：`safety-fixes`（基于 `main`/d2b87a8）
- 范围：plan.md 的 S1–S6 P0/P1 修复、S7 整合验收
- 环境：Windows 10.0.26100、Python 3.13.12、tkinter 可用；VHD 写入在管理员提权的独立 PowerShell 中执行
- 裁决：仅支持完整磁盘 MBR/GPT 镜像；VHD 验收由总调度发起提权；Git 使用新分支逐步提交

## 1. 提交

| Commit | 内容 |
|---|---|
| `abcd41e` | S1：VHD 测试护栏（登记 VHD → Get-DiskImage DevicePath → 盘号，fail closed） |
| `93a6acd` | S2：完整卷枚举 + 锁定/弹出失败即拒写 |
| `5334bfb` | S3+S4 commit 组：源盘保护 + 严格镜像门禁/写前预检 |
| `6bd5df0` | S5：尾部补零只写不记账，读回只校验原始长度 |
| `057f66d` | S6：verify 启动快照与逐盘校验状态 |
| `32ac03a` | S7 验收修正：offline 写保护、扇区对齐读回、guard DevicePath |

## 2. 模拟测试

| 测试文件 | 项数 | 结果 |
|---|---|---|
| `tests/test_vhd_guard.py` | 16 | PASS |
| `tests/test_volume_safety.py` | 14 | PASS |
| `tests/test_source_guard.py` | 14 | PASS |
| `tests/test_image_preflight.py` | 25 | PASS |
| `tests/test_tail_accounting.py` | 12 | PASS |
| `tests/test_verify_snapshot.py` | 8 | PASS |
| 合计 | 89 | PASS |

其他：`python -m compileall -q tf_flasher.py flasher_build tests` 通过；
`python tf_flasher.py --smoke` 通过；`python tf_flasher.py --image SPEC.md` 退出码 2
（“缺少 MBR 引导签名 0x55AA”）；2MiB 合法 MBR 镜像 `--image` 退出码 0。

## 3. 专用 VHD E2E

测试盘：`%TEMP%\x3wtest.vhd`（PD1）、`x3wtest2.vhd`（PD2）、`x3wtest3.vhd`（PD3），均为 64MB 固定 VHD。

| 用例 | 结果 | 关键输出 |
|---|---|---|
| 单盘空白 VHD（16MiB 合法 MBR 镜像） | PASS | `✔ 成功 bytes=16777216` |
| 非对齐尾部 `--tail`（16MiB+1） | PASS | `✔ 成功 bytes=16777217` |
| 含 NTFS 卷的 VHD（Y:，锁/弹出 + offline） | PASS | `✔ 成功 bytes=16777216` |
| 三路并发 | PASS 3/3 | `并发完成 3/3`，PD1/2/3 均成功 |
| 负向：显式 PhysicalDrive0（系统盘） | PASS（拒绝） | “PhysicalDrive0 不是已登记的测试 VHD…”，退出码 2 |

日志：`%TEMP%\tf_e2e_single.log`、`tf_e2e_tail.log`、`tf_e2e_disk3.log`、`tf_e2e_multi.log`、`tf_e2e_negative.log`。

## 4. 验收中发现并修复的问题

1. **非对齐读回 err=87**：物理盘 `ReadFile` 要求扇区对齐长度。修复：读回按扇区向上对齐读取，仅原始 `img_size` 字节计入 sha256。
2. **固定 VHD + 已挂载 NTFS 写保护**：锁定/弹出返回成功，但 ≥1MiB `WriteFile` 报 err=5；磁盘 offline 后可正常写入。修复：锁定/弹出成功后 best-effort 置磁盘 offline，结束/异常时恢复 online；offline 失败不影响“锁定失败即拒写”的契约。
3. **vhd_guard 取盘号依赖管理员**：`Get-Disk` 非管理员返回空。修复：改用 `Get-DiskImage.DevicePath` 解析 `PhysicalDriveN`，只读且非管理员可用。

## 5. 独立复核

- S1：只读复核 PASS（护栏身份来源、fail-closed、写前拦截、测试覆盖）。
- S2–S4：只读复核 PASS（卷拓扑、锁失败拒写、源盘保护、镜像门禁与预检时序、测试覆盖）。
- S5–S7：终验只读复核 PASS（尾部对账/对齐读回、verify 快照与逐盘字段、offline 降级与恢复、测试覆盖），未发现阻断问题。

## 6. 未验证 / 未修复

- 真实 TF 卡、真实读卡器、扩容假卡/坏块卡未测试；需用户提供专用测试卡做硬件终验。
- GUI 真实点击流程未自动化；当前为状态机单测 + `--smoke` 构建自检。
- SPEC #5（frozen 提权）、#6（台账异常）、#8（pending 兜底，加固建议）、#9（BAT/CLI 进度）按计划留待后续包。
- 含卷 VHD 的 offline 方案是否同样适用于 USB 读卡器未实测；无法 offline 时仍会安全失败，不会静默损坏。
