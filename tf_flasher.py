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

写入流程：写入是**流式**的——边解压 gzip 边直写物理盘，不产生临时 .img；
解压大小/sha256 预扫描在开写后**后台并行**（GUI 选完镜像即可开写，不阻塞），
写完 EOF 后才用预扫描结果对账（预扫描远快于卡速，通常早已就绪）；
CLI --flash 仍是先解析后写（脚本语义，fail-fast）。

安全机制：
  1. 只允许勾选「可移除介质」磁盘；含系统卷（如 C:）的磁盘强制排除不可勾选；
     超过 64 GiB 的大磁盘干脆不枚举（MAX_TARGET_BYTES，防误选大硬盘）；
  2. 写入前二次确认，明列每个目标的盘号/容量/型号/盘符；
  3. 写入前对目标盘上所有卷 FSCTL_LOCK + DISMOUNT（失败则警示继续）；
  4. 写入流 sha256 写完与镜像解压流预期哈希比对（预扫描结果内容寻址缓存）；
  5. 默认写后整段读回再算一次 sha256（检验卡坏块/扩容假卡）；
  6. 容量保护：已知解压大小时目标卡过小直接拒绝；未知时写入中途报错兜底；
  7. 每次运行在 tools/flash_logs/ 落 JSON 台账（目标、哈希、速度、结果）。

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
import string
import subprocess
import sys
import threading
import time
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
MAX_TARGET_BYTES = 64 << 30              # 超过 64 GiB 的磁盘一律不枚举为写卡目标（防误选大硬盘）

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    _TK_OK = True
except Exception:  # GUI 非必需；CLI 模式不受影响
    _TK_OK = False


class Cancelled(Exception):
    """用户取消。"""


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


def _local_volume_letters() -> list:
    """返回本地卷盘符列表（仅对象管理器查询，绝不阻塞）。
    网络映射盘断网时打开 \\\\.\\X: 会阻塞 ~20s/个（SMB 超时），必须先过滤掉：
    只保留 QueryDosDeviceW 指向 \\Device\\Harddisk* 的盘符。"""
    out = []
    for L in string.ascii_uppercase:
        buf = ctypes.create_unicode_buffer(512)
        n = kernel32.QueryDosDeviceW(f"{L}:", buf, 512)
        if n and buf.value.startswith("\\Device\\Harddisk"):
            out.append(L)
    return out


def _volume_map() -> tuple[dict, set]:
    """盘符 → 物理盘号映射；(映射表, 系统卷所在物理盘号集合)。只碰本地卷。"""
    letters: dict[str, int] = {}
    for L in _local_volume_letters():
        h = _open(f"\\\\.\\{L}:", GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE)
        if h is None:
            continue
        num = STORAGE_DEVICE_NUMBER()
        if _ioctl(h, IOCTL_STORAGE_GET_DEVICE_NUMBER, None, num):
            letters[L] = int(num.DeviceNumber)
        kernel32.CloseHandle(h)
    by_disk: dict[int, list] = {}
    for L, dn in letters.items():
        by_disk.setdefault(dn, []).append(L)
    sys_disks = set()
    sysdrive = os.environ.get("SystemDrive", "C:").rstrip(":").upper()
    if sysdrive in letters:
        sys_disks.add(letters[sysdrive])
    return by_disk, sys_disks


def enumerate_disks() -> tuple[list, int]:
    """枚举物理磁盘。返回 (磁盘列表, 因权限无法访问的个数)。"""
    by_disk, sys_disks = _volume_map()
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
            "letters": sorted(by_disk.get(i, [])),
            "is_system": i in sys_disks,
        })
    return disks, denied


def selectable(d: dict) -> bool:
    """可写目标判定：有容量、非系统盘、可移除介质、≤64 GiB。
    可移除判定三选一：属性 RemovableMedia 位 / 几何查询 MediaType=RemovableMedia
    （部分 USB 读卡器固件把可移除位报成 0）/ SD/MMC 总线。"""
    return (
        0 < d["size"] <= MAX_TARGET_BYTES
        and not d["is_system"]
        and (d["removable"] or d.get("media_removable", False) or d["bus"] in (12, 13))
    )


