/**
 * DrishX Tactical Command Terminal v1.0.0
 */

class DrishXDashboard {
    constructor() {
        this.map = null;
        this.chart = null;
        this.markers = {};
        this.roadLayer = null;
        this.sites = [];
        this.currentView = 'dashboard';
        this.isSatellite = false;
        this.selectedMissionIds = new Set();
        this.allMissions = [];
        this.allMissionsFetched = false;

        this.init();
    }

    async init() {
        console.log("Initializing DrishX Operational Link...");
        this.setupMap();
        this.setupEventListeners();
        this.setupConnectModal();
        this.setupDocs();

        // Initial data fetch
        await this.fetchSites();

        // Boot Auth: check real link state, then silently re-link or prompt.
        this.initAuth();
    }

    setupMap() {
        // Use the German A2 (Fisser et al. Validation Site) as default view
        const testArea = [52.345, 10.550];
        this.map = L.map('main-map', {
            center: testArea,
            zoom: 14,
            zoomControl: false,
            attributionControl: false
        });

        this.updateBasemap();

        L.control.zoom({ position: 'bottomright' }).addTo(this.map);

        // Initialize drawing layer
        this.drawnItems = new L.FeatureGroup();
        this.map.addLayer(this.drawnItems);

        this.drawControl = new L.Control.Draw({
            draw: {
                polygon: false,
                marker: false,
                circle: false,
                circlemarker: false,
                polyline: false,
                rectangle: {
                    shapeOptions: {
                        color: 'var(--accent-blue)',
                        weight: 2
                    }
                }
            },
            edit: {
                featureGroup: this.drawnItems,
                remove: true
            }
        });

        this.map.on(L.Draw.Event.CREATED, (e) => {
            const layer = e.layer;
            this.drawnItems.clearLayers();
            this.drawnItems.addLayer(layer);
            const bbox = layer.getBounds();
            this.handleAOISelection(bbox);
        });
    }

    updateBasemap() {
        if (this.currentBasemap) this.map.removeLayer(this.currentBasemap);

        const url = this.isSatellite
            ? 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}'
            : 'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png';

