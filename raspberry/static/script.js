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

function showLive() {
    document.getElementById('live-view').classList.remove('hidden');
    document.getElementById('archive-view').classList.add('hidden');
    const player = document.getElementById('archive-player');
    if (player) player.pause();
}

function showArchive() {
    document.getElementById('live-view').classList.add('hidden');
    document.getElementById('archive-view').classList.remove('hidden');
    loadVideoList(); 
}


// ==========================================
// 3. ARCHÍVUM LEJÁTSZÓ (RÉGI, MEGTARTOTT)
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
                btn.innerText = "🎥 " + file;
                btn.onclick = () => playVideo(file);
                list.appendChild(btn);
            });
        })
        .catch(err => console.error("Hiba a videók lekérésekor:", err));
}

function playVideo(filename) {
    document.getElementById('player-container').classList.remove('hidden');
    const player = document.getElementById('archive-player');

    player.pause();
    player.innerHTML = `<source src="/archivum_video/${filename}" type="video/${filename.endsWith('.webm') ? 'webm' : 'mp4'}">`;
    player.load(); 
    player.play();
}