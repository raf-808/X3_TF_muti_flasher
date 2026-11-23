# -*- coding: utf-8 -*-
"""S2 卷安全测试：卷拓扑映射与锁定/弹出失败即拒写（纯模拟，不碰真实盘）。

运行: python tests\\test_volume_safety.py -v
"""
import os
import queue
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tf_flasher as t  # noqa: E402


def _sysdrive_path() -> str:
    return os.environ.get("SystemDrive", "C:").rstrip(":").upper() + ":\\"


class MapVolumesTest(unittest.TestCase):
    def test_multi_extent_volume_belongs_to_every_disk(self):
        vol = {"name": "\\\\?\\Volume{a}\\", "mounts": ["D:\\", "C:\\mnt\\"],
               "letters": ["D"], "disks": [3, 5],
               "extents": [(3, 0, 10), (5, 0, 20)]}
        by_disk, sys_disks = t._map_volumes([vol])
        self.assertEqual(by_disk[3], [vol])
        self.assertEqual(by_disk[5], [vol])
        self.assertEqual(sys_disks, set())

    def test_system_disk_from_systemdrive_mount(self):
        sysdrive = _sysdrive_path()
        vol = {"name": "\\\\?\\Volume{b}\\", "mounts": [sysdrive],
               "letters": [sysdrive[0]], "disks": [2], "extents": [(2, 0, 1)]}
        by_disk, sys_disks = t._map_volumes([vol])
        self.assertEqual(by_disk[2], [vol])
        self.assertEqual(sys_disks, {2})

    def test_no_letter_volume_still_mapped(self):
        vol = {"name": "\\\\?\\Volume{c}\\", "mounts": [], "letters": [],
               "disks": [4], "extents": [(4, 0, 1)]}
        by_disk, _sys = t._map_volumes([vol])
        self.assertEqual(by_disk[4], [vol])


class SelectableVolumeKnownTest(unittest.TestCase):
    def _disk(self, **kw):
        d = {"size": 64 << 20, "is_system": False, "volume_known": True,
             "removable": True, "media_removable": False, "bus": 7}
        d.update(kw)
        return d

    def test_volume_scan_unknown_is_not_selectable(self):
        self.assertFalse(t.selectable(self._disk(volume_known=False)))

    def test_normal_removable_disk_still_selectable(self):
        self.assertTrue(t.selectable(self._disk()))

    def test_system_disk_not_selectable(self):
        self.assertFalse(t.selectable(self._disk(is_system=True)))


class LockTargetVolumesTest(unittest.TestCase):
    def setUp(self):
        self.vols = [{"name": "\\\\?\\Volume{a}\\"}, {"name": "\\\\?\\Volume{b}\\"}]

    def test_scan_error_refuses_and_opens_nothing(self):
        with mock.patch.object(t, "scan_volumes", return_value=({}, set(), "boom")), \
             mock.patch.object(t, "_open") as op:
            with self.assertRaises(t.VolumeSafetyError) as cm:
                t.lock_target_volumes(7)
        self.assertIn("卷枚举失败", str(cm.exception))
        op.assert_not_called()

    def test_blank_disk_with_verified_enumeration_allowed(self):
        with mock.patch.object(t, "scan_volumes", return_value=({7: []}, set(), None)), \
             mock.patch.object(t, "_open") as op:
            self.assertEqual(t.lock_target_volumes(7), [])
        op.assert_not_called()

    def test_lock_failure_closes_all_handles(self):
        opened, closed = [], []

        def fake_open(path, access, share):
            opened.append(path)
            return 100 + len(opened)

        def fake_ioctl(h, code, *args):
            return h == 101  # 第二个卷锁定失败

        with mock.patch.object(t, "_open", side_effect=fake_open), \
             mock.patch.object(t, "_ioctl", side_effect=fake_ioctl), \
             mock.patch.object(t, "kernel32") as k:
            k.CloseHandle.side_effect = closed.append
            with self.assertRaises(t.VolumeSafetyError) as cm:
                t.lock_target_volumes(7, self.vols)
        self.assertIn("锁定失败", str(cm.exception))
        self.assertEqual(sorted(closed), [101, 102])

    def test_dismount_failure_closes_handle(self):
        closed = []

        def fake_ioctl(h, code, *args):
            return code == t.FSCTL_LOCK_VOLUME

        with mock.patch.object(t, "_open", return_value=101), \
             mock.patch.object(t, "_ioctl", side_effect=fake_ioctl), \
             mock.patch.object(t, "kernel32") as k:
            k.CloseHandle.side_effect = closed.append
            with self.assertRaises(t.VolumeSafetyError) as cm:
                t.lock_target_volumes(7, self.vols[:1])
        self.assertIn("弹出失败", str(cm.exception))
        self.assertEqual(closed, [101])

    def test_success_keeps_handles_for_caller(self):
        calls = []

        def fake_ioctl(h, code, *args):
            calls.append((h, code))
            return True

        with mock.patch.object(t, "_open", side_effect=[101, 102]), \
             mock.patch.object(t, "_ioctl", side_effect=fake_ioctl), \
             mock.patch.object(t, "kernel32") as k:
            held = t.lock_target_volumes(7, self.vols)
        self.assertEqual(held, [101, 102])
        self.assertEqual([c[1] for c in calls],
                         [t.FSCTL_LOCK_VOLUME, t.FSCTL_DISMOUNT_VOLUME] * 2)
        k.CloseHandle.assert_not_called()


class DiskOfflineTest(unittest.TestCase):
    def test_offline_and_online_attributes(self):
        seen = []

        def fake_ioctl(h, code, inbuf=None, outbuf=None):
            seen.append((code, inbuf.Attributes, inbuf.AttributesMask))
            return True

        with mock.patch.object(t, "_ioctl", side_effect=fake_ioctl):
            self.assertTrue(t._set_disk_offline(1, True))
            self.assertTrue(t._set_disk_offline(1, False))
        self.assertEqual(seen[0][0], t.IOCTL_DISK_SET_DISK_ATTRIBUTES)
        self.assertEqual(seen[0][1], t.DISK_ATTRIBUTE_OFFLINE)
        self.assertEqual(seen[0][2], t.DISK_ATTRIBUTE_OFFLINE)
        self.assertEqual(seen[1][1], 0)

    def test_offline_failure_returns_false(self):
        with mock.patch.object(t, "_ioctl", return_value=False):
            self.assertFalse(t._set_disk_offline(1, True))


class FlashOneLockGateTest(unittest.TestCase):
    def test_lock_failure_never_opens_physicaldrive(self):
        target = {"index": 7, "model": "m", "serial": "", "size": 64 << 20,
                  "letters": [], "sector": 512, "volumes": [], "volume_known": True}
        q: queue.Queue = queue.Queue()
        meta_box = {"meta": {"img_size": 1, "img_sha256": "x"},
                    "error": None, "ready": threading.Event()}
        meta_box["ready"].set()
        with mock.patch.object(t, "lock_target_volumes",
                               side_effect=t.VolumeSafetyError("卷锁定失败")), \
             mock.patch.object(t, "_open") as op:
            res = t.flash_one(target, "missing.img", True, threading.Event(), q, meta_box)
        self.assertFalse(res["ok"])
        self.assertIn("卷锁定失败", res["error"])
        op.assert_not_called()
        kinds = [msg[0] for msg in list(q.queue)]
        self.assertEqual(kinds.count("final"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
