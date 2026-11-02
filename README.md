# ŐRSzem — ESP32-RPi Kamera Rendszer

Több kamerás megfigyelőrendszer ESP32-CAM modulokkal és Raspberry Pi szerverrel. A webfelület élő képet, H.264 felvételt, LED vezérlést és eseménynaplót biztosít, Tailscale-en keresztül HTTPS-sel elérhető bárhonnan.

## Hardver

- **Raspberry Pi 4** (ajánlott: 4GB+ RAM) — szerver, stream feldolgozás, felvételek tárolása
- **ESP32-CAM AI Thinker** — kamera modul, RTSP stream, flash LED

## Funkciók

- Élő MJPEG stream tetszőleges számú kamerától (a határ a gép teljesítménye és a sávszélesség)
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
    ├── docker-compose.yml     # Gépfüggetlen alapbeállítás
    ├── docker-compose.pi.yml  # Raspberry Pi kiegészítés
    ├── requirements.txt
    ├── templates/
    └── static/
```

## Telepítés

Bármilyen gépen futtatható, ahol van Docker. A Raspberry Pi-re vonatkozó lépéseket külön jelöljük.

### 1. Docker telepítése

Windowson és macOS-en a Docker Desktopot telepítsd. Linuxon és Raspberry Pi-n:

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
    "auth": {
        "password_hash": "A_JELSZO_HASH_ERTEKE",
        "secret_key"   : "HOSSZU_VELETLEN_SZOVEG"
    }
}
```

A jelszó hash értékét a `hash_password.py` állítja elő:

```bash
docker compose run --rm camera-server python hash_password.py
```

A `secret_key` értéke legyen hosszú, véletlen szöveg, például ennek a parancsnak a kimenete:

```bash
docker compose run --rm camera-server python -c "import secrets; print(secrets.token_hex(32))"
```

Új kamera a webes felület **Beállítások** oldalán is hozzáadható névvel, típussal (ESP32-CAM, Luckfox) és IP-címmel, vagy egyéb kameránál a teljes stream URL-lel. A kamera neve és címe ugyanitt módosítható, a kamera pedig törölhető is (a felvételei az archívumban maradnak). Ezek újraindítás nélkül életbe lépnek, és a `config.json`-ba mentődnek.

Az Élő Kép oldal oldalanként 4 kamerát mutat, mert a böngésző egy címhez csak kb. 6 kapcsolatot tart nyitva.

A `width`, `height`, `fps` és `rotation` mezők kameránként opcionálisak, alapértelmezésük 640×480, 6,67 FPS és 0°. A `rotation` értéke 0, 90, 180 vagy 270 lehet (az óramutató járásával megegyezően); 90° és 270° esetén a kép álló lesz, a szélesség és a magasság felcserélődik. A webes felület **Beállítások** oldalán is módosíthatók: a változás újraindítás nélkül életbe lép, és a `config.json`-ba mentődik. Választható felbontások: 320×240, 640×480, 800×600, 1280×720, 1920×1080; az FPS 1 és 30 között lehet. Ha felvétel közben változik a beállítás, a felvétel új fájlban folytatódik.

### 4. Indítás

```bash
docker compose up -d --build
```

A szerver biztonsági okból alapból csak a saját gépről érhető el: `http://localhost:5000`. A felvételek a `felvetelek` mappába kerülnek, más mappához állítsd be az `ARCHIVE_PATH` változót a `.env` fájlban.

### 5. Távoli elérés HTTPS-sel (Tailscale)

A Tailscale admin felületén, a DNS oldalon kapcsold be a **HTTPS Certificates** opciót, majd futtasd ezt a szervert futtató gépen:

```bash
tailscale serve --bg 5000
```

A parancs kiírja a gép saját címét (`https://GEPNEV.TAILNET.ts.net`), a projektben semmit nem kell átírni hozzá. A tanúsítványt a Tailscale kezeli és újítja meg, a szerver pedig csak a Tailscale-hálózatodról érhető el. Kikapcsolás: `tailscale serve --https=443 off`.

