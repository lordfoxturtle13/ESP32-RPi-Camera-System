import atexit
import signal
import sys
import cv2
import time
import json
import logging
import numpy as np
import threading
import queue
import os
import requests
import ipaddress
import secrets
import shutil
import re
import unicodedata
from urllib.parse import urlparse
from datetime import datetime, timedelta
from werkzeug.middleware.proxy_fix import ProxyFix
from flask.sessions import SecureCookieSessionInterface
from flask import Flask, abort, render_template, Response, jsonify, send_from_directory, request, session, redirect, url_for
from functools import wraps
import bcrypt
import imageio

import storage

app = Flask(__name__)
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0  # Statikus fájlok (CSS, JS) nem cache-elődnek

# Alapból csak a saját gépen figyel: a külső elérést a HTTPS proxy (tailscale serve) adja
HOST = os.environ.get("HOST", "127.0.0.1")

# A proxy mögött a valódi látogató címe az X-Forwarded-For fejlécben érkezik. Csak akkor bízunk
# benne, ha a szerver közvetlenül nem érhető el kívülről, különben a fejléc hamisítható lenne.
if os.environ.get("TRUST_PROXY", "1" if HOST == "127.0.0.1" else "0") == "1":
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)


class ProxyAwareSessionInterface(SecureCookieSessionInterface):
    """A süti Secure jelölést kap HTTPS-en, de a http://localhost elérés is működik marad."""

    def get_cookie_secure(self, app):
        return request.is_secure


app.session_interface = ProxyAwareSessionInterface()
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"  # Idegen oldalról küldött POST nem kapja meg a sütit
app.permanent_session_lifetime = timedelta(days=7)  # Használat közben megújul

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ==========================================
# KONFIGURÁCIÓ — config.json-ból töltődik be
# ==========================================

_config_path = os.environ.get("CONFIG_PATH", os.path.join(os.path.dirname(__file__), "config.json"))
with open(_config_path, encoding="utf-8") as _f:
    _config = json.load(_f)

CAMERAS   = {int(k): v for k, v in _config["cameras"].items()}
_ssl      = _config.get("ssl")
_auth     = _config["auth"]

app.secret_key = _auth["secret_key"]

MAX_RETRIES       = 5          # Ennyi sikertelen csatlakozás után áll "standby"-ba a kamera
OPEN_TIMEOUT_MS   = 5000       # Ennyi ideig próbálkozunk egy kamera stream megnyitásával
REC_W, REC_H      = 640, 480   # Alapértelmezett felbontás (ha a kamerához nincs megadva más)
DEFAULT_FPS       = round(1000 / 150, 2)  # Alapértelmezett képkockasebesség (150ms ≈ ~6.67 FPS)
MIN_FPS, MAX_FPS  = 1, 30
# A webes felületen választható felbontások
RESOLUTIONS       = [(320, 240), (640, 480), (800, 600), (1280, 720), (1920, 1080)]
MAX_NAME_LENGTH   = 40
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_BYTES  = 72       # A bcrypt ennél hosszabb jelszót csendben levágna
# Belépési próbák korlátozása IP-címenként: a szabad próbák után egyre hosszabb a várakozás
LOGIN_FREE_ATTEMPTS = 5
LOGIN_BASE_LOCK_S   = 30
LOGIN_MAX_LOCK_S    = 3600
LOGIN_FORGET_AFTER_S = 24 * 3600
# Forgatás az óramutató járásával megegyezően, fokban
ROTATE_CODES      = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_COUNTERCLOCKWISE}
ROTATIONS         = [0, *ROTATE_CODES]
# Kameratípusonként a stream és a LED címe az IP alapján (a LED opcionális)
CAMERA_TYPES      = {
    "esp32"  : {"url": "rtsp://{ip}:554/mjpeg/1", "led_url": "http://{ip}/flash"},
    "luckfox": {"url": "rtsp://{ip}/live/0"},
}
STREAM_URL_SCHEMES = ("rtsp", "http", "https")  # A kamera URL nem mutathat pl. helyi fájlra
LED_URL_SCHEMES    = ("http", "https")
ARCHIVE_DIR       = os.environ.get("ARCHIVE_DIR", os.path.join(os.path.dirname(__file__), "archivum"))

# Szoftveres kódoló: minden gépen működik. A hardveres h264_v4l2m2m Pi 3 B+-on hibás fájlokat készített.
H264_CODEC = os.environ.get("H264_CODEC", "libx264")

# Kulcsképek távolsága másodpercben: tekeréskor a lejátszó a legközelebbi kulcsképtől dekódol.
# Az al-stream a gyors tekeréshez készül, ezért abban sűrűbbek.
KEYFRAME_INTERVAL_MAIN_S = 2
KEYFRAME_INTERVAL_SUB_S  = 1

# Tárhelykezelés alapértékei és a beállítható tartományok. A 0 az adott szabályt kikapcsolja.
DEFAULT_STORAGE_SETTINGS = {"retention_days": 30, "min_free_percent": 10, "segment_minutes": 15}
STORAGE_SETTING_RANGES   = {"retention_days": (0, 3650), "min_free_percent": (0, 50), "segment_minutes": (1, 120)}
STORAGE_CHECK_INTERVAL_S = 60
EVENT_LOG_MAX_BYTES      = 5 * 1024 * 1024  # E fölött új naplófájl kezdődik, és csak az előző marad meg
VIDEOS_PER_PAGE          = 50
MAX_VIDEOS_PER_PAGE      = 200
# Ha be van kapcsolva, csak olyan helyre rögzítünk, ahol ez a fájl megvan. Így a kihúzott USB
# helyett nem a rendszerkártya telik meg, ahová a csatolási pont mutat.
ARCHIVE_MARKER_FILE      = ".orszem-archive"
REQUIRE_ARCHIVE_MARKER   = os.environ.get("REQUIRE_ARCHIVE_MARKER", "0") == "1"
SHUTDOWN_WAIT_S          = 8        # A docker stop 10 mp után kényszerrel leállít

# Sikertelen jelszópróbák IP-címenként: {ip: {"fails", "locked_until", "last"}}
# Csak a memóriában él, újraindításkor törlődik.
_login_attempts = {}
_login_attempts_lock = threading.Lock()


