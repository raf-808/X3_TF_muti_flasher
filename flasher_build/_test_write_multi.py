# -*- coding: utf-8 -*-
"""临时 E2E 多路并发写入测试：对所有测试 VHD（bus=15 且 <=128MiB）并行 flash_one。"""
import queue
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # tools/
sys.path.insert(0, str(Path(__file__).resolve().parent))         # flasher_build/
import tf_flasher as t  # noqa: E402
from _test_write_e2e import build_image  # noqa: E402


def main() -> int:
    disks, _ = t.enumerate_disks()
    targets = [d for d in disks if d["bus"] == 15 and 0 < d["size"] <= (128 << 20)]
    if not targets:
        print("未找到测试 VHD")
        return 2
    meta = build_image()
    meta_box = {"meta": meta, "error": None, "ready": threading.Event()}
    meta_box["ready"].set()
    print(f"{len(targets)} 路并发：{[d['index'] for d in targets]}", flush=True)

    q: queue.Queue = queue.Queue()
    stop = threading.Event()
    threads = []
    for d in targets:
        th = threading.Thread(target=t.flash_one,
                              args=(d, meta["path"], True, stop, q, meta_box))
        th.start()
        threads.append(th)
    finals = 0
    ok = 0
    while finals < len(targets):
        try:
            msg = q.get(timeout=0.5)
        except queue.Empty:
            continue
        kind = msg[0]
        if kind == "phase":
            print(f"[PD{msg[1]}] {msg[2]}", flush=True)
        elif kind == "final":
            res = msg[2]
            finals += 1
            tag = "OK" if res["ok"] else f"FAIL: {res.get('error')}"
            if res["ok"]:
                ok += 1
            print(f"[PD{msg[1]}] {tag}  bytes={res['bytes']}  "
                  f"secs={res.get('secs', 0):.2f}s", flush=True)
    for th in threads:
        th.join()
    print(f"并发完成 {ok}/{len(targets)}", flush=True)
    return 0 if ok == len(targets) else 1


if __name__ == "__main__":
    sys.exit(main())