def lock_volumes(letters: list) -> tuple[list, list]:
    """对目标盘所有卷尝试锁定+弹出（句柄保持打开以维持锁定）。
    返回 (成功锁定的句柄列表, 警告文案列表)。
    注意：打开卷必须带 FILE_SHARE_READ|WRITE——挂载中的卷用 share=0 打开会
    直接 err=32 sharing violation（即使管理员）；独占靠 FSCTL_LOCK 实现。"""
    held, warns = [], []
    for L in letters:
        h = _open(f"\\\\.\\{L}:", GENERIC_READ | GENERIC_WRITE,
                  FILE_SHARE_READ | FILE_SHARE_WRITE)
        if h is None:
            warns.append(f"卷 {L}: 打开失败 err={ctypes.get_last_error()}")
            continue
        locked = _ioctl(h, FSCTL_LOCK_VOLUME)
        dismounted = _ioctl(h, FSCTL_DISMOUNT_VOLUME)
        if dismounted:
            held.append(h)
            if not locked:
                warns.append(f"卷 {L}: 未能独占锁定（可能有程序占用），已弹出后继续")
        else:
            kernel32.CloseHandle(h)
            warns.append(f"卷 {L}: 锁定/弹出均失败 err={ctypes.get_last_error()}（继续原样写入）")
    return held, warns


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


