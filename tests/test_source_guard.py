# -*- coding: utf-8 -*-
"""S3 源盘保护测试：镜像/工具所在物理盘绝不成为写入目标（纯模拟）。

运行: python tests\\test_source_guard.py -v
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tf_flasher as t  # noqa: E402

V1 = "\\\\?\\Volume{1111}\\"
V2 = "\\\\?\\Volume{2222}\\"


def vol(name, disks, mounts=None):
    return {"name": name, "disks": list(disks), "mounts": mounts or [],
            "letters": [], "extents": []}


def disk(index, volumes, size=64 << 20, **kw):
    d = {"index": index, "size": size, "is_system": False, "volume_known": True,
         "removable": True, "media_removable": False, "bus": 7,
         "volumes": volumes, "letters": [], "model": "m", "bus_name": "USB",
         "sector": 512, "serial": ""}
    d.update(kw)
    return d


class VolumeNameForPathTest(unittest.TestCase):
    def setUp(self):
        fd, self.tmp = tempfile.mkstemp(suffix=".img")
        os.close(fd)

    def tearDown(self):
        try:
            os.unlink(self.tmp)
        except OSError:
            pass

    def test_local_path_maps_to_volume_guid(self):
        with mock.patch.object(t, "kernel32") as k:
            k.GetDriveTypeW.return_value = 3  # DRIVE_FIXED

            def gvpn(path, buf, n):
                buf.value = "D:\\"
                return 1

            def gvn(mount, buf, n):
                buf.value = V1
                return 1

            k.GetVolumePathNameW.side_effect = gvpn
            k.GetVolumeNameForVolumeMountPointW.side_effect = gvn
            self.assertEqual(t._volume_name_for_path(self.tmp), V1)

    def test_unc_path_is_network(self):
        with mock.patch.object(t, "kernel32") as k:
            self.assertIsNone(t._volume_name_for_path(r"\\server\share\x.img"))
            k.GetVolumePathNameW.assert_not_called()

    def test_mapped_remote_drive_is_network(self):
        with mock.patch.object(t, "kernel32") as k:
            k.GetDriveTypeW.return_value = t.DRIVE_REMOTE
            self.assertIsNone(t._volume_name_for_path(self.tmp))
            k.GetVolumePathNameW.assert_not_called()

    def test_volume_path_failure_is_fail_closed(self):
        with mock.patch.object(t, "kernel32") as k:
            k.GetDriveTypeW.return_value = 3
            k.GetVolumePathNameW.return_value = 0
            with self.assertRaises(t.VolumeSafetyError):
                t._volume_name_for_path(self.tmp)

    def test_volume_guid_failure_is_fail_closed(self):
        with mock.patch.object(t, "kernel32") as k:
            k.GetDriveTypeW.return_value = 3

            def gvpn(path, buf, n):
                buf.value = "D:\\"
                return 1

            k.GetVolumePathNameW.side_effect = gvpn
            k.GetVolumeNameForVolumeMountPointW.return_value = 0
            with self.assertRaises(t.VolumeSafetyError):
                t._volume_name_for_path(self.tmp)


class SourceDisksTest(unittest.TestCase):
    def _disks(self):
        return [disk(3, [vol(V1, [3])]),
                disk(4, [vol(V2, [4, 5])]),
                disk(5, [vol(V2, [4, 5])])]

    def test_directory_or_multi_extent_volume_maps_all_disks(self):
        with mock.patch.object(t, "_volume_name_for_path",
                               side_effect=lambda p: {self.tmp_a: V1, self.tmp_b: V2}[p]):
            self.tmp_a, self.tmp_b = "a.img", "tool_dir"
            self.assertEqual(t.source_disks([self.tmp_a], self._disks()), {3})
            self.assertEqual(t.source_disks([self.tmp_b], self._disks()), {4, 5})
            self.assertEqual(t.source_disks([self.tmp_a, self.tmp_b], self._disks()),
                             {3, 4, 5})

    def test_network_source_maps_to_no_local_disk(self):
        with mock.patch.object(t, "_volume_name_for_path", return_value=None):
            self.assertEqual(t.source_disks(["\\\\server\\share\\x.img"], self._disks()),
                             set())

    def test_unknown_volume_is_fail_closed(self):
        with mock.patch.object(t, "_volume_name_for_path", return_value=V2):
            with self.assertRaises(t.VolumeSafetyError):
                t.source_disks(["x"], self._disks()[:1])

    def test_unreliable_volume_topology_is_fail_closed(self):
        with mock.patch.object(t, "_volume_name_for_path", return_value=V1):
            with self.assertRaises(t.VolumeSafetyError):
                t.source_disks(["x"], [disk(3, [vol(V1, [3])], volume_known=False)])


class PartitionTargetsTest(unittest.TestCase):
    def test_forbidden_disk_rejected_even_if_selectable(self):
        disks = [disk(3, [vol(V1, [3])]), disk(4, [vol(V2, [4])])]
        targets, rejects = t.partition_targets(disks, [3], {3}, None)
        self.assertEqual(targets, [])
        self.assertIn("镜像或工具所在", rejects[0])

    def test_non_forbidden_disk_passes(self):
        disks = [disk(3, [vol(V1, [3])]), disk(4, [vol(V2, [4])])]
        targets, rejects = t.partition_targets(disks, [4], {3}, None)
        self.assertEqual([d["index"] for d in targets], [4])
        self.assertEqual(rejects, [])

    def test_capacity_and_position_checks(self):
        disks = [disk(4, [vol(V2, [4])], size=1 << 20)]
        _targets, rejects = t.partition_targets(disks, [4, 9], set(), 2 << 20)
        self.assertEqual(len(rejects), 2)
        self.assertTrue(any("装不下" in r for r in rejects))
        self.assertTrue(any("不在位" in r for r in rejects))


class CliSourceGuardTest(unittest.TestCase):
    def test_cli_refuses_source_disk(self):
        args = SimpleNamespace(flash="img.img", drives="3", no_verify=False, yes=True)
        disks = [disk(3, [vol(V1, [3])]), disk(4, [vol(V2, [4])])]
        meta = {"img_size": 1 << 20, "img_sha256": "x", "path": "img.img", "cached": True}
        with mock.patch.object(t, "is_admin", return_value=True), \
             mock.patch.object(t, "analyze_image", return_value=meta), \
             mock.patch.object(t, "enumerate_disks", return_value=(disks, 0)), \
             mock.patch.object(t, "source_disks", return_value={3}), \
             mock.patch.object(t, "flash_one") as flash:
            self.assertEqual(t.cli_flash(args), 1)
        flash.assert_not_called()


class GuiSourceGuardTest(unittest.TestCase):
    def test_start_refuses_source_disk(self):
        try:
            root = t.tk.Tk()
        except Exception:
            self.skipTest("无 tkinter/显示环境")
        root.withdraw()
        fd, tmp = tempfile.mkstemp(suffix=".img")
        os.close(fd)
        try:
            app = t.App(root)
            app.var_path.set(tmp)
            app.rows = {3: {"var": mock.Mock(get=lambda: True), "ok": True}}
            disks = [disk(3, [vol(V1, [3])]), disk(4, [vol(V2, [4])])]
            with mock.patch.object(t, "is_admin", return_value=True), \
                 mock.patch.object(t, "enumerate_disks", return_value=(disks, 0)), \
                 mock.patch.object(t, "source_disks", return_value={3}), \
                 mock.patch.object(t, "flash_one") as flash, \
                 mock.patch.object(t.messagebox, "showerror") as err:
                app.start()
            err.assert_called()
            flash.assert_not_called()
        finally:
            root.destroy()
            try:
                os.unlink(tmp)
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
