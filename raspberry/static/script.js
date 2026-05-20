// ================================================
// ÁLLAPOT — globális változók
// ================================================

// Felvételi állapotok kameránként (szerver igazolja vissza, nem feltételezzük)
const recordingStates = {};

// LED állapotok kameránként
const ledStates = {};

// Éppen aktív nézet azonosítója
let currentView = 'live';

// Auto-refresh timer a Settings nézethez
let settingsRefreshTimer = null;


// ================================================
// STÁTUSZ POLLING
// A /status endpoint 2 másodpercenként lekérdezi az összes kamera állapotát.
// Ez frissíti: állapotpontokat, overlay-eket, felvétel jelzőket, oldalsáv listát.
// ================================================

function startStatusPolling() {
    fetchStatus();                         // Azonnal lekér, nem vár 2 másodpercet
    setInterval(fetchStatus, 2000);
}

async function fetchStatus() {
    try {
        const resp = await fetch('/status');
        const data = await resp.json();

        Object.entries(data).forEach(([camId, state]) => {
            updateCameraUI(Number(camId), state);
        });
    } catch (err) {
        // Hálózati hiba esetén csendesen tűrjük (pl. RPi újraindulás)
        console.warn('[STATUS] Nem sikerült lekérni:', err);
    }
}

/**
 * Egy kamera összes UI elemét frissíti a szerver állapota alapján.
 * @param {number} camId  - Kamera azonosítója
 * @param {object} state  - { status, retries, recording, led, has_led }
 */
function updateCameraUI(camId, state) {
    const { status, recording, led } = state;

    // --- Állapotpontok frissítése ---
    // A kártyán és az oldalsávon lévő ponton is frissítjük az osztályokat
    ['dot', 'sidebar-dot', 'settings-dot'].forEach(prefix => {
        const dot = document.getElementById(`${prefix}-${camId}`);
        if (!dot) return;
        dot.className = 'cam-status-dot ' + statusToCssClass(status);
    });

    // --- No-signal overlay ---
    const overlay = document.getElementById(`overlay-${camId}`);
    const overlayText = document.getElementById(`overlay-text-${camId}`);

    if (overlay) {
        const isOnline = (status === 'online');
        overlay.classList.toggle('visible', !isOnline);

        // Kereső animáció csak "offline" és "connecting" állapotban (nem "standby")
        overlay.classList.toggle('searching', status === 'offline' || status === 'connecting');

        if (overlayText) {
            overlayText.textContent = statusToHungarianText(status);
        }
    }

    // --- Felvétel gomb: offline + nem felvesz → letiltva ---
    const recBtn = document.getElementById(`record-btn-${camId}`);
    if (recBtn) {
        const canRecord = (status === 'online') || recording;
        recBtn.disabled = !canRecord;
        recBtn.classList.toggle('btn-disabled', !canRecord);
    }

    // --- Felvétel állapot ---
    if (recordingStates[camId] !== recording) {
        recordingStates[camId] = recording;
        applyRecordingUI(camId, recording);
    }

    // --- LED állapot (csak ha van LED) ---
    if (led !== null && ledStates[camId] !== led) {
        ledStates[camId] = led;
        const ledBtn = document.getElementById(`led-btn-${camId}`);
        if (ledBtn) ledBtn.classList.toggle('led-on', led);
    }
}

/** Kamera állapot string → CSS class */
function statusToCssClass(status) {
    const map = {
        'online'    : 'online',
        'offline'   : 'offline',
        'standby'   : 'standby',
        'connecting': 'connecting',
    };
    return map[status] || 'offline';
}

/** Kamera állapot string → Magyar szöveg az overlay-hez */
function statusToHungarianText(status) {
    const map = {
        'online'    : 'ONLINE',
        'offline'   : 'KERESÉS...',
        'standby'   : 'KAPCSOLAT MEGSZAKADT',
        'connecting': 'CSATLAKOZÁS...',
    };
    return map[status] || 'ISMERETLEN';
}

// ================================================
// FELVÉTEL VEZÉRLÉS
// ================================================

/**
 * Egy kamera felvételét indítja vagy állítja le.
 * A szerver /toggle_record endpoint igazolja vissza az új állapotot.
 */