        this.currentBasemap = L.tileLayer(url, {
            subdomains: 'abcd',
            maxZoom: 20
        }).addTo(this.map);
    }

    setupEventListeners() {
        // AOI Selector
        document.getElementById('draw-aoi')?.addEventListener('click', () => {
            const rectDrawer = new L.Draw.Rectangle(this.map, this.drawControl.options.draw.rectangle);
            rectDrawer.enable();
            this.notify("Select an area on the map to analyze.", "info");
        });

        // Satellite Toggle
        document.getElementById('toggle-satellite')?.addEventListener('click', () => {
            this.isSatellite = !this.isSatellite;
            this.updateBasemap();
            this.notify(`Basemap switched to ${this.isSatellite ? 'Satellite' : 'Dark Mode'}`, "info");
        });

        // Navigation
        document.querySelectorAll('.nav-item').forEach(btn => {
            btn.addEventListener('click', (e) => {
                const view = e.currentTarget.dataset.view;
                this.switchView(view);
            });
        });

        // Trends Controls
        document.getElementById('refresh-trends')?.addEventListener('click', () => this.updateTrends());

        // Initialize Flatpickr for better calendar experience
        const fpConfig = {
            theme: "dark",
            dateFormat: "Y-m-d",
            onChange: () => this.updateTrends()
        };

        flatpickr("#trend-from", fpConfig);
        flatpickr("#trend-to", fpConfig);

        // Search Bar
        const searchBtn = document.getElementById('execute-search');
        const searchInput = document.getElementById('map-search-input');

        searchBtn?.addEventListener('click', () => this.handleLocationSearch());
        searchInput?.addEventListener('keypress', (e) => {
            if (e.key === 'Enter') this.handleLocationSearch();
        });

        // Close Overlays
        document.querySelector('.close-overlay')?.addEventListener('click', () => {
            document.getElementById('site-overlay').classList.add('hidden');
        });

        document.getElementById('close-intel')?.addEventListener('click', () => {
            document.getElementById('intel-drawer').classList.add('hidden');
        });
    }

    async handleLocationSearch() {
        const query = document.getElementById('map-search-input').value;
        if (!query) return;

        const dropdown = document.getElementById('search-results-dropdown');
        dropdown.innerHTML = '<div class="search-result"><span class="main-text">Querying satellites...</span></div>';
        dropdown.classList.remove('hidden');

        try {
            const resp = await fetch(`https://nominatim.openstreetmap.org/search?format=json&q=${encodeURIComponent(query)}&limit=5`);
            const results = await resp.json();

            if (results.length === 0) {
                dropdown.innerHTML = '<div class="search-result"><span class="main-text">No sectors found.</span></div>';
                return;
            }

            dropdown.innerHTML = results.map(res => `
                <div class="search-result" onclick="window.dashboard.jumpToLocation(${res.lat}, ${res.lon}, '${res.display_name.split(',')[0]}')">
                    <span class="main-text">${res.display_name.split(',')[0]}</span>
                    <span class="sub-text">${res.display_name.split(',').slice(1).join(',')}</span>
                </div>
            `).join('');

        } catch (e) {
            this.notify("Search engine offline.", "error");
            dropdown.classList.add('hidden');
        }
    }

    jumpToLocation(lat, lon, label) {
        this.map.flyTo([lat, lon], 15, { duration: 1.5 });
        document.getElementById('search-results-dropdown').classList.add('hidden');
        this.notify(`Navigating to sector: ${label}`, "info");

        // Brief highlight
        const circle = L.circle([lat, lon], {
            radius: 500,
            color: 'var(--accent-blue)',
            fillColor: 'var(--accent-blue)',
            fillOpacity: 0.1,
            dashArray: '5, 10'
        }).addTo(this.map);

        setTimeout(() => this.map.removeLayer(circle), 3000);
    }

    switchView(view) {
        this.currentView = view;
        document.querySelectorAll('.view').forEach(v => v.classList.add('hidden'));
        document.getElementById(`${view}-view`)?.classList.remove('hidden');

        // Update tabs
        document.querySelectorAll('.nav-item').forEach(btn => {
            btn.classList.toggle('active', btn.dataset.view === view);
        });

        if (view === 'trends') {
            this.updateTrends();
        }

        if (view === 'docs') {
            this.syncDocsActive?.();
        }

        // Update header
        const titles = {
            dashboard: 'Operations',
            trends: 'Tactical Trends',
            docs: 'Docs'
        };
        const titleEl = document.querySelector('.top-header h1');
        if (titleEl && titles[view]) titleEl.textContent = titles[view];
    }

    async updateTrends() {
        const fromDate = document.getElementById('trend-from').value;
        const toDate = document.getElementById('trend-to').value;
        const siteIdsArray = Array.from(this.selectedMissionIds || []);
        const siteIds = siteIdsArray.join(',');

        try {
            const resp = await fetch(`/api/analytics/trends?from_date=${fromDate}&to_date=${toDate}${siteIds ? `&site_ids=${siteIds}` : ''}`);
            const data = await resp.json();

            // Update stats
            document.getElementById('stat-total').textContent = data.summary.total_detections;
            document.getElementById('stat-peak').textContent = data.summary.missions_count + " Sectors";
            document.getElementById('stat-avg').textContent = data.datasets.length;

            this.renderTrendChart(data);
            this.updateMissionSelector();
        } catch (e) {
            console.error("Trends fetch error:", e);
            this.notify("Failed to sync historical trends.", "error");
        }
    }

    updateMissionSelector() {
        const container = document.getElementById('mission-comparison-selector');
        if (!container) return;

        if (this.allMissionsFetched) {
            this.renderMissionChecklist(container);
            return;
        }

        fetch('/api/sites').then(r => r.json()).then(sites => {
            this.allMissions = sites.filter(s => s.type === 'history');
            this.allMissionsFetched = true;
            this.renderMissionChecklist(container);
        });
    }

    renderMissionChecklist(container) {
        if (!this.selectedMissionIds) this.selectedMissionIds = new Set();

        container.innerHTML = this.allMissions.map((m, i) => {
            const isActive = this.selectedMissionIds.has(m.id);
            const colors = ["#3b82f6", "#f59e0b", "#10b981", "#ef4444", "#a855f7", "#ec4899"];
            const color = colors[i % colors.length];

            return `
                <div class="comparison-item ${isActive ? 'active' : ''}" onclick="window.dashboard.toggleMissionComparison('${m.id}')">
                    <span class="color-dot" style="background: ${color}"></span>
                    <span>${m.name}</span>
                </div>
            `;
        }).join('');
    }

    toggleMissionComparison(id) {
        if (!this.selectedMissionIds) this.selectedMissionIds = new Set();
        if (this.selectedMissionIds.has(id)) {
            this.selectedMissionIds.delete(id);
        } else {
            this.selectedMissionIds.add(id);
        }
        this.updateTrends();
    }

    renderTrendChart(data) {
        const ctx = document.getElementById('trend-chart')?.getContext('2d');
        if (!ctx) return;

        if (this.trendChartInstance) {
            this.trendChartInstance.destroy();
        }

        this.trendChartInstance = new Chart(ctx, {
            type: 'line',
            data: {
                labels: data.labels,
                datasets: data.datasets.map(ds => ({
                    ...ds,
                    borderWidth: 3,
                    fill: true,
                    tension: 0.4,
                    pointRadius: 4,
                    pointBackgroundColor: ds.borderColor
                }))
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: {
                        display: true,
                        labels: { color: '#94a3b8', boxWidth: 12, padding: 20 }
                    },
                    tooltip: {
                        mode: 'index',
                        intersect: false,
                        backgroundColor: '#1e293b',
                        titleColor: '#94a3b8',
                        bodyColor: '#fff',
                        borderColor: 'rgba(255,255,255,0.1)',
                        borderWidth: 1
                    }
                },
                scales: {
                    y: {
                        grid: { color: 'rgba(255,255,255,0.05)', drawBorder: false },
                        ticks: { color: '#94a3b8', font: { size: 10 } }
                    },
                    x: {
                        grid: { display: false },
                        ticks: { color: '#94a3b8', font: { size: 10 } }
                    }
                }
            }
        });
    }

    async handleAOISelection(bounds, siteId = 'custom', siteName = null) {
        const sw = bounds instanceof L.LatLngBounds ? bounds.getSouthWest() : { lat: bounds[0], lng: bounds[1] };
        const ne = bounds instanceof L.LatLngBounds ? bounds.getNorthEast() : { lat: bounds[2], lng: bounds[3] };
        const bbox = bounds instanceof L.LatLngBounds ? [sw.lat, sw.lng, ne.lat, ne.lng] : bounds;

        // Prepare HUD
        const hud = document.getElementById('progress-hud');
        const progressBar = document.getElementById('hud-progress-bar');
        const stepText = document.getElementById('hud-step-text');
        const percentText = document.getElementById('hud-percent-text');
        const logConsole = document.getElementById('hud-log');

        hud.classList.remove('hidden');
        progressBar.style.width = '0%';
        stepText.innerText = "Initializing mission...";
        percentText.innerText = "0%";
        logConsole.innerHTML = '';

        const appendLog = (msg) => {
            const entry = document.createElement('div');
            entry.className = 'log-entry';
            entry.innerHTML = `
                <span class="time">[${new Date().toLocaleTimeString()}]</span>
                <span class="indicator">»</span>
                <span class="msg">${msg}</span>
            `;
            logConsole.appendChild(entry);
            logConsole.scrollTop = logConsole.scrollHeight;
        };

        const months = parseInt(document.getElementById('mission-months')?.value || 4);
        const frames = parseInt(document.getElementById('mission-frames')?.value || 10);
        const label = siteName ? `Mission: ${siteName}` : `Analysis Area ${new Date().toLocaleTimeString()} (${months}mo, ${frames}fr)`;

        try {
            const resp = await fetch('/api/analyze', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    bbox: bbox,
                    label: label,
                    months: months,
                    max_frames: frames,
                    site_id: siteId
                })
            });

            const reader = resp.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;

                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split('\n');
                buffer = lines.pop(); // Keep partial line in buffer

                for (const line of lines) {
                    if (!line.trim()) continue;
                    try {
                        const evt = JSON.parse(line);

                        if (evt.type === 'progress') {
                            progressBar.style.width = `${evt.percent}%`;
                            percentText.innerText = `${evt.percent}%`;
                            stepText.innerText = evt.message;
                            appendLog(evt.message);
                        } else if (evt.type === 'result') {
                            appendLog("Mission complete. Synchronizing results...");
                            this.notify(evt.message, "success");

                            // Successful finish
                            setTimeout(() => {
                                hud.classList.add('hidden');
                                this.fetchRoads(bbox);
                                this.fetchSites();

                                // Show observation markers if available
                                if (evt.mission_id) {
                                    this.loadMissionMarkers(evt.mission_id);
                                }
                            }, 1500);
                        } else if (evt.type === 'error') {
                            this.notify(evt.status === 'error' ? evt.message : "Analysis failed.", "error");
                            appendLog(`ERROR: ${evt.message}`);
                            setTimeout(() => hud.classList.add('hidden'), 3000);
                        }
                    } catch (err) {
                        console.error("Parse error in stream:", err);
                    }
                }
            }
        } catch (e) {
            this.notify("Network error in satellite link.", "error");
            appendLog("CRITICAL: Connection timed out.");
            setTimeout(() => hud.classList.add('hidden'), 3000);
        }
    }

    async fetchRoads(bbox) {
        try {
            const [minLat, minLon, maxLat, maxLon] = bbox;
            const resp = await fetch(`/api/roads?min_lat=${minLat}&min_lon=${minLon}&max_lat=${maxLat}&max_lon=${maxLon}`);
            const geojson = await resp.json();
            this.renderRoads(geojson);
        } catch (e) {
            console.error("Failed to fetch roads:", e);
        }
    }

    renderRoads(geojson) {
        if (this.roadLayer) this.map.removeLayer(this.roadLayer);

        this.roadLayer = L.geoJSON(geojson, {
            style: {
                color: 'var(--accent-amber)',
                weight: 3,
                opacity: 0.6,
                dashArray: '5, 5'
            }
        }).addTo(this.map);

        this.notify("Road corridors identified and highlighted.", "info");
    }

    async loadMissionMarkers(missionId) {
        try {
            const resp = await fetch(`/api/detections/${missionId}`);
            const detections = await resp.json();
            this.renderObservationMarkers(detections);
        } catch (e) {
            console.error("Failed to load mission markers:", e);
        }
    }

    renderObservationMarkers(detections) {
        // Clear existing observation markers
        if (this.obsMarkers) {
            this.obsMarkers.forEach(m => this.map.removeLayer(m));
        }
        this.obsMarkers = [];

        detections.forEach(d => {
            const marker = L.marker([d.lat, d.lon], {
                icon: L.divIcon({
                    className: 'observation-pip',
                    html: '<div class="pip-core"></div>',
                    iconSize: [12, 12],
                    iconAnchor: [6, 6]
                })
            }).addTo(this.map);

            marker.on('click', () => {
                this.showDetectionIntel(d);
                this.map.setView([d.lat, d.lon], 18);
            });

            this.obsMarkers.push(marker);
        });

        if (detections.length > 0) {
            this.notify(`Mapped ${detections.length} tactical detections.`, "success");
        }
    }

    showDetectionIntel(d) {
        const drawer = document.getElementById('intel-drawer');
        const content = document.getElementById('intel-content');
        if (!drawer || !content) return;

        drawer.classList.remove('hidden');
        content.innerHTML = `
            <div class="intel-profile">
                <div class="multispectral-view">
                    <img src="${d.image_url}" alt="Target Signature">
                </div>
                <div class="telemetry-grid">
                    <div class="tel-item">
                        <span class="hud-label">Sensed Domain</span>
                        <span class="tel-value">${new Date(d.timestamp).toLocaleDateString()}</span>
                    </div>
                    <div class="tel-item">
                        <span class="hud-label">Time (UTC)</span>
                        <span class="tel-value">${new Date(d.timestamp).toLocaleTimeString()}</span>
                    </div>
                    <div class="tel-item accent-blue">
                        <span class="hud-label">Logistics Speed</span>
                        <span class="tel-value">${d.speed_kmh} KM/H</span>
                    </div>
                    <div class="tel-item">
                        <span class="hud-label">Heading Vector</span>
                        <span class="tel-value">${d.heading}°</span>
                    </div>
                    <div class="tel-item">
                        <span class="hud-label">Coords</span>
                        <span class="tel-value">${d.lat.toFixed(4)}, ${d.lon.toFixed(4)}</span>
                    </div>
                    <div class="tel-item highlight-amber">
                        <span class="hud-label">Spectral Conf.</span>
                        <span class="tel-value">${(d.confidence * 100).toFixed(1)}%</span>
                    </div>
                </div>
            </div>
        `;
    }

    async fetchSites() {
        try {
            const resp = await fetch('/api/sites');
            this.sites = await resp.json();
            this.updateMarkers();
        } catch (e) {
            console.error("Failed to fetch sites:", e);
        }
    }

    updateMarkers() {
        Object.values(this.markers).forEach(m => this.map.removeLayer(m));
        this.markers = {};

        const icon = L.divIcon({
            className: 'custom-marker',
            html: '<div class="marker-pin"></div>',
            iconSize: [20, 20],
            iconAnchor: [10, 10]
        });

        this.sites.forEach(site => {
            const marker = L.marker([site.lat, site.lng], { icon })
                .addTo(this.map);

            const popupContent = document.createElement('div');
            popupContent.className = 'marker-popup';
            popupContent.innerHTML = `
                <div class="popup-title">${site.name}</div>
                <div class="popup-meta">${site.country} • ${site.type.toUpperCase()}</div>
                <div class="popup-actions">
                    <button class="btn btn-hud-primary btn-sm analyze-site-btn">Analyze Node</button>
                </div>
            `;

            popupContent.querySelector('.analyze-site-btn').onclick = () => {
                this.handleAOISelection(site.bbox, site.id, site.name);
                marker.closePopup();
            };

            marker.bindPopup(popupContent);
            marker.bindTooltip(`<b>${site.name}</b>`, { direction: 'top' });

            this.markers[site.id] = marker;
        });
    }

    notify(msg, type = 'info') {
        // Console-only for now: the sidebar ticker this used to write to was
        // replaced by the Copernicus status card. A proper toast system is a
        // planned follow-up.
        console.log(`[${type.toUpperCase()}] ${msg}`);
    }

    setupDocs() {
        const DOC_SECTIONS = [
            { id: 'doc-introduction', title: 'Introduction',          group: 'Getting Started' },
            { id: 'doc-quickstart',   title: 'Quick Start',           group: 'Getting Started' },
            { id: 'doc-connect',      title: 'Connecting Copernicus', group: 'Getting Started' },
            { id: 'doc-interface',    title: 'The Interface',         group: 'Using DrishX' },
            { id: 'doc-usecases',     title: 'Use Cases',             group: 'Using DrishX' },
            { id: 'doc-targets',      title: 'Interesting Targets',   group: 'Using DrishX' },
            { id: 'doc-howitworks',   title: 'How It Works',          group: 'Reference' },
            { id: 'doc-api',          title: 'API Reference',         group: 'Reference' },
            { id: 'doc-technical',    title: 'Technical Notes',       group: 'Reference' },
            { id: 'doc-references',   title: 'References & License',   group: 'Reference' },
        ];

        const subnav = document.getElementById('docs-subnav');
        const scroller = document.getElementById('docs-content');
        const view = document.getElementById('docs-view');
        if (!subnav || !scroller || !view) return;

        // Grouped sub-nav (single source: the manifest above)
        const groups = [];
        DOC_SECTIONS.forEach(s => {
            let g = groups.find(x => x.label === s.group);
            if (!g) { g = { label: s.group, items: [] }; groups.push(g); }
            g.items.push(s);
        });
        subnav.innerHTML = groups.map(g => `
            <div class="docs-nav-group">
                <div class="docs-nav-group-label">${g.label}</div>
                ${g.items.map(s => `<a class="docs-nav-link" data-target="${s.id}" href="#${s.id}">${s.title}</a>`).join('')}
            </div>
        `).join('');

        // Smooth-scroll for any in-docs anchor (sub-nav, cards, CTA)
        view.addEventListener('click', (e) => {
            const link = e.target.closest('a[href^="#doc-"]');
            if (!link) return;
            e.preventDefault();
            const target = document.getElementById(link.getAttribute('href').slice(1));
            if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
        });

        // Scroll-spy: highlight the section nearest the top of the scroller
        const setActive = (id) => {
            document.querySelectorAll('.docs-nav-link').forEach(el => {
                el.classList.toggle('active', el.dataset.target === id);
            });
        };
        const sections = DOC_SECTIONS.map(s => document.getElementById(s.id)).filter(Boolean);
        const syncActive = () => {
            // Skip while hidden — a display:none view reports all rects as
            // zero, which would otherwise fall through to the last section.
            if (view.classList.contains('hidden')) return;
            // At the bottom, the last section can't reach the top of the scroller —
            // force it active so short trailing sections still highlight.
            if (scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 4) {
                setActive(sections[sections.length - 1].id);
                return;
            }
            const line = scroller.getBoundingClientRect().top + 120;
            let current = sections[0].id;
            for (const el of sections) {
                if (el.getBoundingClientRect().top <= line) current = el.id;
                else break;
            }
            setActive(current);
        };
        scroller.addEventListener('scroll', syncActive, { passive: true });
        this.syncDocsActive = syncActive;
        syncActive();

        // Tabs (e.g. Technical Notes → Regional accuracy)
        document.querySelectorAll('.docs-tabs').forEach(tabs => {
            const btns = tabs.querySelectorAll('.docs-tab');
            const panels = tabs.querySelectorAll('.docs-tab-panel');
            btns.forEach(btn => btn.addEventListener('click', () => {
                btns.forEach(b => b.classList.remove('active'));
                panels.forEach(p => p.classList.remove('active'));
                btn.classList.add('active');
                const panel = tabs.querySelector(`.docs-tab-panel[data-panel="${btn.dataset.tab}"]`);
                if (panel) panel.classList.add('active');
            }));
        });
    }

    // ── Connect modal (Copernicus onboarding) ──────────────────────────────

    setupConnectModal() {
        const modal = document.getElementById('connect-modal');
        if (!modal) return;

        // Reveal / hide the secret field
        document.querySelectorAll('.reveal-toggle').forEach(btn => {
            btn.addEventListener('click', () => {
                const input = document.getElementById(btn.dataset.reveal);
                if (!input) return;
                const show = input.type === 'password';
                input.type = show ? 'text' : 'password';
                btn.querySelector('i').className = show ? 'fas fa-eye-slash' : 'fas fa-eye';
                btn.setAttribute('aria-label', show ? 'Hide secret' : 'Show secret');
            });
        });

        // State A ⇄ B toggle ("Don't have credentials?" / "Enter my keys")
        document.querySelectorAll('[data-goto-state]').forEach(btn => {
            btn.addEventListener('click', () => this.setConnectState(btn.dataset.gotoState));
        });

        // Close: ✕ button, backdrop click, Esc — but never mid-verification
        document.querySelectorAll('[data-close-connect]').forEach(btn => {
            btn.addEventListener('click', () => this.closeConnectModal());
        });
        modal.addEventListener('click', (e) => {
            if (e.target === modal && !this.connecting) this.closeConnectModal();
        });
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && !modal.classList.contains('hidden') && !this.connecting) {
                this.closeConnectModal();
            }
        });

        // Submit (button + Enter in either field)
        document.getElementById('connect-submit')?.addEventListener('click', () => this.handleConnect());
        ['connect-id', 'connect-secret'].forEach(id => {
            document.getElementById(id)?.addEventListener('keypress', (e) => {
                if (e.key === 'Enter') this.handleConnect();
            });
        });
    }

    setConnectState(state) {
        document.getElementById('connect-state-a')?.classList.toggle('hidden', state !== 'a');
        document.getElementById('connect-state-b')?.classList.toggle('hidden', state !== 'b');
    }

    openConnectModal(state = 'a') {
        const modal = document.getElementById('connect-modal');
        if (!modal) return;
        // Reset any banner left over from a previous session (e.g. a stale
        // success message after the user has since disconnected).
        const banner = document.getElementById('connect-status');
        if (banner) {
            banner.className = 'connect-banner hidden';
            banner.innerHTML = '';
        }
        this.setConnectState(state);
        modal.classList.remove('hidden');
    }

    closeConnectModal() {
        const modal = document.getElementById('connect-modal');
        if (!modal) return;
        modal.classList.add('hidden');
        // Don't re-prompt automatically for the rest of this session.
        sessionStorage.setItem('drishx_connect_dismissed', '1');
    }

    // Boot: ask the backend for the real link state, render the sidebar card, then decide what to show.
    async initAuth() {
        const status = await this.refreshAuthCard();
        if (status.linked) return; // already linked (UI or env)

        // Not linked: try a silent re-link from credentials saved in this browser.
        const id = localStorage.getItem('drishx_copernicus_id');
        const secret = localStorage.getItem('drishx_copernicus_secret');
        if (id && secret) {
            if (await this.silentRelink(id, secret)) { await this.refreshAuthCard(); return; }
            // Stored keys exist but no longer verify → show the error state, prompt reconnect.
            this.renderAuthCard('error');
            return;
        }

        // No stored keys → prompt with the connect modal (once per session).
        if (!sessionStorage.getItem('drishx_connect_dismissed')) {
            this.openConnectModal('a');
        }
    }

    // ── Sidebar Copernicus status card ──────────────────────────────────────

    async refreshAuthCard() {
        let status = { linked: false, source: null, last_verified: null };
        try {
            status = await (await fetch('/api/auth/status')).json();
        } catch (e) {
            console.warn('Auth status check failed (backend unreachable?):', e);
        }
        this.authStatus = status;
        this.renderAuthCard(status.linked ? 'connected' : 'off', status);
        return status;
    }

    renderAuthCard(mode, status = {}) {
        const card = document.getElementById('copernicus-card');
        if (!card) return;
        card.innerHTML = '';

        const header = document.createElement('div');
        header.className = 'cop-header';
        const dot = document.createElement('span');
        const title = document.createElement('span');
        title.className = 'cop-title';
        title.textContent = 'Copernicus Link';

        if (mode === 'connected') {
            dot.className = 'cop-dot cop-dot--connected';
            const unlink = document.createElement('button');
            unlink.className = 'cop-unlink';
            unlink.title = 'Disconnect';
            unlink.setAttribute('aria-label', 'Disconnect');
            unlink.innerHTML = '<i class="fas fa-link-slash"></i>';
            unlink.addEventListener('click', () => this.handleDisconnect());
            header.append(dot, title, unlink);
            const meta = document.createElement('div');
            meta.className = 'cop-meta';
            const rel = this.relTime(status.last_verified);
            meta.innerHTML = '<span class="cop-status--connected">Connected</span>'
                + (rel ? ` · verified ${rel}` : '');
            card.append(header, meta);
        } else if (mode === 'error') {
            dot.className = 'cop-dot cop-dot--error';
            header.append(dot, title);
            const msg = document.createElement('div');
            msg.className = 'cop-msg';
            msg.textContent = "Couldn't verify your saved keys. Reconnect to continue.";
            const btn = document.createElement('button');
            btn.className = 'cop-btn';
            btn.textContent = 'Reconnect';
            btn.addEventListener('click', () => this.openConnectModal('a'));
            card.append(header, msg, btn);
        } else { // 'off' — not connected
            dot.className = 'cop-dot cop-dot--off';
            header.append(dot, title);
            const meta = document.createElement('div');
            meta.className = 'cop-meta cop-status--off';
            meta.textContent = 'Not connected';
            const btn = document.createElement('button');
            btn.className = 'cop-btn';
            btn.textContent = 'Connect';
            btn.addEventListener('click', () => this.openConnectModal('a'));
            card.append(header, meta, btn);
        }
    }

    relTime(iso) {
        if (!iso) return null;
        const then = new Date(iso).getTime();
        if (isNaN(then)) return null;
        const s = Math.max(0, Math.floor((Date.now() - then) / 1000));
        if (s < 60) return 'just now';
        const m = Math.floor(s / 60); if (m < 60) return `${m}m ago`;
        const h = Math.floor(m / 60); if (h < 24) return `${h}h ago`;
        return `${Math.floor(h / 24)}d ago`;
    }

    async handleDisconnect() {
        if (!confirm('Disconnect from Copernicus? Live satellite features stop until you reconnect.')) return;
        try {
            await fetch('/api/auth', { method: 'DELETE' });
        } catch (e) {
            this.notify('Disconnect failed — is the server running?', 'error');
            return;
        }
        localStorage.removeItem('drishx_copernicus_id');
        localStorage.removeItem('drishx_copernicus_secret');
        this.notify('Copernicus disconnected.', 'info');
        this.renderAuthCard('off');
    }

    async silentRelink(id, secret) {
        try {
            const res = await fetch('/api/auth', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ client_id: id, client_secret: secret })
            });
            const data = await res.json();
            return data.status === 'success';
        } catch {
            return false;
        }
    }

    async handleConnect() {
        if (this.connecting) return;
        const idInput = document.getElementById('connect-id');
        const secretInput = document.getElementById('connect-secret');
        const submitBtn = document.getElementById('connect-submit');
        const id = idInput.value.trim();
        const secret = secretInput.value.trim();

        // Basic validation + input intelligence (catch mistakes before the round-trip)
        if (!id || !secret) {
            this.showConnectBanner('error', 'Enter both your Client ID and Client Secret.');
            return;
        }
        if (id.includes('@')) {
            this.showConnectBanner('error', 'That looks like your account email — you need the OAuth Client ID from the dashboard.');
            return;
        }

        this.connecting = true;
        submitBtn.disabled = true;
        submitBtn.textContent = 'CONNECTING…';
        this.showConnectBanner('connecting', 'Verifying with Copernicus…');

        try {
            const res = await fetch('/api/auth', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ client_id: id, client_secret: secret })
            });
            const data = await res.json();

            if (data.status === 'success') {
                this.showConnectBanner('success', 'Linked. Satellite access is live.');
                localStorage.setItem('drishx_copernicus_id', id);
                localStorage.setItem('drishx_copernicus_secret', secret);
                this.notify('Copernicus link active.', 'success');
                this.refreshAuthCard(); // reflect the new linked state in the sidebar
                setTimeout(() => this.closeConnectModal(), 1200);
            } else {
                this.showConnectBanner('error', this.connectErrorCopy(data));
            }
        } catch (err) {
            // Request never reached / parsed the API — the server-unreachable case.
            console.error('Connect failed:', err);
            this.showConnectBanner('error', "Can't reach the DrishX server — is it still running?");
        } finally {
            this.connecting = false;
            submitBtn.disabled = false;
            submitBtn.textContent = 'CONNECT';
        }
    }

    // Frontend owns the display copy; branch on the backend's machine-readable code.
    connectErrorCopy(data) {
        switch (data.code) {
            case 'invalid_credentials':
                return 'Copernicus rejected these keys. Re-copy them, or create a new OAuth client.';
            case 'cdse_unreachable':
                return "Copernicus isn't responding — your keys may be fine. Try again in a few minutes.";
            case 'cdse_error':
                return data.message || 'Copernicus returned an unexpected error.';
            default:
                return data.message || 'Could not establish the link.';
        }
    }

    showConnectBanner(type, message) {
        const el = document.getElementById('connect-status');
        if (!el) return;
        const icons = { connecting: 'fa-spinner fa-spin', success: 'fa-circle-check', error: 'fa-circle-xmark' };
        el.className = 'connect-banner connect-banner--' + type;
        el.innerHTML = '';
        const i = document.createElement('i');
        i.className = 'fas ' + (icons[type] || 'fa-circle-info');
        const span = document.createElement('span');
        span.textContent = message; // textContent → no injection from server messages
        el.append(i, span);
        el.classList.remove('hidden');
    }
}

window.addEventListener('DOMContentLoaded', () => {
    window.dashboard = new DrishXDashboard();
});