def reserve_password_attempt(ip):
    """
    Próba lefoglalása a jelszó ellenőrzése ELŐTT, így párhuzamos kérésekkel sem kerülhető meg.
    Visszaad: 0, ha próbálkozhat, különben a hátralévő várakozás másodpercben.
    """
    now = time.monotonic()
    with _login_attempts_lock:
        for key in [k for k, v in _login_attempts.items() if now - v["last"] > LOGIN_FORGET_AFTER_S]:
            del _login_attempts[key]

        entry = _login_attempts.setdefault(ip, {"fails": 0, "locked_until": 0, "last": now})
        if now < entry["locked_until"]:
            return int(entry["locked_until"] - now) + 1

        entry["fails"] += 1
        entry["last"]   = now
        if entry["fails"] >= LOGIN_FREE_ATTEMPTS:
            extra = entry["fails"] - LOGIN_FREE_ATTEMPTS
            entry["locked_until"] = now + min(LOGIN_MAX_LOCK_S, LOGIN_BASE_LOCK_S * 2 ** min(extra, 16))
        return 0


def clear_password_attempts(ip):
    with _login_attempts_lock:
        _login_attempts.pop(ip, None)


# config.json írását védi, hogy két egyidejű mentés ne írja felül egymást
_config_lock = threading.Lock()
# A kameralista módosításait sorosítja (pl. két hozzáadás ne kapjon azonos azonosítót)
_cameras_lock = threading.Lock()


def update_config(change):
    """config.json beolvasása, módosítása a kapott függvénnyel, majd visszaírása."""
    with _config_lock:
        with open(_config_path, encoding="utf-8") as f:
            config_data = json.load(f)
        change(config_data)
        with open(_config_path, "w", encoding="utf-8") as f:
            json.dump(config_data, f, indent=4, ensure_ascii=False)


def validate_camera_settings(width, height, fps, rotation):
    """Hibaüzenetet ad vissza, ha a beállítás érvénytelen, különben None-t."""
    if (width, height) not in RESOLUTIONS:
        return "Nem támogatott felbontás"
    if isinstance(fps, bool) or not isinstance(fps, (int, float)) or not MIN_FPS <= fps <= MAX_FPS:
        return f"Az FPS {MIN_FPS} és {MAX_FPS} között lehet"
    if type(rotation) is not int or rotation not in ROTATIONS:
        return "Nem támogatott forgatás"
    return None


def clean_camera_name(name):
    """Felesleges szóközök és sortörések nélküli név, vagy None, ha érvénytelen."""
    if not isinstance(name, str):
        return None
    name = " ".join(name.split())
    return name if 0 < len(name) <= MAX_NAME_LENGTH else None


def is_valid_url(url, schemes):
    parsed = urlparse(url)
    return parsed.scheme in schemes and bool(parsed.netloc)


def url_in_use(url, except_id=None):
    return any(cam["url"] == url for cam_id, cam in CAMERAS.items() if cam_id != except_id)


def build_camera_entry(camera_type, address):
    """Kamera címeinek összeállítása típus és IP/URL alapján. Visszaad: (címek, hibaüzenet)."""
    address = address.strip() if isinstance(address, str) else ""

    if camera_type == "custom":
        if not is_valid_url(address, STREAM_URL_SCHEMES):
            return None, "Érvénytelen URL (rtsp://, http:// vagy https:// kezdetű legyen)"
        return {"url": address}, None

    template = CAMERA_TYPES.get(camera_type)
    if not template:
        return None, "Ismeretlen kameratípus"
    try:
        ip = str(ipaddress.IPv4Address(address))
    except ValueError:
        return None, "Érvénytelen IP-cím"
    return {key: value.format(ip=ip) for key, value in template.items()}, None


def validate_storage_setting(key, value):
    low, high = STORAGE_SETTING_RANGES[key]
    return type(value) is int and low <= value <= high


def load_storage_settings(saved):
    """Tárhely-beállítások a configból; hibás érték esetén az alapértelmezés."""
    settings = dict(DEFAULT_STORAGE_SETTINGS)
    for key, value in (saved or {}).items():
        if key in settings:
            if validate_storage_setting(key, value):
                settings[key] = value
            else:
                log.warning(f"Tárhely: hibás config ({key}), alapértelmezés használva")
    return settings


def load_camera_settings(camera_id, cam):
    """Kamera felbontása és FPS-e a configból; hibás érték esetén az alapértelmezés."""
    width  = cam.get("width", REC_W)
    height = cam.get("height", REC_H)
    fps    = cam.get("fps", DEFAULT_FPS)
    rotation = cam.get("rotation", 0)
    error  = validate_camera_settings(width, height, fps, rotation)
    if error:
        log.warning(f"Kamera {camera_id}: hibás config ({error}), alapértelmezés használva")
        return {"width": REC_W, "height": REC_H, "fps": DEFAULT_FPS, "rotation": 0}
    return {"width": width, "height": height, "fps": fps, "rotation": rotation}


# Kameránkénti kép-beállítások — mentéskor a teljes dict cserélődik, a szálak menet közben olvassák
camera_settings = {cam_id: load_camera_settings(cam_id, cam) for cam_id, cam in CAMERAS.items()}

# A törölt kamerák azonosítója nem kerül újra kiosztásra, mert az archívum mappái ehhez kötődnek
_next_camera_id = max(_config.get("next_camera_id", 0), max(CAMERAS, default=-1) + 1)

# Kameránként egy esemény: beállítása leállítja a kamera szálait (törléskor)
camera_stop_events = {}
writer_threads = {}

storage_settings = load_storage_settings(_config.get("storage"))
# Sorosítja a törlést és a védett lista módosítását, és védi a készülő felvételek halmazát
_storage_lock = threading.Lock()
active_recordings = set()  # A készülő vagy lezárás alatt álló felvételek relatív útvonala
storage_state = {"problem": None, "write_error": None}

# ==========================================
# MEGOSZTOTT ÁLLAPOT
# Fontos: mind a 4 dict itt van definiálva, a szálak indítása ELŐTT.
# ==========================================

# Kamerák hálózati állapota és újracsatlakozási számlálója
camera_states = {cam_id: {"status": "offline", "retries": 0} for cam_id in CAMERAS}

# Felvételi zászlók — Flask route állítja, writer_worker olvassa
recording_flags = {cam_id: False for cam_id in CAMERAS}

# LED állapotok — csak led_url-lel rendelkező kamerákhoz
led_states = {cam_id: False for cam_id in CAMERAS if CAMERAS[cam_id].get("led_url")}

# Képkocka sor a felvevő szálnak (maxsize=30 gátolja a RAM telítődést lassú írás esetén)
write_queues = {cam_id: queue.Queue(maxsize=30) for cam_id in CAMERAS}

