import atexit
import cv2
import time
import json
import logging
import numpy as np
import threading
import queue
import os
import requests
from datetime import datetime
from flask import Flask, render_template, Response, jsonify, send_from_directory, request
import imageio

app = Flask(__name__)

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
_ssl      = _config["ssl"]

MAX_RETRIES       = 5          # Ennyi sikertelen csatlakozás után áll "standby"-ba a kamera
REC_W, REC_H      = 640, 480   # Rögzítési felbontás (minden kamera erre standardizálódik)
FRAME_DURATION_MS = 150        # Webstream képkocka-időköz (150ms ≈ ~6.6 FPS)
CALCULATED_FPS    = 1000 / FRAME_DURATION_MS
ARCHIVE_DIR       = os.path.join(os.path.dirname(__file__), "archivum")

# RPi4 hardveres H.264 enkóder (V4L2 M2M) — kb. 5-10x kevesebb CPU mint szoftveres libx264.
# Ha PC-n teszteled, írd vissza: "libx264"
H264_CODEC = "h264_v4l2m2m"

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


def camera_worker(camera_id):
    url      = CAMERAS[camera_id]["url"]
    cam_name = CAMERAS[camera_id]["name"]

    while True:

        # Standby: max újracsatlakozási kísérlet elérve, várunk amíg a reset endpoint nem nulláz
        if camera_states[camera_id]["retries"] >= MAX_RETRIES:
            camera_states[camera_id]["status"] = "standby"
            latest_frames[camera_id] = FRAME_OFFLINE
            time.sleep(1)
            continue

        cap = cv2.VideoCapture(url)

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

        while True:
            success, frame = cap.read()
            if not success:
                break  # Stream megszakadt → kilépés, újracsatlakozás következik

            # Egységes felbontásra méretezés (kodek és dual-stream miatt szükséges)
            if frame.shape[1] != REC_W or frame.shape[0] != REC_H:
                frame = cv2.resize(frame, (REC_W, REC_H), interpolation=cv2.INTER_LINEAR)

            # OSD: kettős szöveg (fekete árnyék + fehér előtér) a kontrasztért
            ts  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            osd = f"{cam_name} | {ts}"
            cv2.putText(frame, osd, (12, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0),       2)
            cv2.putText(frame, osd, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)

            # Élőkép frissítése RAM-ban
            ret, buffer = cv2.imencode(".jpg", frame)
            if ret:
                latest_frames[camera_id] = buffer.tobytes()

            # Felvétel aktív: képkocka átadása a writer_worker-nek
            if recording_flags[camera_id]:
                try:
                    write_queues[camera_id].put_nowait(frame.copy())
                except queue.Full:
                    pass  # Inkább kihagyunk egy képkockát, mint hogy blokkoljuk a szálat

        cap.release()
        if recording_flags[camera_id]:
            log_event(camera_id, "Kapcsolat megszakadt felvétel közben")


# ==========================================
# 2. FELVEVŐ SZÁL — Consumer (Dupla stream H.264)
# ==========================================

def writer_worker(camera_id):
    out_main = None  # Főstream (640x480)
    out_sub  = None  # Al-stream (160x120) — gyors tekeréshez a lejátszóban

    while True:
        try:
            frame = write_queues[camera_id].get(timeout=0.5)

            # Felvétel indítása: fájlok megnyitása az első képkocka érkezésekor
            if recording_flags[camera_id] and out_main is None:
                ts        = datetime.now().strftime("%Y%m%d_%H%M%S")
                path_main = os.path.join(ARCHIVE_DIR, f"cam_{camera_id}_{ts}_main.mp4")
                path_sub  = os.path.join(ARCHIVE_DIR, f"cam_{camera_id}_{ts}_sub.mp4")

                # macro_block_size=16: H.264 szabvány 16x16 pixeles makroblokkokat használ
                out_main = imageio.get_writer(path_main, fps=CALCULATED_FPS, codec=H264_CODEC, macro_block_size=16)
                out_sub  = imageio.get_writer(path_sub,  fps=CALCULATED_FPS, codec=H264_CODEC, macro_block_size=16)
                log.info(f"[REC START] Kamera {camera_id}: {path_main}")

            if out_main is not None:
                # OpenCV BGR-t használ, az imageio RGB-t vár → csatornák felcserélése
                frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                out_main.append_data(frame_rgb)

                # Al-stream: negyed felbontás (160x120)
                frame_sub = cv2.resize(frame_rgb, (REC_W // 4, REC_H // 4), interpolation=cv2.INTER_LINEAR)
                out_sub.append_data(frame_sub)

        except queue.Empty:
            # Sor üres + felvétel leállítva → fájlok biztonságos lezárása
            if not recording_flags[camera_id] and out_main is not None:
                out_main.close()
                out_sub.close()
                out_main = None
                out_sub  = None
                log.info(f"[REC STOP]  Kamera {camera_id}: fájlok lezárva.")


# ==========================================
# SZÁLAK INDÍTÁSA
# ==========================================

os.makedirs(ARCHIVE_DIR, exist_ok=True)

for cam_id in CAMERAS:
    threading.Thread(target=camera_worker, args=(cam_id,), daemon=True).start()
    threading.Thread(target=writer_worker, args=(cam_id,), daemon=True).start()


# ==========================================
# 3. WEBSZERVER — Flask route-ok
# ==========================================

def generate_web_stream(camera_id):
    """MJPEG stream: multipart/x-mixed-replace MIME típussal a böngésző folyamatosan frissíti a képet."""
    while True:
        frame_bytes = latest_frames.get(camera_id)
        if frame_bytes:
            yield (b"--frame\r\n"
                   b"Content-Type: image/jpeg\r\n\r\n" + frame_bytes + b"\r\n")
        time.sleep(FRAME_DURATION_MS / 1000)


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
    for cam_id, state in camera_states.items():
        payload[cam_id] = {
            **state,
            "recording": recording_flags[cam_id],
            "led"      : led_states.get(cam_id, None),
            "has_led"  : bool(CAMERAS[cam_id].get("led_url")),
        }
    return jsonify(payload)


@app.route("/api/videos")
def list_videos():
    """Archívum videólista — csak _main.mp4 fájlok (a _sub rejtett, de lejátszáskor használt)."""
    if not os.path.exists(ARCHIVE_DIR):
        return jsonify([])
    files = sorted(
        (f for f in os.listdir(ARCHIVE_DIR) if f.endswith("_main.mp4")),
        reverse=True
    )
    return jsonify(files)


@app.route("/archivum_video/<filename>")
def serve_video(filename):
    return send_from_directory(ARCHIVE_DIR, filename)


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
    if camera_id not in camera_states:
        return jsonify({"status": "error", "message": "Kamera nem található"}), 404
    camera_states[camera_id]["retries"] = 0
    camera_states[camera_id]["status"]  = "connecting"
    return jsonify({"status": "success"})

if __name__ == "__main__":
    log_system_event("[SZERVER] Elindult")
    atexit.register(lambda: log_system_event("[SZERVER] Leállt"))
    app.run(
        host="0.0.0.0",
        port=5000,
        debug=False,
        threaded=True,
        ssl_context=(_ssl["cert"], _ssl["key"])
    )
