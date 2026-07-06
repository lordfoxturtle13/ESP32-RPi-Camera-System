# ŐRSzem — ESP32-RPi Kamera Rendszer

Több kamerás megfigyelőrendszer ESP32-CAM modulokkal és Raspberry Pi szerverrel. A webfelület élő képet, H.264 felvételt, LED vezérlést és eseménynaplót biztosít, Tailscale-en keresztül HTTPS-sel elérhető bárhonnan.

## Hardver

- **Raspberry Pi 4** (ajánlott: 4GB+ RAM) — szerver, stream feldolgozás, felvételek tárolása
- **ESP32-CAM AI Thinker** — kamera modul, RTSP stream, flash LED

## Funkciók

- Élő MJPEG stream legfeljebb 5 kamerától
- H.264 felvétel hardveres encoderrel (RPi V4L2) — dupla stream (640×480 főstream + 160×120 al-stream gyors tekeréshez)
- Flash LED vezérlés ESP32-CAM kamerákon
- Kamera állapotjelzés (online / offline / standby) automatikus újracsatlakozással
- Eseménynapló (belépések, kilépések, felvételek, kapcsolatmegszakadások)
- Aktív nézők listája
- Reszponzív UI — asztali és mobil nézettel
- Tailscale HTTPS — biztonságos távoli elérés VPN-en át
- Docker — automatikus újraindulás áramkimaradás vagy Pi reboot után

## Projekt struktúra

```
ESP32-RPi-Camera-System/
├── esp32_cam/              # ESP32-CAM firmware (PlatformIO / Arduino)
│   └── src/
│       ├── CameraWebServer_Proba01.ino
│       ├── secrets.h       # WiFi adatok (gitignore-ban!)
│       └── ...
└── raspberry/              # Raspberry Pi szerver
    ├── main.py             # Flask szerver
    ├── config.json         # Kamera config (gitignore-ban!)
    ├── config.example.json # Sablon
    ├── Dockerfile
    ├── docker-compose.yml
    ├── requirements.txt
    ├── templates/
    └── static/
```

## Telepítés (Raspberry Pi)

### 1. Docker telepítése

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker
```

### 2. Repo klónozása

```bash
git clone https://github.com/lordfoxturtle13/ESP32-RPi-Camera-System
cd ESP32-RPi-Camera-System/raspberry
```

### 3. Konfiguráció

```bash
cp config.example.json config.json
```

Szerkeszd a `config.json`-t a valós kamera adatokkal:

```json
{
    "cameras": {
        "0": {
            "name"    : "Nappali (ESP32)",
            "url"     : "rtsp://192.168.1.XX:554/mjpeg/1",
            "led_url" : "http://192.168.1.XX/flash"
        }
    },
    "ssl": {
        "cert": "/etc/tailscale-certs/HOSTNAME.ts.net.crt",
        "key" : "/etc/tailscale-certs/HOSTNAME.ts.net.key"
    }
}
```

### 4. Tailscale HTTPS cert

```bash
sudo mkdir -p /etc/tailscale-certs

# A HOSTNAME a `tailscale status` paranccsal kérdezhető le
sudo tailscale cert \
  --cert-file /etc/tailscale-certs/HOSTNAME.ts.net.crt \
  --key-file  /etc/tailscale-certs/HOSTNAME.ts.net.key \
  HOSTNAME.ts.net
```

### 5. Indítás

```bash
docker compose up -d --build
```

A szerver elérhető: `https://HOSTNAME.ts.net:5000`

## ESP32-CAM firmware

A firmware PlatformIO-val töltható fel. A WiFi adatokat a `secrets.h` fájlba kell írni (ez gitignore-ban van, a `secrets.h.example` alapján hozd létre):

```cpp
const char* ssid     = "wifi-nev";
const char* password = "wifi-jelszo";
```

Feltöltés után az ESP32 RTSP streamen szolgálja a képet: `rtsp://IP:554/mjpeg/1`

## Hasznos parancsok

```bash
# Logok követése
docker compose logs -f

# Újraindítás
docker compose restart

# Leállítás
docker compose down
```
