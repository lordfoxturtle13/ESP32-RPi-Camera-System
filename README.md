# ŐRSzem — ESP32-RPi Kamera Rendszer

Több kamerás megfigyelőrendszer ESP32-CAM és Luckfox kamerákkal. A szerver Raspberry Pi-n vagy bármilyen más, Dockert futtató gépen működik. A webfelület élő képet, H.264 felvételt, visszajátszást gyors tekeréssel és eseménynaplót biztosít. Kizárólag Tailscale-en keresztül, HTTPS-sel érhető el.

## Hardver

- **Szerver:** Raspberry Pi 4 (ajánlott: 4GB+ RAM) vagy bármilyen más gép Dockerrel — stream feldolgozás, felvételek tárolása
- **ESP32-CAM AI Thinker** — kamera modul, RTSP stream
- **Luckfox** — kamera modul, RTSP stream
- **Egyéb kamera** — a teljes stream URL megadásával

## Funkciók

- Élő MJPEG stream a `config.json`-ban megadott kameráktól
- H.264 felvétel kamerákként indítva és leállítva, szoftveres kódolóval (`libx264`), dupla streammel (főstream + negyed felbontású al-stream a gyors tekeréshez)
- Archívum dátum és kamera szerint csoportosítva, lejátszás és letöltés
- Kamera állapotjelzés (online / offline) folyamatos, automatikus újracsatlakozással
- Eseménynapló (belépések, felvételek, automatikus törlések, kapcsolatmegszakadások), méretkorláttal
- Tárhelykezelés: felvételek darabolása, automatikus törlés idő és szabad hely alapján
- Jelszavas belépés (bcrypt), késleltetés sikertelen próbák után, CSRF elleni védelem
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
    ├── main.py             # Flask alkalmazás, Waitress szerverrel
    ├── storage.py          # Felvételek listázása és automatikus törlése
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
        "0": { "name": "Nappali (ESP32)", "url": "rtsp://192.168.1.XX:554/mjpeg/1" },
        "1": { "name": "Bejárat (Luckfox)", "url": "rtsp://192.168.1.XX/live/1", "fps": 5, "rotate_180": true }
    },
    "storage": {
        "retention_days"  : 30,
        "min_free_percent": 10,
        "segment_minutes" : 15
    },
    "auth": {
        "password_hash": "A_JELSZO_HASH_ERTEKE",
        "secret_key"   : "HOSSZU_VELETLEN_SZOVEG"
    }
}
```

A kamerák csak itt adhatók meg és módosíthatók, a változás a szerver újraindítása után lép életbe. Kameránként:

| Mező | Kötelező | Jelentés |
|---|---|---|
| `name` | igen | A felületen és a képen megjelenő név |
| `url` | igen | A kamera streamjének címe (`rtsp://`, `http://` vagy `https://`) |
| `fps` | nem | Ennyi képkockát dolgoz fel a szerver másodpercenként, alapérték 6,67 |
| `rotate_180` | nem | `true` esetén a kép 180°-kal elfordul (fejjel lefelé szerelt kamera) |

A kép a kamera saját felbontásában kerül a felületre és a felvételbe. A kamera azonosítója (a kulcs) ne változzon, mert a felvételek mappái ehhez kötődnek.

A jelszó hash értékét a `hash_password.py` állítja elő. Jelszócseréhez is ezt kell futtatni, majd az új értéket beírni a `config.json`-ba és újraindítani a szervert:

```bash
docker compose run --rm camera-server python hash_password.py
```

A `secret_key` értéke legyen hosszú, véletlen szöveg, például ennek a parancsnak a kimenete:

```bash
docker compose run --rm camera-server python -c "import secrets; print(secrets.token_hex(32))"
```

### 4. Indítás

```bash
docker compose up -d --build
```

A szerver csak a saját gépről érhető el (`http://localhost:5000`), kívülről kizárólag a következő pontban leírt Tailscale HTTPS-en át. A felvételek a `felvetelek` mappába kerülnek, más mappához állítsd be az `ARCHIVE_PATH` változót a `.env` fájlban.