# Aktív nézők: {ip: utolsó_látott_timestamp} — /status polling alapján frissül
active_viewers = {}
VIEWER_TIMEOUT = 10  # másodperc: ennyi ideig számít aktívnak az utoljára látott IP


def generate_dark_frame():
    """
    Egyszerű fekete JPEG képkocka offline állapothoz.
    A vizuális megjelenítés (ikon, szöveg) a frontend CSS/JS feladata —
    így sávszélesség takarékos. Quality=10 → ~1-2 KB fájlméret.
    """
    frame = np.zeros((REC_H, REC_W, 3), dtype=np.uint8)
    _, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 10])
    return buffer.tobytes()


FRAME_OFFLINE = generate_dark_frame()

# Élőkép RAM-puffer: camera_worker szálak ide írják a legfrissebb JPEG-et,
# a webszerver innen olvassa ki. Python GIL védi a dict-hozzáférést.
latest_frames = {cam_id: FRAME_OFFLINE for cam_id in CAMERAS}


# ==========================================
# 1. HÁLÓZATI OLVASÓ SZÁL — Producer
# ==========================================

_event_log_lock = threading.Lock()


def write_event_line(line):
    """Sor írása az eseménynaplóba. A korlát fölött új fájl kezdődik, így a napló nem nő a végtelenségig."""
    path = os.path.join(ARCHIVE_DIR, "events.log")
    try:
        with _event_log_lock:
            if os.path.exists(path) and os.path.getsize(path) >= EVENT_LOG_MAX_BYTES:
                os.replace(path, path + ".1")
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
    except OSError:
        pass


def log_event(camera_id, message):
    """Kamera esemény naplózása az archívum events.log fájlba."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    camera = CAMERAS.get(camera_id)
    label = camera["name"] if camera else "törölt"
    line = f"{ts} | Kamera {camera_id} ({label}) | {message}\n"
    write_event_line(line)
    log.warning(f"[EVENT] {line.strip()}")


def log_system_event(message):
    """Rendszerszintű esemény naplózása (belépés, kilépés) — nincs kamera kontextus."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    write_event_line(f"{ts} | RENDSZER | {message}\n")
    log.info(f"[SYSTEM] {message}")


def camera_worker(camera_id, stop):
    # A dict-et tartjuk meg: a név és a cím menet közben változhat, törléskor pedig
    # a szál még biztonságosan befejezheti az aktuális kört
    camera = CAMERAS[camera_id]

    while not stop.is_set():
        url = camera["url"]

        # Standby: max újracsatlakozási kísérlet elérve, várunk amíg a reset endpoint nem nulláz
        if camera_states[camera_id]["retries"] >= MAX_RETRIES:
            camera_states[camera_id]["status"] = "standby"
            latest_frames[camera_id] = FRAME_OFFLINE
            time.sleep(1)
            continue

        # Az OpenCV egyszerre csak egy streamet nyit meg, ezért az időkorlát nélkül egy elérhetetlen
        # kamera a többi csatlakozását is feltartaná (alapból 30 mp-ig kísérletenként)
        cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG, [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, OPEN_TIMEOUT_MS])

        # CAP_PROP_BUFFERSIZE=1: csak a legfrissebb képkocka kerül a belső pufferbe,
        # megakadályozva a késleltetést (lag) amit a pufferelés okozna.
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        # Első frame-mel teszteljük a kapcsolatot
        success, _ = cap.read()
        if not success:
            camera_states[camera_id]["status"] = "offline"
            camera_states[camera_id]["retries"] += 1
            latest_frames[camera_id] = FRAME_OFFLINE
            cap.release()
            time.sleep(0.5)
            continue

        camera_states[camera_id]["status"] = "online"
        camera_states[camera_id]["retries"] = 0
        if recording_flags[camera_id]:
            log_event(camera_id, "Kapcsolat helyreállt — felvétel folytatódik")

        # Törléskor vagy címváltáskor kilépünk, utóbbi esetben az új címre csatlakozunk
        next_frame = 0
        while not stop.is_set() and camera["url"] == url:
            # A streamet mindig a kamera ütemében olvassuk, különben lemaradnánk tőle, és a kép
            # lassítva, egyre nagyobb késéssel érkezne. Csak a beállított FPS-nek
            # megfelelő képkockákat dolgozzuk fel, a többit eldobjuk.
            if not cap.grab():
                break  # Stream megszakadt → kilépés, újracsatlakozás következik

            settings = camera_settings[camera_id]
            interval = 1 / settings["fps"]
            now      = time.monotonic()
            # A fél intervallum tűrés kell, hogy a kamera FPS-ével azonos beállításnál az
            # időzítés ingadozása miatt ne maradjon ki minden második képkocka
            if now < next_frame - interval / 2:
                continue
            next_frame = max(next_frame + interval, now)

            success, frame = cap.retrieve()
            if not success:
                break

            # Egységes felbontásra méretezés (kodek és dual-stream miatt szükséges)
            width, height = settings["width"], settings["height"]
            if frame.shape[1] != width or frame.shape[0] != height:
                frame = cv2.resize(frame, (width, height), interpolation=cv2.INTER_LINEAR)

            # A forgatás az OSD előtt történik, hogy a felirat egyenes maradjon.
            # 90° és 270° esetén a kép álló lesz, a szélesség és a magasság felcserélődik.
            rotate_code = ROTATE_CODES.get(settings["rotation"])
            if rotate_code is not None:
                frame = cv2.rotate(frame, rotate_code)

            # OSD: kettős szöveg (fekete árnyék + fehér előtér) a kontrasztért.
            # A méret a felbontással arányos, így nagy képen is olvasható marad.
            ts    = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            # A név menet közben is változhat; a cv2 betűkészlete nem ismeri az ékezeteket
            name  = unicodedata.normalize("NFKD", camera["name"]).encode("ascii", "ignore").decode()
            osd   = f"{name} | {ts}"
            scale = frame.shape[1] / REC_W
            thick = max(1, round(2 * scale))
            x, y  = round(10 * scale), round(30 * scale)
            cv2.putText(frame, osd, (x + 2, y + 2), cv2.FONT_HERSHEY_SIMPLEX, 0.6 * scale, (0, 0, 0),       thick)
            cv2.putText(frame, osd, (x, y),         cv2.FONT_HERSHEY_SIMPLEX, 0.6 * scale, (255, 255, 255), thick)

            # Élőkép frissítése RAM-ban
            ret, buffer = cv2.imencode(".jpg", frame)
            if ret:
                latest_frames[camera_id] = buffer.tobytes()

            # Felvétel aktív: képkocka átadása a writer_worker-nek
            if recording_flags[camera_id]:
                try:
                    write_queues[camera_id].put_nowait((frame.copy(), settings["fps"], settings["rotation"]))
                except queue.Full:
                    pass  # Inkább kihagyunk egy képkockát, mint hogy blokkoljuk a szálat

        cap.release()
        if recording_flags[camera_id] and camera["url"] == url:
            log_event(camera_id, "Kapcsolat megszakadt felvétel közben")
        if camera["url"] != url:
            camera_states[camera_id] = {"status": "connecting", "retries": 0}
            latest_frames[camera_id] = FRAME_OFFLINE

    camera_states[camera_id]["status"] = "offline"
    latest_frames[camera_id] = FRAME_OFFLINE


