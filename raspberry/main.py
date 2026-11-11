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
import secrets
import shutil
import unicodedata
from datetime import datetime, timedelta
from werkzeug.middleware.proxy_fix import ProxyFix
from flask.sessions import SecureCookieSessionInterface
from flask import Flask, abort, render_template, Response, jsonify, send_from_directory, request, session, redirect, url_for
import bcrypt
import imageio
from waitress import serve

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

OPEN_TIMEOUT_MS   = 5000       # Ennyi ideig próbálkozunk egy kamera stream megnyitásával
RETRY_DELAY_S     = 0.5        # Sikertelen csatlakozás után ennyit várunk a következő próbáig
REC_W, REC_H      = 640, 480   # Az OSD méretezésének alapja és az offline kép mérete
DEFAULT_FPS       = round(1000 / 150, 2)  # Alapértelmezett képkockasebesség (150ms ≈ ~6.67 FPS)
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_BYTES  = 72       # A bcrypt ennél hosszabb jelszót csendben levágna
# Belépési próbák korlátozása IP-címenként: a szabad próbák után egyre hosszabb a várakozás
LOGIN_FREE_ATTEMPTS = 5
LOGIN_BASE_LOCK_S   = 30
LOGIN_MAX_LOCK_S    = 3600
LOGIN_FORGET_AFTER_S = 24 * 3600
ARCHIVE_DIR       = os.environ.get("ARCHIVE_DIR", os.path.join(os.path.dirname(__file__), "archivum"))

# Szoftveres kódoló: minden gépen működik. A hardveres h264_v4l2m2m Pi 3 B+-on hibás fájlokat készített.
H264_CODEC = os.environ.get("H264_CODEC", "libx264")

# Kulcsképek távolsága másodpercben: tekeréskor a lejátszó a legközelebbi kulcsképtől dekódol.
# Az al-stream a gyors tekeréshez készül, ezért abban sűrűbbek.
KEYFRAME_INTERVAL_MAIN_S = 2
KEYFRAME_INTERVAL_SUB_S  = 1

# Tárhelykezelés alapértékei, a config.json "storage" része felülírja. A 0 az adott szabályt kikapcsolja.
DEFAULT_STORAGE_SETTINGS = {"retention_days": 30, "min_free_percent": 10, "segment_minutes": 15}
STORAGE_CHECK_INTERVAL_S = 60
EVENT_LOG_MAX_BYTES      = 5 * 1024 * 1024  # E fölött új naplófájl kezdődik, és csak az előző marad meg
SHUTDOWN_WAIT_S          = 8        # A docker stop 10 mp után kényszerrel leállít
SERVER_PORT              = 5000
# Minden megnyitott élőkép végig lefoglal egy szálat. A korlát miatt a felület többi kérésének
# mindig marad szabad szál, különben sok megnyitott élőkép mellett az egész oldal megállna.
SERVER_THREADS           = 24
MAX_LIVE_STREAMS         = 20
# A Waitress alapból gyűjti a kis válaszokat, az élőkép képkockáit viszont azonnal ki kell küldeni
SERVER_SEND_BYTES        = 1
# A Waitress a kimeneti puffert csak ennél a méretnél cseréli újra, addig a már elküldött adat is
# a memóriában marad. Az alapérték (16 MB) streamenként kb. 12 MB RAM-ot és lassú kliensnél akár
# több másodperc késést okozna az élőképben. A videófájlokat ez nem érinti, azok nem ide kerülnek.
SERVER_OUTBUF_HIGH_WATERMARK = 1024 * 1024

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


def update_config(change):
    """config.json beolvasása, módosítása a kapott függvénnyel, majd visszaírása."""
    with _config_lock:
        with open(_config_path, encoding="utf-8") as f:
            config_data = json.load(f)
        change(config_data)
        with open(_config_path, "w", encoding="utf-8") as f:
            json.dump(config_data, f, indent=4, ensure_ascii=False)


def load_storage_settings(saved):
    """Tárhely-beállítások a config.json-ból; hiányzó vagy hibás érték esetén az alapérték."""
    settings = dict(DEFAULT_STORAGE_SETTINGS)
    for key, value in (saved or {}).items():
        if key not in settings:
            continue
        minimum = 1 if key == "segment_minutes" else 0
        if type(value) is int and value >= minimum:
            settings[key] = value
        else:
            log.warning(f"Tárhely: hibás config ({key}), alapértelmezés használva")
    return settings