### 5. Távoli elérés HTTPS-sel (Tailscale)

A Tailscale admin felületén, a DNS oldalon kapcsold be a **HTTPS Certificates** opciót, majd futtasd ezt a szervert futtató gépen:

```bash
tailscale serve --bg 5000
```

A parancs kiírja a gép saját címét (`https://GEPNEV.TAILNET.ts.net`), a projektben semmit nem kell átírni hozzá. A tanúsítványt a Tailscale kezeli és újítja meg, a szerver pedig csak a Tailscale-hálózatodról érhető el. Kikapcsolás: `tailscale serve --https=443 off`.

## Tárhelykezelés

A beállítások a `config.json` `storage` részéből jönnek, a felületről nem módosíthatók. Hiányzó vagy hibás érték esetén az alapérték él.

| Beállítás | Alapérték | Jelentés |
|---|---|---|
| `retention_days` | 30 | Az ennél több napja készült felvételek automatikusan törlődnek (0: nincs korlát) |
| `min_free_percent` | 10 | Ez alatt a legrégebbi felvételektől kezdve törlődnek a felvételek (0: nincs korlát) |
| `segment_minutes` | 15 | Ilyen hosszú fájlokra bomlik a felvétel. Áramszünetnél legfeljebb az utolsó darab veszik el |

- **Az ellenőrzés** percenként és minden felvétel indításakor lefut. Minden törlés bekerül az eseménynaplóba. A készülő felvételhez a törlés nem nyúl.
- **Régi fájlok:** a rendszer csak a `ÉÉÉÉ-HH-NN/cam_N/` mappákban lévő felvételeket kezeli. Az archívum gyökerében lévő videókat nem listázza és nem törli. Ha egy felvételt meg akarsz tartani, másold ki a dátumos mappákból (például az archívum gyökerébe).
- **Kevés hely:** ha a törlés után sincs elég szabad hely, a felvétel leáll, és a felületen figyelmeztetés jelenik meg. Ugyanez történik írási hibánál is.
- **Közös lemez:** a szabad hely a teljes lemezre vonatkozik. Ha a felvételek olyan lemezen vannak, amelyet más adat is foglal, a szabály az összes felvételt törölheti, ezért ilyenkor állítsd 0-ra, vagy használj külön tárolót.

Az eseménynapló 5 MB-nál új fájlt kezd, az előző `events.log.1` néven marad meg.

## Raspberry Pi kiegészítés

### 1. Pi-s beállítások bekapcsolása

Az alábbi sor után a sima `docker compose` parancsok a Pi-s kiegészítést is használják: a felvételek az `archivum` mappába kerülnek (ide csatold az USB-s tárolót).

```bash
echo "COMPOSE_FILE=docker-compose.yml:docker-compose.pi.yml" > .env
```

A kódoló itt is szoftveres (`libx264`). A hardveres `h264_v4l2m2m` kódoló nincs támogatva: Raspberry Pi 3 B+-on kipróbálva lejátszhatatlan fájlokat készített.

### Teljesítmény

A kamerák dekódolása és a szoftveres kódolás is a processzort terheli. Raspberry Pi 3 B+-on (906 MB RAM) ezt mértük:

- A szerver csak a beállított FPS-nek megfelelő képkockákat dolgozza fel. Így a Luckfox kisebb streamje (`live/1`, 704×576, HEVC, 5 FPS) és az ESP32-CAM (6,67 FPS) együtt, felvétel nélkül kb. 80%-ra terheli a processzort a négy mag 400%-ából.
- Ezzel a beállítással a két kamera egyidejű felvétele valós sebességgel rögzít (stopperrel ellenőrizve).
- A dekódolás a stream felbontásától függ: a Luckfox fő streamje (`live/0`, 2304×1296, HEVC) a javítás előtti mérésben jóval nagyobb terhelést adott, mint a `live/1`.

Ezért Pi 3-on a Luckfoxhoz a `live/1` streamet és alacsony FPS-t állíts be, több kamerához pedig erősebb gépet használj.

A távoli elérés a Pi-n is a fenti `tailscale serve --bg 5000` paranccsal kapcsolható be.

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