async function toggleRecord(cameraId) {
    try {
        const resp = await fetch(`/toggle_record/${cameraId}`, { method: 'POST' });
        const data = await resp.json();

        if (data.recording !== undefined) {
            recordingStates[cameraId] = data.recording;
            applyRecordingUI(cameraId, data.recording);
        }
    } catch (err) {
        console.error('[REC] Hiba a felvétel indításakor:', err);
    }
}

/**
 * Összes kamera felvételét egyszerre indítja/állítja le.
 * Ha legalább egy fut → mindent leállít. Ha egy sem fut → mindent elindít.
 */
function toggleAllRecords() {
    const isAnyRecording = Object.values(recordingStates).includes(true);
    const targetState = !isAnyRecording;

    // Csak azokat a kamerákat érinti, amik nincsenek a célállapotban
    Object.keys(recordingStates).forEach(id => {
        if (!!recordingStates[id] !== targetState) {
            toggleRecord(Number(id));
        }
    });

    // Globális gomb megjelenésének frissítése
    const globalBtn = document.getElementById('record-all-btn');
    if (globalBtn) {
        globalBtn.classList.toggle('is-recording', targetState);
        globalBtn.innerHTML = targetState
            ? '<svg viewBox="0 0 16 16" fill="currentColor"><rect x="3" y="3" width="10" height="10" rx="1"/></svg> Leállítás'
            : '<svg viewBox="0 0 16 16" fill="currentColor"><circle cx="8" cy="8" r="5"/></svg> Összes Felvétele';
    }
}

/**
 * Az összes felvétellel kapcsolatos UI elemet frissíti egy kameránál.
 * Hívja a toggleRecord() és az updateCameraUI() is.
 */
function applyRecordingUI(cameraId, isRecording) {
    const btn       = document.getElementById(`record-btn-${cameraId}`);
    const card      = document.getElementById(`card-${cameraId}`);
    const indicator = document.getElementById(`rec-indicator-${cameraId}`);

    if (btn) {
        btn.classList.toggle('is-recording', isRecording);
        btn.innerHTML = isRecording
            ? '<svg viewBox="0 0 16 16" fill="currentColor"><rect x="3" y="3" width="10" height="10" rx="1"/></svg> Leállítás'
            : '<svg viewBox="0 0 16 16" fill="currentColor"><circle cx="8" cy="8" r="4"/></svg> Felvétel';
    }

    if (card)      card.classList.toggle('is-recording', isRecording);
    if (indicator) indicator.classList.toggle('visible', isRecording);
}

// ================================================
// LED VEZÉRLÉS (ESP32-CAM)
// ================================================

/**
 * A kamera flash LED-jét kapcsolja ki/be.
 * A Flask /api/led endpoint közvetíti a parancsot az ESP32-nek.
 */
async function toggleLED(cameraId) {
    const newState = !ledStates[cameraId];

    try {
        const resp = await fetch(`/api/led/${cameraId}/${newState ? 1 : 0}`, { method: 'POST' });
        const data = await resp.json();

        if (data.led !== undefined) {
            ledStates[cameraId] = data.led;
            const btn = document.getElementById(`led-btn-${cameraId}`);
            if (btn) btn.classList.toggle('led-on', data.led);
        }
    } catch (err) {
        console.error('[LED] Hiba az LED vezérléskor:', err);
    }
}

// ================================================
// KAMERA ÚJRACSATLAKOZTATÁS
// ================================================

/**
 * A szerver oldalon nullázza a retries számlálót → camera_worker újracsatlakozik.
 */
async function resetCamera(cameraId) {
    try {
        await fetch(`/api/reset/${cameraId}`, { method: 'POST' });
    } catch (err) {
        console.error('[RESET] Hiba:', err);
    }
}

// ================================================
// NÉZETEK VÁLTÁSA (LIVE / ARCHIVE / SETTINGS)
// ================================================

/**
 * Megmutatja a kért nézetet, elrejti a többit.
 * Frissíti az oldalsáv és bottom nav aktív gombját is.
 * @param {'live'|'archive'|'settings'} viewName
 */