# ==========================================
# 2. FELVEVŐ SZÁL — Consumer (Dupla stream H.264)
# ==========================================

def open_recording_writer(path, fps, keyframe_interval_s):
    # macro_block_size=16: H.264 szabvány 16x16 pixeles makroblokkokat használ
    # +faststart: az index a fájl elejére kerül, így a böngésző a lejátszáshoz nem a végéről kéri le
    return imageio.get_writer(
        path, fps=fps, codec=H264_CODEC, macro_block_size=16,
        output_params=["-g", str(max(1, round(fps * keyframe_interval_s))), "-movflags", "+faststart"],
    )


def open_recording_files(camera_id, fps):
    """Új felvételpár megnyitása. Visszaad: {"path", "main", "sub"}."""
    now    = datetime.now()
    date   = now.strftime("%Y-%m-%d")
    stem   = f"cam_{camera_id}_{now.strftime('%Y%m%d_%H%M%S')}"
    subdir = os.path.join(ARCHIVE_DIR, date, f"cam_{camera_id}")
    path   = f"{date}/cam_{camera_id}/{stem}_main.mp4"

    # Már a megnyitás előtt készülőnek jelöljük, hogy az automatikus törlés ne érje el
    with _storage_lock:
        active_recordings.add(path)
    out_main = None
    try:
        os.makedirs(subdir, exist_ok=True)
        out_main = open_recording_writer(os.path.join(subdir, f"{stem}_main.mp4"), fps, KEYFRAME_INTERVAL_MAIN_S)
        out_sub  = open_recording_writer(os.path.join(subdir, f"{stem}_sub.mp4"), fps, KEYFRAME_INTERVAL_SUB_S)
    except Exception:
        if out_main is not None:
            out_main.close()
        with _storage_lock:
            active_recordings.discard(path)
        raise
    return {"path": path, "main": out_main, "sub": out_sub}


def close_recording_files(camera_id, recording):
    """A felvételpár lezárása. Egy sikertelen lezárás nem akadályozza a másikat."""
    for key in ("main", "sub"):
        try:
            recording[key].close()
        except Exception as e:
            log.error(f"[REC] Kamera {camera_id}: nem sikerült lezárni ({key}): {e}")
    with _storage_lock:
        active_recordings.discard(recording["path"])


def handle_recording_error(camera_id, error):
    """Írási hiba (pl. betelt vagy kihúzott tároló): a felvétel szabályosan leáll és jelzést ad."""
    log.exception(f"[REC] Kamera {camera_id}: írási hiba")
    was_recording = recording_flags.get(camera_id)
    recording_flags[camera_id] = False
    if was_recording:
        storage_state["write_error"] = f"A felvétel írási hiba miatt leállt (kamera {camera_id}). Ellenőrizd a tárolót."
        log_event(camera_id, f"[FELVÉTEL] Írási hiba miatt leállt: {type(error).__name__}")