def analyze_image(path: str, progress=None, cancel=None) -> dict:
    """解析镜像：返回压缩包/解压后大小与 sha256。缓存键 = 压缩包文件 sha256。"""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"文件不存在: {p}")
    if p.suffix.lower() == ".gz":
        gz_sha, gz_size = sha256_file(p, progress, cancel)
        cached = _cache_load(gz_sha)
        if cached:
            return {**cached, "gz_sha256": gz_sha, "gz_size": gz_size,
                    "cached": True, "path": str(p)}
        h = hashlib.sha256()
        n = 0
        with open(p, "rb") as raw, gzip.GzipFile(fileobj=raw) as g:
            while True:
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                d = g.read(CHUNK)
                if not d:
                    break
                h.update(d)
                n += len(d)
                if progress:
                    progress("dec", n, 0)
        meta = {"img_sha256": h.hexdigest(), "img_size": n}
        _cache_store(gz_sha, meta, p.name)
        return {**meta, "gz_sha256": gz_sha, "gz_size": gz_size,
                "cached": False, "path": str(p)}
    # 裸 .img：文件哈希即预期哈希
    sha, size = sha256_file(p, progress, cancel)
    return {"img_sha256": sha, "img_size": size, "gz_sha256": sha, "gz_size": size,
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
    """
    idx = target["index"]
    res = {
        "drive": idx, "model": target["model"], "serial": target.get("serial", ""),
        "size": target["size"], "letters": target.get("letters", []),
        "bytes": 0, "ok": False, "cancelled": False, "error": None, "warns": [],
    }
    f = None
    held = []
    t0 = time.time()
    try:
        q.put(("phase", idx, "锁定卷"))
        held, warns = lock_volumes(target.get("letters", []))
        for w in warns:
            res["warns"].append(w)
            q.put(("warn", idx, w))

        h = _open(f"\\\\.\\PhysicalDrive{idx}", GENERIC_READ | GENERIC_WRITE,
                  FILE_SHARE_READ | FILE_SHARE_WRITE)
        if h is None:
            err = ctypes.get_last_error()
            raise OSError(f"打开 \\\\.\\PhysicalDrive{idx} 失败 err={err}"
                          + ("（需要管理员权限）" if err == 5 else ""))
        f = _RawDiskFile(h)

        sector = target.get("sector") or 512
        sha = hashlib.sha256()
        n = 0
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
                    n += CHUNK
                    del buf[:CHUNK]
                    speed = n / max(time.time() - t0, 1e-6) / 1e6
                    q.put(("prog", idx, n, known_total(), speed))
            tail = bytes(buf)
            if tail:
                pad = (-len(tail)) % sector
                if pad:
                    res["warns"].append(f"镜像尾部非扇区对齐，补零 {pad}B")
                    tail += b"\x00" * pad
                _write_all(f, tail)
                sha.update(tail)
                n += len(tail)

        f.flush()  # FlushFileBuffers：直落盘（等效原 fsync）

        # 写到 EOF 了，此时才需要预扫描结果做对账（卡速远慢于解压，通常已就绪）
        meta_box["ready"].wait()
        meta = meta_box["meta"]
        if meta is None:
            raise OSError(f"镜像预扫描失败，无法对账: {meta_box['error']}")
        if n != meta["img_size"]:
            raise OSError(f"写入字节数不符: {n} != {meta['img_size']}"
                          "（若卡容量小于镜像会出现此错）")
        if sha.hexdigest() != meta["img_sha256"]:
            raise OSError("写入流 sha256 与镜像预期哈希不符")

        if verify:
            q.put(("phase", idx, "校验"))
            f.seek(0)
            vsha = hashlib.sha256()
            m = 0
            while m < n:
                if stop.is_set():
                    raise Cancelled()
                d = f.read(min(CHUNK, n - m))
                if not d:
                    raise OSError(f"读回提前到 EOF（offset {m}）——卡可能已掉线")
                vsha.update(d)
                m += len(d)
                speed = m / max(time.time() - t0, 1e-6) / 1e6
                q.put(("prog", idx, m, n, speed))
            if vsha.hexdigest() != meta["img_sha256"]:
                raise OSError("读回校验 sha256 不匹配——卡可能损坏或为扩容假卡，请换卡重试")

        f.close()
        f = None
        res.update(ok=True, bytes=n, secs=time.time() - t0)
        q.put(("final", idx, res))
        return res
    except Cancelled:
        res.update(cancelled=True, error="已取消", secs=time.time() - t0, bytes=n if f else 0)
    except Exception as e:  # noqa: BLE001 — 统一落台账
        res.update(error=str(e), secs=time.time() - t0)
    finally:
        if f is not None:
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

        root.title("X3 批量写卡工具（img.gz → 多 TF 读卡器）")
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
        ttk.Checkbutton(opt, text="写入后读回校验（整卡 sha256 比对，防坏块/扩容卡，推荐）",
                        variable=self.var_verify).pack(side="left")
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
        disks = [d for d in disks if d["size"] <= MAX_TARGET_BYTES]
        notes = []
        if denied:
            notes.append(f"⚠ {denied} 个磁盘因权限未能访问（需管理员）")
        if oversize:
            notes.append(f"{len(oversize)} 个磁盘超过 {_fmt_size(MAX_TARGET_BYTES)} 未枚举（容量上限保护）")
        self.lbl_denied.config(text="   ".join(notes))
        if not disks:
            self.lbl_empty = ttk.Label(self.list_frame,
                                       text="未枚举到磁盘——请插入 TF 读卡器后重试，"
                                            "或以管理员运行（写盘本身也必须管理员）。",
                                       foreground="#888")
            self.lbl_empty.pack(anchor="w", pady=8)
            return
        for d in disks:
            ok = selectable(d)
            tag = "PhysicalDrive%d" % d["index"]
            info = f"{_fmt_size(d['size'])}  {d['bus_name']}  {d['model']}"
            if d["letters"]:
                info += "  卷:" + ",".join(x + ":" for x in d["letters"])
            if d["is_system"]:
                info += "  ⛔系统盘"
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

    # ---- 镜像选择（选完即可开写；解析在开写时后台并行） ----
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
                 "写入是流式的（边解压边写），选好即可开始；解压大小/sha256 将在"
                 "开始写入时后台解析（有缓存则秒出），用于写后对账。",
            foreground="#333")
        self._on_toggle()

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
        by_idx = {d["index"]: d for d in disks}
        targets, rejects = [], []
        for i in picked:
            d = by_idx.get(i)
            if d is None:
                rejects.append(f"PhysicalDrive{i}: 已不在位（可能被拔出）")
            elif not selectable(d):
                rejects.append(f"PhysicalDrive{i}: 不满足可写条件"
                               f"（非可移除/系统盘/超 {_fmt_size(MAX_TARGET_BYTES)} 上限）")
            elif self.meta and d["size"] < self.meta["img_size"]:
                rejects.append(
                    f"PhysicalDrive{i}: 容量 {_fmt_size(d['size'])} < 镜像 "
                    f"{_fmt_size(self.meta['img_size'])}，装不下")
            else:
                targets.append(d)
        if rejects:
            messagebox.showerror("以下目标不可用，已中止", "\n".join(rejects))
            self.refresh()
            return

        if self.meta:
            img_txt = (f"解压后 {_fmt_size(self.meta['img_size'])}  "
                       f"sha256 {self.meta['img_sha256'][:16]}…")
        else:
            img_txt = ("解压大小/sha256 后台解析中（不阻塞写入；"
                       "卡容量若小于镜像，写入中途会报错）")
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
        self.pending = len(targets)
        self.finals = []
        self.rows_i = {d["index"]: d for d in targets}
        # 共享元数据盒：写入线程开写不等待，写完 EOF 后才等预扫描结果对账
        self.meta_box = {"meta": None, "error": None, "ready": threading.Event()}
        self._set_running(True)
        for i, _r in self.rows.items():
            if i not in self.rows_i:
                self.rows[i]["status"].config(text="未参与", fg="#aaa")
            else:
                self.rows[i]["bar"].config(mode="indeterminate", value=0)
                self.rows[i]["bar"].start(40)
                self.rows[i]["status"].config(text="写入中…", fg="#0a5bb5")
        threading.Thread(target=self._run_analyzer, args=(img_path, self.meta_box),
                         daemon=True).start()
        for d in targets:
            threading.Thread(
                target=flash_one,
                args=(d, img_path, self.var_verify.get(), self.stop, self.q, self.meta_box),
                daemon=True).start()

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
                         f"sha256 {m['img_sha256'][:16]}…  ({cache_tag})",
                    foreground="#333")
            else:
                self.meta = None
                self.lbl_img.config(text=text, foreground="#b3261e")
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
        log_path = write_run_log(self.var_path.get(), self.meta,
                                 self.var_verify.get(), results)
        lines = []
        for res in sorted(results, key=lambda x: x["drive"]):
            if res["ok"]:
                lines.append(f"✔ PhysicalDrive{res['drive']}  {res['model']}  "
                             f"写入+{'校验通过' if self.var_verify.get() else '完成'}  "
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

    meta = analyze_image(path, prog)
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
    meta = analyze_image(args.flash)
    print(f"解压后 {meta['img_size']:,} B ({_fmt_size(meta['img_size'])})  "
          f"sha256 {meta['img_sha256'][:16]}…")
    disks, _ = enumerate_disks()
    try:
        want = {int(x) for x in args.drives.replace(",", " ").split()}
    except ValueError:
        print("--drives 格式应为物理盘号列表，如 --drives 2,3（先 --list 查看）")
        return 2
    targets = [d for d in disks if d["index"] in want]
    bad = []
    for i in sorted(want - {d["index"] for d in targets}):
        bad.append(f"PhysicalDrive{i}: 不在位或不可访问")
    for d in targets:
        if not selectable(d):
            if d["size"] > MAX_TARGET_BYTES:
                bad.append(f"PhysicalDrive{d['index']}: 容量 {_fmt_size(d['size'])} "
                           f"超过 {_fmt_size(MAX_TARGET_BYTES)} 上限，拒绝写入")
            else:
                bad.append(f"PhysicalDrive{d['index']}: 非可移除介质/系统盘，拒绝写入")
        elif d["size"] < meta["img_size"]:
            bad.append(f"PhysicalDrive{d['index']}: 容量不足 "
                       f"{_fmt_size(d['size'])} < {_fmt_size(meta['img_size'])}")
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
