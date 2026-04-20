import cv2
import time
import numpy as np
import threading
import queue
from flask import jsonify
import os
from datetime import datetime
from flask import Flask, render_template, Response, jsonify, send_from_directory

app = Flask(__name__)

# --- KONFIGURÁCIÓ ---
MAX_RETRIES = 5 
RECORDING_ENABLED = False
REC_W, REC_H = 640, 480 # Standard felbontás a stabil rögzítéshez

CAMERAS = {
    0: {"name": "Nappali (ESP32)", "url": "rtsp://192.168.1.87:554/mjpeg/1"},
    1: {"name": "Konyha",          "url": "http://192.168.0.106:81/stream"},
    2: {"name": "Garázs",          "url": "http://192.168.0.107:81/stream"},
    3: {"name": "Kert",            "url": "http://192.168.0.108:81/stream"},
    4: {"name": "Bejárat",         "url": "http://192.168.0.109:81/stream"},
}

camera_states = {cam_id: {'status': 'offline', 'retries': 0} for cam_id in CAMERAS}

def generate_noise(text="NO SIGNAL"):
    """Zaj generálása a standardizált REC felbontásban"""
    noise = np.random.randint(0, 256, (REC_H, REC_W, 3), dtype=np.uint8)
    text_size = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 1.0, 3)[0]
    text_x = (REC_W - text_size[0]) // 2
    text_y = (REC_H + text_size[1]) // 2
    cv2.putText(noise, text, (text_x, text_y), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 255), 3)
    ret, buffer = cv2.imencode('.jpg', noise)
    return buffer.tobytes()

NOISE_FRAME = generate_noise("KERESES...")
OFFLINE_FRAME = generate_noise("KAPCSOLAT MEGSZAKADT")

# Memória és írási pufferek inicializálása
latest_frames = {cam_id: NOISE_FRAME for cam_id in CAMERAS}
write_queues = {cam_id: queue.Queue(maxsize=30) for cam_id in CAMERAS}


# ==========================================
# 1. HÁLÓZATI OLVASÓ SZÁL (Producer)
# ==========================================
def camera_worker(camera_id):
    global RECORDING_ENABLED
    url = CAMERAS[camera_id]["url"]

    while True:
        if camera_states[camera_id]['retries'] >= MAX_RETRIES:
            camera_states[camera_id]['status'] = 'standby'
            latest_frames[camera_id] = OFFLINE_FRAME
            time.sleep(1)
            continue

        cap = cv2.VideoCapture(url)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        success, frame = cap.read()

        if success:
            camera_states[camera_id]['status'] = 'online'
            camera_states[camera_id]['retries'] = 0

            while True:
                success, frame = cap.read()
                if not success:
                    break
                
                # Standardizáljuk a méretet a hibamentes kodekhez
                if frame.shape[1] != REC_W or frame.shape[0] != REC_H:
                    frame = cv2.resize(frame, (REC_W, REC_H), interpolation=cv2.INTER_LINEAR)

                # OSD (Feliratok)
                cam_name = CAMERAS[camera_id]["name"]
                timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                osd_text = f"{cam_name} | {timestamp_str}"
                cv2.putText(frame, osd_text, (12, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 2)
                cv2.putText(frame, osd_text, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)

                # RAM frissítése az élőképhez
                ret, buffer = cv2.imencode('.jpg', frame)
                if ret:
                    latest_frames[camera_id] = buffer.tobytes()

                # Sorba rakás a rögzítéshez
                if recording_flags[camera_id]:
                    try:
                        write_queues[camera_id].put_nowait(frame.copy())
                    except queue.Full:
                        pass 

            cap.release()
        else:
            camera_states[camera_id]['status'] = 'offline'
            camera_states[camera_id]['retries'] += 1
            latest_frames[camera_id] = NOISE_FRAME
            cap.release()
            time.sleep(0.5)


# ==========================================
# 2. SD KÁRTYA ÍRÓ SZÁL (Consumer)
# ==========================================
def writer_worker(camera_id):
    out = None

    while True:
        try:
            frame = write_queues[camera_id].get(timeout=0.5)
            
            if recording_flags[camera_id] and out is None:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                
                # --- TELJES ÁTÁLLÁS WEBM/VP8-RA ---
                filename = f"archivum/cam_{camera_id}_{timestamp}.webm"
                fourcc = cv2.VideoWriter_fourcc(*'VP80')
                out = cv2.VideoWriter(filename, fourcc, 5.0, (int(REC_W), int(REC_H)))
                
                print(f"[REC] Kamera {camera_id}: Mentés indítva -> {filename}")

            if out is not None:
                out.write(frame)

        except queue.Empty:
            if not recording_flags[camera_id] and out is not None:
                out.release()
                out = None
                print(f"[STOP] Kamera {camera_id}: Mentés lezárva.")


# --- SZÁLAK ELINDÍTÁSA ---
for cam_id in CAMERAS:
    threading.Thread(target=camera_worker, args=(cam_id,), daemon=True).start()
    threading.Thread(target=writer_worker, args=(cam_id,), daemon=True).start()


# ==========================================
# 3. A WEBSZERVER (CONSUMER)
# ==========================================
def generate_web_stream(camera_id):
    while True:
        frame_bytes = latest_frames.get(camera_id)
        if frame_bytes is not None:
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
        time.sleep(0.03)

@app.route('/')
def index():
    return render_template('index.html', cameras=CAMERAS)

@app.route('/video_feed/<int:camera_id>')
def video_feed(camera_id):
    return Response(generate_web_stream(camera_id), mimetype='multipart/x-mixed-replace; boundary=frame')

# A kamerák független rögzítési állapota
recording_flags = {cam_id: False for cam_id in CAMERAS}

@app.route('/toggle_record/<int:camera_id>', methods=['POST'])
def toggle_record(camera_id):
    if camera_id in CAMERAS:
        recording_flags[camera_id] = not recording_flags[camera_id]
        állapot = "ELINDÍTVA" if recording_flags[camera_id] else "LEÁLLÍTVA"
        print(f"\n--- Kamera {camera_id} RÖGZÍTÉS: {állapot} ---\n")
        return jsonify({"camera_id": camera_id, "recording": recording_flags[camera_id]})
    return jsonify({"error": "Camera not found"}), 404

@app.route('/status')
def status():
    return jsonify(camera_states)

# --- ARCHÍVUM API JAVÍTVA WEBM-RE ---
@app.route('/api/videos')
def list_videos():
    if not os.path.exists('archivum'):
        return jsonify([])
    
    files = [f for f in os.listdir('archivum') if f.endswith(('.mp4', '.webm'))]
    files.sort(reverse=True)
    return jsonify(files)

@app.route('/archivum_video/<filename>')
def serve_video(filename):
    return send_from_directory('archivum', filename)

@app.route('/api/reset/<int:camera_id>', methods=['POST'])
def reset_camera(camera_id):
    if camera_id in camera_states:
        # A próbálkozások nullázása újraindítja a csatlakozási folyamatot a worker szálban
        camera_states[camera_id]['retries'] = 0
        camera_states[camera_id]['status'] = 'connecting'
        return jsonify({"status": "success"})
    return jsonify({"status": "error", "message": "Kamera nem található"}), 404


if __name__ == '__main__':
    if not os.path.exists('archivum'):
        os.makedirs('archivum')
        
    app.run(host='0.0.0.0', port=5000, debug=True, ssl_context='adhoc', threaded=True)