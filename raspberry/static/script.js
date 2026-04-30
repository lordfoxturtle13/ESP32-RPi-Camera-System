// ==========================================
// 1. KAMERA ÉS FELVÉTEL VEZÉRLÉS (ÚJ KÁRTYÁS)
// ==========================================
let recordingStates = {};

function toggleRecord(cameraId) {
    const btn = document.getElementById(`record-btn-${cameraId}`);
    const feed = document.getElementById(`feed-${cameraId}`);
    
    // Elküldjük a parancsot a Flask-nek
    fetch(`/toggle_record/${cameraId}`, { method: 'POST' })
        .then(response => response.json())
        .then(data => {
            // Csak akkor változtatjuk meg a színt, ha a szerver visszaigazolta
            if (data.recording !== undefined) {
                recordingStates[cameraId] = data.recording;
                
                if (recordingStates[cameraId]) {
                    btn.classList.add('recording');
                    btn.innerText = '🔴 Felvétel megy';
                    feed.classList.add('recording-border'); 
                } else {
                    btn.classList.remove('recording');
                    btn.innerText = '⏺ Felvétel';
                    feed.classList.remove('recording-border'); 
                }
            }
        })
        .catch(error => console.error("Hiba a felvétel indításakor:", error));
}

function toggleAllRecords() {
    const globalBtn = document.getElementById('record-all-btn');
    const isAnyRecording = Object.values(recordingStates).includes(true);
    const newState = !isAnyRecording;
    
    document.querySelectorAll('.btn-record').forEach(btn => {
        const id = btn.id.replace('record-btn-', '');
        if (!!recordingStates[id] !== newState) {
            toggleRecord(id);
        }
    });

    if (newState) {
        globalBtn.classList.add('recording');
        globalBtn.innerText = '⏹ Összes Felvétel Leállítása';
    } else {
        globalBtn.classList.remove('recording');
        globalBtn.innerText = '🔴 Összes Felvétele';
    }
}

function resetCamera(cameraId) {
    fetch(`/api/reset/${cameraId}`, { method: 'POST' })
        .then(response => response.json())
        .then(data => {
            if (data.status === "success") {
                console.log("Újracsatlakozás indítva a " + cameraId + " kamerán.");
            }
        })
        .catch(error => console.error("Hiba a reset során:", error));
}


// ==========================================
// 2. FELÜLET ÉS NÉZETEK (RÉGI, MEGTARTOTT)
// ==========================================
function toggleSidebar() {
    const sidebar = document.querySelector('.sidebar');
    sidebar.classList.toggle('hidden');
}

function toggleFullScreen(element) {
    if (!document.fullscreenElement && !document.webkitFullscreenElement) {
        if (element.requestFullscreen) {
            element.requestFullscreen();
        } else if (element.webkitRequestFullscreen) {
            element.webkitRequestFullscreen();
        }
    } else {
        if (document.exitFullscreen) {
            document.exitFullscreen();
        } else if (document.webkitExitFullscreen) {
            document.webkitExitFullscreen();
        }
    }
}
//--------------------------
function showLive() {
    document.getElementById('live-view').classList.remove('hidden');
    document.getElementById('archive-view').classList.add('hidden');
    document.getElementById('settings-view').classList.add('hidden'); // Eltünteti a beállításokat
    const player = document.getElementById('archive-player');
    if (player) player.pause();
}

function showArchive() {
    document.getElementById('live-view').classList.add('hidden');
    document.getElementById('archive-view').classList.remove('hidden');
    document.getElementById('settings-view').classList.add('hidden'); // Eltünteti a beállításokat
    loadVideoList(); 
}

// EZ A TELJESEN ÚJ FÜGGVÉNY A GOMBODHOZ
function showSettings() {
    document.getElementById('live-view').classList.add('hidden');
    document.getElementById('archive-view').classList.add('hidden');
    document.getElementById('settings-view').classList.remove('hidden'); // Megjeleníti a beállításokat
    const player = document.getElementById('archive-player');
    if (player) player.pause(); // Ha ment a videó az archívumban, állítsa le
}

// ==========================================
// 3. ARCHÍVUM LEJÁTSZÓ (DUPLA STREAM)
// ==========================================
function loadVideoList() {
    fetch('/api/videos')
        .then(response => response.json())
        .then(files => {
            const list = document.getElementById('video-list');
            list.innerHTML = ''; 

            if (files.length === 0) {
                list.innerHTML = '<p style="color: var(--text-color);">Nincsenek mentett felvételek az archivum mappában.</p>';
                return;
            }

            files.forEach(file => {
                const btn = document.createElement('button');
                btn.className = 'video-item-btn';
                // A gomb feliratából kivesszük a "_main" szót, hogy szebb legyen
                btn.innerText = "🎥 " + file.replace('_main.mp4', '');
                btn.onclick = () => playVideo(file);
                list.appendChild(btn);
            });
        })
        .catch(err => console.error("Hiba a videók lekérésekor:", err));
}

function playVideo(filename) {
    document.getElementById('player-container').classList.remove('hidden');
    
    const mainPlayer = document.getElementById('archive-player');
    const subPlayer = document.getElementById('archive-player-sub');

    // Mivel a fájl pl. "cam_0_idő_main.webm", a sub fájl "cam_0_idő_sub.webm" lesz
    const subFilename = filename.replace('_main.mp4', '_sub.mp4');

    mainPlayer.pause();
    subPlayer.pause();

    // Mindkét videó betöltése
    mainPlayer.innerHTML = `<source src="/archivum_video/${filename}" type="video/mp4">`;
    subPlayer.innerHTML = `<source src="/archivum_video/${subFilename}" type="video/mp4">`;

    mainPlayer.load();
    subPlayer.load(); 
    mainPlayer.play();
}

// === AZ ESEMÉNYVEZÉRELT TEKERÉS LOGIKÁJA ===
document.addEventListener('DOMContentLoaded', () => {
    const mainPlayer = document.getElementById('archive-player');
    const subPlayer = document.getElementById('archive-player-sub');

    if (mainPlayer && subPlayer) {
        // Amikor elkezded húzni a csúszkát (seeking)
        mainPlayer.addEventListener('seeking', () => {
            // A nagy videót átlátszóvá tesszük, de a vezérlői megmaradnak!
            mainPlayer.style.opacity = '0.01'; 
            // A háttérben lévő kicsi videó idejét rászinkronizáljuk a tekerőre
            subPlayer.currentTime = mainPlayer.currentTime;
        });

        // Amikor elengeded a csúszkát (seeked)
        mainPlayer.addEventListener('seeked', () => {
            // Visszahozzuk a nagy videót láthatóra
            mainPlayer.style.opacity = '1';
        });
    }
});