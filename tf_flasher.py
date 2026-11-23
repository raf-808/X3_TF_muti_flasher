#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tf_flasher.py — Windows 批量写卡工具（img.gz / img → 多个 TF 读卡器）

X3 项目配套：把 release/ 下的整机镜像（如 X320261117OK.img.gz）批量烧进插在
本机的多个 TF 读卡器。tkinter 简易 GUI + 无 GUI 命令行模式，零第三方依赖。

用法（写物理磁盘需要管理员权限；GUI 内可一键提权重启）:
    python tf_flasher.py                        # GUI（默认）
    python tf_flasher.py --list                 # 枚举物理磁盘（只读安全）
    python tf_flasher.py --image <img.gz>       # 解析镜像（大小+sha256，带缓存）
    python tf_flasher.py --flash <img.gz> --drives 2,3 [--no-verify] [--yes]
    python tf_flasher.py --smoke                # GUI 构建自检（不出窗口）

双击启动：tools/tf_flasher.bat（自动挑带 tkinter 的解释器）。

写入流程：先完整预检镜像（gzip 完整性 / 解压大小 / sha256 / MBR 或 GPT 结构），
通过后才锁定卷并**流式**写入（边解压 gzip 边直写物理盘，不产生临时 .img）；
CLI --flash 同样先解析后写（fail-fast）。

安全机制：
  1. 只允许勾选「可移除介质」磁盘；含系统卷（如 C:）的磁盘强制排除不可勾选；
     超过 64 GiB 的大磁盘干脆不枚举（MAX_TARGET_BYTES，防误选大硬盘）；
  2. 镜像门禁：仅接受结构有效的整盘 MBR/GPT 镜像；空文件、文本/zip、伪 gzip、
     gzip CRC/截断、分区越界一律在写盘前拒绝；GUI 预检失败零写入；
  3. 源盘保护：镜像/程序所在物理盘（含目录挂载/多 extent 映射，网络路径除外）
     不得成为目标；映射不可靠时拒绝写入；
  4. 写入前完整枚举目标盘所有卷并逐个 FSCTL_LOCK + DISMOUNT；任一失败即拒写
     该盘；打开 PhysicalDrive 后还会 best-effort 置磁盘 offline 解除 extent 写保护；
  5. 写入流 sha256 与整盘镜像哈希对账；尾部补零只写设备，不计入逻辑字节/哈希；
  6. 默认写后只读回原始镜像长度并再次 sha256（检验卡坏块/扩容假卡）；
  7. verify 在任务启动时冻结，台账与完成摘要按逐盘实际执行状态记录；
  8. 每次运行在 flash_logs/ 落 JSON 台账（目标、哈希、速度、逐盘校验状态、结果）。