function showView(viewName) {
    currentView = viewName;

    ['live', 'archive', 'settings'].forEach(name => {
        const isActive = name === viewName;
        document.getElementById(`view-${name}`)?.classList.toggle('hidden', !isActive);
        document.getElementById(`nav-${name}`)?.classList.toggle('active', isActive);
        document.getElementById(`bnav-${name}`)?.classList.toggle('active', isActive);
    });

    // Archívum betöltése, ha arra váltunk
    if (viewName === 'archive') loadVideoList();

    // Eseménynapló + nézők: auto-refresh indítása/leállítása
    if (viewName === 'settings') {
        loadViewers();
        loadEvents();
        settingsRefreshTimer = setInterval(() => loadViewers(), 5000);
    } else {
        clearInterval(settingsRefreshTimer);
    }

    // Lejátszó leállítása, ha elhagyjuk az archívumot
    if (viewName !== 'archive') {
        const player = document.getElementById('archive-player');
        if (player) player.pause();
    }

    // Mobilon oldalsáv bezárása nézetváltás után
    closeSidebarMobile();
}

// ================================================
// OLDALSÁV VEZÉRLÉS
// ================================================

function toggleSidebar() {
    const sidebar = document.getElementById('sidebar');

    if (window.innerWidth <= 700) {
        // Mobilon overlay-ként nyílik/zárul
        sidebar.classList.toggle('mobile-open');
    } else {
        // Asztali nézetben benyomódik/kinyomódik
        sidebar.classList.toggle('collapsed');
    }
}

function closeSidebarMobile() {
    if (window.innerWidth <= 700) {
        document.getElementById('sidebar')?.classList.remove('mobile-open');
    }
}

// Mobilon a sidebar mögötti területre kattintva bezárul
document.addEventListener('click', (e) => {
    const sidebar = document.getElementById('sidebar');
    if (!sidebar) return;

    if (window.innerWidth <= 700
        && sidebar.classList.contains('mobile-open')
        && !sidebar.contains(e.target)
        && !e.target.closest('.hamburger')) {
        sidebar.classList.remove('mobile-open');
    }
});

// ================================================
// TELJES KÉPERNYŐ
// ================================================

/**
 * Egy feed-wrapper elemet teljes képernyőre vált (dupla kattintás/érintés).
 * @param {string} wrapperId - A div elem ID-ja
 */
function toggleFullscreen(wrapperId) {
    const el = document.getElementById(wrapperId);
    if (!el) return;

    // Ha már CSS-fullscreen módban van → kilépés
    if (el.classList.contains('custom-fullscreen')) {
        el.classList.remove('custom-fullscreen');
        return;
    }

    // Natív fullscreen API kísérlet (asztali + Android)
    if (!document.fullscreenElement && !document.webkitFullscreenElement) {
        const req = el.requestFullscreen || el.webkitRequestFullscreen;
        if (req) {
            req.call(el).catch(() => {
                // iOS Safari: requestFullscreen nem támogatott → CSS fallback
                el.classList.add('custom-fullscreen');
            });
            return;
        }
        // Nincs fullscreen API (régi iOS) → CSS fallback
        el.classList.add('custom-fullscreen');
    } else {
        (document.exitFullscreen || document.webkitExitFullscreen).call(document);
    }
}

// Natív fullscreen kilépésekor eltávolítjuk a CSS osztályt is (konzisztencia)
document.addEventListener('fullscreenchange', () => {
    if (!document.fullscreenElement) {
        document.querySelectorAll('.feed-wrapper.custom-fullscreen')
            .forEach(el => el.classList.remove('custom-fullscreen'));
    }
});
document.addEventListener('webkitfullscreenchange', () => {
    if (!document.webkitFullscreenElement) {
        document.querySelectorAll('.feed-wrapper.custom-fullscreen')
            .forEach(el => el.classList.remove('custom-fullscreen'));
    }
});

// ================================================
// ARCHÍVUM LEJÁTSZÓ
// ================================================

const MONTHS = ['január','február','március','április','május','június','július','augusztus','szeptember','október','november','december'];

function parseDateFromFilename(filename) {
    // cam_0_20260520_171234_main.mp4
    const parts = filename.split('_');
    if (parts.length < 5) return null;
    const d = parts[2], t = parts[3];
    if (d.length !== 8 || t.length !== 6) return null;
    return {
        date     : d,
        dateLabel: `${d.slice(0,4)}. ${MONTHS[parseInt(d.slice(4,6)) - 1]} ${parseInt(d.slice(6,8))}.`,
        timeLabel: `${t.slice(0,2)}:${t.slice(2,4)}:${t.slice(4,6)}`,
        camLabel : `CAM${String(parts[1]).padStart(2, '0')}`,
    };
}