def load_camera_fps(camera_id, cam):
    """Kamera FPS-e a configból (opcionális); hibás érték esetén az alapértelmezés."""
    fps = cam.get("fps", DEFAULT_FPS)
    if isinstance(fps, bool) or not isinstance(fps, (int, float)) or fps <= 0:
        log.warning(f"Kamera {camera_id}: hibás FPS a configban, alapértelmezés használva")
        return DEFAULT_FPS
    return fps


# A kamerák a config.json-ból jönnek, futás közben nem változnak
camera_fps = {cam_id: load_camera_fps(cam_id, cam) for cam_id, cam in CAMERAS.items()}
writer_threads = {}

storage_settings = load_storage_settings(_config.get("storage"))
# Sorosítja a törlést, és védi a készülő felvételek halmazát
_storage_lock = threading.Lock()
active_recordings = set()  # A készülő vagy lezárás alatt álló felvételek relatív útvonala
storage_state = {"problem": None, "write_error": None}

# ==========================================
# MEGOSZTOTT ÁLLAPOT
# Fontos: minden dict itt van definiálva, a szálak indítása ELŐTT.
# ==========================================

# Kamerák hálózati állapota: "online" vagy "offline"
camera_status = {cam_id: "offline" for cam_id in CAMERAS}

# Felvételi zászlók — Flask route állítja, writer_worker olvassa
recording_flags = {cam_id: False for cam_id in CAMERAS}

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
live_stream_slots = threading.BoundedSemaphore(MAX_LIVE_STREAMS)
# Leálláskor az élőképek is befejeződnek, különben a Waitress a szálaikra várna
server_stopping = threading.Event()


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
    line = f"{ts} | Kamera {camera_id} ({CAMERAS[camera_id]['name']}) | {message}\n"
    write_event_line(line)
    log.warning(f"[EVENT] {line.strip()}")