### Elérés Tailscale nélkül

Ha nem használsz Tailscale-t, de a helyi hálózatról is el akarod érni a szervert, add hozzá a `.env` fájlhoz az alábbi sorokat. Ilyenkor a forgalom titkosítatlan, ezért csak megbízható hálózaton használd.

```bash
BIND_ADDRESS=0.0.0.0
TRUST_PROXY=0
```

## Raspberry Pi kiegészítés

### 1. Pi-s beállítások bekapcsolása

Az alábbi sor után a sima `docker compose` parancsok a Pi-s kiegészítést is használják: a felvételek az `archivum` mappába kerülnek, és csatolódnak a tanúsítványok és a videóeszközök.

```bash
echo "COMPOSE_FILE=docker-compose.yml:docker-compose.pi.yml" > .env
```

A kódoló alapból itt is szoftveres (`libx264`). A hardveres kódolóhoz add hozzá a `.env` fájlhoz a `H264_CODEC=h264_v4l2m2m` sort, és próbáld ki egy felvétellel.

### 2. Tailscale HTTPS cert

A Pi-n is a fenti `tailscale serve` a legegyszerűbb, ilyenkor ez és a következő két lépés kihagyható. Az alábbi lépések ahhoz kellenek, ha a szerver maga kezeli a tanúsítványt.

```bash
sudo mkdir -p /etc/tailscale-certs

# A HOSTNAME a `tailscale status` paranccsal kérdezhető le
sudo tailscale cert \
  --cert-file /etc/tailscale-certs/HOSTNAME.ts.net.crt \
  --key-file  /etc/tailscale-certs/HOSTNAME.ts.net.key \
  HOSTNAME.ts.net
```

### 3. Cert auto-renewal beállítása

A Tailscale cert 90 napig érvényes. Az alábbi script naponta ellenőrzi és szükség esetén megújítja, majd újraindítja a containert:

```bash
sudo tee /usr/local/bin/tailscale-cert-renew.sh > /dev/null << 'EOF'
#!/bin/bash
HOSTNAME="HOSTNAME.ts.net"
CERT_DIR="/etc/tailscale-certs"
COMPOSE_DIR="/home/lordf/ESP32-RPi-Camera-System/raspberry"
CERT_FILE="$CERT_DIR/$HOSTNAME.crt"

BEFORE=$(stat -c %Y "$CERT_FILE" 2>/dev/null || echo 0)

tailscale cert \
  --cert-file "$CERT_FILE" \
  --key-file  "$CERT_DIR/$HOSTNAME.key" \
  "$HOSTNAME"

AFTER=$(stat -c %Y "$CERT_FILE" 2>/dev/null || echo 0)

if [ "$AFTER" -gt "$BEFORE" ]; then
  cd "$COMPOSE_DIR" && docker compose restart
fi
EOF
sudo chmod +x /usr/local/bin/tailscale-cert-renew.sh
```

Cron job (minden éjjel 3-kor):

```bash
echo "0 3 * * * root /usr/local/bin/tailscale-cert-renew.sh >> /var/log/tailscale-cert-renew.log 2>&1" \
  | sudo tee /etc/cron.d/tailscale-cert-renew
```

### 4. HTTPS bekapcsolása és indítás

A szervernek ehhez közvetlenül elérhetőnek kell lennie, ezért add hozzá a `.env` fájlhoz a `BIND_ADDRESS=0.0.0.0` és a `TRUST_PROXY=0` sort. Ezután add hozzá a `config.json`-hoz a tanúsítványok helyét:

```json
"ssl": {
    "cert": "/etc/tailscale-certs/HOSTNAME.ts.net.crt",
    "key" : "/etc/tailscale-certs/HOSTNAME.ts.net.key"
}
```

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
