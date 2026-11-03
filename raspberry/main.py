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
import unicodedata
from urllib.parse import urlparse
from datetime import datetime, timedelta
from werkzeug.middleware.proxy_fix import ProxyFix
from flask.sessions import SecureCookieSessionInterface
from flask import Flask, abort, render_template, Response, jsonify, send_from_directory, request, session, redirect, url_for
from functools import wraps
import bcrypt
import imageio

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

_config_path = os.path.join(os.path.dirname(__file__), "config.json")
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

# RPi4: "h264_v4l2m2m" (hardveres), PC/Docker: "libx264" (szoftveres)
H264_CODEC = os.environ.get("H264_CODEC", "libx264")

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

def log_event(camera_id, message):
    """Kamera esemény naplózása az archívum events.log fájlba."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} | Kamera {camera_id} ({CAMERAS[camera_id]['name']}) | {message}\n"
    try:
        with open(os.path.join(ARCHIVE_DIR, "events.log"), "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass
    log.warning(f"[EVENT] {line.strip()}")


def log_system_event(message):
    """Rendszerszintű esemény naplózása (belépés, kilépés) — nincs kamera kontextus."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} | RENDSZER | {message}\n"
    try:
        with open(os.path.join(ARCHIVE_DIR, "events.log"), "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass
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

def writer_worker(camera_id, stop):
    out_main = None  # Főstream (640x480)
    out_sub  = None  # Al-stream (negyed felbontás) — gyors tekeréshez a lejátszóban
    open_params = None  # A nyitott fájlok (szélesség, magasság, FPS, forgatás) adatai

    while True:
        try:
            frame, fps, rotation = write_queues[camera_id].get(timeout=0.5)
            height, width = frame.shape[:2]

            # Beállítás változott felvétel közben: egy fájlon belül nem változhat a formátum,
            # ezért lezárjuk a fájlokat, és a felvétel új fájlban folytatódik
            if out_main is not None and (width, height, fps, rotation) != open_params:
                out_main.close()
                out_sub.close()
                out_main = None
                out_sub  = None
                log.info(f"[REC SPLIT] Kamera {camera_id}: új beállítás, új fájl kezdődik.")

            # Felvétel indítása: fájlok megnyitása az első képkocka érkezésekor
            if recording_flags[camera_id] and out_main is None:
                now       = datetime.now()
                ts        = now.strftime("%Y%m%d_%H%M%S")
                subdir    = os.path.join(ARCHIVE_DIR, now.strftime("%Y-%m-%d"), f"cam_{camera_id}")
                os.makedirs(subdir, exist_ok=True)
                path_main = os.path.join(subdir, f"cam_{camera_id}_{ts}_main.mp4")
                path_sub  = os.path.join(subdir, f"cam_{camera_id}_{ts}_sub.mp4")

                # macro_block_size=16: H.264 szabvány 16x16 pixeles makroblokkokat használ
                out_main = imageio.get_writer(path_main, fps=fps, codec=H264_CODEC, macro_block_size=16)
                out_sub  = imageio.get_writer(path_sub,  fps=fps, codec=H264_CODEC, macro_block_size=16)
                log.info(f"[REC START] Kamera {camera_id}: {path_main} ({width}x{height}, {fps} FPS)")
                open_params    = (width, height, fps, rotation)
                rec_start      = time.monotonic()
                frames_written = 0

            if out_main is not None:
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
                    out_main.append_data(frame_rgb)
                    out_sub.append_data(frame_sub)
                    frames_written += 1

        except queue.Empty:
            # Sor üres + felvétel leállítva vagy kamera törölve → fájlok biztonságos lezárása
            if (stop.is_set() or not recording_flags[camera_id]) and out_main is not None:
                out_main.close()
                out_sub.close()
                out_main = None
                out_sub  = None
                log.info(f"[REC STOP]  Kamera {camera_id}: fájlok lezárva.")
            if stop.is_set():
                return


# ==========================================
# SZÁLAK INDÍTÁSA
# ==========================================

os.makedirs(ARCHIVE_DIR, exist_ok=True)


def start_camera_threads(camera_id):
    stop = camera_stop_events[camera_id] = threading.Event()
    threading.Thread(target=camera_worker, args=(camera_id, stop), daemon=True).start()
    threading.Thread(target=writer_worker, args=(camera_id, stop), daemon=True).start()


for cam_id in CAMERAS:
    start_camera_threads(cam_id)


# ==========================================
# 3. WEBSZERVER — Flask route-ok
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
        payload[cam_id] = {
            **camera_states[cam_id],
            "recording": recording_flags[cam_id],
            "led"      : led_states.get(cam_id, None),
            "has_led"  : bool(camera.get("led_url")),
        }
    return jsonify(payload)


@app.route("/api/videos")
def list_videos():
    """Archívum videólista — {dátum: {kamera: [relative_path, ...]}} struktúrában."""
    if not os.path.exists(ARCHIVE_DIR):
        return jsonify({})
    result = {}
    for date_dir in sorted(os.listdir(ARCHIVE_DIR), reverse=True):
        date_path = os.path.join(ARCHIVE_DIR, date_dir)
        if not os.path.isdir(date_path):
            continue
        for cam_dir in sorted(os.listdir(date_path)):
            cam_path = os.path.join(date_path, cam_dir)
            if not os.path.isdir(cam_path):
                continue
            files = sorted(
                (f for f in os.listdir(cam_path) if f.endswith("_main.mp4")),
                reverse=True
            )
            if files:
                result.setdefault(date_dir, {})[cam_dir] = [
                    f"{date_dir}/{cam_dir}/{f}" for f in files
                ]
    return jsonify(result)


@app.route("/archivum_video/<path:filepath>")
def serve_video(filepath):
    # A teljes útvonalat a send_from_directory ellenőrzi, így nem lehet kilépni az archívumból
    if not filepath.endswith(".mp4"):
        abort(404)
    return send_from_directory(ARCHIVE_DIR, filepath)


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
    """SIGTERM / SIGINT: tiszta kilépés — a finally blokk logol."""
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
