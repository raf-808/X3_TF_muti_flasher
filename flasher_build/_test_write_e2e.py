# -*- coding: utf-8 -*-
"""临时 E2E 写入测试：对挂载的 64MB 测试 VHD 跑真实 flash_one（写+fsync+读回校验）。

用法: python _test_write_e2e.py [PhysicalDriveIndex]
不带参数则只选已登记（mk_vhd_*.py 创建的 %TEMP%\\x3wtest*.vhd）且当前挂载的
磁盘；显式传入的盘号同样必须通过 vhd_guard 身份核验，否则在调用 flash_one
之前拒绝退出。
"""
import gzip
import hashlib
import queue
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tf_flasher as t  # noqa: E402
import vhd_guard  # noqa: E402

TEST = Path(__file__).resolve().parent / "test_img.gz"


def build_image(mib: int = 16) -> dict:
    """确定性 16MiB 整盘镜像（合法 MBR + 伪随机数据）→ gzip，返回预期 meta。"""
    total = mib << 20
    start_lba = 2048
    count = total // 512 - start_lba
    mbr = bytearray(512)
    entry = bytearray(16)              # 分区项 1：type=0x83，起点 1MiB，覆盖到镜像末尾
    entry[1:4] = b"\xfe\xff\xff"       # CHS 起始（占位，结构校验不依赖 CHS）
    entry[4] = 0x83
    entry[5:8] = b"\xfe\xff\xff"       # CHS 结束（占位）
    entry[8:12] = start_lba.to_bytes(4, "little")
    entry[12:16] = count.to_bytes(4, "little")
    mbr[446:462] = entry
    mbr[510:512] = b"\x55\xaa"
    block = bytes(range(256)) * 4096   # 1 MiB 确定性数据
    body = bytearray(block * mib)
    body[:512] = mbr
    raw = bytes(body)
    h = hashlib.sha256(raw)
    with gzip.open(TEST, "wb", compresslevel=6) as g:
        g.write(raw)
    return {"img_sha256": h.hexdigest(), "img_size": len(raw),
            "path": str(TEST)}


def main() -> int:
    idx = int(sys.argv[1]) if len(sys.argv) > 1 else -1
    disks, _ = t.enumerate_disks()
    if idx < 0:
        try:
            cand = vhd_guard.select_test_disks(disks)
        except vhd_guard.VhdGuardError as e:
            print(f"拒绝自动选盘: {e}")
            print("当前盘列表：")
            for d in disks:
                print("  PD%d %s bus=%d" % (d["index"], t._fmt_size(d["size"]), d["bus"]))
            return 2
        idx = cand[0]["index"]
    target = next((d for d in disks if d["index"] == idx), None)
    if target is None:
        print(f"PhysicalDrive{idx} 不在位")
        return 2
    try:
        vhd_path = vhd_guard.ensure_test_disk(idx)
    except vhd_guard.VhdGuardError as e:
        print(f"拒绝写入: {e}")
        return 2
    print(f"目标: PhysicalDrive{idx}  {t._fmt_size(target['size'])}  bus={target['bus']}  "
          f"sector={target.get('sector')}  VHD={vhd_path.name}", flush=True)

    meta = build_image()
    print(f"测试镜像: {meta['img_size']:,} B  sha256 {meta['img_sha256'][:16]}…", flush=True)
    meta_box = {"meta": meta, "error": None, "ready": threading.Event()}
    meta_box["ready"].set()

    q: queue.Queue = queue.Queue()
    stop = threading.Event()
    th = threading.Thread(target=t.flash_one,
                          args=(target, meta["path"], True, stop, q, meta_box))
    th.start()
    while th.is_alive() or not q.empty():
        try:
            msg = q.get(timeout=0.3)
        except queue.Empty:
            continue
        kind = msg[0]
        if kind == "phase":
            print(f"[PD{msg[1]}] {msg[2]}", flush=True)
        elif kind == "prog":
            _, i, done, total, speed = msg
            if total:
                print(f"[PD{i}] {done / total * 100:.1f}%  {speed:.0f} MB/s", flush=True)
        elif kind == "warn":
            print(f"[PD{msg[1]}] ⚠ {msg[2]}", flush=True)
        elif kind == "final":
            res = msg[2]
            tag = "✔ 成功" if res["ok"] else f"✖ 失败: {res.get('error')}"
            print(f"[PD{msg[1]}] {tag}  bytes={res['bytes']}  "
                  f"secs={res.get('secs', 0):.2f}s", flush=True)
            return 0 if res["ok"] else 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
