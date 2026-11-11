"""
A felvételek API-jának és a felvevő szálnak a tesztje valódi kamera nélkül.
A szerver függőségei kellenek hozzá, ezért a legegyszerűbb a konténerben futtatni:
docker compose run --rm camera-server python -m unittest discover -s tests
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from collections import namedtuple
from unittest import mock

import bcrypt
import numpy as np

TEMP = tempfile.mkdtemp()
PASSWORD = "teszt-jelszo-1234"
ARCHIVE = os.path.join(TEMP, "archivum")
os.makedirs(ARCHIVE)
CONFIG = {
    "cameras": {"0": {"name": "Teszt", "url": "rtsp://127.0.0.1:1/nincs"}},
    "auth": {"password_hash": bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(4)).decode(), "secret_key": "teszt"},
}
with open(os.path.join(TEMP, "config.json"), "w", encoding="utf-8") as config_file:
    json.dump(CONFIG, config_file)
os.environ.update(CONFIG_PATH=os.path.join(TEMP, "config.json"), ARCHIVE_DIR=ARCHIVE)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import main    # noqa: E402
import storage  # noqa: E402

Usage = namedtuple("Usage", "total used free")


def tearDownModule():
    shutil.rmtree(TEMP, ignore_errors=True)


def add_recording(date="2026-11-01", camera=0, time_part="120000"):
    folder = os.path.join(ARCHIVE, date, f"cam_{camera}")
    os.makedirs(folder, exist_ok=True)
    stem = f"cam_{camera}_{date.replace('-', '')}_{time_part}"
    for suffix in ("main", "sub"):
        with open(os.path.join(folder, f"{stem}_{suffix}.mp4"), "wb") as f:
            f.write(b"x" * 10)
    return f"{date}/cam_{camera}/{stem}_main.mp4"


class ApiTestCase(unittest.TestCase):
    def setUp(self):
        for name in os.listdir(ARCHIVE):
            path = os.path.join(ARCHIVE, name)
            shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
        main.active_recordings.clear()
        main.storage_state.update(problem=None, write_error=None)
        main.storage_settings.update(main.DEFAULT_STORAGE_SETTINGS)
        main.recording_flags[0] = False
        self.client = main.app.test_client()
        with self.client.session_transaction() as session:
            session["authenticated"] = True

    def exists(self, path):
        return os.path.exists(os.path.join(ARCHIVE, *path.split("/")))


class AccessTests(ApiTestCase):
    def test_every_endpoint_requires_login(self):
        anonymous = main.app.test_client()
        for url in ("/api/videos", "/api/events", "/status", "/video_feed/0"):
            self.assertEqual(anonymous.get(url).status_code, 401, url)
        # Az oldal és a felvétel kapcsolója a belépéshez irányít, és nem indít felvételt
        self.assertEqual(anonymous.get("/").status_code, 302)
        self.assertEqual(anonymous.post("/toggle_record/0").status_code, 302)
        self.assertFalse(main.recording_flags[0])

    def test_session_cookie_is_https_only(self):
        self.assertTrue(main.app.config["SESSION_COOKIE_SECURE"])

    def test_removed_endpoints_are_gone(self):
        for method, url in (("post", "/api/cameras"), ("post", "/api/led/0/1"), ("post", "/api/reset/0"),
                            ("post", "/api/change_password"), ("get", "/api/viewers"), ("get", "/api/storage"),
                            ("post", "/api/videos/protect"), ("delete", "/api/videos"), ("post", "/api/disconnect")):
            self.assertIn(getattr(self.client, method)(url).status_code, (404, 405), url)


class LiveStreamLimitTests(ApiTestCase):
    def test_limit_keeps_threads_for_the_rest_of_the_site(self):
        self.assertLess(main.MAX_LIVE_STREAMS, main.SERVER_THREADS)

    def test_streams_over_the_limit_are_refused_and_closing_frees_a_slot(self):
        streams = [self.client.get("/video_feed/0", buffered=False) for _ in range(main.MAX_LIVE_STREAMS)]
        try:
            self.assertTrue(all(stream.status_code == 200 for stream in streams))
            self.assertEqual(self.client.get("/video_feed/0", buffered=False).status_code, 503)
            self.assertEqual(self.client.get("/api/videos").status_code, 200)
            streams.pop().close()
            streams.append(self.client.get("/video_feed/0", buffered=False))
            self.assertEqual(streams[-1].status_code, 200)
        finally:
            for stream in streams:
                stream.close()

    def test_stream_ends_when_the_server_stops(self):
        self.addCleanup(main.server_stopping.clear)
        stream = main.generate_web_stream(0)
        self.assertIn(b"--frame", next(stream))
        main.server_stopping.set()
        self.assertEqual(list(stream), [])


class ListTests(ApiTestCase):
    def test_lists_every_recording_newest_first_with_camera_name(self):
        add_recording("2026-10-30", camera=0)
        add_recording("2026-11-01", camera=0)
        newest = add_recording("2026-11-02", camera=1)

        items = self.client.get("/api/videos").get_json()
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0]["path"], newest)
        self.assertEqual((items[0]["date"], items[0]["time"]), ("2026-11-02", "12:00:00"))
        # A configban nem szereplő kamera felvétele is listázódik, azonosítóval
        self.assertEqual([i["camera_name"] for i in items], ["CAM01", "Teszt", "Teszt"])
        self.assertEqual(items[0]["size"], 20)

    def test_legacy_root_files_are_not_listed(self):
        with open(os.path.join(ARCHIVE, "cam_0_20260429_182656_main.mp4"), "wb"):
            pass
        self.assertEqual(self.client.get("/api/videos").get_json(), [])


class DownloadTests(ApiTestCase):
    def test_download_is_attachment_and_playback_is_not(self):
        path = add_recording()
        response = self.client.get(f"/archivum_video/{path}?download=1")
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response.headers["Content-Disposition"])
        self.assertIn(os.path.basename(path), response.headers["Content-Disposition"])
        response.close()
        response = self.client.get(f"/archivum_video/{path}")
        self.assertNotIn("attachment", response.headers.get("Content-Disposition", ""))
        response.close()

    def test_download_requires_login(self):
        path = add_recording()
        self.assertEqual(main.app.test_client().get(f"/archivum_video/{path}?download=1").status_code, 401)

    def test_active_recording_cannot_be_downloaded(self):
        path = add_recording()
        main.active_recordings.add(path)
        self.assertEqual(self.client.get(f"/archivum_video/{path}?download=1").status_code, 409)

    def test_only_videos_inside_archive_are_served(self):
        for path in ("events.log", "../config.json", "2026-11-01/../../config.json"):
            self.assertIn(self.client.get(f"/archivum_video/{path}?download=1").status_code, (404, 308))


class SettingsTests(ApiTestCase):
    def test_broken_config_values_fall_back_to_defaults(self):
        loaded = main.load_storage_settings({"retention_days": "x", "segment_minutes": 0, "ismeretlen": 1})
        self.assertEqual(loaded, main.DEFAULT_STORAGE_SETTINGS)

    def test_valid_config_values_are_used(self):
        values = {"retention_days": 7, "min_free_percent": 0, "segment_minutes": 5}
        self.assertEqual(main.load_storage_settings(values), values)

    def test_camera_fps_comes_from_config(self):
        self.assertEqual(main.load_camera_fps(0, {"fps": 5}), 5)
        for bad in ("5", 0, -1, True):
            self.assertEqual(main.load_camera_fps(0, {"fps": bad}), main.DEFAULT_FPS, bad)
        self.assertEqual(main.load_camera_fps(0, {}), main.DEFAULT_FPS)


class MaintenanceTests(ApiTestCase):
    def tight_disk(self):
        return mock.patch.object(main.shutil, "disk_usage", return_value=Usage(100, 96, 4))

    def test_expired_recording_is_deleted_and_logged(self):
        path = add_recording("2020-01-01")
        main.run_storage_maintenance()
        self.assertFalse(self.exists(path))
        with open(os.path.join(ARCHIVE, "events.log"), encoding="utf-8") as f:
            self.assertIn(f"[AUTOMATIKUS TÖRLÉS] {path}", f.read())

    def test_low_space_stops_recording_and_refuses_start(self):
        add_recording()
        main.recording_flags[0] = True
        with self.tight_disk(), mock.patch.object(storage, "run_cleanup", return_value=([], False)):
            self.assertIn("szabad hely", main.run_storage_maintenance())
            self.assertFalse(main.recording_flags[0])
            response = self.client.post("/toggle_record/0")
        self.assertEqual(response.status_code, 507)
        self.assertFalse(main.recording_flags[0])
        self.assertIn("szabad hely", self.client.get("/status").get_json()["storage_alert"])

    def test_event_log_rotates_at_limit(self):
        with mock.patch.object(main, "EVENT_LOG_MAX_BYTES", 50):
            for index in range(6):
                main.log_system_event(f"esemény {index}")
        self.assertTrue(os.path.exists(os.path.join(ARCHIVE, "events.log.1")))
        self.assertLess(os.path.getsize(os.path.join(ARCHIVE, "events.log")), 100)


class WriterTests(ApiTestCase):
    """A felvevő szál valódi kódolással, kitalált képkockákkal."""

    def feed(self, seconds, fps=10):
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            main.write_queues[0].put((frame.copy(), fps))
            time.sleep(1 / fps)

    def wait_until(self, condition, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return True
            time.sleep(0.1)
        return False

    def stop_recording(self):
        main.recording_flags[0] = False
        self.assertTrue(self.wait_until(lambda: not main.active_recordings), "a fájlok nem zárultak le")

    def test_segments_recording_and_every_part_is_valid(self):
        main.storage_settings["segment_minutes"] = 0.05  # 3 másodperc
        main.recording_flags[0] = True
        self.feed(7.5)
        self.stop_recording()

        recordings = storage.scan_recordings(ARCHIVE)
        self.assertGreaterEqual(len(recordings), 2)
        for recording in recordings:
            for path in (recording.path, storage.sub_path(recording.path)):
                with open(os.path.join(ARCHIVE, *path.split("/")), "rb") as f:
                    head = f.read(64)
                # Faststart: az index a fájl elején van, tehát a lezárás rendben lefutott
                self.assertIn(b"moov", head)

    def test_active_recording_is_marked_while_written(self):
        main.recording_flags[0] = True
        self.feed(1)
        self.assertEqual(len(main.active_recordings), 1)
        active_path = next(iter(main.active_recordings))
        items = self.client.get("/api/videos").get_json()
        self.assertTrue(next(i for i in items if i["path"] == active_path)["recording"])
        self.stop_recording()

    def test_write_error_stops_recording_and_thread_survives(self):
        main.recording_flags[0] = True
        with mock.patch.object(main, "open_recording_writer", side_effect=OSError("No space left on device")):
            self.feed(1)
            self.assertTrue(self.wait_until(lambda: not main.recording_flags[0]))
        self.assertIn("írási hiba", self.client.get("/status").get_json()["storage_alert"])
        self.assertEqual(main.active_recordings, set())

        # A szál életben maradt: a következő indítás rendesen rögzít, és törli a figyelmeztetést
        self.assertEqual(self.client.post("/toggle_record/0").status_code, 200)
        self.assertIsNone(main.storage_state["write_error"])
        self.feed(1)
        self.stop_recording()
        self.assertEqual(len(storage.scan_recordings(ARCHIVE)), 1)


if __name__ == "__main__":
    unittest.main()
