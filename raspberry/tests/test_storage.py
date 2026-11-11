"""Futtatás a raspberry mappából: python -m unittest discover -s tests"""
import os
import sys
import tempfile
import unittest
from collections import namedtuple
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import storage

Usage = namedtuple("Usage", "total used free")
NOW   = datetime(2026, 11, 3, 12, 0, 0)


class StorageTestCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.archive = temp.name

    def add(self, date="2026-11-01", camera=0, time="120000", size=10):
        """Felvételpár létrehozása, visszaadja a főstream relatív útvonalát."""
        folder = os.path.join(self.archive, date, f"cam_{camera}")
        os.makedirs(folder, exist_ok=True)
        stem = f"cam_{camera}_{date.replace('-', '')}_{time}"
        for suffix in ("main", "sub"):
            with open(os.path.join(folder, f"{stem}_{suffix}.mp4"), "wb") as f:
                f.write(b"x" * size)
        return f"{date}/cam_{camera}/{stem}_main.mp4"

    def exists(self, path):
        return os.path.exists(os.path.join(self.archive, *path.split("/")))

    def cleanup(self, retention=30, min_free=0, active=(), usage=None):
        return storage.run_cleanup(self.archive, retention, min_free, set(active), now=NOW,
                                   usage_fn=usage or (lambda _: Usage(100, 50, 50)))


class ParseTests(unittest.TestCase):
    def test_valid_path(self):
        self.assertEqual(storage.parse_recording_path("2026-11-03/cam_5/cam_5_20261103_120409_main.mp4"),
                         (5, datetime(2026, 11, 3, 12, 4, 9)))

    def test_rejects_everything_else(self):
        for path in ("cam_0_20260429_182656_main.mp4",               # Régi, mappa nélküli felvétel
                     "2026-11-03/cam_5/cam_5_20261103_120409_sub.mp4",
                     "2026-11-03/cam_5/cam_6_20261103_120409_main.mp4",
                     "../2026-11-03/cam_5/cam_5_20261103_120409_main.mp4",
                     "2026-11-03/cam_5/../../main.py",
                     "2026-11-03/cam_5/cam_5_20261399_120409_main.mp4",  # Nem létező dátum
                     "", None, 5):
            self.assertIsNone(storage.parse_recording_path(path), path)


class ScanTests(StorageTestCase):
    def test_newest_first_with_flags_and_both_sizes(self):
        old = self.add("2026-10-01")
        new = self.add("2026-11-01", camera=1, size=20)
        result = storage.scan_recordings(self.archive, active={new})
        self.assertEqual([r.path for r in result], [new, old])
        self.assertEqual((result[0].size, result[0].active), (40, True))
        self.assertEqual((result[1].size, result[1].active), (20, False))

    def test_ignores_unmanaged_files(self):
        with open(os.path.join(self.archive, "cam_0_20260429_182656_main.mp4"), "wb"):
            pass
        with open(os.path.join(self.archive, "events.log"), "wb"):
            pass
        self.assertEqual(storage.scan_recordings(self.archive), [])


class DeleteTests(StorageTestCase):
    def test_deletes_pair_and_empty_folders(self):
        path = self.add()
        storage.delete_recording(self.archive, path)
        self.assertEqual(os.listdir(self.archive), [])

    def test_keeps_folder_with_other_recording(self):
        path  = self.add()
        other = self.add(time="130000")
        storage.delete_recording(self.archive, path)
        self.assertFalse(self.exists(path))
        self.assertTrue(self.exists(other))

    def test_refuses_unmanaged_path(self):
        target = os.path.join(self.archive, "fontos.mp4")
        with open(target, "wb"):
            pass
        for path in ("fontos.mp4", "../fontos.mp4", "2026-11-01/cam_0/../../fontos.mp4"):
            with self.assertRaises(ValueError):
                storage.delete_recording(self.archive, path)
        self.assertTrue(os.path.exists(target))


class CleanupTests(StorageTestCase):
    def test_retention_deletes_only_expired(self):
        expired = self.add("2026-10-03", time="115959")
        kept    = self.add("2026-10-04")
        deleted, ok = self.cleanup()
        self.assertEqual(deleted, [(expired, "lejárt")])
        self.assertTrue(ok)
        self.assertTrue(self.exists(kept))

    def test_retention_zero_disables_rule(self):
        path = self.add("2020-01-01")
        self.assertEqual(self.cleanup(retention=0), ([], True))
        self.assertTrue(self.exists(path))

    def test_active_survives_every_rule(self):
        active = self.add("2020-01-02")
        deleted, ok = self.cleanup(min_free=99, active={active})
        self.assertEqual(deleted, [])
        self.assertFalse(ok)
        self.assertTrue(self.exists(active))

    def test_legacy_root_files_survive_every_rule(self):
        legacy = os.path.join(self.archive, "cam_0_20200101_120000_main.mp4")
        with open(legacy, "wb"):
            pass
        self.cleanup(retention=1, min_free=99)
        self.assertTrue(os.path.exists(legacy))

    def test_low_space_deletes_oldest_first_until_target(self):
        oldest = self.add("2026-11-01", time="100000")
        middle = self.add("2026-11-01", time="110000")
        newest = self.add("2026-11-01", time="120000")
        freed  = iter([5, 8, 12])  # Az első törlés után 8%, a második után 12% lesz szabad

        current = {"free": next(freed)}

        def usage(_):
            return Usage(100, 100 - current["free"], current["free"])

        original = storage.delete_recording

        def delete_and_free(archive, path):
            original(archive, path)
            current["free"] = next(freed)

        storage.delete_recording = delete_and_free
        self.addCleanup(setattr, storage, "delete_recording", original)

        deleted, ok = self.cleanup(min_free=10, usage=usage)
        self.assertEqual([path for path, _ in deleted], [oldest, middle])
        self.assertTrue(ok)
        self.assertTrue(self.exists(newest))

    def test_reports_low_space_when_nothing_can_be_deleted(self):
        active = self.add()
        deleted, ok = self.cleanup(min_free=10, active={active}, usage=lambda _: Usage(100, 95, 5))
        self.assertEqual(deleted, [])
        self.assertFalse(ok)

    def test_min_free_zero_never_deletes_for_space(self):
        path = self.add()
        deleted, ok = self.cleanup(min_free=0, usage=lambda _: Usage(100, 100, 0))
        self.assertEqual(deleted, [])
        self.assertTrue(ok)
        self.assertTrue(self.exists(path))


if __name__ == "__main__":
    unittest.main()
