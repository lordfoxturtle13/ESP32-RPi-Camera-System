import cv2
import time
import numpy as np
from flask import Flask, render_template, Response, jsonify

app = Flask(__name__)

# --- KONFIGURÁCIÓ ---
# Hányszor próbálkozzon, mielőtt feladja?
MAX_RETRIES = 5 

CAMERAS = {
    0: {"name": "Nappali (ESP32)", "url": "http://192.168.1.87:81/stream"},
    1: {"name": "Konyha",          "url": "http://192.168.0.106:81/stream"},
    2: {"name": "Garázs",          "url": "http://192.168.0.107:81/stream"},
    3: {"name": "Kert",            "url": "http://192.168.0.108:81/stream"},
    4: {"name": "Bejárat",         "url": "http://192.168.0.109:81/stream"},
}

# Állapotok tárolása
# status: 'online' (zöld), 'offline' (piros - épp próbálkozik), 'standby' (sárga - feladta)
camera_states = {
    0: {'status': 'offline', 'retries': 0},
    1: {'status': 'offline', 'retries': 0},
    2: {'status': 'offline', 'retries': 0},
    3: {'status': 'offline', 'retries': 0},
    4: {'status': 'offline', 'retries': 0}
}

def generate_noise(text="NO SIGNAL"):
    """Zaj generálása felirattal"""
    noise = np.random.randint(0, 256, (240, 320, 3), dtype=np.uint8)
    # Szöveg középre igazítása (kb)
    cv2.putText(noise, text, (20, 120), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
    return noise

def get_frame(camera_id):
    url = CAMERAS[camera_id]["url"]
    
    while True:
        # 1. Ellenőrizzük, hogy nem adtuk-e már fel
        if camera_states[camera_id]['retries'] >= MAX_RETRIES:
            camera_states[camera_id]['status'] = 'standby'
            # Nem próbálunk csatlakozni, csak zajt küldünk (kíméljük a CPU-t)
            noise = generate_noise("KAPCSOLAT MEGSZAKADT")
            ret, buffer = cv2.imencode('.jpg', noise)
            yield (b'--frame\r\n'b'Content-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')
            time.sleep(1) # Ritkábban frissítjük a zajt standby módban
            continue

        # 2. Ha még van "életünk", próbálunk csatlakozni
        cap = cv2.VideoCapture(url)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        
        # Gyors ellenőrzés
        success, frame = cap.read()
        
        if success:
            # SIKER! Nullázzuk a számlálót
            camera_states[camera_id]['status'] = 'online'
            camera_states[camera_id]['retries'] = 0
            
            # Streameljük a videót, amíg meg nem szakad
            while True:
                success, frame = cap.read()
                if not success:
                    break
                ret, buffer = cv2.imencode('.jpg', frame)
                yield (b'--frame\r\n'b'Content-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')
            
            # Ha kilépett a while-ból, megszakadt a kapcsolat
            cap.release()
        else:
            # HIBA! Növeljük a számlálót
            camera_states[camera_id]['status'] = 'offline'
            camera_states[camera_id]['retries'] += 1
            print(f"Kamera {camera_id} hiba. Próbálkozás: {camera_states[camera_id]['retries']}/{MAX_RETRIES}")
            cap.release()
            
            # Küldünk egy kis zajt, amíg újra nem próbálkozunk
            noise = generate_noise("KERESES...")
            ret, buffer = cv2.imencode('.jpg', noise)
            yield (b'--frame\r\n'b'Content-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')
            # Kicsit várunk a következő próbálkozás előtt (hogy ne floodoljuk a hálózatot)
            time.sleep(0.5)

@app.route('/')
def index():
    return render_template('index.html', cameras=CAMERAS)

@app.route('/video_feed/<int:camera_id>')
def video_feed(camera_id):
    return Response(get_frame(camera_id), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/status')
def status():
    """Visszaadja a frontendnek az állapotokat"""
    return jsonify(camera_states)

@app.route('/reset_camera/<int:camera_id>')
def reset_camera(camera_id):
    """Gombnyomásra nullázza a hibaszámlálót"""
    camera_states[camera_id]['retries'] = 0
    camera_states[camera_id]['status'] = 'offline'
    return jsonify({"success": True})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True, ssl_context='adhoc', threaded=True)