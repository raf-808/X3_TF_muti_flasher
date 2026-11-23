# -*- coding: utf-8 -*-
"""纯模拟测试：vhd_guard 的拒绝路径与两个 E2E 脚本的接线。

不访问任何真实磁盘/VHD；PowerShell 查询通过注入的 runner 模拟。
运行: python tests\\test_vhd_guard.py -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "flasher_build"))

import vhd_guard  # noqa: E402


def make_runner(lines=(), rc=0, stderr="", exc=None):
    calls = []

    def runner(script):
        calls.append(script)
        if exc is not None:
            raise exc
        return rc, "\n".join(lines), stderr

    runner.calls = calls
    return runner


class AttachedMapTest(unittest.TestCase):
    def test_maps_registered_attached_vhd(self):
        paths = [Path(r"C:\Temp\x3wtest.vhd"), Path(r"C:\Temp\x3wtest2.vhd")]
        runner = make_runner([r"OK	C:\Temp\x3wtest.vhd	3"])
        mapping = vhd_guard.attached_vhd_map(paths, runner)
        self.assertEqual(mapping, {3: paths[0]})
        self.assertEqual(len(runner.calls), 1)
        for p in paths:  # 查询脚本必须覆盖全部登记路径
            self.assertIn(str(p), runner.calls[0])

    def test_unattached_vhd_absent_from_map(self):
        paths = [Path(r"C:\Temp\x3wtest.vhd")]
        mapping = vhd_guard.attached_vhd_map(paths, make_runner([]))
        self.assertEqual(mapping, {})

    def test_query_error_fails_closed(self):
        with self.assertRaises(vhd_guard.VhdGuardError):
            vhd_guard.attached_vhd_map([Path(r"C:\Temp\x3wtest.vhd")],
                                       make_runner([], rc=1, stderr="Access denied"))

    def test_runner_exception_fails_closed(self):
        with self.assertRaises(vhd_guard.VhdGuardError):
            vhd_guard.attached_vhd_map([Path(r"C:\Temp\x3wtest.vhd")],
                                       make_runner(exc=OSError("powershell 缺失")))

    def test_err_line_fails_closed(self):
        with self.assertRaises(vhd_guard.VhdGuardError):
            vhd_guard.attached_vhd_map(
                [Path(r"C:\Temp\x3wtest.vhd")],
                make_runner([r"ERR	C:\Temp\x3wtest.vhd	The file is not a virtual disk"]))

    def test_malformed_number_fails_closed(self):
        with self.assertRaises(vhd_guard.VhdGuardError):
            vhd_guard.attached_vhd_map(
                [Path(r"C:\Temp\x3wtest.vhd")],
                make_runner([r"OK	C:\Temp\x3wtest.vhd	not-a-number"]))

    def test_unknown_output_fails_closed(self):
        with self.assertRaises(vhd_guard.VhdGuardError):
            vhd_guard.attached_vhd_map(
                [Path(r"C:\Temp\x3wtest.vhd")],
                make_runner(["some unexpected output"]))


class EnsureTestDiskTest(unittest.TestCase):
    def test_accepts_registered_disk(self):
        paths = [Path(r"C:\Temp\x3wtest.vhd")]
        got = vhd_guard.ensure_test_disk(
            3, paths, make_runner([r"OK	C:\Temp\x3wtest.vhd	3"]))
        self.assertEqual(got, paths[0])

    def test_rejects_unregistered_disk(self):
        paths = [Path(r"C:\Temp\x3wtest.vhd")]
        with self.assertRaises(vhd_guard.VhdGuardError):
            vhd_guard.ensure_test_disk(
                2, paths, make_runner([r"OK	C:\Temp\x3wtest.vhd	3"]))

    def test_rejects_when_nothing_attached(self):
        with self.assertRaises(vhd_guard.VhdGuardError):
            vhd_guard.ensure_test_disk(3, [Path(r"C:\Temp\x3wtest.vhd")],
                                       make_runner([]))


class SelectTestDisksTest(unittest.TestCase):
    def test_only_registered_disks_are_candidates(self):
        paths = [Path(r"C:\Temp\x3wtest.vhd")]
        disks = [
            {"index": 3, "bus": 15, "size": 64 << 20},   # 登记 VHD
            {"index": 4, "bus": 15, "size": 64 << 20},   # 未登记的 bus=15 磁盘
            {"index": 5, "bus": 7, "size": 16 << 30},    # 普通 U 盘
        ]
        picked = vhd_guard.select_test_disks(
            disks, paths, make_runner([r"OK	C:\Temp\x3wtest.vhd	3"]))
        self.assertEqual([d["index"] for d in picked], [3])

    def test_no_candidates_refuses(self):
        with self.assertRaises(vhd_guard.VhdGuardError):
            vhd_guard.select_test_disks(
                [{"index": 4, "bus": 15, "size": 64 << 20}],
                [Path(r"C:\Temp\x3wtest.vhd")], make_runner([]))

    def test_attached_disk_missing_from_enumeration_refuses(self):
        with self.assertRaises(vhd_guard.VhdGuardError):
            vhd_guard.select_test_disks(
                [{"index": 9, "bus": 15, "size": 64 << 20}],
                [Path(r"C:\Temp\x3wtest.vhd")],
                make_runner([r"OK	C:\Temp\x3wtest.vhd	3"]))


def _fake_disks():
    return [{"index": 7, "bus": 15, "size": 64 << 20, "sector": 512,
             "model": "test", "letters": [], "removable": True}]


class E2EWiringTest(unittest.TestCase):
    def test_explicit_unregistered_disk_refuses_before_flash(self):
        import _test_write_e2e as e2e
        with mock.patch.object(e2e.t, "enumerate_disks",
                               return_value=(_fake_disks(), 0)), \
             mock.patch.object(e2e.vhd_guard, "ensure_test_disk",
                               side_effect=vhd_guard.VhdGuardError("未登记")), \
             mock.patch.object(e2e, "build_image") as build, \
             mock.patch.object(sys, "argv", ["_test_write_e2e.py", "7"]):
            self.assertEqual(e2e.main(), 2)
            build.assert_not_called()

    def test_auto_selection_refuses_without_registered_vhd(self):
        import _test_write_e2e as e2e
        with mock.patch.object(e2e.t, "enumerate_disks",
                               return_value=(_fake_disks(), 0)), \
             mock.patch.object(e2e.vhd_guard, "select_test_disks",
                               side_effect=vhd_guard.VhdGuardError("无候选")), \
             mock.patch.object(e2e, "build_image") as build, \
             mock.patch.object(sys, "argv", ["_test_write_e2e.py"]):
            self.assertEqual(e2e.main(), 2)
            build.assert_not_called()


class MultiWiringTest(unittest.TestCase):
    def test_refuses_before_flash(self):
        import _test_write_multi as multi
        with mock.patch.object(multi.t, "enumerate_disks",
                               return_value=(_fake_disks(), 0)), \
             mock.patch.object(multi.vhd_guard, "select_test_disks",
                               side_effect=vhd_guard.VhdGuardError("无候选")), \
             mock.patch.object(multi.t, "flash_one") as flash:
            self.assertEqual(multi.main(), 2)
            flash.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
