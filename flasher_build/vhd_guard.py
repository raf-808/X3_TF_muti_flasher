# -*- coding: utf-8 -*-
"""vhd_guard.py — E2E 测试写盘护栏：只允许写本项目登记并已挂载的测试 VHD。

背景：flasher_build 下的 E2E 脚本会真实调用 flash_one 写 \\\\.\\PhysicalDriveN。
仅凭「bus=15 且容量小」无法证明该盘就是本项目创建的 VHD；一旦误传真实盘号，
可能擦掉 U 盘/移动硬盘。本模块以「登记 VHD 路径 → Get-DiskImage 只读关联 →
磁盘号」作为唯一身份来源：只有映射到登记 VHD 的物理盘才放行，任何查询失败
一律拒绝（fail closed）。

登记来源：mk_vhd_script.py / mk_vhd_more.py 在 %TEMP% 下创建
x3wtest.vhd / x3wtest2.vhd / x3wtest3.vhd。这里沿用同一命名规则；
新增测试 VHD 时两处必须同步。

仅为测试台服务，不属于 tf_flasher 的运行依赖。
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

# 与 mk_vhd_script.py / mk_vhd_more.py 的 VHD 文件名保持一致。
VHD_NAMES = ("x3wtest.vhd", "x3wtest2.vhd", "x3wtest3.vhd")


class VhdGuardError(RuntimeError):
    """目标盘无法证明是登记测试 VHD；调用方必须拒绝写入。"""


def registered_vhds() -> list[Path]:
    """本项目登记（由 mk_vhd_*.py 创建）的测试 VHD 路径列表。"""
    base = Path(tempfile.gettempdir())
    return [base / name for name in VHD_NAMES]


def _ps_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _query_script(paths: list[Path]) -> str:
    """生成只读查询脚本：对每个登记路径输出 OK/ERR 行（TAB 分隔）。"""
    lines = [
        "try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}",
        "$ErrorActionPreference = 'Stop'",
    ]
    for p in paths:
        q = _ps_quote(str(p))
        lines.append(f"$p = {q}")
        lines.append("if (Test-Path -LiteralPath $p) {")
        lines.append("  try {")
        lines.append("    $img = Get-DiskImage -ImagePath $p -ErrorAction Stop")
        lines.append("    if ($img.Attached) {")
        lines.append("      $n = ($img | Get-Disk -ErrorAction Stop).Number")
        lines.append('      Write-Output ("OK`t" + $p + "`t" + $n)')
        lines.append("    }")
        lines.append("  } catch {")
        lines.append('    Write-Output ("ERR`t" + $p + "`t" + $_.Exception.Message)')
        lines.append("  }")
        lines.append("}")
    return "\n".join(lines)


def _run_powershell(script: str) -> tuple[int, str, str]:
    p = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    return p.returncode, p.stdout, p.stderr


def attached_vhd_map(paths: list[Path] | None = None, runner=None) -> dict[int, Path]:
    """只读查询：物理盘号 → 已挂载的登记 VHD 路径。

    paths / runner 仅供测试注入；生产调用使用登记路径与本机 PowerShell。
    任何一条登记 VHD 查询失败都会整体拒绝——无法证明环境一致时不写盘。
    """
    paths = [Path(p) for p in (paths if paths is not None else registered_vhds())]
    runner = runner or _run_powershell
    try:
        rc, out, err = runner(_query_script(paths))
    except Exception as e:  # noqa: BLE001 — 查询失败必须 fail closed
        raise VhdGuardError(f"无法查询 VHD 挂载关联: {e}") from e
    if rc != 0:
        raise VhdGuardError(
            f"查询 VHD 挂载关联失败 rc={rc}: {err.strip()[:300]}")
    mapping: dict[int, Path] = {}
    for raw in out.splitlines():
        line = raw.lstrip("\ufeff").strip()
        if not line:
            continue
        parts = line.split("\t")
        if parts[0] == "ERR":
            detail = parts[2] if len(parts) > 2 else line
            raise VhdGuardError(f"VHD 挂载关联查询失败: {detail}")
        if parts[0] == "OK" and len(parts) >= 3:
            try:
                num = int(parts[2].strip())
            except ValueError as e:
                raise VhdGuardError(f"VHD 关联输出无法解析: {line}") from e
            mapping[num] = Path(parts[1])
            continue
        raise VhdGuardError(f"VHD 关联输出无法识别: {line}")
    return mapping


def ensure_test_disk(disk_index: int, paths: list[Path] | None = None,
                     runner=None) -> Path:
    """确认 disk_index 是已登记的测试 VHD，返回其 VHD 路径；否则抛错。"""
    mapping = attached_vhd_map(paths, runner)
    path = mapping.get(int(disk_index))
    if path is None:
        known = ", ".join(f"PD{i}={p.name}" for i, p in sorted(mapping.items())) or "无"
        raise VhdGuardError(
            f"PhysicalDrive{disk_index} 不是已登记的测试 VHD"
            f"（当前已登记挂载: {known}）。"
            "E2E 写盘只允许 %TEMP%\\x3wtest*.vhd，拒绝写入。")
    return path


def select_test_disks(disks: list[dict], paths: list[Path] | None = None,
                      runner=None) -> list[dict]:
    """从枚举结果中只挑出映射到登记 VHD 的磁盘；无候选时拒绝而不是猜测。"""
    mapping = attached_vhd_map(paths, runner)
    picked = [d for d in disks if int(d["index"]) in mapping]
    if not picked:
        raise VhdGuardError(
            "未找到已挂载的登记测试 VHD（%TEMP%\\x3wtest*.vhd）；"
            "拒绝自动选择其他磁盘。请先按 AGENTS.md §7 创建并挂载。")
    return sorted(picked, key=lambda d: d["index"])
