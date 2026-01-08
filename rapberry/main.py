import cv2
import urllib.request
import numpy as np
import threading

# --- BEÁLLÍTÁSOK ---
ESP_IP = "192.168.1.87" 
STREAM_URL = f"http://{ESP_IP}:81/stream"
CONTROL_URL = f"http://{ESP_IP}/control?var=led_intensity&val="

# Globális változó a LED állapotának követésére (0-255)
current_led_value = 0

def send_led_command(value):
    """
    Ez a függvény küldi el a parancsot az ESP32-nek.
    Egy külön kis szálon futtatjuk, hogy NE akassza meg a videót, 
    amíg a hálózat válaszol.
    """
    def _request():
        try:
            urllib.request.urlopen(f"{CONTROL_URL}{value}", timeout=1)
            print(f"💡 LED parancs: {value}")
        except Exception as e:
            print(f"⚠️ Hiba a LED küldésekor: {e}")
    
    # "Tűz és felejtsd el" módszer - elindítjuk és nem várjuk meg
    threading.Thread(target=_request, daemon=True).start()

def trackbar_callback(pos):
    """
    Ezt hívja meg a csúszka, amikor mozgatod.
    pos: a csúszka aktuális értéke (0-255)
    """
    global current_led_value
    current_led_value = pos
    send_led_command(pos)

def main():
    global current_led_value
    
    print(f"Kapcsolódás: {STREAM_URL} ...")
    
    # Megpróbáljuk megnyitni a streamet
    try:
        stream = urllib.request.urlopen(STREAM_URL, timeout=5)
    except Exception as e:
        print(f"FATAL HIBA: Nem sikerült megnyitni a streamet. Ellenőrizd az IP címet! ({e})")
        return

    bytes_data = b''
    
    # Ablak létrehozása
    window_name = "ESP32 Camera & Control"
    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
    
    # Csúszka létrehozása (Neve: 'LED', Ablak, Kezdőérték, Max érték, Callback fv)
    cv2.createTrackbar('LED', window_name, 0, 255, trackbar_callback)

    print("--- VEZÉRLÉS ---")
    print("Egérrel: Húzd a csúszkát")
    print("Billentyűvel: 'l' = Be/Ki kapcsolás (max/min)")
    print("Kilépés: 'q'")

    while True:
        try:
            # Olvasunk 4096 bájtot a hálózatról
            bytes_data += stream.read(4096)
            
            # Megkeressük a JPEG kép elejét (0xffd8) és végét (0xffd9)
            a = bytes_data.find(b'\xff\xd8')
            b = bytes_data.find(b'\xff\xd9')
            
            if a != -1 and b != -1:
                # Megvan a teljes kép
                jpg = bytes_data[a:b+2]
                
                # A bufferből töröljük a feldolgozott részt
                bytes_data = bytes_data[b+2:]
                
                # Dekódolás
                frame = cv2.imdecode(np.frombuffer(jpg, dtype=np.uint8), cv2.IMREAD_COLOR)
                
                # Megjelenítés
                if frame is not None:
                    # Opcionális: Szöveg írása a képre
                    cv2.putText(frame, f"LED: {current_led_value}", (10, 30), 
                                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                    
                    cv2.imshow(window_name, frame)
            
            # --- Billentyűzet figyelése ---
            key = cv2.waitKey(1) & 0xFF
            
            if key == ord('q'):
                break
            
            elif key == ord('l'):
                # Ha 'l'-t nyomsz, váltunk 0 és 255 között
                if current_led_value > 0:
                    new_val = 0
                else:
                    new_val = 255
                
                # Frissítjük a változót, a csúszkát és küldjük a parancsot
                current_led_value = new_val
                cv2.setTrackbarPos('LED', window_name, new_val)
                # (A setTrackbarPos automatikusan meghívja a trackbar_callback-et, 
                # így nem kell külön send_led_command-ot hívni)

        except Exception as e:
            print(f"Hiba a loop-ban: {e}")
            break

    cv2.destroyAllWindows()

if __name__ == "__main__":
    main()