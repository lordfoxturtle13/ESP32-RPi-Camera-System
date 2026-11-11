// ================================================
// ÁLLAPOT
// ================================================

// Felvételi állapotok kameránként (szerver igazolja vissza, nem feltételezzük)
const recordingStates = {};

// Éppen aktív nézet: 'live', 'archive' vagy 'events'
let currentView = 'live';


// ================================================
// STÁTUSZ LEKÉRDEZÉS
// A /status 2 másodpercenként adja a kamerák állapotát és az esetleges tárhely-figyelmeztetést.
// ================================================

async function fetchStatus() {
    try {
        const resp = await fetch('/status');
        const { storage_alert: storageAlert, ...cameras } = await resp.json();

        Object.entries(cameras).forEach(([camId, state]) => updateCameraUI(Number(camId), state));
        showStorageAlert(storageAlert);
    } catch (err) {
        // Hálózati hiba esetén csendesen tűrjük (pl. újraindul a szerver)
        console.warn('[STATUS] Nem sikerült lekérni:', err);
    }
}

/**
 * Egy kamera UI elemeit frissíti a szerver állapota alapján.
 * @param {number} camId  - Kamera azonosítója
 * @param {object} state  - { status: 'online' | 'offline', recording }
 */
function updateCameraUI(camId, { status, recording }) {
    const isOnline = status === 'online';

    document.getElementById(`dot-${camId}`)?.classList.toggle('online', isOnline);
    document.getElementById(`overlay-${camId}`)?.classList.toggle('visible', !isOnline);

    // Offline kamerán csak a futó felvétel állítható le
    const recBtn = document.getElementById(`record-btn-${camId}`);
    if (recBtn) recBtn.disabled = !isOnline && !recording;

    if (recordingStates[camId] !== recording) {
        recordingStates[camId] = recording;
        applyRecordingUI(camId, recording);
    }
}

function showStorageAlert(message) {
    const banner = document.getElementById('storage-alert');
    banner.textContent = message || '';
    banner.classList.toggle('hidden', !message);
}


// ================================================
// FELVÉTEL VEZÉRLÉS
// ================================================

async function toggleRecord(cameraId) {
    try {
        const resp = await fetch(`/toggle_record/${cameraId}`, { method: 'POST' });
        const data = await resp.json();

        if (data.recording !== undefined) {
            recordingStates[cameraId] = data.recording;
            applyRecordingUI(cameraId, data.recording);
        } else if (data.error) {
            showStorageAlert(data.error);
        }
    } catch (err) {
        console.error('[REC] Hiba a felvétel indításakor:', err);
    }
}

function applyRecordingUI(cameraId, isRecording) {
    const btn = document.getElementById(`record-btn-${cameraId}`);
    if (btn) {
        btn.classList.toggle('is-recording', isRecording);
        btn.textContent = isRecording ? 'Leállítás' : 'Felvétel';
    }
    document.getElementById(`card-${cameraId}`)?.classList.toggle('is-recording', isRecording);
    document.getElementById(`rec-indicator-${cameraId}`)?.classList.toggle('visible', isRecording);
}


// ================================================
// NÉZETEK VÁLTÁSA
// ================================================

function showView(viewName) {
    currentView = viewName;

    ['live', 'archive', 'events'].forEach(name => {
        const isActive = name === viewName;
        document.getElementById(`view-${name}`).classList.toggle('hidden', !isActive);
        document.getElementById(`nav-${name}`).classList.toggle('active', isActive);
    });

    updateStreams();
    if (viewName === 'archive') loadVideoList();
    if (viewName === 'events') loadEvents();

    // Lejátszó leállítása, ha elhagyjuk az archívumot
    if (viewName !== 'archive') document.getElementById('archive-player').pause();
}

/** Az élőképek csak az Élőkép nézetben futnak, máshol nem foglalnak sávszélességet. */
function updateStreams() {
    document.querySelectorAll('.camera-feed').forEach(img => {
        if (currentView === 'live') img.src = `/video_feed/${img.dataset.camId}?_=${Date.now()}`;
        else img.removeAttribute('src');
    });
}


// ================================================
// ARCHÍVUM
// ================================================

const MONTHS = ['január','február','március','április','május','június','július','augusztus','szeptember','október','november','december'];

/** "2026-07-06" → "2026. július 6." */
function formatDateLabel(dateKey) {
    const [y, m, d] = dateKey.split('-');
    return `${y}. ${MONTHS[parseInt(m) - 1]} ${parseInt(d)}.`;
}

/** "cam_0" → "CAM00" */
function camDirToLabel(camDir) {
    return `CAM${String(camDir.replace('cam_', '')).padStart(2, '0')}`;
}

/** "2026-07-06/cam_0/cam_0_20260706_183045_main.mp4" → "18:30:45" */
function parseTimeFromPath(filepath) {
    const parts = filepath.split('/').pop().split('_');
    if (parts.length < 4) return filepath;
    const t = parts[3];
    return t.length === 6 ? `${t.slice(0,2)}:${t.slice(2,4)}:${t.slice(4,6)}` : filepath;
}

/** Bájtok olvasható alakban: "152 MB" */
function formatBytes(bytes) {
    if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
    if (bytes >= 1024 ** 2) return `${Math.round(bytes / 1024 ** 2)} MB`;
    return `${Math.round(bytes / 1024)} KB`;
}

