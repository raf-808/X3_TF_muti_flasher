# -*- coding: utf-8 -*-
"""S6 verify 快照测试：校验选项启动即冻结，台账/摘要以逐盘实际执行结果为准。

运行: python tests\\test_verify_snapshot.py -v
"""
import hashlib
import os
import queue
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import tf_flasher as t  # noqa: E402
from test_tail_accounting import FakeRaw  # noqa: E402


def _disk(index=3, size=64 << 20):
    return {"index": index, "size": size, "is_system": False, "volume_known": True,
            "removable": True, "media_removable": False, "bus": 7,
            "volumes": [], "letters": [], "model": "m", "bus_name": "USB",
            "sector": 512, "serial": ""}


def _result(drive=3, ok=True, completed=False):
    return {"drive": drive, "model": "m", "ok": ok, "bytes": 1 << 20,
            "secs": 1.0, "cancelled": False, "error": None,
            "verify_requested": completed or True, "verify_completed": completed}


class FlashOneVerifyFieldsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, verify, corrupt=False):
        raw = b"\x5a" * 4097
        img = self.dir / "x.img"
        img.write_bytes(raw)
        meta_box = {"meta": {"img_size": len(raw),
                             "img_sha256": hashlib.sha256(raw).hexdigest()},
                    "error": None, "ready": threading.Event()}
        meta_box["ready"].set()
        target = {"index": 3, "model": "vhd", "serial": "", "size": 64 << 20,
                  "letters": [], "sector": 512}
        with mock.patch.object(t, "lock_target_volumes", return_value=[]), \
             mock.patch.object(t, "_open", return_value=1), \
             mock.patch.object(t, "_RawDiskFile", lambda h: FakeRaw(corrupt=corrupt)):
            return t.flash_one(target, str(img), verify, threading.Event(),
                               queue.Queue(), meta_box)

    def test_verified_success(self):
        res = self._run(True)
        self.assertTrue(res["ok"], res.get("error"))
        self.assertTrue(res["verify_requested"])
        self.assertTrue(res["verify_completed"])

    def test_unverified_success(self):
        res = self._run(False)
        self.assertTrue(res["ok"], res.get("error"))
        self.assertFalse(res["verify_requested"])
        self.assertFalse(res["verify_completed"])

    def test_verify_failure_not_completed(self):
        res = self._run(True, corrupt=True)
        self.assertFalse(res["ok"])
        self.assertTrue(res["verify_requested"])
        self.assertFalse(res["verify_completed"])


class GuiVerifySnapshotTest(unittest.TestCase):
    def setUp(self):
        try:
            self.root = t.tk.Tk()
        except Exception:
            self.skipTest("无 tkinter/显示环境")
        self.root.withdraw()
        self.app = t.App(self.root)
        self.app.meta_box = {"meta": {"img_size": 1 << 20}, "error": None,
                             "ready": threading.Event()}
        self.app.meta_box["ready"].set()
        self.app.var_path.set("x.img")

    def tearDown(self):
        self.root.destroy()

    def _finish_with(self, pending_verify, ui_verify, results):
        self.app.running = True
        self.app.finals = results
        self.app._pending_verify = pending_verify
        self.app.var_verify.set(ui_verify)
        with mock.patch.object(t, "write_run_log", return_value=Path("log.json")) as log, \
             mock.patch.object(t.messagebox, "showinfo") as info:
            self.app._finish()
        return log.call_args.args[2], info.call_args.args[1]

    def test_summary_and_log_follow_snapshot_not_ui(self):
        verify_arg, summary = self._finish_with(
            pending_verify=False, ui_verify=True, results=[_result(completed=False)])
        self.assertFalse(verify_arg)
        self.assertIn("未读回校验", summary)
        self.assertNotIn("校验通过", summary)

    def test_verified_result_reports_passed_even_if_ui_off(self):
        verify_arg, summary = self._finish_with(
            pending_verify=True, ui_verify=False, results=[_result(completed=True)])
        self.assertTrue(verify_arg)
        self.assertIn("校验通过", summary)

    def test_per_disk_status_is_independent(self):
        _verify_arg, summary = self._finish_with(
            pending_verify=True, ui_verify=True,
            results=[_result(drive=3, completed=True),
                     _result(drive=4, completed=False)])
        self.assertIn("PhysicalDrive3", summary)
        self.assertIn("校验通过", summary)
        self.assertIn("未读回校验", summary)

    def test_verify_checkbox_disabled_while_running(self):
        self.app._set_running(True)
        self.assertEqual(str(self.app.chk_verify.cget("state")), "disabled")
        self.app._set_running(False)
        self.assertEqual(str(self.app.chk_verify.cget("state")), "normal")

    def test_start_freezes_verify_value(self):
        fd, img = tempfile.mkstemp(suffix=".img")
        os.close(fd)
        try:
            self.app.var_path.set(img)
            self.app.var_verify.set(False)
            self.app.rows = {3: {"var": mock.Mock(get=lambda: True), "ok": True,
                                 "cb": mock.Mock(), "bar": mock.Mock(),
                                 "status": mock.Mock()}}
            with mock.patch.object(t, "is_admin", return_value=True), \
                 mock.patch.object(t, "enumerate_disks", return_value=([_disk()], 0)), \
                 mock.patch.object(t, "source_disks", return_value=set()), \
                 mock.patch.object(t.messagebox, "askyesno", return_value=True), \
                 mock.patch.object(t.App, "_run_analyzer") as analyzer:
                self.app.start()
            self.assertFalse(self.app._pending_verify)
            time.sleep(0.05)
            analyzer.assert_called()
        finally:
            try:
                os.unlink(img)
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
