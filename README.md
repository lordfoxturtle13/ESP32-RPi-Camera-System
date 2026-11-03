# ŐRSzem — ESP32-RPi Kamera Rendszer

Több kamerás megfigyelőrendszer ESP32-CAM és Luckfox kamerákkal. A szerver Raspberry Pi-n vagy bármilyen más, Dockert futtató gépen működik. A webfelület élő képet, H.264 felvételt, LED vezérlést és eseménynaplót biztosít, Tailscale-en keresztül HTTPS-sel elérhető bárhonnan.

## Hardver

- **Szerver:** Raspberry Pi 4 (ajánlott: 4GB+ RAM) vagy bármilyen más gép Dockerrel — stream feldolgozás, felvételek tárolása
- **ESP32-CAM AI Thinker** — kamera modul, RTSP stream, flash LED
- **Luckfox** — kamera modul, RTSP stream
- **Egyéb kamera** — a teljes stream URL megadásával

## Funkciók

- Élő MJPEG stream tetszőleges számú kamerától (a határ a gép teljesítménye és a sávszélesség)
- H.264 felvétel szoftveres kódolóval (`libx264`) — dupla stream (főstream + negyed felbontású al-stream gyors tekeréshez)
- Flash LED vezérlés ESP32-CAM kamerákon
- Kamera állapotjelzés (online / offline / standby) automatikus újracsatlakozással
- Eseménynapló (belépések, kilépések, felvételek, törlések, kapcsolatmegszakadások), méretkorláttal
- Tárhelykezelés: felvételek darabolása, automatikus törlés, védett felvételek, letöltés, szűrés és lapozás
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
    ├── storage.py          # Felvételek listázása, védelme és automatikus törlése
    ├── tests/              # Automatikus tesztek
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

## Tárhelykezelés

A beállítások a webes felület **Beállítások** oldalán módosíthatók, és a `config.json` `storage` részébe mentődnek:

| Beállítás | Alapérték | Jelentés |
|---|---|---|
| Megőrzés | 30 nap | Az ennél régebbi felvételek automatikusan törlődnek (0: nincs korlát) |
| Minimális szabad hely | 10% | Ez alatt a legrégebbi felvételektől kezdve törlődnek a felvételek (0: nincs korlát) |
| Darabolás | 15 perc | Ilyen hosszú fájlokra bomlik a felvétel. Áramszünetnél legfeljebb az utolsó darab veszik el |

- **Az ellenőrzés** percenként és minden felvétel indításakor lefut. Minden törlés bekerül az eseménynaplóba.
- **Védett felvétel:** az Archívum oldalon jelölhető meg. Nem törlődik sem automatikusan, sem kézzel, amíg a védelmet le nem veszed. A lista a tároló `protected.json` fájljában van.
- **Első indítás:** ha még nincs `protected.json`, minden meglévő felvétel védett lesz, így az automatikus törlés csak az ezután készült felvételeket érinti.
- **Régi fájlok:** a rendszer csak a `ÉÉÉÉ-HH-NN/cam_N/` mappákban lévő felvételeket kezeli. Az archívum gyökerében lévő régebbi videókat nem listázza és nem törli.
- **Kevés hely:** ha a törlés után sincs elég szabad hely, a felvétel leáll, és a felületen figyelmeztetés jelenik meg. Ugyanez történik írási hibánál is.
- **Közös lemez:** a szabad hely a teljes lemezre vonatkozik. Ha a felvételek olyan lemezen vannak, amelyet más adat is foglal, a szabály az összes nem védett felvételt törölheti, ezért ilyenkor állítsd 0-ra, vagy használj külön tárolót.

Az eseménynapló 5 MB-nál új fájlt kezd, az előző `events.log.1` néven marad meg.

## Raspberry Pi kiegészítés

### 1. Pi-s beállítások bekapcsolása

Az alábbi sor után a sima `docker compose` parancsok a Pi-s kiegészítést is használják: a felvételek az `archivum` mappába kerülnek, és csatolódnak a tanúsítványok.

```bash
echo "COMPOSE_FILE=docker-compose.yml:docker-compose.pi.yml" > .env
```

A Pi-s beállítás csak akkor rögzít, ha a felvételek tárolóján ott van egy jelölőfájl. Így kihúzott USB esetén nem a rendszerkártya telik meg. Az USB csatlakoztatása után egyszer hozd létre:

```bash
touch archivum/.orszem-archive
```

A kódoló itt is szoftveres (`libx264`). A hardveres `h264_v4l2m2m` kódoló nincs támogatva: Raspberry Pi 3 B+-on kipróbálva lejátszhatatlan fájlokat készített.

### Teljesítmény

A kamerák dekódolása és a szoftveres kódolás is a processzort terheli. Raspberry Pi 3 B+-on (906 MB RAM) ezt mértük:

- A szerver csak a beállított FPS-nek megfelelő képkockákat dolgozza fel. Így a Luckfox kisebb streamje (`live/1`, 704×576, HEVC, 5 FPS) és az ESP32-CAM (6,67 FPS) együtt, felvétel nélkül kb. 80%-ra terheli a processzort a négy mag 400%-ából.
- Ezzel a beállítással a két kamera egyidejű felvétele valós sebességgel rögzít (stopperrel ellenőrizve).
- A dekódolás a stream felbontásától függ: a Luckfox fő streamje (`live/0`, 2304×1296, HEVC) a javítás előtti mérésben jóval nagyobb terhelést adott, mint a `live/1`.

Ezért Pi 3-on a Luckfoxhoz a `live/1` streamet és alacsony FPS-t állíts be, több kamerához pedig erősebb gépet használj.

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

A Tailscale cert 90 napig érvényes. Az alábbi script naponta ellenőrzi és szükség esetén megújítja, majd újraindítja a containert. A futtatás előtt írd át a `HOSTNAME` értékét a gép nevére, a `/UTVONAL/A/REPOHOZ` részt pedig annak a mappának a teljes útvonalára, ahová a repót klónoztad:

```bash
sudo tee /usr/local/bin/tailscale-cert-renew.sh > /dev/null << 'EOF'
#!/bin/bash
HOSTNAME="HOSTNAME.ts.net"
CERT_DIR="/etc/tailscale-certs"
COMPOSE_DIR="/UTVONAL/A/REPOHOZ/raspberry"
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

A firmware PlatformIO-val tölthető fel. A WiFi adatokhoz hozd létre az `esp32_cam/src/secrets.h` fájlt az alábbi tartalommal (ez gitignore-ban van, ezért nem része a repónak):

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

# Automatikus tesztek
docker compose run --rm camera-server python -m unittest discover -s tests
```