def writer_worker(camera_id, stop):
    recording   = None  # A nyitott fájlpár: főstream + negyed felbontású al-stream a gyors tekeréshez
    open_params = None  # A nyitott fájlok (szélesség, magasság, FPS, forgatás) adatai

    while True:
        try:
            frame, fps, rotation = write_queues[camera_id].get(timeout=0.5)
            height, width = frame.shape[:2]

            # Beállítás változott felvétel közben: egy fájlon belül nem változhat a formátum,
            # ezért lezárjuk a fájlokat, és a felvétel új fájlban folytatódik
            if recording is not None and (width, height, fps, rotation) != open_params:
                close_recording_files(camera_id, recording)
                recording = None
                log.info(f"[REC SPLIT] Kamera {camera_id}: új beállítás, új fájl kezdődik.")

            # Darabolás: az új fájl megnyílik, és csak utána zárul a régi a háttérben, mert a lezárás
            # másodpercekig tarthat, és ennyi ideig nem szabad képkockát veszíteni
            elif (recording is not None and recording_flags[camera_id]
                  and time.monotonic() - rec_start >= storage_settings["segment_minutes"] * 60):
                finished  = recording
                recording = open_recording_files(camera_id, fps)
                rec_start = time.monotonic()
                frames_written = 0
                threading.Thread(target=close_recording_files, args=(camera_id, finished)).start()
                log.info(f"[REC SPLIT] Kamera {camera_id}: új darab: {recording['path']}")

            # Felvétel indítása: fájlok megnyitása az első képkocka érkezésekor
            if recording_flags[camera_id] and recording is None:
                recording = open_recording_files(camera_id, fps)
                log.info(f"[REC START] Kamera {camera_id}: {recording['path']} ({width}x{height}, {fps} FPS)")
                open_params    = (width, height, fps, rotation)
                rec_start      = time.monotonic()
                frames_written = 0

            if recording is not None:
                # A fájl fix FPS-sel játszódik le, ezért a kamera tényleges FPS-étől
                # függetlenül a valós időhöz igazítjuk a képkockaszámot: gyors kameránál
                # eldobunk, lassúnál ismétlünk — így a lejátszás mindig valós idejű.
                target = int((time.monotonic() - rec_start) * fps) + 1
                if frames_written >= target:
                    continue
                # Hosszabb kimaradást (újracsatlakozás) nem töltünk ki állóképpel
                if target - frames_written > fps * 2:
                    frames_written = target - 1

                # OpenCV BGR-t használ, az imageio RGB-t vár → csatornák felcserélése
                frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                # Al-stream: negyed felbontás
                frame_sub = cv2.resize(frame_rgb, (width // 4, height // 4), interpolation=cv2.INTER_LINEAR)

                while frames_written < target:
                    recording["main"].append_data(frame_rgb)
                    recording["sub"].append_data(frame_sub)
                    frames_written += 1

        except queue.Empty:
            # Sor üres + felvétel leállítva vagy kamera törölve → fájlok biztonságos lezárása
            if (stop.is_set() or not recording_flags[camera_id]) and recording is not None:
                close_recording_files(camera_id, recording)
                recording = None
                log.info(f"[REC STOP]  Kamera {camera_id}: fájlok lezárva.")
            if stop.is_set():
                return

        except Exception as e:
            # A szálnak életben kell maradnia, különben a kamera többé nem tudna rögzíteni
            handle_recording_error(camera_id, e)
            if recording is not None:
                close_recording_files(camera_id, recording)
                recording = None


# ==========================================
# 3. TÁRHELYKEZELÉS — automatikus törlés és ellenőrzés
# ==========================================

def archive_unavailable_reason():
    if REQUIRE_ARCHIVE_MARKER and not os.path.exists(os.path.join(ARCHIVE_DIR, ARCHIVE_MARKER_FILE)):
        return "A felvételek tárolója nem érhető el (lehet, hogy ki van húzva az USB)."
    return None


def low_space_reason():
    minimum = storage_settings["min_free_percent"]
    if minimum and storage.free_percent(shutil.disk_usage(ARCHIVE_DIR)) < minimum:
        return f"Kevesebb mint {minimum}% a szabad hely, és nincs több törölhető felvétel."
    return None


def stop_all_recordings(reason):
    for camera_id, is_recording in list(recording_flags.items()):
        if is_recording:
            recording_flags[camera_id] = False
            log_event(camera_id, f"[FELVÉTEL] Automatikusan leállt: {reason}")


def run_storage_maintenance():
    """Automatikus törlés és a tároló ellenőrzése. Visszaadja a felvételt akadályozó okot, ha van."""
    problem = archive_unavailable_reason()
    if problem is None:
        try:
            with _storage_lock:
                protected, newly_protected = storage.initialize_protected(ARCHIVE_DIR)
                deleted, enough_space = storage.run_cleanup(
                    ARCHIVE_DIR, storage_settings["retention_days"], storage_settings["min_free_percent"],
                    protected, set(active_recordings),
                )
            if newly_protected:
                log_system_event(f"[TÁRHELY] {newly_protected} meglévő felvétel védett lett az első induláskor")
            for path, reason in deleted:
                log_system_event(f"[AUTOMATIKUS TÖRLÉS] {path} ({reason})")
            if not enough_space:
                problem = low_space_reason() or "Kevés a szabad hely."
        except storage.ProtectedListError as e:
            # Ha nem tudjuk, mi védett, inkább semmit nem törlünk
            log.error(f"[TÁRHELY] A védett lista nem olvasható: {e}")
            problem = "A védett felvételek listája sérült, ezért az automatikus törlés szünetel."
        except OSError as e:
            log.error(f"[TÁRHELY] A tároló nem érhető el: {e}")
            problem = "A felvételek tárolója nem érhető el."

    if problem and problem != storage_state["problem"]:
        log_system_event(f"[TÁRHELY] {problem}")
    storage_state["problem"] = problem
    # A sérült lista nem akadályozza a rögzítést, a többi ok igen
    if problem and not problem.startswith("A védett"):
        stop_all_recordings(problem)
    return problem


def storage_worker():
    while True:
        try:
            run_storage_maintenance()
        except Exception:
            log.exception("[TÁRHELY] Váratlan hiba az ellenőrzés közben")
        time.sleep(STORAGE_CHECK_INTERVAL_S)


# ==========================================
# SZÁLAK INDÍTÁSA
# ==========================================

os.makedirs(ARCHIVE_DIR, exist_ok=True)


def start_camera_threads(camera_id):
    stop = camera_stop_events[camera_id] = threading.Event()
    threading.Thread(target=camera_worker, args=(camera_id, stop), daemon=True).start()
    writer = threading.Thread(target=writer_worker, args=(camera_id, stop), daemon=True)
    writer_threads[camera_id] = writer
    writer.start()


for cam_id in CAMERAS:
    start_camera_threads(cam_id)

threading.Thread(target=storage_worker, daemon=True).start()


# ==========================================
# 4. WEBSZERVER — Flask route-ok
# ==========================================

def generate_web_stream(camera_id):
    """MJPEG stream: multipart/x-mixed-replace MIME típussal a böngésző folyamatosan frissíti a képet."""
    while camera_id in CAMERAS:  # Törölt kamera streamje véget ér
        frame_bytes = latest_frames.get(camera_id)
        if frame_bytes:
            yield (b"--frame\r\n"
                   b"Content-Type: image/jpeg\r\n\r\n" + frame_bytes + b"\r\n")
        time.sleep(1 / camera_settings[camera_id]["fps"])


@app.before_request
def reject_cross_site_changes():
    """Más webhelyről indított módosító kérés elutasítása (CSRF elleni második védelem)."""
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        if request.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
            abort(403)


@app.before_request
def require_login():
    if request.endpoint in ("login", "logout", "static"):
        return
    if not session.get("authenticated"):
        if (request.path.startswith("/api/")
                or request.path == "/status"
                or request.path.startswith("/video_feed/")
                or request.path.startswith("/archivum_video/")):
            return jsonify({"error": "unauthorized"}), 401
        return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("authenticated"):
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        wait = reserve_password_attempt(request.remote_addr)
        if wait:
            return render_template("login.html", error=f"Túl sok próba, várj {wait} másodpercet"), 429
        if bcrypt.checkpw(request.form.get("password", "").encode(), _auth["password_hash"].encode()):
            clear_password_attempts(request.remote_addr)
            session["authenticated"] = True
            session.permanent = True
            return redirect(url_for("index"))
        log_system_event(f"[HIBÁS JELSZÓ] {request.remote_addr}")
        error = "Hibás jelszó"
    return render_template("login.html", error=error)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/api/change_password", methods=["POST"])
def change_password():
    data       = request.get_json() or {}
    current_pw = data.get("current", "")
    new_pw     = data.get("new", "")

    wait = reserve_password_attempt(request.remote_addr)
    if wait:
        return jsonify({"error": f"Túl sok próba, várj {wait} másodpercet"}), 429
    if not bcrypt.checkpw(current_pw.encode(), _auth["password_hash"].encode()):
        log_system_event(f"[HIBÁS JELSZÓ] {request.remote_addr} (jelszócsere)")
        return jsonify({"error": "Hibás jelenlegi jelszó"}), 400
    clear_password_attempts(request.remote_addr)

    if len(new_pw) < MIN_PASSWORD_LENGTH:
        return jsonify({"error": f"Az új jelszó legalább {MIN_PASSWORD_LENGTH} karakter legyen"}), 400
    if len(new_pw.encode()) > MAX_PASSWORD_BYTES:
        return jsonify({"error": f"Az új jelszó legfeljebb {MAX_PASSWORD_BYTES} bájt lehet"}), 400

    new_hash   = bcrypt.hashpw(new_pw.encode(), bcrypt.gensalt(12)).decode()
    new_secret = secrets.token_hex(32)

    update_config(lambda config_data: config_data["auth"].update(password_hash=new_hash, secret_key=new_secret))

    # Az új kulccsal minden korábbi belépés érvénytelen lesz: a többi eszköz kilép,
    # ez a munkamenet pedig az új kulccsal aláírva marad érvényes
    _auth["password_hash"] = new_hash
    _auth["secret_key"]    = new_secret
    app.secret_key         = new_secret
    session.modified       = True

    log_system_event(f"[JELSZÓCSERE] {request.remote_addr} megváltoztatta a jelszót, a többi eszköz kilépett")
    return jsonify({"success": True})


@app.route("/")
def index():
    log_system_event(f"[BELÉPÉS] {request.remote_addr} megnyitotta az oldalt")
    return render_template("index.html", cameras=dict(CAMERAS), camera_settings=camera_settings,
                           resolutions=RESOLUTIONS, rotations=ROTATIONS, min_fps=MIN_FPS, max_fps=MAX_FPS,
                           max_name_length=MAX_NAME_LENGTH)


@app.route("/api/disconnect", methods=["POST"])
def disconnect():
    log_system_event(f"[KILÉPÉS] {request.remote_addr} elhagyta az oldalt")
    return "", 204


@app.route("/video_feed/<int:camera_id>")
def video_feed(camera_id):
    if camera_id not in CAMERAS:
        return jsonify({"error": "Kamera nem található"}), 404
    resp = Response(generate_web_stream(camera_id),
                    mimetype="multipart/x-mixed-replace; boundary=frame")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return resp




@app.route("/toggle_record/<int:camera_id>", methods=["POST"])
def toggle_record(camera_id):
    if camera_id not in CAMERAS:
        return jsonify({"error": "Kamera nem található"}), 404
    if not recording_flags[camera_id]:
        # Indítás előtt friss állapot kell: lehet, hogy a törlés fel tud szabadítani helyet
        problem = run_storage_maintenance()
        if problem and not problem.startswith("A védett"):
            return jsonify({"error": problem}), 507
        storage_state["write_error"] = None
    recording_flags[camera_id] = not recording_flags[camera_id]
    is_recording = recording_flags[camera_id]
    log.info(f"[REC] Kamera {camera_id}: {'ELINDÍTVA' if is_recording else 'LEÁLLÍTVA'}")
    action = "elindította" if is_recording else "leállította"
    log_event(camera_id, f"[FELVÉTEL] {request.remote_addr} {action} a felvételt")
    return jsonify({"camera_id": camera_id, "recording": is_recording})


@app.route("/api/camera_settings/<int:camera_id>", methods=["POST"])
def save_camera_settings(camera_id):
    """Kamera nevének, címeinek, felbontásának és FPS-ének mentése — újraindítás nélkül életbe lép."""
    data    = request.get_json(silent=True) or {}
    width   = data.get("width")
    height  = data.get("height")
    fps     = data.get("fps")
    rotation = data.get("rotation")
    name    = clean_camera_name(data.get("name"))
    url     = data.get("url").strip() if isinstance(data.get("url"), str) else ""
    led_url = data.get("led_url").strip() if isinstance(data.get("led_url"), str) else ""

    if not name:
        return jsonify({"error": f"A név 1–{MAX_NAME_LENGTH} karakter lehet"}), 400
    if not is_valid_url(url, STREAM_URL_SCHEMES):
        return jsonify({"error": "Érvénytelen stream URL (rtsp://, http:// vagy https:// kezdetű legyen)"}), 400
    if led_url and not is_valid_url(led_url, LED_URL_SCHEMES):
        return jsonify({"error": "Érvénytelen LED URL (http:// vagy https:// kezdetű legyen)"}), 400
    error = validate_camera_settings(width, height, fps, rotation)
    if error:
        return jsonify({"error": error}), 400

    with _cameras_lock:
        camera = CAMERAS.get(camera_id)
        if camera is None:
            return jsonify({"error": "Kamera nem található"}), 404
        if url_in_use(url, except_id=camera_id):
            return jsonify({"error": "Ez a cím már egy másik kameránál szerepel"}), 400

        new_settings = {"width": width, "height": height, "fps": fps, "rotation": rotation}
        url_changed  = url != camera["url"]
        led_changed  = bool(led_url) != bool(camera.get("led_url"))

        def change(config_data):
            entry = config_data["cameras"][str(camera_id)]
            entry.update(name=name, url=url, **new_settings)
            entry.pop("led_url", None)
            if led_url:
                entry["led_url"] = led_url

        update_config(change)
        camera_settings[camera_id] = new_settings
        camera["name"] = name

        if led_url:
            if led_url != camera.get("led_url"):
                led_states[camera_id] = False
            camera["led_url"] = led_url
        else:
            camera.pop("led_url", None)
            led_states.pop(camera_id, None)

        if url_changed:
            # A szál észreveszi a változást; a nullázás a készenlétből is kihozza
            camera["url"] = url
            camera_states[camera_id] = {"status": "connecting", "retries": 0}

    log_event(camera_id, f"[BEÁLLÍTÁS] {request.remote_addr} módosította: {width}x{height}, {fps} FPS, forgatás: {rotation}°")
    if url_changed:
        log_event(camera_id, f"[CÍM] {request.remote_addr} új címet adott meg: {url}")
    return jsonify({"success": True, "name": name, "led_changed": led_changed, **new_settings})


@app.route("/api/cameras", methods=["POST"])
def add_camera():
    """Új kamera felvétele: a configba mentődik, és újraindítás nélkül elindul."""
    data = request.get_json(silent=True) or {}
    name = clean_camera_name(data.get("name"))
    if not name:
        return jsonify({"error": f"A név 1–{MAX_NAME_LENGTH} karakter lehet"}), 400

    entry, error = build_camera_entry(data.get("type"), data.get("address"))
    if error:
        return jsonify({"error": error}), 400

    global _next_camera_id
    with _cameras_lock:
        if url_in_use(entry["url"]):
            return jsonify({"error": "Ez a kamera már szerepel a listában"}), 400

        camera_id = _next_camera_id
        camera    = {"name": name, **entry}

        def change(config_data):
            config_data["cameras"][str(camera_id)] = camera
            config_data["next_camera_id"] = camera_id + 1

        update_config(change)
        _next_camera_id = camera_id + 1

        # Először a kiegészítő állapotok jönnek létre, mert a CAMERAS-ban már létezőnek
        # látszó kamerát a route-ok azonnal használhatják
        camera_settings[camera_id] = load_camera_settings(camera_id, camera)
        camera_states[camera_id]   = {"status": "offline", "retries": 0}
        recording_flags[camera_id] = False
        write_queues[camera_id]    = queue.Queue(maxsize=30)
        latest_frames[camera_id]   = FRAME_OFFLINE
        if camera.get("led_url"):
            led_states[camera_id]  = False
        CAMERAS[camera_id]         = camera
        start_camera_threads(camera_id)

    log_event(camera_id, f"[ÚJ KAMERA] {request.remote_addr} hozzáadta: {entry['url']}")
    return jsonify({"success": True, "camera_id": camera_id})


@app.route("/api/cameras/<int:camera_id>", methods=["DELETE"])
def delete_camera(camera_id):
    """Kamera törlése a configból és a szálak leállítása. A felvételei az archívumban maradnak."""
    with _cameras_lock:
        if camera_id not in CAMERAS:
            return jsonify({"error": "Kamera nem található"}), 404
        if recording_flags[camera_id]:
            return jsonify({"error": "A kamera éppen felvételt készít, előbb állítsd le"}), 409

        def change(config_data):
            del config_data["cameras"][str(camera_id)]
            config_data["next_camera_id"] = _next_camera_id

        update_config(change)
        log_event(camera_id, f"[TÖRLÉS] {request.remote_addr} törölte a kamerát")
        del CAMERAS[camera_id]
        camera_stop_events.pop(camera_id).set()
        writer_threads.pop(camera_id, None)

    return jsonify({"success": True})


@app.route("/api/led/<int:camera_id>/<int:state>", methods=["POST"])
def toggle_led(camera_id, state):
    """
    ESP32-CAM flash LED vezérlés.
    state=1 → bekapcsol (val=255), state=0 → kikapcsol (val=0).
    Standard ESP32-CAM firmware endpoint: /control?var=flash&val=[0-255]
    """
    if camera_id not in CAMERAS:
        return jsonify({"error": "Kamera nem található"}), 404

    led_url = CAMERAS[camera_id].get("led_url")
    if not led_url:
        return jsonify({"error": "Ez a kamera nem támogatja az LED vezérlést"}), 400

    try:
        requests.get(f"{led_url}?v={255 if state else 0}", timeout=2)
        led_states[camera_id] = bool(state)
        return jsonify({"camera_id": camera_id, "led": bool(state)})
    except requests.RequestException as e:
        return jsonify({"error": f"Nem sikerült elérni a kamerát: {e}"}), 503


@app.route("/status")
def status():
    """Összesített állapot — a frontend 2 másodpercenként lekéri."""
    active_viewers[request.remote_addr] = time.time()

    payload = {}
    # list(): közben változhat a kameralista
    for cam_id, camera in list(CAMERAS.items()):
        # Szöveges kulcs, mert a JSON-rendezés nem bírja a vegyes kulcstípusokat
        payload[str(cam_id)] = {
            **camera_states[cam_id],
            "recording": recording_flags[cam_id],
            "led"      : led_states.get(cam_id, None),
            "has_led"  : bool(camera.get("led_url")),
        }
    # A kamerák mellett ez az egy kulcs nem kameraazonosító, a frontend külön kezeli
    payload["storage_alert"] = storage_state["write_error"] or storage_state["problem"]
    return jsonify(payload)


def read_protected():
    """A védett felvételek halmaza. Hibás lista esetén ProtectedListError."""
    return storage.load_protected(ARCHIVE_DIR) or set()


def camera_label(camera_id):
    camera = CAMERAS.get(camera_id)
    return camera["name"] if camera else f"CAM{camera_id:02d}"


@app.route("/api/videos")
def list_videos():
    """Felvételek lapozva, kamera és dátum szerint szűrve, a legújabbal kezdve."""
    camera    = request.args.get("camera", type=int)
    date_from = request.args.get("from", "")
    date_to   = request.args.get("to", "")
    page      = max(1, request.args.get("page", 1, type=int))
    per_page  = min(MAX_VIDEOS_PER_PAGE, max(1, request.args.get("per_page", VIDEOS_PER_PAGE, type=int)))

    for value in (date_from, date_to):
        if value and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return jsonify({"error": "Hibás dátum"}), 400

    try:
        protected = read_protected()
    except storage.ProtectedListError:
        return jsonify({"error": "A védett felvételek listája sérült"}), 500

    with _storage_lock:
        active = set(active_recordings)
    recordings = storage.scan_recordings(ARCHIVE_DIR, protected, active)
    cameras    = sorted({r.camera for r in recordings})

    def matches(recording):
        date = recording.started.strftime("%Y-%m-%d")
        return ((camera is None or recording.camera == camera)
                and (not date_from or date >= date_from)
                and (not date_to or date <= date_to))

    filtered = [r for r in recordings if matches(r)]
    pages    = max(1, (len(filtered) + per_page - 1) // per_page)
    page     = min(page, pages)
    start    = (page - 1) * per_page

    return jsonify({
        "items": [{
            "path"     : r.path,
            "camera"   : r.camera,
            "date"     : r.started.strftime("%Y-%m-%d"),
            "time"     : r.started.strftime("%H:%M:%S"),
            "size"     : r.size,
            "protected": r.protected,
            "recording": r.active,
        } for r in filtered[start:start + per_page]],
        "total"  : len(filtered),
        "page"   : page,
        "pages"  : pages,
        "cameras": [{"id": c, "name": camera_label(c)} for c in cameras],
    })


@app.route("/api/videos", methods=["DELETE"])
def delete_video():
    path = (request.get_json(silent=True) or {}).get("path")
    if storage.parse_recording_path(path) is None:
        return jsonify({"error": "Érvénytelen felvétel"}), 400
    if not os.path.exists(os.path.join(ARCHIVE_DIR, *path.split("/"))):
        return jsonify({"error": "A felvétel nem található"}), 404

    try:
        with _storage_lock:
            if path in active_recordings:
                return jsonify({"error": "A felvétel éppen készül"}), 409
            if path in read_protected():
                return jsonify({"error": "Védett felvétel nem törölhető, előbb vedd le a védelmet"}), 409
            storage.delete_recording(ARCHIVE_DIR, path)
    except storage.ProtectedListError:
        return jsonify({"error": "A védett felvételek listája sérült"}), 500
    except OSError as e:
        log.error(f"[TÁRHELY] Nem sikerült törölni: {path}: {e}")
        return jsonify({"error": "Nem sikerült törölni a felvételt"}), 500

    log_system_event(f"[FELVÉTEL TÖRLÉSE] {request.remote_addr} törölte: {path}")
    return jsonify({"success": True})


@app.route("/api/videos/protect", methods=["POST"])
def protect_video():
    data      = request.get_json(silent=True) or {}
    path      = data.get("path")
    protected = data.get("protected")
    if storage.parse_recording_path(path) is None or not isinstance(protected, bool):
        return jsonify({"error": "Érvénytelen kérés"}), 400
    if not os.path.exists(os.path.join(ARCHIVE_DIR, *path.split("/"))):
        return jsonify({"error": "A felvétel nem található"}), 404

    try:
        with _storage_lock:
            current = read_protected()
            (current.add if protected else current.discard)(path)
            storage.save_protected(ARCHIVE_DIR, current)
    except storage.ProtectedListError:
        return jsonify({"error": "A védett felvételek listája sérült"}), 500
    except OSError as e:
        log.error(f"[TÁRHELY] Nem sikerült menteni a védett listát: {e}")
        return jsonify({"error": "Nem sikerült menteni"}), 500

    action = "védetté tette" if protected else "levette a védelmet erről"
    log_system_event(f"[VÉDELEM] {request.remote_addr} {action}: {path}")
    return jsonify({"success": True, "protected": protected})


@app.route("/api/storage")
def storage_status():
    """Tárhely állapota és beállításai a felület számára."""
    try:
        usage = shutil.disk_usage(ARCHIVE_DIR)
        protected = read_protected()
    except storage.ProtectedListError:
        return jsonify({"error": "A védett felvételek listája sérült"}), 500
    except OSError:
        return jsonify({"error": "A tároló nem érhető el"}), 503

    recordings = storage.scan_recordings(ARCHIVE_DIR, protected)
    return jsonify({
        "total"        : usage.total,
        "free"         : usage.free,
        "free_percent" : round(storage.free_percent(usage), 1),
        "archive_size" : sum(r.size for r in recordings),
        "recordings"   : len(recordings),
        "protected"    : sum(1 for r in recordings if r.protected),
        "unmanaged"    : storage.count_unmanaged_videos(ARCHIVE_DIR),
        "alert"        : storage_state["write_error"] or storage_state["problem"],
        "settings"     : storage_settings,
        "ranges"       : STORAGE_SETTING_RANGES,
    })


@app.route("/api/storage/settings", methods=["POST"])
def save_storage_settings():
    data = request.get_json(silent=True) or {}
    new_settings = {key: data.get(key) for key in DEFAULT_STORAGE_SETTINGS}
    for key, value in new_settings.items():
        if not validate_storage_setting(key, value):
            low, high = STORAGE_SETTING_RANGES[key]
            return jsonify({"error": f"Érvénytelen érték ({key}): {low} és {high} között lehet"}), 400

    update_config(lambda config_data: config_data.update(storage=new_settings))
    storage_settings.update(new_settings)
    log_system_event(
        f"[TÁRHELY] {request.remote_addr} módosította: megőrzés {new_settings['retention_days']} nap, "
        f"szabad hely {new_settings['min_free_percent']}%, darabolás {new_settings['segment_minutes']} perc"
    )
    return jsonify({"success": True, "settings": storage_settings, "alert": run_storage_maintenance()})


@app.route("/archivum_video/<path:filepath>")
def serve_video(filepath):
    # A teljes útvonalat a send_from_directory ellenőrzi, így nem lehet kilépni az archívumból
    if not filepath.endswith(".mp4"):
        abort(404)
    download = request.args.get("download") == "1"
    if download:
        with _storage_lock:
            if filepath in active_recordings:  # A készülő fájlnak még nincs indexe, nem lenne lejátszható
                return jsonify({"error": "A felvétel éppen készül"}), 409
    return send_from_directory(ARCHIVE_DIR, filepath, as_attachment=download)


@app.route("/api/viewers")
def list_viewers():
    """Jelenleg az oldalon lévő IP-k (utolsó 10 mp-ben pingelt /status)."""
    now = time.time()
    current = [ip for ip, last in active_viewers.items() if now - last < VIEWER_TIMEOUT]
    return jsonify(current)


@app.route("/api/events")
def list_events():
    """Eseménynapló utolsó 200 sora — legújabb először."""
    log_path = os.path.join(ARCHIVE_DIR, "events.log")
    if not os.path.exists(log_path):
        return jsonify([])
    with open(log_path, encoding="utf-8") as f:
        lines = [l.rstrip() for l in f if l.strip()]
    return jsonify(list(reversed(lines[-200:])))


@app.route("/api/reset/<int:camera_id>", methods=["POST"])
def reset_camera(camera_id):
    """Retries nullázása → camera_worker automatikusan újracsatlakozik."""
    if camera_id not in CAMERAS:
        return jsonify({"status": "error", "message": "Kamera nem található"}), 404
    camera_states[camera_id]["retries"] = 0
    camera_states[camera_id]["status"]  = "connecting"
    return jsonify({"status": "success"})

def _graceful_shutdown(signum, frame):
    """SIGTERM / SIGINT: a készülő felvételek lezárása, majd tiszta kilépés — a finally blokk logol."""
    # Lezárás nélkül az MP4 fájlnak nem készül el az indexe, és nem lehetne lejátszani
    for camera_id in list(recording_flags):
        recording_flags[camera_id] = False
    for stop_event in list(camera_stop_events.values()):
        stop_event.set()
    deadline = time.monotonic() + SHUTDOWN_WAIT_S
    for writer in list(writer_threads.values()):
        writer.join(max(0, deadline - time.monotonic()))
    sys.exit(0)

if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _graceful_shutdown)
    signal.signal(signal.SIGINT,  _graceful_shutdown)
    log_system_event("[SZERVER] Elindult")
    try:
        ssl_context = (_ssl["cert"], _ssl["key"]) if _ssl else None
        app.run(
            host=HOST,
            port=5000,
            debug=False,
            threaded=True,
            ssl_context=ssl_context
        )
    finally:
        log_system_event("[SZERVER] Leállt")
