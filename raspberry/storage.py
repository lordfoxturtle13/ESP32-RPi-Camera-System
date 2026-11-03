"""
Felvételek tárolásának kezelése: listázás, védett felvételek és automatikus törlés.

A modul csak a ÉÉÉÉ-HH-NN/cam_N/cam_N_ÉÉÉÉHHNN_ÓÓPPMM_main.mp4 felépítésű felvételekkel
foglalkozik. Minden más fájlhoz (például a régi, mappa nélküli felvételekhez) nem nyúl.
"""
import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta

MAIN_SUFFIX    = "_main.mp4"
SUB_SUFFIX     = "_sub.mp4"
PROTECTED_FILE = "protected.json"

_RECORDING_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})/cam_(?P<camera>\d+)/cam_(?P=camera)_(?P<stamp>\d{8}_\d{6})_main\.mp4$"
)


class ProtectedListError(Exception):
    """A védett felvételek listája nem olvasható, ezért törölni sem szabad."""


@dataclass
class Recording:
    path: str             # Az archívumhoz viszonyított útvonal, a főstream fájlja
    camera: int
    started: datetime
    size: int             # A fő- és az al-stream együttes mérete bájtban
    protected: bool = False
    active: bool = False  # Éppen készül vagy lezárás alatt áll


def parse_recording_path(path):
    """Visszaad: (kamera, kezdés), vagy None, ha ez nem a rendszer által kezelt felvétel."""
    match = _RECORDING_RE.match(path) if isinstance(path, str) else None
    if not match:
        return None
    try:
        return int(match["camera"]), datetime.strptime(match["stamp"], "%Y%m%d_%H%M%S")
    except ValueError:
        return None


def sub_path(path):
    return path[:-len(MAIN_SUFFIX)] + SUB_SUFFIX


def _full_path(archive_dir, path):
    return os.path.join(archive_dir, *path.split("/"))


def _size_or_zero(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def scan_recordings(archive_dir, protected=frozenset(), active=frozenset()):
    """A kezelt felvételek listája, a legújabbal kezdve."""
    recordings = []
    for date_dir in _list_dir(archive_dir):
        for cam_dir in _list_dir(os.path.join(archive_dir, date_dir)):
            for name in _list_dir(os.path.join(archive_dir, date_dir, cam_dir)):
                path   = f"{date_dir}/{cam_dir}/{name}"
                parsed = parse_recording_path(path)
                if parsed is None:
                    continue
                size = (_size_or_zero(_full_path(archive_dir, path))
                        + _size_or_zero(_full_path(archive_dir, sub_path(path))))
                recordings.append(Recording(path, parsed[0], parsed[1], size, path in protected, path in active))
    recordings.sort(key=lambda r: (r.started, r.path), reverse=True)
    return recordings


def _list_dir(path):
    try:
        return os.listdir(path)
    except OSError:  # Nem mappa, vagy közben eltűnt
        return []


def count_unmanaged_videos(archive_dir):
    """Az archívum gyökerében lévő régi videók száma: ezeket a rendszer nem listázza és nem törli."""
    return sum(1 for name in _list_dir(archive_dir) if name.endswith(".mp4"))


def load_protected(archive_dir):
    """A védett felvételek halmaza, vagy None, ha még nincs lista."""
    try:
        with open(os.path.join(archive_dir, PROTECTED_FILE), encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise ProtectedListError(str(e)) from e
    if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
        raise ProtectedListError("Hibás formátum")
    return set(data)


def save_protected(archive_dir, protected):
    # Ideiglenes fájlon át írunk, hogy egy áramszünet ne hagyjon félkész listát
    path = os.path.join(archive_dir, PROTECTED_FILE)
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as f:
        json.dump(sorted(protected), f, indent=2, ensure_ascii=False)
    os.replace(temp, path)


def initialize_protected(archive_dir):
    """
    Az első induláskor minden meglévő felvétel védett lesz, így az automatikus törlés
    csak a funkció bevezetése után készült felvételeket érinti.
    Visszaad: (védett felvételek, újonnan védetté tett felvételek száma).
    """
    protected = load_protected(archive_dir)
    if protected is not None:
        return protected, 0
    protected = {recording.path for recording in scan_recordings(archive_dir)}
    save_protected(archive_dir, protected)
    return protected, len(protected)


def delete_recording(archive_dir, path):
    """A fő- és az al-stream törlése, majd a kiürült mappák eltávolítása."""
    if parse_recording_path(path) is None:
        raise ValueError(f"Nem kezelt felvétel: {path}")

    # Az al-stream az első: ha közben hiba történik, a felvétel a listában marad, és újra törölhető
    for target in (sub_path(path), path):
        try:
            os.remove(_full_path(archive_dir, target))
        except FileNotFoundError:
            pass

    date_dir, cam_dir, _ = path.split("/")
    for folder in (os.path.join(archive_dir, date_dir, cam_dir), os.path.join(archive_dir, date_dir)):
        try:
            os.rmdir(folder)
        except OSError:
            break  # Van még benne más


def free_percent(usage):
    return usage.free / usage.total * 100 if usage.total else 0


def run_cleanup(archive_dir, retention_days, min_free_percent, protected, active, now=None, usage_fn=shutil.disk_usage):
    """
    Automatikus törlés, mindig a legrégebbi felvételtől kezdve. A védett és a készülő
    felvételekhez nem nyúl. A 0 érték az adott szabályt kikapcsolja.
    Visszaad: (törölt felvételek [(útvonal, ok)], van-e elég szabad hely).
    """
    now        = now or datetime.now()
    candidates = [r for r in scan_recordings(archive_dir, protected, active) if not r.protected and not r.active]
    candidates.sort(key=lambda r: (r.started, r.path))
    deleted    = []

    def remove(recording, reason):
        try:
            delete_recording(archive_dir, recording.path)
        except OSError:
            return  # A következő körben újra megpróbáljuk
        deleted.append((recording.path, reason))

    remaining = []
    for recording in candidates:
        if retention_days and now - recording.started > timedelta(days=retention_days):
            remove(recording, "lejárt")
        else:
            remaining.append(recording)

    if not min_free_percent:
        return deleted, True

    for recording in remaining:
        if free_percent(usage_fn(archive_dir)) >= min_free_percent:
            break
        remove(recording, "kevés a szabad hely")

    return deleted, free_percent(usage_fn(archive_dir)) >= min_free_percent
