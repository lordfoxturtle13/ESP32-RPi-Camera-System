// ================================================
// ÁLLAPOT — globális változók
// ================================================

// Felvételi állapotok kameránként (szerver igazolja vissza, nem feltételezzük)
const recordingStates = {};

// LED állapotok kameránként
const ledStates = {};

// Éppen aktív nézet azonosítója
let currentView = 'live';

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

    if (!document.fullscreenElement && !document.webkitFullscreenElement) {
        (el.requestFullscreen || el.webkitRequestFullscreen).call(el);
    } else {
        (document.exitFullscreen || document.webkitExitFullscreen).call(document);
    }
}

// ================================================
// ARCHÍVUM LEJÁTSZÓ
// ================================================

/** Betölti és megjeleníti az archívum videólistát. */
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

        files.forEach(filename => {
            const btn = document.createElement('button');
            btn.className   = 'video-item-btn';
            // A "_main.mp4" végzést levágjuk a szebb megjelenítésért
            btn.textContent = filename.replace('_main.mp4', '').replace(/_/g, ' ');
            btn.dataset.filename = filename;
            btn.addEventListener('click', () => {
                // Aktív kiemelés
                list.querySelectorAll('.video-item-btn').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                playVideo(filename);
            });
            list.appendChild(btn);
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

    if (title) title.textContent = filename.replace('_main.mp4', '').replace(/_/g, ' ');

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
// ARCHÍVUM — Nyíl billentyűs navigáció
// ================================================

document.addEventListener('keydown', (e) => {
    if (currentView !== 'archive') return;
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;

    const buttons = [...document.querySelectorAll('#video-list .video-item-btn')];
    if (buttons.length === 0) return;

    e.preventDefault();

    const activeBtn   = document.querySelector('#video-list .video-item-btn.active');
    const currentIdx  = activeBtn ? buttons.indexOf(activeBtn) : -1;
    const nextIdx     = e.key === 'ArrowDown'
        ? Math.min(currentIdx + 1, buttons.length - 1)
        : Math.max(currentIdx - 1, 0);

    buttons[nextIdx].click();
    buttons[nextIdx].scrollIntoView({ block: 'nearest', behavior: 'smooth' });
});