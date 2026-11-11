// ================================================
// ÁLLAPOT — globális változók
// ================================================

// Felvételi állapotok kameránként (szerver igazolja vissza, nem feltételezzük)
const recordingStates = {};

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
        const { storage_alert: storageAlert, ...cameras } = await resp.json();

        Object.entries(cameras).forEach(([camId, state]) => {
            updateCameraUI(Number(camId), state);
        });
        showStorageAlert(storageAlert);
    } catch (err) {
        // Hálózati hiba esetén csendesen tűrjük (pl. RPi újraindulás)
        console.warn('[STATUS] Nem sikerült lekérni:', err);
    }
}

/**
 * Egy kamera összes UI elemét frissíti a szerver állapota alapján.
 * @param {number} camId  - Kamera azonosítója
 * @param {object} state  - { status, recording }
 */
function updateCameraUI(camId, state) {
    const { status, recording } = state;

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
        } else if (data.error) {
            showStorageAlert(data.error);
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

    updateStreams();

    // Archívum betöltése, ha arra váltunk
    if (viewName === 'archive') loadVideoList();
    if (viewName === 'settings') loadStorageStatus();

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

function showStorageAlert(message) {
    const banner = document.getElementById('storage-alert');
    if (!banner) return;
    banner.textContent = message || '';
    banner.classList.toggle('hidden', !message);
}

/** A tárhely összesítőjét és a beállítások űrlapját tölti ki. */
async function loadStorageStatus() {
    const summary = document.getElementById('storage-summary-text');
    try {
        const resp = await fetch('/api/storage');
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error);

        const usedPercent = 100 - data.free_percent;
        document.getElementById('storage-meter-used').style.width = `${usedPercent}%`;
        const legacy = data.unmanaged ? ` · ${data.unmanaged} régi fájl a listán kívül` : '';
        summary.textContent =
            `${formatBytes(data.free)} szabad / ${formatBytes(data.total)} · ` +
            `${data.recordings} felvétel (${formatBytes(data.archive_size)})${legacy}`;

        [['storage-retention', 'retention_days'],
         ['storage-min-free', 'min_free_percent'],
         ['storage-segment', 'segment_minutes']].forEach(([id, key]) => {
            const input = document.getElementById(id);
            input.min = data.ranges[key][0];
            input.max = data.ranges[key][1];
            // Gépelés közben ne írjuk felül az értéket
            if (document.activeElement !== input) input.value = data.settings[key];
        });
        showStorageAlert(data.alert);
    } catch (err) {
        if (summary) summary.textContent = 'A tárhely adatai nem érhetők el.';
        console.error('[STORAGE] Hiba:', err);
    }
}

async function saveStorageSettings(event) {
    event.preventDefault();
    const msg = document.getElementById('storage-msg');
    msg.className = 'pw-msg';
    try {
        const resp = await fetch('/api/storage/settings', {
            method : 'POST',
            headers: { 'Content-Type': 'application/json' },
            body   : JSON.stringify({
                retention_days  : Number(document.getElementById('storage-retention').value),
                min_free_percent: Number(document.getElementById('storage-min-free').value),
                segment_minutes : Number(document.getElementById('storage-segment').value),
            }),
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error);
        msg.textContent = 'Mentve';
        msg.classList.add('pw-msg-ok');
        showStorageAlert(data.alert);
        loadStorageStatus();
    } catch (err) {
        msg.textContent = err.message || 'Nem sikerült menteni';
        msg.classList.add('pw-msg-error');
    }
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
    download.className = 'video-action-btn';
    download.textContent = 'Letöltés';
    if (item.recording) download.disabled = true;
    else download.href = `/archivum_video/${item.path}?download=1`;

    row.append(play, download);
    return row;
}

/** Betölti az archívum felvételeit, dátum és azon belül kamera szerint csoportosítva. */
async function loadVideoList() {
    const list = document.getElementById('video-list');
    if (!list) return;

    list.innerHTML = '<p class="empty-msg">Betöltés...</p>';
    loadStorageStatus();

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

// A böngésző címenként kb. 6 kapcsolatot tart nyitva, és minden élőkép folyamatosan foglal egyet.
// Ha ezek elfogynak, a többi kérés (állapot, beállítások) is megáll, ezért egyszerre legfeljebb
// ennyi élőkép fut. A többi kamera lapozással érhető el.
const LIVE_PAGE_SIZE = 4;
let livePage = 0;

/** Csak a látható kamerák streamje fut: más nézetben és háttérbe került lapon nem. */
function updateStreams() {
    const active = currentView === 'live' && document.visibilityState === 'visible';

    document.querySelectorAll('.camera-card').forEach(card => {
        const img = card.querySelector('.camera-feed');
        const wanted = active && !card.classList.contains('hidden');

        if (wanted && !img.hasAttribute('src')) {
            img.src = `/video_feed/${img.dataset.camId}?_=${Date.now()}`;
        } else if (!wanted && img.hasAttribute('src')) {
            img.removeAttribute('src');
        }
    });
}

function renderLivePage() {
    const cards = document.querySelectorAll('.camera-card');
    const pages = Math.max(1, Math.ceil(cards.length / LIVE_PAGE_SIZE));
    livePage = Math.min(Math.max(livePage, 0), pages - 1);

    cards.forEach((card, index) => {
        card.classList.toggle('hidden', Math.floor(index / LIVE_PAGE_SIZE) !== livePage);
    });

    document.getElementById('camera-pager').classList.toggle('hidden', pages <= 1);
    document.getElementById('pager-label').textContent = `${livePage + 1} / ${pages}`;
    document.getElementById('pager-prev').disabled = livePage === 0;
    document.getElementById('pager-next').disabled = livePage === pages - 1;

    updateStreams();
}

function changeLivePage(delta) {
    livePage += delta;
    renderLivePage();
}

function reconnectStreams() {
    document.querySelectorAll('.camera-feed').forEach(img => img.removeAttribute('src'));
    updateStreams();
}

document.addEventListener('DOMContentLoaded', renderLivePage);

// Bfcache (vissza gombbal visszatérés) esetén a stream meghal — újraindítjuk
window.addEventListener('pageshow', (e) => {
    if (e.persisted) reconnectStreams();
});

// Tab váltás / minimalizálás után visszatéréskor
document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') reconnectStreams();
    else updateStreams();
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

// ================================================
// JELSZÓ MÓDOSÍTÁS
// ================================================

async function changePassword() {
    const current = document.getElementById('pw-current').value;
    const newPw   = document.getElementById('pw-new').value;
    const confirm = document.getElementById('pw-confirm').value;
    const msg     = document.getElementById('pw-msg');

    msg.className = 'pw-msg';

    if (newPw !== confirm) {
        msg.textContent = 'A két új jelszó nem egyezik.';
        msg.classList.add('pw-msg-error');
        return;
    }

    try {
        const resp = await fetch('/api/change_password', {
            method : 'POST',
            headers: { 'Content-Type': 'application/json' },
            body   : JSON.stringify({ current, new: newPw }),
        });
        const data = await resp.json();

        if (data.success) {
            msg.textContent = 'Jelszó sikeresen megváltoztatva.';
            msg.classList.add('pw-msg-ok');
            document.getElementById('pw-current').value = '';
            document.getElementById('pw-new').value     = '';
            document.getElementById('pw-confirm').value = '';
        } else {
            msg.textContent = data.error || 'Hiba történt.';
            msg.classList.add('pw-msg-error');
        }
    } catch (err) {
        msg.textContent = 'Hálózati hiba.';
        msg.classList.add('pw-msg-error');
    }
}