/** Betölti és megjeleníti az archívum videólistát, dátum szerint csoportosítva. */
async function loadVideoList() {
    const list = document.getElementById('video-list');
    if (!list) return;

    list.innerHTML = '<p class="empty-msg">Betöltés...</p>';

    try {
        const resp  = await fetch('/api/videos');
        const files = await resp.json();

        list.innerHTML = '';

        if (files.length === 0) {
            list.innerHTML = '<p class="empty-msg">Nincsenek mentett felvételek.</p>';
            return;
        }

        // Dátum szerint csoportosítás (a szerver már visszafele rendezett listát ad)
        const groups = {};
        const order  = [];
        files.forEach(filename => {
            const parsed = parseDateFromFilename(filename);
            const key    = parsed ? parsed.date : 'egyéb';
            if (!groups[key]) {
                groups[key] = { label: parsed ? parsed.dateLabel : 'Egyéb', files: [] };
                order.push(key);
            }
            groups[key].files.push({ filename, parsed });
        });

        order.forEach(key => {
            const group = groups[key];

            const header = document.createElement('p');
            header.className   = 'video-date-header';
            header.innerHTML   = `<span class="date-arrow">▾</span>${group.label}`;

            const groupDiv = document.createElement('div');
            groupDiv.className = 'video-date-group';

            header.addEventListener('click', () => {
                const collapsed = groupDiv.classList.toggle('collapsed');
                header.querySelector('.date-arrow').textContent = collapsed ? '▸' : '▾';
            });

            list.appendChild(header);
            list.appendChild(groupDiv);

            group.files.forEach(({ filename, parsed }) => {
                const btn = document.createElement('button');
                btn.className        = 'video-item-btn';
                btn.textContent      = parsed ? `${parsed.camLabel} · ${parsed.timeLabel}` : filename.replace('_main.mp4', '');
                btn.dataset.filename = filename;
                btn.addEventListener('click', () => {
                    list.querySelectorAll('.video-item-btn').forEach(b => b.classList.remove('active'));
                    btn.classList.add('active');
                    playVideo(filename);
                });
                groupDiv.appendChild(btn);
            });
        });

    } catch (err) {
        list.innerHTML = '<p class="empty-msg">Hiba a lista betöltésekor.</p>';
        console.error('[ARCHIVE] Hiba:', err);
    }
}

/**
 * Betölt és lejátszik egy archív videót a dupla streames lejátszóban.
 * @param {string} filename - A _main.mp4 fájl neve
 */
function playVideo(filename) {
    const mainPlayer = document.getElementById('archive-player');
    const subPlayer  = document.getElementById('archive-player-sub');
    const title      = document.getElementById('player-title');

    if (!mainPlayer || !subPlayer) return;

    // Al-stream neve: ugyanaz, de _sub.mp4 végzéssel
    const subFilename = filename.replace('_main.mp4', '_sub.mp4');

    const parsed = parseDateFromFilename(filename);
    if (title) title.textContent = parsed
        ? `${parsed.camLabel} — ${parsed.dateLabel} ${parsed.timeLabel}`
        : filename.replace('_main.mp4', '').replace(/_/g, ' ');

    mainPlayer.pause();
    subPlayer.pause();

    // Forrás beállítása és lejátszás indítása
    mainPlayer.src = `/archivum_video/${filename}`;
    subPlayer.src  = `/archivum_video/${subFilename}`;

    mainPlayer.load();
    subPlayer.load();
    mainPlayer.play();
}

// ================================================
// SEEKING LOGIKA — Dupla stream szinkronizáció
// Tekerés közben az al-stream (alacsony felbontású) előnézetet mutat,
// így a csúszka mozgatása azonnal reagál a nagy videó buffering helyett.
// ================================================

document.addEventListener('DOMContentLoaded', () => {
    const mainPlayer = document.getElementById('archive-player');
    const subPlayer  = document.getElementById('archive-player-sub');

    if (mainPlayer && subPlayer) {

        // Tekerés kezdete: főstream eltűnik, al-stream előugrik
        mainPlayer.addEventListener('seeking', () => {
            mainPlayer.style.opacity    = '0.01';   // Átlátszó, de a vezérlők megmaradnak
            subPlayer.currentTime       = mainPlayer.currentTime;
        });

        // Tekerés vége: főstream visszajön
        mainPlayer.addEventListener('seeked', () => {
            mainPlayer.style.opacity = '1';
        });
    }

    // Oldal betöltésekor indítjuk el a státusz pollert
    startStatusPolling();
});