function buildVideoRow(item, list) {
    const row = document.createElement('div');
    row.className = 'video-row';

    const play = document.createElement('button');
    play.className = 'video-item-btn';
    play.textContent = `${item.time} · ${formatBytes(item.size)}${item.recording ? ' · készül' : ''}`;
    play.disabled = item.recording;  // A készülő fájlnak még nincs indexe, nem játszható le
    play.addEventListener('click', () => {
        list.querySelectorAll('.video-item-btn').forEach(b => b.classList.remove('active'));
        play.classList.add('active');
        playVideo(item.path);
    });

    // A készülő felvétel nem tölthető le: ott nincs link, csak egy tiltott gomb
    const download = document.createElement(item.recording ? 'button' : 'a');
    download.className = 'small-btn';
    download.textContent = 'Letöltés';
    if (item.recording) download.disabled = true;
    else download.href = `/archivum_video/${item.path}?download=1`;

    row.append(play, download);
    return row;
}

/** Betölti az archívum felvételeit, dátum és azon belül kamera szerint csoportosítva. */
async function loadVideoList() {
    const list = document.getElementById('video-list');
    list.innerHTML = '<p class="empty-msg">Betöltés...</p>';

    try {
        const resp  = await fetch('/api/videos');
        const items = await resp.json();
        if (!resp.ok) throw new Error(items.error);

        list.innerHTML = '';
        if (items.length === 0) {
            list.innerHTML = '<p class="empty-msg">Még nincs felvétel.</p>';
            return;
        }

        const groups = {};
        items.forEach(item => {
            const byCamera = groups[item.date] = groups[item.date] || {};
            (byCamera[item.camera_name] = byCamera[item.camera_name] || []).push(item);
        });

        Object.entries(groups).forEach(([dateKey, cameras]) => {
            const dateHeader = document.createElement('p');
            dateHeader.className = 'video-date-header';
            dateHeader.textContent = formatDateLabel(dateKey);
            list.appendChild(dateHeader);

            Object.entries(cameras).forEach(([cameraName, cameraItems]) => {
                const camHeader = document.createElement('p');
                camHeader.className = 'video-cam-header';
                camHeader.textContent = cameraName;
                list.appendChild(camHeader);
                cameraItems.forEach(item => list.appendChild(buildVideoRow(item, list)));
            });
        });

    } catch (err) {
        list.innerHTML = '<p class="empty-msg">Hiba a lista betöltésekor.</p>';
        console.error('[ARCHIVE] Hiba:', err);
    }
}

/**
 * Betölt és lejátszik egy archív videót a dupla streames lejátszóban.
 * @param {string} filepath - Relatív útvonal: "2026-07-06/cam_0/cam_0_..._main.mp4"
 */
function playVideo(filepath) {
    const mainPlayer = document.getElementById('archive-player');
    const subPlayer  = document.getElementById('archive-player-sub');
    const title      = document.getElementById('player-title');

    if (!mainPlayer || !subPlayer) return;

    const subFilepath = filepath.replace('_main.mp4', '_sub.mp4');
    const parts       = filepath.split('/');
    if (title) title.textContent =
        `${camDirToLabel(parts[1])} — ${formatDateLabel(parts[0])} ${parseTimeFromPath(filepath)}`;

    mainPlayer.pause();
    subPlayer.pause();

    mainPlayer.src = `/archivum_video/${filepath}`;
    subPlayer.src  = `/archivum_video/${subFilepath}`;

    mainPlayer.load();
    subPlayer.load();
    mainPlayer.play();
}


// ================================================
// ESEMÉNYNAPLÓ
// ================================================

async function loadEvents() {
    const list = document.getElementById('events-list');

    try {
        const resp   = await fetch('/api/events');
        const events = await resp.json();

        if (events.length === 0) {
            list.innerHTML = '<p class="empty-msg">Nincs naplózott esemény.</p>';
            return;
        }

        // textContent: a napló sorai nem kerülnek HTML-ként a lapba
        list.replaceChildren(...events.map(line => {
            const entry = document.createElement('p');
            entry.className = 'event-entry';
            if      (line.includes('elindította')) entry.classList.add('event-rec-on');
            else if (line.includes('leállította')) entry.classList.add('event-rec-off');
            else if (line.includes('megszakadt'))  entry.classList.add('event-disconnect');
            else if (line.includes('helyreállt'))  entry.classList.add('event-reconnect');
            entry.textContent = line;
            return entry;
        }));

    } catch (err) {
        list.innerHTML = '<p class="empty-msg">Hiba a napló betöltésekor.</p>';
    }
}


// ================================================
// INDÍTÁS ÉS TEKERÉS
// ================================================

document.addEventListener('DOMContentLoaded', () => {
    const mainPlayer = document.getElementById('archive-player');
    const subPlayer  = document.getElementById('archive-player-sub');

    // SEEKING LOGIKA — Dupla stream szinkronizáció
    // Tekerés közben az al-stream (alacsony felbontású) előnézetet mutat,
    // így a csúszka mozgatása azonnal reagál a nagy videó buffering helyett.
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

    updateStreams();
    fetchStatus();                         // Azonnal lekér, nem vár 2 másodpercet
    setInterval(fetchStatus, 2000);
});
