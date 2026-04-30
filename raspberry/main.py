import cv2
import time
import numpy as np
import threading
import queue
from flask import jsonify
import os
from datetime import datetime
from flask import Flask, render_template, Response, jsonify, send_from_directory
import imageio

app = Flask(__name__)

# --- KONFIGURÁCIÓ ---
MAX_RETRIES = 5 
RECORDING_ENABLED = False
REC_W, REC_H = 640, 480

FRAME_DURATION_MS = 150
CALCULATED_FPS = 1000 / FRAME_DURATION_MS

#Egyszerű statikus változó, amiben a kamerák neveit és IP címüket írom bele
CAMERAS = {
    0: {"name": "Nappali (ESP32)", "url": "rtsp://192.168.1.87:554/mjpeg/1"},
    1: {"name": "Konyha",          "url": "http://192.168.0.106:81/stream"},
    2: {"name": "Garázs",          "url": "http://192.168.0.107:81/stream"},
    3: {"name": "Kert",            "url": "http://192.168.0.108:81/stream"},
    4: {"name": "Bejárat",         "url": "http://192.168.0.109:81/stream"},
}
# ez a változó tárolja, hogy milyen állásban vannak a kamerák
camera_states = {cam_id: {'status': 'offline', 'retries': 0} for cam_id in CAMERAS}

# ezzel a függvénnyel generáltatok zaj-t, arra az esetre, amikor nincsen csatlakoztatva kamera
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
# Egy szótár, ami folyamatosan, másodpercenként többször felülíródik a 
# legfrissebb képkockával. Ezt olvassa ki a webszerver, hogy megmutassa neked az élőképet.
latest_frames = {cam_id: NOISE_FRAME for cam_id in CAMERAS}
#(Írási Sor): Ez a legfontosabb "csővezeték" a szálak között. A kamerát olvasó szál ide dobálja be a képeket, 
# az író szál pedig innen veszi ki őket,
# hogy elmentse a pendrive-ra. A maxsize=30 megakadályozza, hogy a RAM beteljen, ha a pendrive túl lassú lenne.
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

        cap = cv2.VideoCapture(url) #ezzel kapcsolódok a kamerákra
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        #egy nagyon fontos mérnöki trükk: kikényszeríti, hogy a rendszer ne tárazza be a régi képeket, 
        # hanem mindig a legfrissebbet adja oda, elkerülve a késleltetést (lag).
        success, frame = cap.read() # ha sikerül a kapcsolódás, akkor elkezdi olvasni a képet

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
# 2. SD KÁRTYA ÍRÓ SZÁL (Consumer) - DUPLA STREAM IMAGEIO (H.264)
# ==========================================
def writer_worker(camera_id):
    out_main = None
    out_sub = None

    while True:
        try:
            # Várakozás az új képkockára a memóriából
            frame = write_queues[camera_id].get(timeout=0.5)
            
            # 1. Rögzítés indítása
            if recording_flags[camera_id] and out_main is None:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                
                # Fő adatfolyam (Nagy felbontás - H.264 kódolás)
                filename_main = f"archivum/cam_{camera_id}_{timestamp}_main.mp4"
                out_main = imageio.get_writer(filename_main, fps=CALCULATED_FPS, codec='libx264', macro_block_size=16)
                
                # Al-adatfolyam (Kicsi felbontás - osztva 2-vel)
                filename_sub = f"archivum/cam_{camera_id}_{timestamp}_sub.mp4"
                out_sub = imageio.get_writer(filename_sub, fps=CALCULATED_FPS, codec='libx264', macro_block_size=2)
                
                print(f"[REC] Kamera {camera_id}: Dupla mentés IMAGEIO (H.264) indítva")

            # 2. Képkockák írása a fájlokba
            if out_main is not None:
                # FONTOS: Az OpenCV BGR-t használ, az imageio RGB-t vár! Megfordítjuk a színeket.
                frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                
                # Eredeti nagy kép kiírása
                out_main.append_data(frame_rgb)
                
                # Kép lekicsinyítése a gyors pörgetéshez, majd kiírása (javítva //2-re!)
                frame_sub = cv2.resize(frame_rgb, (int(REC_W)//4, int(REC_H)//4), interpolation=cv2.INTER_LINEAR)
                out_sub.append_data(frame_sub)

        # 3. Rögzítés leállítása
        except queue.Empty:
            if not recording_flags[camera_id] and out_main is not None:
                out_main.close()
                out_sub.close()
                out_main = None
                out_sub = None
                print(f"[STOP] Kamera {camera_id}: Dupla mentés lezárva.")


#--------------------------
# --- SZÁLAK ELINDÍTÁSA ---
#--------------------------
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
        time.sleep(FRAME_DURATION_MS / 1000)

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

@app.route('/api/videos')
def list_videos():
    if not os.path.exists('archivum'):
        return jsonify([])
    
    # Keresés átírva _main.mp4-re
    files = [f for f in os.listdir('archivum') if f.endswith('_main.mp4')]
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