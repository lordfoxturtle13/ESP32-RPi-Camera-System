# ESP32-CAM firmware

A rendszerben az ESP32-CAM kamerák nem saját firmware-t futtatnak, hanem a nyílt forrású
**ESP32CAM-RTSP** projektet, kis módosításokkal.

- **Forrás:** https://github.com/rzeldent/esp32cam-rtsp
- **Kiinduló commit:** `8b7d6e0` (2026. március 4.)
- **Board:** AI Thinker ESP32-CAM (`esp32cam_ai_thinker`)
- **Fő függőségek:** Micro-RTSP (RTSP-szerver), IotWebConf (webes beállítófelület, Wi-Fi-kezelés)

A firmware kódja ezért nem része ennek a repónak. Itt csak a módosítások leírása és a
jelszavak nélküli beállítófájl található.

## Módosítások az eredeti projekthez képest

| Fájl | Változás |
|---|---|
| `platformio.ini` | Alapértelmezett környezet: `esp32cam_ai_thinker` |
| `include/settings.h` | Saját hozzáférési pont neve (`WIFI_SSID`), az első indításhoz szükséges AP-jelszó (`WIFI_PASSWORD`) és OTA-jelszó (`OTA_PASSWORD`) beállítva, `CONFIG_VERSION` 1.6 → 1.7 |

A `src/main.cpp` változatlan.

## Telepítés

1. Klónozd az eredeti projektet, és állj a kiinduló commitra:
   ```bash
   git clone https://github.com/rzeldent/esp32cam-rtsp
   cd esp32cam-rtsp
   git checkout 8b7d6e0
   ```
2. Másold be az itt található `settings.example.h`-t `include/settings.h` néven, és írd át a jelszavakat.
   **A kitöltött `settings.h`-t ne töltsd fel nyilvános repóba.**
3. A `platformio.ini`-ben állítsd be: `default_envs = esp32cam_ai_thinker`
4. Fordítás és feltöltés PlatformIO-val: `pio run -t upload`
5. Első indításkor csatlakozz a kamera saját Wi-Fi hálózatához (`WIFI_SSID`), majd a `http://192.168.4.1`
   oldalon add meg az otthoni Wi-Fi adatait és a hozzáférési jelszót.

## Elérhető szolgáltatások

| Cím | Funkció | Hitelesítés |
|---|---|---|
| `rtsp://IP:554/mjpeg/1` | RTSP-stream (MJPEG) – **ezt használja a szerver** | nincs |
| `http://IP/stream` | MJPEG-stream HTTP-n | nincs |
| `http://IP/snapshot` | Egy JPEG-kép | nincs |
| `http://IP/flash?v=0..255` | Vaku LED fényereje – **ezt használja a szerver** | nincs |
| `http://IP/restart` | Kamera újraindítása | nincs (lásd lent) |
| `http://IP/config` | Beállítófelület | HTTP Basic (`admin` + AP-jelszó) |

A szerver `config.json`-jában az ESP32-CAM kamerák bejegyzése:

```json
"url"     : "rtsp://IP:554/mjpeg/1",
"led_url" : "http://IP/flash"
```

## Biztonsági megjegyzések

- **A streamek és a képek hitelesítés nélkül elérhetők.** Ezt az eredeti projekt README-je
  is kiemeli: a hálózaton bárki megnézheti a képet.
- **A `/restart` végpont a kódban nem kér hitelesítést**, bár az eredeti README szerint kellene.
  A `handle_restart()` függvény (`src/main.cpp`) hitelesítés ellenőrzése nélkül újraindítja
  az eszközt, így a helyi hálózaton bárki újraindíthatja a kamerát egy GET-kéréssel.
  Ugyanez igaz a `/flash` végpontra.
- **Következmény a rendszerre:** a kamerák önmagukban nem védettek, ezért nem szabad őket
  közvetlenül az internetre kitenni (porttovábbítás, UPnP). A védelmet a hálózati elkülönítés
  adja: a kamerák csak a helyi hálózaton érhetők el, kívülről csak a szerveren keresztül,
  Tailscale-en át és jelszavas belépés után.
- **Maradék kockázat:** aki hozzáfér a helyi Wi-Fi-hálózathoz, közvetlenül eléri a kamerákat.
  Ez külön, csak a kameráknak szánt hálózattal (VLAN vagy vendéghálózat) csökkenthető.