// ================================================
// STREAM ÚJRACSATLAKOZÁS — bfcache és tab-váltás után
// ================================================

function reconnectStreams() {
    document.querySelectorAll('.camera-feed').forEach(img => {
        const camId = img.id.replace('feed-', '');
        img.src = `/video_feed/${camId}?_=${Date.now()}`;
    });
}

// Bfcache (vissza gombbal visszatérés) esetén a stream meghal — újraindítjuk
window.addEventListener('pageshow', (e) => {
    if (e.persisted) reconnectStreams();
});

// Tab váltás / minimalizálás után visszatéréskor
document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') reconnectStreams();
});

// Oldal elhagyásakor kilépés naplózása (sendBeacon megbízható, fetch nem)
// pagehide: iOS Safari-n is megbízható (beforeunload iOS-en nem tüzel)
window.addEventListener('pagehide', () => {
    navigator.sendBeacon('/api/disconnect');
});
window.addEventListener('beforeunload', () => {
    navigator.sendBeacon('/api/disconnect');
});

// ================================================
// ARCHÍVUM — Nyíl billentyűs navigáció
// ================================================

document.addEventListener('keydown', (e) => {
    if (currentView !== 'archive') return;
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;

    // Csak a látható (nem becsukott) csoportban lévő gombok
    const buttons = [...document.querySelectorAll('#video-list .video-date-group:not(.collapsed) .video-item-btn')];
    if (buttons.length === 0) return;

    e.preventDefault();

    const activeBtn   = document.querySelector('#video-list .video-date-group:not(.collapsed) .video-item-btn.active');
    const currentIdx  = activeBtn ? buttons.indexOf(activeBtn) : -1;
    const nextIdx     = e.key === 'ArrowDown'
        ? Math.min(currentIdx + 1, buttons.length - 1)
        : Math.max(currentIdx - 1, 0);

    buttons[nextIdx].click();
    buttons[nextIdx].scrollIntoView({ block: 'nearest', behavior: 'smooth' });
});

// ================================================
// SEGÉDFÜGGVÉNY — Spinner animáció gombokhoz
// ================================================

function spinBtn(btn) {
    if (!btn) return;
    btn.classList.remove('spinning');
    void btn.offsetWidth; // reflow hogy az animáció újrainduljon
    btn.classList.add('spinning');
    btn.addEventListener('animationend', () => btn.classList.remove('spinning'), { once: true });
}

// ================================================
// AKTÍV NÉZŐK
// ================================================

async function loadViewers(btn) {
    spinBtn(btn);
    const list = document.getElementById('viewers-list');
    if (!list) return;

    try {
        const resp = await fetch('/api/viewers');
        const ips  = await resp.json();

        if (ips.length === 0) {
            list.innerHTML = '<p class="empty-msg">Jelenleg senki nem néz.</p>';
            return;
        }

        list.innerHTML = ips.map(ip =>
            `<p class="event-entry event-viewer">${ip}</p>`
        ).join('');

    } catch (err) {
        list.innerHTML = '<p class="empty-msg">Hiba a nézők betöltésekor.</p>';
    }
}

// ================================================
// ESEMÉNYNAPLÓ
// ================================================

async function loadEvents(btn) {
    spinBtn(btn);
    const list = document.getElementById('events-list');
    if (!list) return;

    try {
        const resp   = await fetch('/api/events');
        const events = await resp.json();

        if (events.length === 0) {
            list.innerHTML = '<p class="empty-msg">Nincs naplózott esemény.</p>';
            return;
        }

        list.innerHTML = events.map(line => {
            let cls = '';
            if      (line.includes('[SZERVER] Elindult'))  cls = 'event-server-on';
            else if (line.includes('[SZERVER] Leállt'))    cls = 'event-server-off';
            else if (line.includes('[BELÉPÉS]'))           cls = 'event-viewer-on';
            else if (line.includes('[KILÉPÉS]'))           cls = 'event-viewer-off';
            else if (line.includes('elindította'))         cls = 'event-rec-on';
            else if (line.includes('leállította'))         cls = 'event-rec-off';
            else if (line.includes('megszakadt'))          cls = 'event-disconnect';
            else if (line.includes('helyreállt'))          cls = 'event-reconnect';
            return `<p class="event-entry ${cls}">${line}</p>`;
        }).join('');

    } catch (err) {
        list.innerHTML = '<p class="empty-msg">Hiba a napló betöltésekor.</p>';
    }
}