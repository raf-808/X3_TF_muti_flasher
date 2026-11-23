# -*- coding: utf-8 -*-
"""S4 镜像门禁测试：只接受结构有效的整盘 MBR/GPT 镜像，预检先于写盘。

运行: python tests\\test_image_preflight.py -v
"""
import gzip
import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
import zlib
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tf_flasher as t  # noqa: E402


def build_mbr(size=2 << 20, start_lba=2048):
    img = bytearray(size)
    e = bytearray(16)
    e[4] = 0x83
    e[8:12] = start_lba.to_bytes(4, "little")
    e[12:16] = (size // 512 - start_lba).to_bytes(4, "little")
    img[446:462] = e
    img[510:512] = b"\x55\xaa"
    for i in range(512, size, 4096):
        img[i:i + 4] = b"DATA"
    return bytes(img)


def build_gpt(sector=512, size=4 << 20):
    img = bytearray(size)
    img[450] = 0xEE  # 保护 MBR 分区类型
    img[454:458] = (1).to_bytes(4, "little")
    img[458:462] = (0xFFFFFFFF).to_bytes(4, "little")
    img[510:512] = b"\x55\xaa"
    entries = bytearray(128 * 128)
    ent = bytearray(128)
    ent[0:16] = b"\x10" * 16
    ent[16:32] = b"\x20" * 16
    ent[32:40] = (34).to_bytes(8, "little")
    ent[40:48] = (size // sector - 34).to_bytes(8, "little")
    entries[:128] = ent
    arr_crc = zlib.crc32(bytes(entries)) & 0xFFFFFFFF
    hdr = bytearray(92)
    hdr[0:8] = b"EFI PART"
    hdr[8:12] = (0x00010000).to_bytes(4, "little")
    hdr[12:16] = (92).to_bytes(4, "little")
    hdr[24:32] = (1).to_bytes(8, "little")
    hdr[32:40] = (size // sector - 1).to_bytes(8, "little")
    hdr[40:48] = (34).to_bytes(8, "little")
    hdr[48:56] = (size // sector - 34).to_bytes(8, "little")
    hdr[56:72] = b"\x30" * 16
    hdr[72:80] = (2).to_bytes(8, "little")
    hdr[80:84] = (128).to_bytes(4, "little")
    hdr[84:88] = (128).to_bytes(4, "little")
    hdr[88:92] = arr_crc.to_bytes(4, "little")
    hdr[16:20] = b"\x00" * 4
    hdr[16:20] = (zlib.crc32(bytes(hdr)) & 0xFFFFFFFF).to_bytes(4, "little")
    img[sector:sector + 92] = hdr
    img[2 * sector:2 * sector + len(entries)] = entries
    return bytes(img)


def patch_gpt_crcs(img):
    data = bytearray(img)
    arr_off = 2 * 512
    arr = bytes(data[arr_off:arr_off + 128 * 128])
    data[512 + 88:512 + 92] = (zlib.crc32(arr) & 0xFFFFFFFF).to_bytes(4, "little")
    hdr = bytearray(data[512:512 + 92])
    hdr[16:20] = b"\x00" * 4
    data[512 + 16:512 + 20] = (zlib.crc32(bytes(hdr)) & 0xFFFFFFFF).to_bytes(4, "little")
    return bytes(data)


class ValidateLayoutTest(unittest.TestCase):
    def test_valid_mbr(self):
        img = build_mbr()
        self.assertEqual(t.validate_image_layout(img, len(img))["kind"], "MBR")

    def test_valid_gpt_512(self):
        img = build_gpt(512)
        self.assertEqual(t.validate_image_layout(img, len(img))["kind"], "GPT")

    def test_valid_gpt_4k(self):
        img = build_gpt(4096)
        self.assertEqual(t.validate_image_layout(img, len(img))["kind"], "GPT")

    def test_signature_only_rejected(self):
        head = bytearray(1024)
        head[510:512] = b"\x55\xaa"
        with self.assertRaises(t.ImageValidationError):
            t.validate_image_layout(bytes(head), 1024)

    def test_text_rejected(self):
        with self.assertRaises(t.ImageValidationError):
            t.validate_image_layout(b"hello" * 1024, 5120)

    def test_partition_out_of_range_rejected(self):
        img = bytearray(build_mbr())
        img[454:458] = (999999).to_bytes(4, "little")
        with self.assertRaises(t.ImageValidationError):
            t.validate_image_layout(bytes(img), len(img))

    def test_gpt_header_crc_rejected(self):
        img = bytearray(build_gpt(512))
        img[512 + 40] ^= 0xFF
        with self.assertRaises(t.ImageValidationError):
            t.validate_image_layout(bytes(img), len(img))

    def test_gpt_entries_crc_rejected(self):
        img = bytearray(build_gpt(512))
        img[1024] ^= 0xFF
        with self.assertRaises(t.ImageValidationError):
            t.validate_image_layout(bytes(img), len(img))

    def test_gpt_without_used_entries_rejected(self):
        img = bytearray(build_gpt(512))
        img[1024:1040] = b"\x00" * 16
        with self.assertRaises(t.ImageValidationError):
            t.validate_image_layout(patch_gpt_crcs(bytes(img)), len(img))

    def test_protective_mbr_without_gpt_rejected(self):
        img = bytearray(2 << 20)
        img[450] = 0xEE
        img[454:458] = (1).to_bytes(4, "little")
        img[510:512] = b"\x55\xaa"
        with self.assertRaises(t.ImageValidationError):
            t.validate_image_layout(bytes(img), len(img))

    def test_too_small_rejected(self):
        with self.assertRaises(t.ImageValidationError):
            t.validate_image_layout(b"\x55\xaa", 2)


class AnalyzeImageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, name, data):
        p = self.dir / name
        p.write_bytes(data)
        return p

    def test_raw_mbr_img(self):
        raw = build_mbr()
        p = self._write("x.img", raw)
        meta = t.analyze_image(str(p))
        self.assertEqual(meta["layout"], "MBR")
        self.assertEqual(meta["img_size"], len(raw))
        self.assertEqual(meta["img_sha256"], hashlib.sha256(raw).hexdigest())

    def test_gzip_mbr_img(self):
        raw = build_mbr()
        p = self._write("x.img.gz", gzip.compress(raw))
        meta = t.analyze_image(str(p))
        self.assertEqual(meta["layout"], "MBR")
        self.assertEqual(meta["img_size"], len(raw))
        self.assertEqual(meta["img_sha256"], hashlib.sha256(raw).hexdigest())

    def test_empty_file_rejected(self):
        p = self._write("empty.img", b"")
        with self.assertRaises(t.ImageValidationError):
            t.analyze_image(str(p))

    def test_text_file_rejected(self):
        p = self._write("SPEC.md", b"# not an image\n" * 100)
        with self.assertRaises(t.ImageValidationError):
            t.analyze_image(str(p))

    def test_zip_file_rejected(self):
        p = self._write("x.img", b"PK\x03\x04" + b"\x00" * 2000)
        with self.assertRaises(t.ImageValidationError):
            t.analyze_image(str(p))

    def test_fake_gz_rejected(self):
        p = self._write("fake.img.gz", b"not gzip data" * 100)
        with self.assertRaises(t.ImageValidationError):
            t.analyze_image(str(p))

    def test_truncated_gz_rejected(self):
        data = gzip.compress(build_mbr())
        p = self._write("cut.img.gz", data[:len(data) // 2])
        with self.assertRaises(t.ImageValidationError):
            t.analyze_image(str(p))

    def test_gz_crc_error_rejected(self):
        data = bytearray(gzip.compress(build_mbr()))
        data[-1] ^= 0xFF
        p = self._write("bad.img.gz", bytes(data))
        with self.assertRaises(t.ImageValidationError):
            t.analyze_image(str(p))

    def test_cache_hit_and_old_cache_upgrade(self):
        raw = build_mbr()
        p = self._write("x.img.gz", gzip.compress(raw))
        with mock.patch.object(t, "CACHE_DIR", self.dir / "cache"):
            first = t.analyze_image(str(p))
            self.assertFalse(first["cached"])
            second = t.analyze_image(str(p))
            self.assertTrue(second["cached"])
            self.assertEqual(second["layout"], "MBR")
            fp = self.dir / "cache" / f"{first['gz_sha256']}.json"
            rec = json.loads(fp.read_text(encoding="utf-8"))
            rec.pop("layout", None)
            rec.pop("layout_ok", None)
            fp.write_text(json.dumps(rec), encoding="utf-8")
            third = t.analyze_image(str(p))
            self.assertTrue(third["cached"])
            self.assertEqual(third["layout"], "MBR")
            self.assertTrue(json.loads(fp.read_text(encoding="utf-8"))["layout_ok"])

    def test_old_cache_cannot_bypass_validation(self):
        raw = b"not a disk image" * 1000
        p = self._write("text.img.gz", gzip.compress(raw))
        key = hashlib.sha256(p.read_bytes()).hexdigest()
        cache = self.dir / "cache"
        cache.mkdir()
        (cache / f"{key}.json").write_text(json.dumps(
            {"img_size": len(raw), "img_sha256": hashlib.sha256(raw).hexdigest()}),
            encoding="utf-8")
        with mock.patch.object(t, "CACHE_DIR", cache):
            with self.assertRaises(t.ImageValidationError):
                t.analyze_image(str(p))

    def test_cli_image_invalid_returns_2(self):
        p = self._write("bad.img", b"plain text" * 100)
        self.assertEqual(t.cli_image(str(p)), 2)


def _disk(index=3, size=64 << 20):
    return {"index": index, "size": size, "is_system": False, "volume_known": True,
            "removable": True, "media_removable": False, "bus": 7,
            "volumes": [], "letters": [], "model": "m", "bus_name": "USB",
            "sector": 512, "serial": ""}


class GuiPreflightGateTest(unittest.TestCase):
    def setUp(self):
        try:
            self.root = t.tk.Tk()
        except Exception:
            self.skipTest("无 tkinter/显示环境")
        self.root.withdraw()
        self.app = t.App(self.root)
        self.app.running = True
        self.app.pending = 0
        self.app.meta_box = {"meta": None, "error": None,
                             "ready": __import__("threading").Event()}
        self.app.meta_box["ready"].set()
        fd, self.img = tempfile.mkstemp(suffix=".img")
        os.close(fd)
        self.app._pending_img = self.img
        self.app._pending_verify = True

    def tearDown(self):
        self.root.destroy()
        try:
            os.unlink(self.img)
        except OSError:
            pass

    def test_preflight_success_starts_workers(self):
        self.app.meta = {"img_size": 1 << 20, "layout": "MBR", "layout_ok": True,
                         "cached": True, "path": self.img, "gz_size": 1, "gz_sha256": "x",
                         "img_sha256": "x"}
        self.app._pending_targets = [_disk()]
        with mock.patch.object(t, "enumerate_disks", return_value=([_disk()], 0)), \
             mock.patch.object(t, "source_disks", return_value=set()), \
             mock.patch.object(t, "flash_one") as flash:
            self.app._preflight_ready()
            for _ in range(50):
                if flash.called:
                    break
                time.sleep(0.02)
        self.assertTrue(flash.called)
        self.assertEqual(flash.call_args.args[0]["index"], 3)
        self.assertTrue(flash.call_args.args[2])  # verify 启动快照

    def test_preflight_capacity_failure_writes_nothing(self):
        self.app.meta = {"img_size": 1 << 30, "layout": "MBR", "layout_ok": True,
                         "cached": True, "path": self.img, "gz_size": 1, "gz_sha256": "x",
                         "img_sha256": "x"}
        self.app._pending_targets = [_disk()]
        with mock.patch.object(t, "enumerate_disks", return_value=([_disk()], 0)), \
             mock.patch.object(t, "source_disks", return_value=set()), \
             mock.patch.object(t, "flash_one") as flash, \
             mock.patch.object(t.messagebox, "showerror") as err:
            self.app._preflight_ready()
        flash.assert_not_called()
        err.assert_called()
        self.assertFalse(self.app.running)

    def test_preflight_invalid_meta_writes_nothing(self):
        self.app.meta = {"img_size": 1 << 20, "layout": "MBR", "layout_ok": False}
        self.app._pending_targets = [_disk()]
        with mock.patch.object(t, "flash_one") as flash, \
             mock.patch.object(t.messagebox, "showerror") as err:
            self.app._preflight_ready()
        flash.assert_not_called()
        err.assert_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