写完注意：Windows 弹「使用驱动器前需要格式化」一律点取消——那是卡的 ext4
分区 Windows 不认识，误格式化会毁掉刚烧好的镜像（详见 release/*校验清单.md）。
"""

from __future__ import annotations

import argparse
import ctypes
import gzip
import hashlib
import json
import os
import queue
import subprocess
import sys
import threading
import time
import zlib
from ctypes import wintypes
from pathlib import Path

# PyInstaller onefile 下 __file__ 指向 %TEMP% 解包目录（随用随删），
# 缓存/台账必须落在 exe 本体旁边；脚本运行时落在脚本旁边。
if getattr(sys, "frozen", False):
    SCRIPT_DIR = Path(sys.executable).resolve().parent
else:
    SCRIPT_DIR = Path(__file__).resolve().parent
CACHE_DIR = SCRIPT_DIR / "flash_cache"   # 镜像预扫描结果缓存（按压缩包 sha256 内容寻址）
LOG_DIR = SCRIPT_DIR / "flash_logs"      # 每次写入运行的 JSON 台账
CHUNK = 8 << 20                          # 8 MiB 流式块（扇区对齐的整数倍）
HEADER_BYTES = 2 << 20                   # 预检读取的镜像头部（覆盖 512B/4K 扇区 GPT 元数据）
MAX_TARGET_BYTES = 64 << 30              # 超过 64 GiB 的磁盘一律不枚举为写卡目标（防误选大硬盘）

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    _TK_OK = True
except Exception:  # GUI 非必需；CLI 模式不受影响
    _TK_OK = False


class Cancelled(Exception):
    """用户取消。"""


class VolumeSafetyError(RuntimeError):
    """卷枚举/锁定/弹出不可靠——目标盘必须拒写（不得写入任何字节）。"""


class ImageValidationError(ValueError):
    """输入不是可写入的完整磁盘镜像（MBR/GPT 结构或 gzip 完整性不合法）。"""


# ----------------------------------------------------------------------------
# Win32 原生层：物理磁盘枚举 / 卷锁定
# ----------------------------------------------------------------------------

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
]
kernel32.DeviceIoControl.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
    wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
]
kernel32.SetFilePointerEx.argtypes = [
    wintypes.HANDLE, wintypes.LARGE_INTEGER, ctypes.POINTER(wintypes.LARGE_INTEGER), wintypes.DWORD,
]
kernel32.FindFirstVolumeW.restype = wintypes.HANDLE
kernel32.FindFirstVolumeW.argtypes = [wintypes.LPWSTR, wintypes.DWORD]
kernel32.FindNextVolumeW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD]
kernel32.FindVolumeClose.argtypes = [wintypes.HANDLE]
kernel32.GetVolumePathNamesForVolumeNameW.argtypes = [
    wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
]
kernel32.GetVolumePathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
kernel32.GetVolumeNameForVolumeMountPointW.argtypes = [
    wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
]
kernel32.GetDriveTypeW.restype = wintypes.UINT
kernel32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
FILE_SHARE_READ = 0x1
FILE_SHARE_WRITE = 0x2
FILE_END = 2
INVALID_HANDLE = wintypes.HANDLE(-1).value

FSCTL_LOCK_VOLUME = 0x00090018
FSCTL_DISMOUNT_VOLUME = 0x00090020
IOCTL_STORAGE_GET_DEVICE_NUMBER = 0x002D1080
IOCTL_STORAGE_QUERY_PROPERTY = 0x002D1400
IOCTL_DISK_GET_DRIVE_GEOMETRY_EX = 0x000700A0
IOCTL_DISK_SET_DISK_ATTRIBUTES = 0x0007C0F4
IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS = 0x00560000

DISK_ATTRIBUTE_OFFLINE = 0x1

ERROR_INVALID_FUNCTION = 1
ERROR_NO_MORE_FILES = 18
ERROR_NOT_SUPPORTED = 50
ERROR_MORE_DATA = 234
DRIVE_REMOTE = 4

BUS_NAMES = {
    0: "未知", 1: "SCSI", 2: "ATAPI", 3: "ATA", 4: "1394", 6: "FC", 7: "USB",
    8: "RAID", 9: "iSCSI", 10: "SAS", 11: "SATA", 12: "SD", 13: "MMC",
    14: "虚拟", 15: "VHD", 17: "NVMe",
}


class STORAGE_DEVICE_DESCRIPTOR(ctypes.Structure):
    _fields_ = [
        ("Version", wintypes.DWORD), ("Size", wintypes.DWORD),
        ("DeviceType", wintypes.BYTE), ("RemovableMedia", wintypes.BYTE),
        ("CommandQueueing", wintypes.BYTE),
        ("VendorIdOffset", wintypes.DWORD), ("ProductIdOffset", wintypes.DWORD),
        ("ProductRevisionOffset", wintypes.DWORD), ("SerialNumberOffset", wintypes.DWORD),
        ("BusType", wintypes.DWORD), ("RawPropertiesLength", wintypes.DWORD),
        ("RawDeviceProperties", wintypes.BYTE * 1),
    ]


class DISK_GEOMETRY_EX(ctypes.Structure):
    _fields_ = [
        ("Cylinders", ctypes.c_longlong), ("MediaType", wintypes.DWORD),
        ("TracksPerCylinder", wintypes.DWORD), ("SectorsPerTrack", wintypes.DWORD),
        ("BytesPerSector", wintypes.DWORD), ("DiskSize", ctypes.c_longlong),
    ]


class STORAGE_DEVICE_NUMBER(ctypes.Structure):
    _fields_ = [
        ("DeviceType", wintypes.DWORD), ("DeviceNumber", wintypes.DWORD),
        ("PartitionNumber", wintypes.DWORD),
    ]


MAX_VOLUME_EXTENTS = 32


class DISK_EXTENT(ctypes.Structure):
    _fields_ = [
        ("DiskNumber", wintypes.DWORD),
        ("StartingOffset", ctypes.c_longlong),
        ("ExtentLength", ctypes.c_longlong),
    ]


class VOLUME_DISK_EXTENTS(ctypes.Structure):
    _fields_ = [
        ("NumberOfDiskExtents", wintypes.DWORD),
        ("Extents", DISK_EXTENT * MAX_VOLUME_EXTENTS),
    ]


class SET_DISK_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("Version", ctypes.c_ulonglong),
        ("Persist", wintypes.BOOLEAN),
        ("Reserved1", wintypes.BYTE * 3),
        ("Attributes", ctypes.c_ulonglong),
        ("AttributesMask", ctypes.c_ulonglong),
    ]


def _open(path: str, access: int, share: int):
    h = kernel32.CreateFileW(path, access, share, None, OPEN_EXISTING, 0, None)
    return None if (not h or h == INVALID_HANDLE) else int(h)


def _buf_size(b) -> int:
    if b is None:
        return 0
    if isinstance(b, (bytes, bytearray)):
        return len(b)
    return ctypes.sizeof(b)


def _ioctl(h: int, code: int, inbuf=None, outbuf=None) -> bool:
    """DeviceIoControl 包装；bytes/缓冲数组/结构体均可传（结构体自动 byref）。"""
    ret = wintypes.DWORD()
    in_size, out_size = _buf_size(inbuf), _buf_size(outbuf)
    if inbuf is not None and not isinstance(inbuf, (bytes, bytearray)):
        inbuf = ctypes.byref(inbuf)
    if outbuf is not None and not isinstance(outbuf, (bytes, bytearray, ctypes.Array)):
        outbuf = ctypes.byref(outbuf)
    ok = kernel32.DeviceIoControl(h, code, inbuf, in_size, outbuf, out_size,
                                  ctypes.byref(ret), None)
    return bool(ok)


def _desc_str(buf, offset) -> str:
    if not offset:
        return ""
    raw = bytes(buf)
    end = raw.find(b"\x00", offset)
    return raw[offset:end].decode("utf-8", "replace").strip()


def _query_property(h: int) -> dict:
    """查询 STORAGE_DEVICE_DESCRIPTOR：总线类型 / 可移除位 / 厂商型号序列号。
    注意：输入缓冲必须 ≥12 字节（x64 下 STORAGE_PROPERTY_QUERY 对齐尺寸），
    传 8 字节会被内核以 ERROR_BAD_LENGTH(24) 拒绝。"""
    out = ctypes.create_string_buffer(4096)
    if not _ioctl(h, IOCTL_STORAGE_QUERY_PROPERTY, b"\x00" * 12, out):
        return {}
    desc = ctypes.cast(out, ctypes.POINTER(STORAGE_DEVICE_DESCRIPTOR)).contents
    return {
        "bus": desc.BusType,
        "removable": bool(desc.RemovableMedia),
        "vendor": _desc_str(out, desc.VendorIdOffset),
        "product": _desc_str(out, desc.ProductIdOffset),
        "serial": _desc_str(out, desc.SerialNumberOffset),
    }


def _disk_size(h: int) -> int:
    geo = DISK_GEOMETRY_EX()
    if _ioctl(h, IOCTL_DISK_GET_DRIVE_GEOMETRY_EX, None, geo):
        if geo.DiskSize > 0:
            return int(geo.DiskSize)
        return int(geo.Cylinders * geo.TracksPerCylinder * geo.SectorsPerTrack * geo.BytesPerSector)
    cur = wintypes.LARGE_INTEGER()
    if kernel32.SetFilePointerEx(h, wintypes.LARGE_INTEGER(0), ctypes.byref(cur), FILE_END):
        return int(cur.value)
    return 0


def _find_volumes() -> list[str]:
    r"""枚举全部本地卷（\\?\Volume{GUID}\ 形式），包含无盘符/仅目录挂载的卷。

    走 FindFirstVolume/FindNextVolume，不碰网络映射盘（不会触发 SMB 超时）。"""
    buf = ctypes.create_unicode_buffer(261)
    h = kernel32.FindFirstVolumeW(buf, len(buf))
    if not h or h == INVALID_HANDLE:
        err = ctypes.get_last_error()
        if err == ERROR_NO_MORE_FILES:
            return []
        raise VolumeSafetyError(f"FindFirstVolumeW 失败 err={err}")
    names = []
    try:
        while True:
            names.append(buf.value)
            if not kernel32.FindNextVolumeW(h, buf, len(buf)):
                err = ctypes.get_last_error()
                if err == ERROR_NO_MORE_FILES:
                    break
                raise VolumeSafetyError(f"FindNextVolumeW 失败 err={err}")
    finally:
        kernel32.FindVolumeClose(h)
    return names


def _volume_mount_paths(volume: str) -> list[str]:
    """卷的全部挂载点（盘符根目录与目录挂载点）。"""
    size = 1024
    while True:
        buf = ctypes.create_unicode_buffer(size)
        need = wintypes.DWORD(0)
        if kernel32.GetVolumePathNamesForVolumeNameW(volume, buf, size, ctypes.byref(need)):
            raw = ctypes.wstring_at(buf, need.value)
            return [p for p in raw.split("\x00") if p]
        err = ctypes.get_last_error()
        if err == ERROR_MORE_DATA and need.value > size:
            size = int(need.value)
            continue
        raise VolumeSafetyError(f"查询卷 {volume} 挂载点失败 err={err}")


def _volume_extents(h: int, volume: str) -> list[tuple[int, int, int]] | None:
    """卷 → (物理盘号, 起始偏移, 长度) 列表；非磁盘卷（如光驱）返回 None。"""
    vde = VOLUME_DISK_EXTENTS()
    if not _ioctl(h, IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS, None, vde):
        err = ctypes.get_last_error()
        if err in (ERROR_INVALID_FUNCTION, ERROR_NOT_SUPPORTED):
            return None
        raise VolumeSafetyError(f"查询卷 {volume} 磁盘 extent 失败 err={err}")
    n = int(vde.NumberOfDiskExtents)
    if n <= 0 or n > MAX_VOLUME_EXTENTS:
        raise VolumeSafetyError(f"卷 {volume} extent 数量异常: {n}")
    return [(int(vde.Extents[i].DiskNumber),
             int(vde.Extents[i].StartingOffset),
             int(vde.Extents[i].ExtentLength)) for i in range(n)]


def _volume_info(name: str) -> dict | None:
    # 卷设备必须去掉尾部反斜杠打开（带尾杠会被当成根目录 → err=3）；
    # 访问权限用 0 即可查询 extent，非管理员也能安全枚举拓扑。
    h = _open(name.rstrip("\\"), 0, FILE_SHARE_READ | FILE_SHARE_WRITE)
    if h is None:
        raise VolumeSafetyError(f"打开卷 {name} 失败 err={ctypes.get_last_error()}")
    try:
        extents = _volume_extents(h, name)
        if extents is None:
            return None
        mounts = _volume_mount_paths(name)
    finally:
        kernel32.CloseHandle(h)
    letters = sorted({p[0].upper() for p in mounts
                      if len(p) == 3 and p[1] == ":" and p[2] == "\\" and p[0].isalpha()})
    return {"name": name, "mounts": mounts, "letters": letters,
            "disks": sorted({e[0] for e in extents}), "extents": extents}


def _map_volumes(infos: list[dict]) -> tuple[dict[int, list[dict]], set[int]]:
    """卷信息列表 → (物理盘号 → 卷列表, 系统卷所在盘号集合)。

    跨多个 extent（多个物理盘）的卷会归入每个相关盘，锁定/拒写时都覆盖。"""
    by_disk: dict[int, list[dict]] = {}
    for info in infos:
        for disk in info["disks"]:
            by_disk.setdefault(disk, []).append(info)
    sysdrive = os.environ.get("SystemDrive", "C:").rstrip(":").upper() + ":\\"
    sys_disks = {
        disk for disk, vols in by_disk.items()
        if any(str(p).upper() == sysdrive for v in vols for p in v["mounts"])
    }
    return by_disk, sys_disks


def scan_volumes() -> tuple[dict[int, list[dict]], set[int], str | None]:
    """完整枚举本地卷并按物理盘归档。

    返回 (盘号 → 卷信息列表, 系统卷所在盘号集合, 错误信息)。
    错误信息非 None 表示卷拓扑不可靠，任何写盘都必须拒绝——不能把枚举失败
    当成“空盘”。成功且某盘无任何卷，才是真正的空白盘。"""
    try:
        infos = []
        for name in _find_volumes():
            info = _volume_info(name)
            if info is not None:
                infos.append(info)
        by_disk, sys_disks = _map_volumes(infos)
        return by_disk, sys_disks, None
    except Exception as e:  # noqa: BLE001 — 统一转成“不可靠”信号
        return {}, set(), str(e)


def enumerate_disks() -> tuple[list, int]:
    """枚举物理磁盘。返回 (磁盘列表, 因权限无法访问的个数)。"""
    by_disk, sys_disks, vol_err = scan_volumes()
    disks, denied, miss = [], 0, 0
    for i in range(33):
        h = _open(f"\\\\.\\PhysicalDrive{i}", GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE)
        if h is None:
            err = ctypes.get_last_error()
            if err == 5:
                denied += 1
            miss += 1
            if miss >= 16:
                break
            continue
        miss = 0
        prop = _query_property(h)
        size = _disk_size(h)
        sector, media_removable = 512, False
        geo = DISK_GEOMETRY_EX()
        if _ioctl(h, IOCTL_DISK_GET_DRIVE_GEOMETRY_EX, None, geo):
            if geo.BytesPerSector:
                sector = int(geo.BytesPerSector)
            media_removable = int(geo.MediaType) == 11  # RemovableMedia
        kernel32.CloseHandle(h)
        model = " ".join(x for x in (prop.get("vendor", ""), prop.get("product", "")) if x) or "(未知型号)"
        disks.append({
            "index": i,
            "model": model[:48],
            "serial": prop.get("serial", ""),
            "bus": prop.get("bus", 0),
            "bus_name": BUS_NAMES.get(prop.get("bus", 0), str(prop.get("bus", 0))),
            "size": size,
            "sector": sector,
            "removable": prop.get("removable", False),
            "media_removable": media_removable,  # 几何查询的介质类型（部分 USB 读卡器可移除位=0）
            "letters": sorted({L for v in by_disk.get(i, []) for L in v["letters"]}),
            "is_system": i in sys_disks,
            "volumes": by_disk.get(i, []),
            "volume_known": vol_err is None,
        })
    return disks, denied


def selectable(d: dict) -> bool:
    """可写目标判定：有容量、非系统盘、可移除介质、≤64 GiB、卷拓扑可信。
    可移除判定三选一：属性 RemovableMedia 位 / 几何查询 MediaType=RemovableMedia
    （部分 USB 读卡器固件把可移除位报成 0）/ SD/MMC 总线。
    卷枚举失败时 volume_known=False，一律不可选（不能把枚举失败当空盘）。"""
    return (
        0 < d["size"] <= MAX_TARGET_BYTES
        and not d["is_system"]
        and d.get("volume_known", False)
        and (d["removable"] or d.get("media_removable", False) or d["bus"] in (12, 13))
    )


def _existing_ancestor(p: Path) -> Path:
    while not p.exists():
        if p.parent == p:
            break
        p = p.parent
    return p


def _volume_name_for_path(path: str) -> str | None:
    """本地路径 → 所在卷 GUID 名称；UNC/网络映射盘返回 None（不属于本地物理盘）。

    映射失败抛 VolumeSafetyError——调用方必须拒绝写盘，不能默认可写。"""
    raw = os.path.abspath(os.fspath(path))
    if raw.startswith("\\\\"):
        return None
    drive = os.path.splitdrive(raw)[0]
    if drive and kernel32.GetDriveTypeW(drive + "\\") == DRIVE_REMOTE:
        return None
    probe = _existing_ancestor(Path(raw))
    root = ctypes.create_unicode_buffer(261)
    if not kernel32.GetVolumePathNameW(str(probe), root, len(root)):
        raise VolumeSafetyError(
            f"无法解析 {path} 所在卷 err={ctypes.get_last_error()}")
    vol = ctypes.create_unicode_buffer(261)
    if not kernel32.GetVolumeNameForVolumeMountPointW(root.value, vol, len(vol)):
        raise VolumeSafetyError(
            f"无法解析 {path} 所在卷 GUID err={ctypes.get_last_error()}")
    return vol.value


def source_disks(paths: list[str], disks: list[dict]) -> set[int]:
    """返回镜像/工具路径所在的本地物理盘号集合（网络路径忽略）。

    disks 必须来自同一轮 enumerate_disks()。任何路径映射失败、卷拓扑不可靠或
    卷找不到归属盘，都抛 VolumeSafetyError——宁可拒写，不默认可写。"""
    names = {name for name in (_volume_name_for_path(p) for p in paths) if name}
    if not names:
        return set()
    wanted = {n.upper() for n in names}
    if any(not d.get("volume_known", False) for d in disks):
        raise VolumeSafetyError("存在卷拓扑不可靠的磁盘，无法排除源盘")
    known = {v["name"].upper() for d in disks for v in d.get("volumes", [])}
    missing = wanted - known
    if missing:
        raise VolumeSafetyError(f"无法在卷列表中定位: {', '.join(sorted(missing))}")
    return {
        d["index"] for d in disks
        if any(v["name"].upper() in wanted for v in d.get("volumes", []))
    }


def partition_targets(disks: list[dict], picked: list[int], forbidden: set[int],
                      img_size: int | None = None) -> tuple[list[dict], list[str]]:
    """GUI/CLI 共用的最终写盘准入：通过返回 targets，否则给出拒绝原因。"""
    by_idx = {d["index"]: d for d in disks}
    targets, rejects = [], []
    for i in picked:
        d = by_idx.get(i)
        if d is None:
            rejects.append(f"PhysicalDrive{i}: 已不在位（可能被拔出）")
        elif i in forbidden:
            rejects.append(f"PhysicalDrive{i}: 镜像或工具所在物理盘，禁止写入")
        elif not selectable(d):
            rejects.append(f"PhysicalDrive{i}: 不满足可写条件"
                           f"（非可移除/系统盘/卷枚举不可靠/超 "
                           f"{_fmt_size(MAX_TARGET_BYTES)} 上限）")
        elif img_size and d["size"] < img_size:
            rejects.append(f"PhysicalDrive{i}: 容量 {_fmt_size(d['size'])} < 镜像 "
                           f"{_fmt_size(img_size)}，装不下")
        else:
            targets.append(d)
    return targets, rejects


def lock_target_volumes(disk_index: int, volumes: list[dict] | None = None) -> list[int]:
    """锁定并弹出目标盘关联的全部卷；任何失败抛 VolumeSafetyError 并释放全部句柄。

    volumes 为 None 时现场重新枚举（GUI 列表可能已过期）。只有“枚举成功且该盘
    确实没有任何卷”的空白盘才允许继续；枚举失败不能当成空盘。
    打开卷必须带 FILE_SHARE_READ|WRITE——挂载中的卷 share=0 会直接 err=32
    sharing violation；独占靠 FSCTL_LOCK_VOLUME，而非 share 标志。"""
    if volumes is None:
        by_disk, _sys_disks, err = scan_volumes()
        if err:
            raise VolumeSafetyError(f"卷枚举失败，拒绝写入: {err}")
        volumes = by_disk.get(disk_index, [])
    held: list[int] = []
    try:
        for vol in volumes:
            name = vol["name"]
            h = _open(name.rstrip("\\"), GENERIC_READ | GENERIC_WRITE,
                      FILE_SHARE_READ | FILE_SHARE_WRITE)
            if h is None:
                raise VolumeSafetyError(
                    f"卷 {name} 打开失败 err={ctypes.get_last_error()}")
            held.append(h)
            if not _ioctl(h, FSCTL_LOCK_VOLUME):
                raise VolumeSafetyError(
                    f"卷 {name} 独占锁定失败 err={ctypes.get_last_error()}")
            if not _ioctl(h, FSCTL_DISMOUNT_VOLUME):
                raise VolumeSafetyError(
                    f"卷 {name} 弹出失败 err={ctypes.get_last_error()}")
        return held
    except Exception:
        for h in held:
            kernel32.CloseHandle(h)
        raise


def _set_disk_offline(h: int, offline: bool) -> bool:
    """best-effort 将物理盘置离线/恢复在线。

    锁定+弹出卷后，Windows 仍可能拒绝写入与已挂载卷 extent 重叠的区间
    （实测固定 VHD+NTFS 会 WriteFile err=5）。离线磁盘可解除该限制；
    该操作失败不改变“锁/弹出失败即拒写”的契约，仅影响能否写入。"""
    attrs = SET_DISK_ATTRIBUTES()
    attrs.Version = ctypes.sizeof(SET_DISK_ATTRIBUTES)
    attrs.Persist = False
    attrs.Attributes = DISK_ATTRIBUTE_OFFLINE if offline else 0
    attrs.AttributesMask = DISK_ATTRIBUTE_OFFLINE
    return _ioctl(h, IOCTL_DISK_SET_DISK_ATTRIBUTES, attrs, None)


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_elevated(extra_args: list | None = None) -> None:
    """以管理员重启自身（ShellExecute runas）。"""
    params = subprocess.list2cmdline(["-I", str(Path(__file__).resolve())] + (extra_args or []))
    ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)


# ----------------------------------------------------------------------------
# 镜像解析：大小 + sha256（预扫描，内容寻址缓存）
# ----------------------------------------------------------------------------

def _fmt_size(n: float) -> str:
    return f"{n / (1 << 30):.2f} GiB"


def _fmt_dur(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 60}m{sec % 60:02d}s" if sec >= 60 else f"{sec}s"


def sha256_file(path: Path, progress=None, cancel=None) -> tuple[str, int]:
    h = hashlib.sha256()
    total = path.stat().st_size
    done = 0
    with open(path, "rb") as f:
        while True:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            d = f.read(CHUNK)
            if not d:
                break
            h.update(d)
            done += len(d)
            if progress:
                progress("sha", done, total)
    return h.hexdigest(), total


def _crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def _read_head(path: Path, is_gz: bool) -> bytes:
    """读取镜像头部（用于结构校验）；gzip 只解压头部，不校验全文 CRC。"""
    try:
        if is_gz:
            with gzip.open(path, "rb") as f:
                return f.read(HEADER_BYTES)
        with open(path, "rb") as f:
            return f.read(HEADER_BYTES)
    except (gzip.BadGzipFile, EOFError) as e:
        raise ImageValidationError(f"gzip 镜像损坏或截断: {e}") from e


def _parse_mbr(head: bytes, img_size: int) -> tuple[list[tuple[int, int, int]], bool]:
    """解析 MBR 分区项；返回 (普通分区列表, 是否含 0xEE 保护项)。"""
    entries, protective = [], False
    for i in range(4):
        e = head[446 + i * 16: 462 + i * 16]
        if len(e) < 16:
            raise ImageValidationError("镜像头部不足 512 字节，无法解析 MBR")
        ptype = e[4]
        start = int.from_bytes(e[8:12], "little")
        count = int.from_bytes(e[12:16], "little")
        if ptype == 0 and start == 0 and count == 0:
            continue
        if ptype == 0xEE:
            protective = True
            continue
        if ptype == 0:
            raise ImageValidationError(f"MBR 分区 {i + 1} 类型为 0 但含非零起止")
        if start == 0 or count == 0:
            raise ImageValidationError(f"MBR 分区 {i + 1} 起始 LBA/长度非法")
        if (start + count) * 512 > img_size:
            raise ImageValidationError(f"MBR 分区 {i + 1} 超出镜像范围")
        entries.append((ptype, start, count))
    return entries, protective


def _validate_gpt(head: bytes, img_size: int, sector: int) -> dict:
    """校验 GPT 头/分区表 CRC 与分区范围（支持 512B/4K 逻辑扇区）。"""
    off = sector
    if len(head) < off + 92:
        raise ImageValidationError("镜像过小，不足以容纳 GPT 头")
    if head[off:off + 8] != b"EFI PART":
        raise ImageValidationError("GPT 签名缺失")
    hsize = int.from_bytes(head[off + 12:off + 16], "little")
    if not (92 <= hsize <= sector) or len(head) < off + hsize:
        raise ImageValidationError(f"GPT 头长度异常: {hsize}")
    hdr = bytearray(head[off:off + hsize])
    stored_crc = int.from_bytes(hdr[16:20], "little")
    hdr[16:20] = b"\x00" * 4
    if _crc32(bytes(hdr)) != stored_crc:
        raise ImageValidationError("GPT 头 CRC 不符（头部损坏）")
    my_lba = int.from_bytes(hdr[24:32], "little")
    if my_lba != 1:
        raise ImageValidationError(f"GPT 头 MyLBA={my_lba}，不是位于 LBA1")
    entry_lba = int.from_bytes(hdr[72:80], "little")
    num = int.from_bytes(hdr[80:84], "little")
    esize = int.from_bytes(hdr[84:88], "little")
    arr_crc = int.from_bytes(hdr[88:92], "little")
    if not (1 <= num <= 4096) or esize < 128 or esize % 128:
        raise ImageValidationError(f"GPT 分区表参数异常: entries={num} size={esize}")
    start = entry_lba * sector
    total = num * esize
    if entry_lba == 0 or start + total > img_size:
        raise ImageValidationError("GPT 分区表超出镜像范围")
    if start + total > len(head):
        raise ImageValidationError("GPT 分区表超出预检读取范围")
    arr = head[start:start + total]
    if _crc32(arr) != arr_crc:
        raise ImageValidationError("GPT 分区表 CRC 不符（分区表损坏）")
    used = 0
    disk_sectors = img_size // sector
    for i in range(num):
        e = arr[i * esize:(i + 1) * esize]
        if not any(e[:16]):
            continue
        first = int.from_bytes(e[32:40], "little")
        last = int.from_bytes(e[40:48], "little")
        if last < first or first < 2 or last >= disk_sectors:
            raise ImageValidationError(f"GPT 分区 {i + 1} 越界（{first}..{last}）")
        used += 1
    if used == 0:
        raise ImageValidationError("GPT 没有有效分区项")
    return {"kind": "GPT", "sector_size": sector, "partitions": used}


def validate_image_layout(head: bytes, img_size: int) -> dict:
    """完整磁盘镜像结构校验：必须有合法 MBR 或 GPT。

    纯 0x55AA、文本、zip、空文件、分区越界、GPT CRC 错误一律拒绝；
    仅支持整盘镜像（不支持单分区镜像/无分区表镜像）。"""
    if img_size < 512:
        raise ImageValidationError(f"镜像过小（{img_size} B），不是完整磁盘镜像")
    if len(head) < 512 or head[510:512] != b"\x55\xaa":
        raise ImageValidationError("不是完整磁盘镜像：缺少 MBR 引导签名 0x55AA")
    mbr_entries, protective = _parse_mbr(head, img_size)
    if protective:
        for sector in (512, 4096):
            if len(head) >= sector + 8 and head[sector:sector + 8] == b"EFI PART":
                return _validate_gpt(head, img_size, sector)
        raise ImageValidationError("存在 GPT 保护 MBR，但未找到有效 GPT 头")
    if not mbr_entries:
        raise ImageValidationError("MBR 中没有有效分区项")
    return {"kind": "MBR", "sector_size": 512, "partitions": len(mbr_entries)}


def analyze_image(path: str, progress=None, cancel=None) -> dict:
    """解析并验证镜像：返回压缩包/解压后大小、sha256 与分区布局（MBR/GPT）。

    缓存键 = 压缩包文件 sha256；旧缓存缺少结构验证结果时只补读头部复核，
    不会绕过新增门禁。"""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"文件不存在: {p}")
    if p.suffix.lower() == ".gz":
        gz_sha, gz_size = sha256_file(p, progress, cancel)
        cached = _cache_load(gz_sha)
        if cached:
            if not cached.get("layout_ok"):
                layout = validate_image_layout(
                    _read_head(p, True), int(cached.get("img_size") or 0))
                cached = {**cached, "layout": layout["kind"], "layout_ok": True}
                _cache_store(gz_sha, cached, p.name)
            return {**cached, "gz_sha256": gz_sha, "gz_size": gz_size,
                    "cached": True, "path": str(p)}
        h = hashlib.sha256()
        n = 0
        head = bytearray()
        try:
            with open(p, "rb") as raw:
                if raw.read(2) != b"\x1f\x8b":
                    raise ImageValidationError("扩展名为 .gz 但不是 gzip 数据（魔数不符）")
                raw.seek(0)
                with gzip.GzipFile(fileobj=raw) as g:
                    while True:
                        if cancel is not None and cancel.is_set():
                            raise Cancelled()
                        d = g.read(CHUNK)
                        if not d:
                            break
                        if len(head) < HEADER_BYTES:
                            head.extend(d[:HEADER_BYTES - len(head)])
                        h.update(d)
                        n += len(d)
                        if progress:
                            progress("dec", n, 0)
        except (gzip.BadGzipFile, EOFError) as e:
            raise ImageValidationError(f"gzip 镜像损坏或截断: {e}") from e
        layout = validate_image_layout(bytes(head), n)
        meta = {"img_sha256": h.hexdigest(), "img_size": n,
                "layout": layout["kind"], "layout_ok": True}
        _cache_store(gz_sha, meta, p.name)
        return {**meta, "gz_sha256": gz_sha, "gz_size": gz_size,
                "cached": False, "path": str(p)}
    # 裸 .img：文件哈希即预期哈希；结构校验不可跳过
    sha, size = sha256_file(p, progress, cancel)
    layout = validate_image_layout(_read_head(p, False), size)
    return {"img_sha256": sha, "img_size": size, "gz_sha256": sha, "gz_size": size,
            "layout": layout["kind"], "layout_ok": True,
            "cached": False, "path": str(p)}


def _cache_load(key: str) -> dict | None:
    try:
        fp = CACHE_DIR / f"{key}.json"
        if fp.is_file():
            return json.loads(fp.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def _cache_store(key: str, meta: dict, src_name: str) -> None:
    try:
        CACHE_DIR.mkdir(exist_ok=True)
        fp = CACHE_DIR / f"{key}.json"
        fp.write_text(json.dumps({**meta, "src": src_name}, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass  # 缓存失败不影响主流程，下次重扫


# ----------------------------------------------------------------------------
# 写入 worker（GUI / CLI 共用）
# ----------------------------------------------------------------------------

kernel32.WriteFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                               ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
kernel32.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                              ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
kernel32.FlushFileBuffers.argtypes = [wintypes.HANDLE]


class _RawDiskFile:
    """物理盘句柄的极薄封装：直接走 Win32 WriteFile/ReadFile，不经 CRT fd 层
    （open_osfhandle/fdopen 在个别运行环境会出现 Errno 9 EBADF，弃用）。
    write 总是整块写入（不足则循环），read 允许短读（调用方按量循环）。"""

    def __init__(self, h: int, iobuf: int = CHUNK):
        self.h = h
        self._iobuf = iobuf

    def write(self, data) -> int:
        if isinstance(data, memoryview):
            data = data.tobytes()  # LPVOID 只收 bytes/bytearray
        off, total = 0, len(data)
        while off < total:
            w = wintypes.DWORD(0)
            chunk = data[off:] if off else data  # bytes 可直接作 LPVOID（零拷贝）
            if not kernel32.WriteFile(self.h, chunk, len(chunk), ctypes.byref(w), None):
                raise OSError(f"WriteFile err={ctypes.get_last_error()}"
                              "（磁盘写入失败——卡可能已拔出/只读/容量到顶）")
            if w.value == 0:
                raise OSError("WriteFile 写入 0 字节——设备已无空间或掉线")
            off += w.value
        return off

    def read(self, size: int) -> bytes:
        parts, got = [], 0
        while got < size:
            want = min(size - got, self._iobuf)
            buf = ctypes.create_string_buffer(want)
            g = wintypes.DWORD(0)
            if not kernel32.ReadFile(self.h, buf, want, ctypes.byref(g), None):
                raise OSError(f"ReadFile err={ctypes.get_last_error()}"
                              "（读回失败——卡可能已掉线）")
            if g.value == 0:
                break  # EOF
            parts.append(buf.raw[:g.value])
            got += g.value
        return b"".join(parts)

    def seek(self, pos: int) -> None:
        cur = wintypes.LARGE_INTEGER()
        if not kernel32.SetFilePointerEx(self.h, wintypes.LARGE_INTEGER(pos),
                                         ctypes.byref(cur), 0):  # FILE_BEGIN
            raise OSError(f"SetFilePointerEx err={ctypes.get_last_error()}")

    def flush(self) -> None:
        if not kernel32.FlushFileBuffers(self.h):
            raise OSError(f"FlushFileBuffers err={ctypes.get_last_error()}")

    def close(self) -> None:
        if self.h is not None:
            kernel32.CloseHandle(self.h)
            self.h = None


def _write_all(f, data: bytes) -> None:
    mv = memoryview(data)
    while mv:
        k = f.write(mv)
        mv = mv[k:]


def flash_one(target: dict, img_path: str, verify: bool,
              stop: threading.Event, q: queue.Queue, meta_box: dict) -> dict:
    """向单个物理盘流式写入镜像（边解压边写）并（可选）读回校验。

    meta_box = {"meta": dict|None, "error": str|None, "ready": Event}：
    写入不等待预扫描——写到 EOF 后才等 ready（预扫描通常远早于写完），
    再对账字节数与 sha256。进度事件的 total 为 0 表示解压大小尚未可知。

    语义：逻辑字节数/sha256 只覆盖镜像原始内容；末尾不足一扇区的补零只用于
    介质 I/O，不参与对账，读回也只校验原始长度。结果 bytes=逻辑字节，
    written_bytes=实际写入设备字节（向上对齐）。"""
    idx = target["index"]
    res = {
        "drive": idx, "model": target["model"], "serial": target.get("serial", ""),
        "size": target["size"], "letters": target.get("letters", []),
        "bytes": 0, "written_bytes": 0, "ok": False, "cancelled": False,
        "error": None, "warns": [], "disk_offline": False,
        "verify_requested": bool(verify), "verify_completed": False,
    }
    f = None
    held = []
    offlined = False
    logical_n = 0   # 逻辑镜像字节（对账与结果 bytes）
    written_n = 0   # 实际写入设备字节（含尾部补零）
    t0 = time.time()
    try:
        q.put(("phase", idx, "锁定卷"))
        # 现场重新枚举卷并全量锁定/弹出；失败即抛错，绝不打开 PhysicalDrive 写句柄。
        held = lock_target_volumes(idx)

        h = _open(f"\\\\.\\PhysicalDrive{idx}", GENERIC_READ | GENERIC_WRITE,
                  FILE_SHARE_READ | FILE_SHARE_WRITE)
        if h is None:
            err = ctypes.get_last_error()
            raise OSError(f"打开 \\\\.\\PhysicalDrive{idx} 失败 err={err}"
                          + ("（需要管理员权限）" if err == 5 else ""))
        f = _RawDiskFile(h)
        # 卷已锁定/弹出；再 best-effort 离线磁盘，解除 Windows 对卷 extent 的写保护
        offlined = _set_disk_offline(h, True)
        res["disk_offline"] = offlined

        sector = target.get("sector") or 512
        sha = hashlib.sha256()
        buf = bytearray()
        q.put(("phase", idx, "写入"))

        def known_total() -> int:
            if meta_box["ready"].is_set() and meta_box["meta"]:
                return int(meta_box["meta"]["img_size"])
            return 0

        opener = gzip.open if img_path.lower().endswith(".gz") else open
        with opener(img_path, "rb") as src:
            while True:
                if stop.is_set():
                    raise Cancelled()
                d = src.read(CHUNK)
                if not d:
                    break
                buf.extend(d)
                while len(buf) >= CHUNK:
                    w = bytes(buf[:CHUNK])
                    _write_all(f, w)
                    sha.update(w)
                    logical_n += CHUNK
                    written_n += CHUNK
                    del buf[:CHUNK]
                    speed = logical_n / max(time.time() - t0, 1e-6) / 1e6
                    q.put(("prog", idx, logical_n, known_total(), speed))
            raw_tail = bytes(buf)
            if raw_tail:
                pad = (-len(raw_tail)) % sector
                if pad:
                    res["warns"].append(f"镜像尾部非扇区对齐，补零 {pad}B")
                # 补零只写设备，不进 sha/逻辑字节
                _write_all(f, raw_tail + b"\x00" * pad)
                sha.update(raw_tail)
                logical_n += len(raw_tail)
                written_n += len(raw_tail) + pad

        f.flush()  # FlushFileBuffers：直落盘（等效原 fsync）

        # 写到 EOF 了，此时才需要预扫描结果做对账（卡速远慢于解压，通常已就绪）
        meta_box["ready"].wait()
        meta = meta_box["meta"]
        if meta is None:
            raise OSError(f"镜像预扫描失败，无法对账: {meta_box['error']}")
        if logical_n != meta["img_size"]:
            raise OSError(f"写入字节数不符: {logical_n} != {meta['img_size']}"
                          "（若卡容量小于镜像会出现此错）")
        if sha.hexdigest() != meta["img_sha256"]:
            raise OSError("写入流 sha256 与镜像预期哈希不符")

        if verify:
            q.put(("phase", idx, "校验"))
            f.seek(0)
            vsha = hashlib.sha256()
            m = 0
            while m < logical_n:  # 只读回原始镜像区间，不校验尾部补零
                if stop.is_set():
                    raise Cancelled()
                want = min(CHUNK, logical_n - m)
                # 物理盘 ReadFile 要求长度为扇区整数倍；多读的补零字节丢弃
                aligned = (want + sector - 1) // sector * sector
                d = f.read(aligned)
                if not d:
                    raise OSError(f"读回提前到 EOF（offset {m}）——卡可能已掉线")
                use = d[:want]
                vsha.update(use)
                m += len(use)
                speed = m / max(time.time() - t0, 1e-6) / 1e6
                q.put(("prog", idx, m, logical_n, speed))
            if vsha.hexdigest() != meta["img_sha256"]:
                raise OSError("读回校验 sha256 不匹配——卡可能损坏或为扩容假卡，请换卡重试")
            res["verify_completed"] = True

        if offlined and not _set_disk_offline(h, False):
            res["warns"].append("磁盘恢复在线失败，可能需要重新插拔")
        f.close()
        f = None
        res.update(ok=True, bytes=logical_n, written_bytes=written_n,
                   secs=time.time() - t0)
        q.put(("final", idx, res))
        return res
    except Cancelled:
        res.update(cancelled=True, error="已取消", secs=time.time() - t0,
                   bytes=logical_n, written_bytes=written_n)
    except Exception as e:  # noqa: BLE001 — 统一落台账
        res.update(error=str(e), secs=time.time() - t0)
    finally:
        if f is not None:
            if offlined and not _set_disk_offline(f.h, False):
                res["warns"].append("磁盘恢复在线失败，可能需要重新插拔")
            try:
                f.close()
            except Exception:
                pass
        for h2 in held:
            kernel32.CloseHandle(h2)
    q.put(("final", idx, res))
    return res


def write_run_log(image: str, meta: dict | None, verify: bool, results: list) -> Path:
    LOG_DIR.mkdir(exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    fp = LOG_DIR / f"flash_{ts}.json"
    rec = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "image": image,
        "img_size": meta.get("img_size") if meta else None,
        "img_sha256": meta.get("img_sha256") if meta else None,
        "gz_sha256": meta.get("gz_sha256") if meta else None,
        "verify": verify,
        "admin": is_admin(),
        "python": sys.version.split()[0],
        "results": results,
    }
    fp.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    return fp


# ----------------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------------

class App:
    def __init__(self, root: "tk.Tk"):
        self.root = root
        self.q: queue.Queue = queue.Queue()
        self.meta: dict | None = None
        self.meta_box: dict | None = None
        self.running = False
        self.stop = threading.Event()
        self.pending = 0
        self.finals: list = []
        self.rows_i: dict = {}
        self.rows: dict[int, dict] = {}
        self._pending_verify = True   # 本任务启动时冻结的“读回校验”选项

        root.title("X3 批量写卡工具（img.gz → 多 TF 读卡器）")
        icon_file = Path(__file__).resolve().parent / "assets" / "tf_flasher.ico"
        if icon_file.is_file():
            try:
                root.iconbitmap(default=str(icon_file))
            except tk.TclError:
                pass  # 图标是可选资源，不影响写卡工具启动
        root.geometry("880x700")
        root.minsize(820, 600)
        main = ttk.Frame(root, padding=10)
        main.pack(fill="both", expand=True)

        # --- 镜像选择 ---
        top = ttk.Frame(main)
        top.pack(fill="x", pady=(0, 4))
        ttk.Label(top, text="镜像文件:").pack(side="left")
        self.var_path = tk.StringVar()
        ttk.Entry(top, textvariable=self.var_path, state="readonly").pack(
            side="left", fill="x", expand=True, padx=6)
        self.btn_browse = ttk.Button(top, text="浏览…", command=self.browse)
        self.btn_browse.pack(side="left")
        self.lbl_img = ttk.Label(main, text="未选择镜像（支持 .img.gz / .img）",
                                 foreground="#555", wraplength=840, justify="left")
        self.lbl_img.pack(fill="x", pady=(0, 8))

        # --- 目标磁盘列表 ---
        lf = ttk.LabelFrame(main, text="目标磁盘（仅可移除介质可勾选；系统盘强制排除）")
        lf.pack(fill="both", expand=True)
        bar = ttk.Frame(lf, padding=(6, 4))
        bar.pack(fill="x")
        self.btn_refresh = ttk.Button(bar, text="刷新列表", command=self.refresh)
        self.btn_refresh.pack(side="left")
        self.btn_all = ttk.Button(bar, text="全选可写", command=self.select_all)
        self.btn_all.pack(side="left", padx=6)
        self.lbl_denied = ttk.Label(bar, text="", foreground="#b26a00")
        self.lbl_denied.pack(side="right")

        self.list_frame = ttk.Frame(lf, padding=(6, 2))
        self.list_frame.pack(fill="both", expand=True)
        self.lbl_empty = ttk.Label(self.list_frame,
                                   text="（尚未枚举——请插入 TF 读卡器后点「刷新列表」）",
                                   foreground="#888")
        self.lbl_empty.pack(anchor="w", pady=8)

        # --- 选项 + 操作 ---
        opt = ttk.Frame(main)
        opt.pack(fill="x", pady=8)
        self.var_verify = tk.BooleanVar(value=True)
        self.chk_verify = ttk.Checkbutton(
            opt, text="写入后读回校验（整卡 sha256 比对，防坏块/扩容卡，推荐）",
            variable=self.var_verify)
        self.chk_verify.pack(side="left")
        self.btn_start = ttk.Button(opt, text="开始写入", command=self.start, state="disabled")
        self.btn_start.pack(side="right")
        self.btn_cancel = ttk.Button(opt, text="取消", command=self.cancel,
                                     state="disabled")
        self.btn_cancel.pack(side="right", padx=6)

        # --- 状态栏 ---
        self.lbl_status = ttk.Label(main, text="就绪。写物理盘需管理员权限；"
                                    "写完后 Windows 弹格式化提示一律点取消。",
                                    foreground="#555", wraplength=840, justify="left")
        self.lbl_status.pack(fill="x", pady=(4, 0))

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.refresh()
        self.root.after(120, self._poll)

    # ---- 磁盘列表 ----
    def refresh(self):
        for w in self.list_frame.winfo_children():
            w.destroy()
        self.rows.clear()
        disks, denied = enumerate_disks()
        oversize = [d for d in disks if d["size"] > MAX_TARGET_BYTES]
        notes = []
        if denied:
            notes.append(f"⚠ {denied} 个磁盘因权限未能访问（需管理员）")
        if oversize:
            notes.append(f"{len(oversize)} 个磁盘超过 {_fmt_size(MAX_TARGET_BYTES)} 未枚举（容量上限保护）")
        try:
            forbidden = source_disks(
                [p for p in (self.var_path.get(), str(SCRIPT_DIR)) if p], disks
            ) if disks else set()
            src_ok = True
        except VolumeSafetyError as e:
            forbidden, src_ok = set(), False
            notes.append(f"⚠ 无法判断源镜像/工具所在盘（禁止写入）: {e}")
        self.forbidden = forbidden
        self.src_ok = src_ok
        disks = [d for d in disks if d["size"] <= MAX_TARGET_BYTES]
        self.lbl_denied.config(text="   ".join(notes))
        if not disks:
            self.lbl_empty = ttk.Label(self.list_frame,
                                       text="未枚举到磁盘——请插入 TF 读卡器后重试，"
                                            "或以管理员运行（写盘本身也必须管理员）。",
                                       foreground="#888")
            self.lbl_empty.pack(anchor="w", pady=8)
            return
        for d in disks:
            ok = selectable(d) and src_ok and d["index"] not in forbidden
            tag = "PhysicalDrive%d" % d["index"]
            info = f"{_fmt_size(d['size'])}  {d['bus_name']}  {d['model']}"
            if d["letters"]:
                info += "  卷:" + ",".join(x + ":" for x in d["letters"])
            if d["is_system"]:
                info += "  ⛔系统盘"
            elif not d.get("volume_known", False):
                info += "  ⛔卷枚举不可靠"
            elif not src_ok:
                info += "  ⛔源盘判定失败"
            elif d["index"] in forbidden:
                info += "  ⛔镜像/工具所在盘"
            elif not ok:
                info += "  ⛔非可移除介质"
            elif not d["removable"]:
                info += "  ⚠可移除位=0（按介质类型判定）"
            row = ttk.Frame(self.list_frame)
            row.pack(fill="x", pady=2)
            var = tk.BooleanVar(value=False)
            cb = ttk.Checkbutton(row, text=tag, variable=var, width=16,
                                 command=self._on_toggle, state="normal" if ok else "disabled")
            cb.pack(side="left")
            ttk.Label(row, text=info, foreground="#333" if ok else "#999").pack(
                side="left", padx=4)
            bar_ = ttk.Progressbar(row, length=180, maximum=100, value=0)
            bar_.pack(side="right")
            st = tk.Label(row, text="待选择", fg="#888", width=26, anchor="e")
            st.pack(side="right", padx=6)
            self.rows[d["index"]] = {"var": var, "cb": cb, "bar": bar_, "status": st,
                                     "info": info, "ok": ok}
        self._on_toggle()

    def select_all(self):
        for r in self.rows.values():
            if r["ok"]:
                r["var"].set(True)
        self._on_toggle()

    def _selected(self) -> list:
        return [i for i, r in self.rows.items() if r["ok"] and r["var"].get()]

    def _on_toggle(self):
        n = len(self._selected())
        ready = bool(self.var_path.get()) and not self.running
        self.btn_start.config(state="normal" if (ready and n) else "disabled")

    # ---- 镜像选择（写入前先完整预检） ----
    def browse(self):
        if self.running:
            return
        path = filedialog.askopenfilename(
            title="选择镜像文件",
            filetypes=[("镜像文件", "*.img.gz *.gz *.img"), ("所有文件", "*.*")])
        if not path:
            return
        self.var_path.set(path)
        self.meta = None
        try:
            gz_size = os.path.getsize(path)
        except OSError:
            gz_size = 0
        self.lbl_img.config(
            text=f"镜像: {path}\n压缩包 {_fmt_size(gz_size)}  |  "
                 "开始写入前会先完整预检（gzip 完整性/大小/sha256/MBR 或 GPT 结构），"
                 "预检失败不会写入任何卡。",
            foreground="#333")
        self.refresh()  # 重新计算哪些盘是镜像/工具所在盘，必须禁用

    def _run_analyzer(self, path: str, meta_box: dict):
        """后台预扫描：算解压大小+sha256（带缓存），供写完后对账。"""
        q = self.q
        try:
            def prog(stage, done, total):
                if stage == "sha":
                    pct = f"{done / max(total, 1) * 100:.0f}%"
                    q.put(("ana", f"[后台解析] 计算压缩包 sha256… {pct}"))
                else:
                    q.put(("ana", f"[后台解析] 流式解压中… 已解压 {_fmt_size(done)}"))

            meta = analyze_image(path, prog, cancel=self.stop)  # 取消写入时联动停止
            meta_box["meta"] = meta
            self.meta = meta
            q.put(("ana_done", None))  # None = 成功
        except Cancelled:
            meta_box["error"] = "已取消"
            q.put(("ana_done", "解析已取消"))
        except Exception as e:  # noqa: BLE001
            meta_box["error"] = str(e)
            q.put(("ana_done", f"解析失败: {e}"))
        finally:
            meta_box["ready"].set()

    # ---- 开始写入 ----
    def start(self):
        if not self.var_path.get() or self.running:
            return
        if not is_admin():
            messagebox.showerror(
                "需要管理员权限",
                "写物理磁盘需要管理员权限，当前进程不是管理员。\n"
                "请重新启动本工具并在 UAC 提示中选「是」。")
            return
        img_path = self.var_path.get()
        if not os.path.isfile(img_path):
            messagebox.showerror("镜像不存在", img_path)
            return
        picked = self._selected()
        if not picked:
            messagebox.showwarning("未选择目标", "请先勾选要写入的读卡器。")
            return
        disks, _ = enumerate_disks()  # 重新枚举，防中途拔插
        forbidden: set[int] = set()
        if disks:
            try:
                forbidden = source_disks([img_path, str(SCRIPT_DIR)], disks)
            except VolumeSafetyError as e:
                messagebox.showerror(
                    "无法判断源盘，已中止",
                    f"{e}\n\n无法排除镜像/工具所在物理盘时禁止写入。")
                return
        targets, rejects = partition_targets(
            disks, picked, forbidden,
            self.meta["img_size"] if self.meta else None)
        if rejects:
            messagebox.showerror("以下目标不可用，已中止", "\n".join(rejects))
            self.refresh()
            return

        if self.meta:
            img_txt = (f"解压后 {_fmt_size(self.meta['img_size'])}  "
                       f"sha256 {self.meta['img_sha256'][:16]}…")
        else:
            img_txt = ("镜像尚未预检：确定后先完整预检（结构/gzip CRC/sha256），"
                       "通过后才开始写入；失败不会写入任何卡")
        tgt_txt = "\n".join(
            f"  PhysicalDrive{d['index']}  {_fmt_size(d['size'])}  {d['bus_name']}  "
            f"{d['model']}" + ("  卷:" + ",".join(x + ":" for x in d["letters"]) if d["letters"] else "")
            for d in targets)
        msg = (
            f"即将向 {len(targets)} 个目标写入，目标盘上数据将全部丢失！\n\n"
            f"{tgt_txt}\n\n"
            f"镜像: {Path(img_path).name}\n"
            f"{img_txt}\n"
            f"写后读回校验: {'开' if self.var_verify.get() else '关'}\n\n确定继续？")
        if not messagebox.askyesno("确认写入", msg, icon="warning"):
            return

        self.stop.clear()
        self.running = True
        self.pending = 0
        self.finals = []
        self.meta = None
        self.rows_i = {d["index"]: d for d in targets}
        self._pending_targets = targets
        self._pending_img = img_path
        self._pending_verify = self.var_verify.get()  # 启动快照：之后改 UI 不影响本任务
        # 预检必须先完成：写入线程在 _preflight_ready() 成功后才启动
        self.meta_box = {"meta": None, "error": None, "ready": threading.Event()}
        self._set_running(True)
        for i, _r in self.rows.items():
            if i not in self.rows_i:
                self.rows[i]["status"].config(text="未参与", fg="#aaa")
            else:
                self.rows[i]["bar"].config(mode="indeterminate", value=0)
                self.rows[i]["bar"].start(40)
                self.rows[i]["status"].config(text="预检中…", fg="#b26a00")
        threading.Thread(target=self._run_analyzer, args=(img_path, self.meta_box),
                         daemon=True).start()

    def _preflight_ready(self):
        """预检成功后才启动写入线程；此时再复核目标（预检可能耗时，盘可能被拔插）。"""
        if not self.running:
            return
        meta = self.meta
        if not meta or not meta.get("layout_ok"):
            self._abort_preflight("预检未产生有效的整盘镜像信息")
            return
        old_indices = [d["index"] for d in self._pending_targets]
        disks, _ = enumerate_disks()
        forbidden: set[int] = set()
        if disks:
            try:
                forbidden = source_disks([self._pending_img, str(SCRIPT_DIR)], disks)
            except VolumeSafetyError as e:
                self._abort_preflight(f"无法判断源盘: {e}")
                return
        targets, rejects = partition_targets(disks, old_indices, forbidden, meta["img_size"])
        if rejects:
            self._abort_preflight("预检后目标不可用，未写入任何卡：\n"
                                  + "\n".join(rejects))
            return
        self.pending = len(targets)
        self.rows_i = {d["index"]: d for d in targets}
        for i, r in self.rows.items():
            if i not in self.rows_i:
                r["status"].config(text="未参与", fg="#aaa")
            else:
                try:
                    r["bar"].stop()
                except Exception:
                    pass
                r["bar"].config(mode="indeterminate", value=0)
                r["bar"].start(40)
                r["status"].config(text="写入中…", fg="#0a5bb5")
        self.lbl_status.config(
            text=f"预检通过（{meta.get('layout', '?')}）。开始写入…",
            foreground="#0a5bb5")
        for d in targets:
            threading.Thread(
                target=flash_one,
                args=(d, self._pending_img, self._pending_verify,
                      self.stop, self.q, self.meta_box),
                daemon=True).start()

    def _abort_preflight(self, text: str):
        self.running = False
        self.pending = 0
        self._set_running(False)
        for r in self.rows.values():
            try:
                r["bar"].stop()
            except Exception:
                pass
            r["bar"].config(mode="determinate", value=0)
            r["status"].config(text="未开始", fg="#888")
        self.lbl_status.config(text=f"预检失败，未写入任何卡: {text}",
                               foreground="#b3261e")
        messagebox.showerror("镜像预检失败", f"{text}\n\n未向任何磁盘写入数据。")

    def cancel(self):
        if self.running:
            self.stop.set()
            self.lbl_status.config(text="取消中…（等待各写入线程退出）", foreground="#b26a00")

    def _set_running(self, running: bool):
        st = "disabled" if running else "normal"
        self.btn_refresh.config(state=st)
        self.btn_all.config(state=st)
        self.btn_browse.config(state=st)
        self.btn_cancel.config(state="normal" if running else "disabled")
        self.chk_verify.config(state=st)  # 运行期不可改；结果以启动快照/逐盘记录为准
        for r in self.rows.values():
            r["cb"].config(state="disabled" if running
                           else ("normal" if r["ok"] else "disabled"))
        self._on_toggle()

    # ---- 队列轮询 ----
    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                self._on_msg(msg)
        except queue.Empty:
            pass
        self.root.after(120, self._poll)

    def _on_msg(self, msg):
        kind = msg[0]
        if kind == "ana":
            self.lbl_img.config(text=msg[1], foreground="#b26a00")
        elif kind == "ana_done":
            text = msg[1]
            if text is None:
                m = self.meta
                cache_tag = "缓存命中" if m["cached"] else "新建缓存"
                self.lbl_img.config(
                    text=f"镜像: {m['path']}\n压缩包 {_fmt_size(m['gz_size'])}  "
                         f"sha256 {m['gz_sha256'][:16]}…  |  解压后 {_fmt_size(m['img_size'])}  "
                         f"sha256 {m['img_sha256'][:16]}…  布局 {m.get('layout', '?')}"
                         f"  ({cache_tag})",
                    foreground="#333")
                self._preflight_ready()
            else:
                self.meta = None
                self.lbl_img.config(text=text, foreground="#b3261e")
                if self.running:
                    self._abort_preflight(text)
        elif kind == "phase":
            _, idx, text = msg
            color = {"写入": "#0a5bb5", "校验": "#7a4fb5", "锁定卷": "#555"}.get(text, "#555")
            self.rows[idx]["status"].config(text=text, fg=color)
        elif kind == "prog":
            _, idx, done, total, speed = msg
            r = self.rows.get(idx)
            if r:
                if total:  # 预扫描已出解压大小 → 切百分比
                    if str(r["bar"].cget("mode")) == "indeterminate":
                        r["bar"].stop()
                        r["bar"].config(mode="determinate", value=0)
                    r["bar"].config(value=min(done / total * 100, 100))
                    r["status"].config(text=f"{done / total * 100:.1f}%  {speed:.0f} MB/s",
                                       fg="#0a5bb5")
                else:  # 大小未知 → 脉动条 + 已写入量
                    r["status"].config(text=f"已写 {_fmt_size(done)}  {speed:.0f} MB/s",
                                       fg="#0a5bb5")
        elif kind == "warn":
            _, idx, text = msg
            self.rows[idx]["status"].config(text=f"⚠ {text[:24]}", fg="#b26a00")
        elif kind == "final":
            _, idx, res = msg
            self.finals.append(res)
            r = self.rows.get(idx)
            if r:
                try:
                    r["bar"].stop()
                except Exception:
                    pass
                r["bar"].config(mode="determinate")
                if res["ok"]:
                    r["bar"].config(value=100)
                    r["status"].config(
                        text=f"✔ 成功 {_fmt_dur(res.get('secs', 0))}  "
                             f"{res['bytes'] / max(res.get('secs', 1), 1) / 1e6:.0f} MB/s",
                        fg="#0a7d32")
                elif res.get("cancelled"):
                    r["status"].config(text="✖ 已取消", fg="#b26a00")
                else:
                    r["status"].config(text=f"✖ 失败: {res.get('error', '')[:28]}", fg="#b3261e")
            self.pending -= 1
            if self.pending <= 0 and self.running:
                self._finish()

    def _finish(self):
        if self.meta_box and not self.meta_box["ready"].is_set():
            self.root.after(400, self._finish)  # 等后台解析收尾（极快失败场景兜底）
            return
        self.running = False
        self._set_running(False)
        results = self.finals
        ok_n = sum(1 for r in results if r["ok"])
        verify_snapshot = self._pending_verify
        log_path = write_run_log(self.var_path.get(), self.meta,
                                 verify_snapshot, results)
        lines = []
        for res in sorted(results, key=lambda x: x["drive"]):
            if res["ok"]:
                tag = ("校验通过" if res.get("verify_completed")
                       else "写入完成（未读回校验）")
                lines.append(f"✔ PhysicalDrive{res['drive']}  {res['model']}  "
                             f"写入+{tag}  "
                             f"{_fmt_dur(res.get('secs', 0))}  "
                             f"{res['bytes'] / max(res.get('secs', 1), 1) / 1e6:.0f} MB/s")
            elif res.get("cancelled"):
                lines.append(f"✖ PhysicalDrive{res['drive']}  已取消（卡内容不完整！）")
            else:
                lines.append(f"✖ PhysicalDrive{res['drive']}  失败: {res.get('error')}")
        summary = (f"完成：{ok_n}/{len(results)} 成功\n\n" + "\n".join(lines)
                   + f"\n\n台账已写入:\n{log_path}")
        self.lbl_status.config(
            text=f"本轮完成 {ok_n}/{len(results)}。台账: {log_path}。"
                 "如弹「需要格式化驱动器」一律点取消。",
            foreground="#0a7d32" if ok_n == len(results) else "#b3261e")
        messagebox.showinfo("写入完成", summary)

    # ---- 关闭 ----
    def on_close(self):
        if self.running:
            if not messagebox.askyesno("正在写入",
                                       "仍有写入在进行。确定退出？（已写入部分不完整）"):
                return
            self.stop.set()
        self.root.destroy()


def run_gui() -> int:
    if not _TK_OK:
        print("[tf_flasher] 当前 Python 没有 tkinter，无法打开 GUI。", file=sys.stderr)
        print("请用 tools/tf_flasher.bat 启动（自动挑带 tkinter 的解释器），"
              "或改用 CLI: --list / --image / --flash", file=sys.stderr)
        return 2
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    if not is_admin():
        if messagebox.askyesno(
                "需要管理员权限",
                "写物理磁盘需要管理员权限。\n现在以管理员身份重启本工具吗？\n"
                "（选「否」仅可浏览列表，不能写入）", icon="warning"):
            relaunch_elevated()
            root.destroy()
            return 0
    App(root)
    root.mainloop()
    return 0


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def cli_list() -> int:
    disks, denied = enumerate_disks()
    oversize = [d for d in disks if d["size"] > MAX_TARGET_BYTES]
    disks = [d for d in disks if d["size"] <= MAX_TARGET_BYTES]
    if not disks:
        if denied:
            print(f"未枚举到磁盘：{denied} 个磁盘访问被拒——请以管理员运行。")
        elif oversize:
            print(f"未枚举到磁盘：{len(oversize)} 个磁盘均超过 "
                  f"{_fmt_size(MAX_TARGET_BYTES)} 容量上限（仅枚举小容量可移除介质）。")
        return 1
    print(f"{'盘号':<16}{'容量':<14}{'总线':<7}{'可移除':<7}{'盘符':<12}{'可写':<5}型号")
    for d in disks:
        letters = ",".join(x + ":" for x in d["letters"]) or "-"
        ok = "✔" if selectable(d) else "✖"
        mark = "（系统盘）" if d["is_system"] else ""
        tag = "PhysicalDrive%d" % d["index"]
        print(f"{tag:<16}{_fmt_size(d['size']):<14}{d['bus_name']:<7}"
              f"{'是' if d['removable'] else '否':<7}{letters:<12}{ok:<4}{d['model']}{mark}")
    foot = []
    if denied:
        foot.append(f"{denied} 个磁盘因权限未能访问（写盘需管理员权限）")
    if oversize:
        foot.append(f"{len(oversize)} 个磁盘超过 {_fmt_size(MAX_TARGET_BYTES)} 未枚举（容量上限保护）")
    if foot:
        print("（" + "；".join(foot) + "）")
    return 0


def cli_image(path: str) -> int:
    t0 = time.time()
    last = {"t": 0.0}

    def prog(stage, done, total):
        now = time.time()
        if now - last["t"] < 0.5 and done < (total or 1):
            return
        last["t"] = now
        if stage == "sha":
            print(f"\r计算压缩包 sha256… {done / max(total, 1) * 100:5.1f}%", end="", flush=True)
        else:
            print(f"\r流式解压中… {_fmt_size(done)}", end="", flush=True)

    try:
        meta = analyze_image(path, prog)
    except (ImageValidationError, OSError) as e:
        print()
        print(f"镜像无效: {e}", file=sys.stderr)
        return 2
    print()
    print(f"镜像: {meta['path']}")
    print(f"压缩包: {meta['gz_size']:,} B ({_fmt_size(meta['gz_size'])})  "
          f"sha256 {meta['gz_sha256']}")
    print(f"解压后: {meta['img_size']:,} B ({_fmt_size(meta['img_size'])})  "
          f"sha256 {meta['img_sha256']}")
    print(f"缓存: {'命中' if meta['cached'] else '新建'}  用时 {_fmt_dur(time.time() - t0)}")
    return 0


def cli_flash(args) -> int:
    if not is_admin():
        print("[tf_flasher] 写物理磁盘需要管理员权限，尝试提权重启…（新窗口中继续）")
        relaunch_elevated(["--flash", args.flash, "--drives", args.drives]
                          + (["--no-verify"] if args.no_verify else [])
                          + (["--yes"] if args.yes else []))
        return 1
    print("解析镜像…")
    try:
        meta = analyze_image(args.flash)
    except (ImageValidationError, OSError) as e:
        print(f"镜像无效，已中止: {e}")
        return 2
    print(f"解压后 {meta['img_size']:,} B ({_fmt_size(meta['img_size'])})  "
          f"sha256 {meta['img_sha256'][:16]}…  布局 {meta.get('layout', '?')}")
    disks, _ = enumerate_disks()
    try:
        want = {int(x) for x in args.drives.replace(",", " ").split()}
    except ValueError:
        print("--drives 格式应为物理盘号列表，如 --drives 2,3（先 --list 查看）")
        return 2
    forbidden: set[int] = set()
    if disks:
        try:
            forbidden = source_disks([args.flash, str(SCRIPT_DIR)], disks)
        except VolumeSafetyError as e:
            print("目标不可用，已中止：")
            print(f"  无法判断源镜像/工具所在物理盘: {e}")
            return 1
    targets, bad = partition_targets(disks, sorted(want), forbidden, meta["img_size"])
    if bad:
        print("目标不可用，已中止：")
        print("\n".join("  " + b for b in bad))
        return 1
    print("目标:")
    for d in targets:
        letters = ",".join(x + ":" for x in d["letters"]) or "-"
        print(f"  PhysicalDrive{d['index']}  {_fmt_size(d['size'])}  {d['bus_name']}  "
              f"{d['model']}  {letters}")
    if not args.yes:
        ans = input(f"向以上 {len(targets)} 个目标写入，数据将全部丢失。输入 yes 继续: ")
        if ans.strip().lower() != "yes":
            print("已取消。")
            return 1
    verify = not args.no_verify
    q: queue.Queue = queue.Queue()
    stop = threading.Event()
    results = []
    phases: dict = {}
    # CLI 语义：先解析完再写（fail-fast），meta_box 预填就绪
    meta_box = {"meta": meta, "error": None, "ready": threading.Event()}
    meta_box["ready"].set()
    for d in targets:
        threading.Thread(target=flash_one,
                         args=(d, meta["path"], verify, stop, q, meta_box),
                         daemon=True).start()
    pending = len(targets)
    printed_mb = {d["index"]: 0 for d in targets}
    while pending:
        try:
            msg = q.get(timeout=0.5)
        except queue.Empty:
            continue
        kind = msg[0]
        if kind == "phase":
            _, idx, text = msg
            if phases.get(idx) != text:
                phases[idx] = text
                print(f"[PhysicalDrive{idx}] {text}")
        elif kind == "prog":
            _, idx, done, total, _speed = msg
            if done - printed_mb[idx] >= (1 << 30):  # 每 1 GiB 打一行
                printed_mb[idx] = done
                print(f"[PhysicalDrive{idx}] {done / total * 100:.1f}% "
                      f"({_fmt_size(done)})")
        elif kind == "warn":
            print(f"[PhysicalDrive{msg[1]}] ⚠ {msg[2]}")
        elif kind == "final":
            _, idx, res = msg
            results.append(res)
            pending -= 1
            if res["ok"]:
                print(f"[PhysicalDrive{idx}] ✔ 成功  "
                      f"{_fmt_dur(res.get('secs', 0))}  "
                      f"{res['bytes'] / max(res.get('secs', 1), 1) / 1e6:.0f} MB/s")
            elif res.get("cancelled"):
                print(f"[PhysicalDrive{idx}] ✖ 已取消（卡内容不完整！）")
            else:
                print(f"[PhysicalDrive{idx}] ✖ 失败: {res.get('error')}")
    ok_n = sum(1 for r in results if r["ok"])
    log_path = write_run_log(meta["path"], meta, verify, results)
    print(f"\n完成 {ok_n}/{len(results)}。台账: {log_path}")
    print("提醒：如 Windows 弹「需要格式化驱动器」，一律点取消。")
    return 0 if ok_n == len(results) else 1


def run_smoke() -> int:
    """GUI 构建自检：创建窗口（不显示）→ 枚举 → 销毁。"""
    if not _TK_OK:
        print("[tf_flasher] 当前 Python 没有 tkinter，smoke 需要带 tkinter 的解释器。",
              file=sys.stderr)
        return 2
    root = tk.Tk()
    root.withdraw()
    app = App(root)
    root.update()
    n = len(app.rows)
    root.destroy()
    print(f"SMOKE OK：GUI 构建成功，枚举到 {n} 个物理磁盘行。")
    return 0


def main(argv: list | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # Git Bash/GBK 控制台打不出 ✔/✖ 等字符
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="X3 批量写卡工具（img.gz → 多 TF 读卡器）")
    ap.add_argument("--list", action="store_true", help="枚举物理磁盘（只读安全）")
    ap.add_argument("--image", metavar="PATH", help="解析镜像（大小+sha256，带缓存）")
    ap.add_argument("--flash", metavar="PATH", help="无 GUI 批量写入（需管理员）")
    ap.add_argument("--drives", metavar="N,N", help="--flash 目标物理盘号，如 2,3")
    ap.add_argument("--no-verify", action="store_true", help="跳过写后读回校验")
    ap.add_argument("--yes", action="store_true", help="跳过 CLI 确认（谨慎）")
    ap.add_argument("--smoke", action="store_true", help="GUI 构建自检")
    args = ap.parse_args(argv)

    if os.name != "nt" and (args.flash or args.list):
        print("[tf_flasher] 物理盘写入/枚举仅支持 Windows。", file=sys.stderr)
        return 2
    if args.list:
        return cli_list()
    if args.image:
        return cli_image(args.image)
    if args.flash:
        if not args.drives:
            ap.error("--flash 需要配合 --drives N,N 指定目标物理盘号（先 --list 查看）")
        return cli_flash(args)
    if args.smoke:
        return run_smoke()
    return run_gui()


if __name__ == "__main__":
    sys.exit(main())