def log_system_event(message):
    """Rendszerszintű esemény naplózása (belépés, kilépés) — nincs kamera kontextus."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    write_event_line(f"{ts} | RENDSZER | {message}\n")
    log.info(f"[SYSTEM] {message}")


def camera_worker(camera_id):
    camera   = CAMERAS[camera_id]
    url      = camera["url"]
    fps      = camera_fps[camera_id]
    interval = 1 / fps
    rotate   = camera.get("rotate_180") is True
    # A cv2 betűkészlete nem ismeri az ékezeteket
    name     = unicodedata.normalize("NFKD", camera["name"]).encode("ascii", "ignore").decode()

    # Folyamatos újrapróbálkozás: a kamera bármikor visszajöhet
    while not server_stopping.is_set():
        # Az OpenCV egyszerre csak egy streamet nyit meg, ezért az időkorlát nélkül egy elérhetetlen
        # kamera a többi csatlakozását is feltartaná (alapból 30 mp-ig kísérletenként)
        cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG, [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, OPEN_TIMEOUT_MS])

        # CAP_PROP_BUFFERSIZE=1: csak a legfrissebb képkocka kerül a belső pufferbe,
        # megakadályozva a késleltetést (lag) amit a pufferelés okozna.
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        # Első frame-mel teszteljük a kapcsolatot
        success, _ = cap.read()
        if not success:
            camera_status[camera_id] = "offline"
            latest_frames[camera_id] = FRAME_OFFLINE
            cap.release()
            time.sleep(RETRY_DELAY_S)
            continue

        camera_status[camera_id] = "online"
        if recording_flags[camera_id]:
            log_event(camera_id, "Kapcsolat helyreállt — felvétel folytatódik")

        next_frame = 0
        while not server_stopping.is_set():
            # A streamet mindig a kamera ütemében olvassuk, különben lemaradnánk tőle, és a kép
            # lassítva, egyre nagyobb késéssel érkezne. Csak a beállított FPS-nek
            # megfelelő képkockákat dolgozzuk fel, a többit eldobjuk.
            if not cap.grab():
                break  # Stream megszakadt → kilépés, újracsatlakozás következik

            now = time.monotonic()
            # A fél intervallum tűrés kell, hogy a kamera FPS-ével azonos beállításnál az
            # időzítés ingadozása miatt ne maradjon ki minden második képkocka
            if now < next_frame - interval / 2:
                continue
            next_frame = max(next_frame + interval, now)

            success, frame = cap.retrieve()
            if not success:
                break

            # Fejjel lefelé szerelt kamera (config.json: "rotate_180": true). A forgatás
            # az OSD előtt történik, hogy a felirat egyenes maradjon.
            if rotate:
                frame = cv2.rotate(frame, cv2.ROTATE_180)

            # OSD: kettős szöveg (fekete árnyék + fehér előtér) a kontrasztért.
            # A méret a felbontással arányos, így nagy képen is olvasható marad.
            ts    = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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
                    write_queues[camera_id].put_nowait((frame.copy(), fps))
                except queue.Full:
                    pass  # Inkább kihagyunk egy képkockát, mint hogy blokkoljuk a szálat

        cap.release()
        if recording_flags[camera_id] and not server_stopping.is_set():
            log_event(camera_id, "Kapcsolat megszakadt felvétel közben")

    camera_status[camera_id] = "offline"
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


def writer_worker(camera_id):
    stop        = server_stopping
    recording   = None  # A nyitott fájlpár: főstream + negyed felbontású al-stream a gyors tekeréshez
    open_size   = None  # A nyitott fájlok képmérete (szélesség, magasság)

    while True:
        try:
            frame, fps = write_queues[camera_id].get(timeout=0.5)
            height, width = frame.shape[:2]

            # A kamera felbontása megváltozott (pl. újracsatlakozás után): egy fájlon belül nem
            # változhat a formátum, ezért lezárjuk a fájlokat, és a felvétel új fájlban folytatódik
            if recording is not None and (width, height) != open_size:
                close_recording_files(camera_id, recording)
                recording = None
                log.info(f"[REC SPLIT] Kamera {camera_id}: új képméret, új fájl kezdődik.")

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
                open_size      = (width, height)
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
    problem = None
    try:
        with _storage_lock:
            deleted, enough_space = storage.run_cleanup(
                ARCHIVE_DIR, storage_settings["retention_days"], storage_settings["min_free_percent"],
                set(active_recordings),
            )
        for path, reason in deleted:
            log_system_event(f"[AUTOMATIKUS TÖRLÉS] {path} ({reason})")
        if not enough_space:
            problem = low_space_reason() or "Kevés a szabad hely."
    except OSError as e:
        log.error(f"[TÁRHELY] A tároló nem érhető el: {e}")
        problem = "A felvételek tárolója nem érhető el."

    if problem and problem != storage_state["problem"]:
        log_system_event(f"[TÁRHELY] {problem}")
    storage_state["problem"] = problem
    if problem:
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


for cam_id in CAMERAS:
    threading.Thread(target=camera_worker, args=(cam_id,), daemon=True).start()
    writer_threads[cam_id] = threading.Thread(target=writer_worker, args=(cam_id,), daemon=True)
    writer_threads[cam_id].start()

threading.Thread(target=storage_worker, daemon=True).start()


# ==========================================
# 4. WEBSZERVER — Flask route-ok
# ==========================================

def generate_web_stream(camera_id):
    """MJPEG stream: multipart/x-mixed-replace MIME típussal a böngésző folyamatosan frissíti a képet."""
    while not server_stopping.is_set():
        yield (b"--frame\r\n"
               b"Content-Type: image/jpeg\r\n\r\n" + latest_frames[camera_id] + b"\r\n")
        server_stopping.wait(1 / camera_fps[camera_id])


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
    return render_template("index.html", cameras=CAMERAS)


@app.route("/api/disconnect", methods=["POST"])
def disconnect():
    log_system_event(f"[KILÉPÉS] {request.remote_addr} elhagyta az oldalt")
    return "", 204


@app.route("/video_feed/<int:camera_id>")
def video_feed(camera_id):
    if camera_id not in CAMERAS:
        return jsonify({"error": "Kamera nem található"}), 404
    if not live_stream_slots.acquire(blocking=False):
        return jsonify({"error": "Túl sok megnyitott élőkép"}), 503
    resp = Response(generate_web_stream(camera_id),
                    mimetype="multipart/x-mixed-replace; boundary=frame")
    resp.call_on_close(live_stream_slots.release)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return resp




@app.route("/toggle_record/<int:camera_id>", methods=["POST"])
def toggle_record(camera_id):
    if camera_id not in CAMERAS:
        return jsonify({"error": "Kamera nem található"}), 404
    if not recording_flags[camera_id]:
        # Indítás előtt friss állapot kell: lehet, hogy a törlés fel tud szabadítani helyet
        problem = run_storage_maintenance()
        if problem:
            return jsonify({"error": problem}), 507
        storage_state["write_error"] = None
    recording_flags[camera_id] = not recording_flags[camera_id]
    is_recording = recording_flags[camera_id]
    log.info(f"[REC] Kamera {camera_id}: {'ELINDÍTVA' if is_recording else 'LEÁLLÍTVA'}")
    action = "elindította" if is_recording else "leállította"
    log_event(camera_id, f"[FELVÉTEL] {request.remote_addr} {action} a felvételt")
    return jsonify({"camera_id": camera_id, "recording": is_recording})


@app.route("/status")
def status():
    """Összesített állapot — a frontend 2 másodpercenként lekéri."""
    active_viewers[request.remote_addr] = time.time()

    # Szöveges kulcs, mert a JSON-rendezés nem bírja a vegyes kulcstípusokat
    payload = {str(cam_id): {"status": camera_status[cam_id], "recording": recording_flags[cam_id]}
               for cam_id in CAMERAS}
    # A kamerák mellett ez az egy kulcs nem kameraazonosító, a frontend külön kezeli
    payload["storage_alert"] = storage_state["write_error"] or storage_state["problem"]
    return jsonify(payload)


def camera_label(camera_id):
    camera = CAMERAS.get(camera_id)
    return camera["name"] if camera else f"CAM{camera_id:02d}"


@app.route("/api/videos")
def list_videos():
    """Az összes felvétel, a legújabbal kezdve. A dátum és kamera szerinti csoportosítás a felület dolga."""
    with _storage_lock:
        active = set(active_recordings)
    return jsonify([{
        "path"       : r.path,
        "camera"     : r.camera,
        "camera_name": camera_label(r.camera),
        "date"       : r.started.strftime("%Y-%m-%d"),
        "time"       : r.started.strftime("%H:%M:%S"),
        "size"       : r.size,
        "recording"  : r.active,
    } for r in storage.scan_recordings(ARCHIVE_DIR, active)])


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


def _graceful_shutdown(signum, frame):
    """SIGTERM / SIGINT: a készülő felvételek lezárása, majd tiszta kilépés — a finally blokk logol."""
    server_stopping.set()
    # Lezárás nélkül az MP4 fájlnak nem készül el az indexe, és nem lehetne lejátszani
    for camera_id in list(recording_flags):
        recording_flags[camera_id] = False
    deadline = time.monotonic() + SHUTDOWN_WAIT_S
    for writer in list(writer_threads.values()):
        writer.join(max(0, deadline - time.monotonic()))
    sys.exit(0)

if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _graceful_shutdown)
    signal.signal(signal.SIGINT,  _graceful_shutdown)
    log_system_event("[SZERVER] Elindult")
    try:
        if _ssl:
            # A Waitress nem kezel HTTPS-t, ezért a saját tanúsítványos módban a Flask szervere fut
            app.run(host=HOST, port=SERVER_PORT, debug=False, threaded=True,
                    ssl_context=(_ssl["cert"], _ssl["key"]))
        else:
            # A proxy fejléceit a Waitress alapból eldobná, a megbízhatóságukról a ProxyFix dönt
            serve(app, host=HOST, port=SERVER_PORT, threads=SERVER_THREADS, send_bytes=SERVER_SEND_BYTES,
                  outbuf_high_watermark=SERVER_OUTBUF_HIGH_WATERMARK, clear_untrusted_proxy_headers=False)
    finally:
        log_system_event("[SZERVER] Leállt")
