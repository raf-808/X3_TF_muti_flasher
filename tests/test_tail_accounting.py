# -*- coding: utf-8 -*-
"""S5 尾部补零对账测试：逻辑字节/哈希不含补零，读回只校验原始长度（纯模拟）。

运行: python tests\\test_tail_accounting.py -v
"""
import hashlib
import queue
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tf_flasher as t  # noqa: E402


class FakeRaw:
    """替代 _RawDiskFile：记录写入内容，读回返回已写数据。"""

    def __init__(self, corrupt=False, short_read=False):
        self.data = bytearray()
        self.pos = 0
        self.flushed = False
        self.reads = 0
        self.bytes_read = 0
        self.corrupt = corrupt
        self.short_read = short_read

    def write(self, data) -> int:
        if isinstance(data, memoryview):
            data = data.tobytes()
        self.data.extend(data)
        return len(data)

    def read(self, size: int) -> bytes:
        self.reads += 1
        if self.short_read:
            return b""
        out = bytearray(self.data[self.pos:self.pos + size])
        if self.corrupt and out:
            out[0] ^= 0xFF
            self.corrupt = False
        self.pos += len(out)
        self.bytes_read += len(out)
        return bytes(out)

    def seek(self, pos: int) -> None:
        self.pos = pos

    def flush(self) -> None:
        self.flushed = True

    def close(self) -> None:
        pass


class AlignedRaw(FakeRaw):
    """模拟真实物理盘：ReadFile 长度必须是扇区整数倍（否则 ERROR_INVALID_PARAMETER）。"""

    def __init__(self, sector):
        super().__init__()
        self.sector = sector

    def read(self, size: int) -> bytes:
        if size % self.sector:
            raise OSError(f"ReadFile err=87（非扇区对齐读取 {size}B）")
        return super().read(size)


class TailAccountingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, raw, sector=512, verify=True, meta_sha=None, fake=None,
             offline=False):
        img = self.dir / "x.img"
        img.write_bytes(raw)
        fake = fake or FakeRaw()
        meta = {"img_size": len(raw),
                "img_sha256": meta_sha or hashlib.sha256(raw).hexdigest()}
        meta_box = {"meta": meta, "error": None, "ready": threading.Event()}
        meta_box["ready"].set()
        target = {"index": 3, "model": "vhd", "serial": "", "size": 64 << 20,
                  "letters": [], "sector": sector}
        q: queue.Queue = queue.Queue()
        with mock.patch.object(t, "lock_target_volumes", return_value=[]), \
             mock.patch.object(t, "_open", return_value=1), \
             mock.patch.object(t, "_set_disk_offline", return_value=offline), \
             mock.patch.object(t, "_RawDiskFile", lambda h: fake):
            res = t.flash_one(target, str(img), verify, threading.Event(), q, meta_box)
        return res, fake, list(q.queue)

    def test_unaligned_512_accounts_logical_bytes_only(self):
        raw = b"\x5a" * 4097
        res, fake, _msgs = self._run(raw, sector=512)
        self.assertTrue(res["ok"], res.get("error"))
        self.assertEqual(res["bytes"], 4097)
        self.assertEqual(res["written_bytes"], 4608)
        self.assertEqual(len(fake.data), 4608)
        self.assertEqual(fake.data[4097:], b"\x00" * 511)
        self.assertTrue(any("补零" in w for w in res["warns"]))

    def test_unaligned_4096_accounts_logical_bytes_only(self):
        raw = b"\x5a" * 4097
        res, fake, _msgs = self._run(raw, sector=4096)
        self.assertTrue(res["ok"], res.get("error"))
        self.assertEqual(res["bytes"], 4097)
        self.assertEqual(res["written_bytes"], 8192)
        self.assertEqual(fake.data[4097:], b"\x00" * 4095)

    def test_aligned_image_has_no_padding(self):
        raw = b"\x5a" * 4096
        res, fake, _msgs = self._run(raw)
        self.assertTrue(res["ok"], res.get("error"))
        self.assertEqual(res["bytes"], 4096)
        self.assertEqual(res["written_bytes"], 4096)
        self.assertEqual(len(fake.data), 4096)
        self.assertEqual(res["warns"], [])

    def test_verify_reads_original_length_only(self):
        raw = b"\x5a" * 4097
        res, fake, _msgs = self._run(raw, sector=4096, verify=True)
        self.assertTrue(res["ok"], res.get("error"))
        self.assertEqual(fake.bytes_read, 8192)  # 对齐读 4096+4096，仅前 4097 计入哈希

    def test_aligned_device_reads_succeed_for_unaligned_image(self):
        raw = b"\x5a" * 4097
        fake = AlignedRaw(512)
        res, _fake, _msgs = self._run(raw, sector=512, verify=True, fake=fake)
        self.assertTrue(res["ok"], res.get("error"))
        self.assertEqual(res["bytes"], 4097)
        self.assertEqual(res["written_bytes"], 4608)

    def test_verify_off_does_not_read_back(self):
        raw = b"\x5a" * 4097
        res, fake, _msgs = self._run(raw, verify=False)
        self.assertTrue(res["ok"], res.get("error"))
        self.assertEqual(fake.reads, 0)

    def test_source_hash_mismatch_still_fails(self):
        raw = b"\x5a" * 4097
        res, _fake, _msgs = self._run(raw, meta_sha="0" * 64)
        self.assertFalse(res["ok"])
        self.assertIn("sha256", res["error"])

    def test_corrupted_readback_fails(self):
        raw = b"\x5a" * 4097
        res, _fake, _msgs = self._run(raw, fake=FakeRaw(corrupt=True))
        self.assertFalse(res["ok"])
        self.assertIn("读回校验", res["error"])

    def test_short_readback_fails(self):
        raw = b"\x5a" * 4097
        res, _fake, _msgs = self._run(raw, fake=FakeRaw(short_read=True))
        self.assertFalse(res["ok"])
        self.assertIn("读回提前到 EOF", res["error"])

    def test_progress_never_exceeds_logical_total(self):
        raw = b"\x5a" * (t.CHUNK + 1)
        res, _fake, msgs = self._run(raw, sector=4096)
        self.assertTrue(res["ok"], res.get("error"))
        prog = [m for m in msgs if m[0] == "prog"]
        self.assertTrue(prog)
        for _kind, _idx, done, total, _speed in prog:
            self.assertEqual(total, len(raw))
            self.assertLessEqual(done, len(raw))

    def test_disk_offline_is_recorded(self):
        res, _fake, _msgs = self._run(b"\x5a" * 4097, offline=True)
        self.assertTrue(res["ok"], res.get("error"))
        self.assertTrue(res["disk_offline"])

    def test_cancel_before_write_reports_zero(self):
        raw = b"\x5a" * 4097
        img = self.dir / "x.img"
        img.write_bytes(raw)
        meta_box = {"meta": {"img_size": len(raw),
                             "img_sha256": hashlib.sha256(raw).hexdigest()},
                    "error": None, "ready": threading.Event()}
        meta_box["ready"].set()
        stop = threading.Event()
        stop.set()
        target = {"index": 3, "model": "vhd", "serial": "", "size": 64 << 20,
                  "letters": [], "sector": 512}
        with mock.patch.object(t, "lock_target_volumes", return_value=[]), \
             mock.patch.object(t, "_open", return_value=1), \
             mock.patch.object(t, "_RawDiskFile", lambda h: FakeRaw()):
            res = t.flash_one(target, str(img), True, stop,
                              queue.Queue(), meta_box)
        self.assertTrue(res["cancelled"])
        self.assertEqual(res["bytes"], 0)
        self.assertEqual(res["written_bytes"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
