/* ── Config & state ──────────────────────────────────────────────── */
const defaultConfig = { noct: 45, gamma: -0.004, inverter_max_w: 4000, dod: 0.9, eta_c: 0.95, eta_d: 0.95 };
let map, siteMarker, windMarker;
const charts = {};
let panelGroupCount = 0;
let sunTimer = null;
let currentWeatherData = null;

/* ── Utilities ───────────────────────────────────────────────────── */
function value(id) { return Number(document.getElementById(id).value); }
function getLat() { return value("lat"); }
function getLon() { return value("lon"); }

/* ── Panel groups ────────────────────────────────────────────────── */
function panelGroupFromElement(el) {
  return {
    count:          Number(el.querySelector("[data-field='count']").value),
    watt_peak:      Number(el.querySelector("[data-field='watt_peak']").value),
    tilt:           Number(el.querySelector("[data-field='tilt']").value),
    azimuth:        Number(el.querySelector("[data-field='azimuth']").value),
    noct:           defaultConfig.noct,
    gamma:          defaultConfig.gamma,
    inverter_max_w: Number(el.querySelector("[data-field='inverter_max_w']").value),
  };
}

function addPanelGroup(cfg = { count: 10, watt_peak: 400, tilt: 35, azimuth: 180, inverter_max_w: 4000 }) {
  panelGroupCount += 1;
  const g = document.createElement("div");
  g.className = "panel-group";
  g.innerHTML = `
    <div class="panel-group-head">
      <strong>Group ${panelGroupCount}</strong>
      <button type="button" class="remove-group" aria-label="Remove">×</button>
    </div>
    <div class="field-grid three">
      <label>Panels<input data-field="count" type="number" min="1" value="${cfg.count}"></label>
      <label>Watt peak<input data-field="watt_peak" type="number" min="1" value="${cfg.watt_peak}"><small>W</small></label>
      <label>Tilt<input data-field="tilt" type="number" min="0" max="90" value="${cfg.tilt}"><small>degrees</small></label>
    </div>
    <div class="field-grid two">
      <label>Azimuth<input data-field="azimuth" type="number" min="0" max="360" value="${cfg.azimuth}"><small>south = 180</small></label>
      <label>Inverter<input data-field="inverter_max_w" type="number" min="1" value="${cfg.inverter_max_w}"><small>W max</small></label>
    </div>`;
  g.querySelector(".remove-group").addEventListener("click", () => {
    if (document.querySelectorAll(".panel-group").length > 1) g.remove();
  });
  document.getElementById("panel-groups").appendChild(g);
}

/* ── Location ────────────────────────────────────────────────────── */
function setLocation(lat, lon) {
  document.getElementById("lat").value = lat.toFixed(4);
  document.getElementById("lon").value = lon.toFixed(4);
  document.getElementById("hud-lat").textContent = `${Math.abs(lat).toFixed(4)}°${lat >= 0 ? "N" : "S"}`;
  document.getElementById("hud-lon").textContent = `${Math.abs(lon).toFixed(4)}°${lon >= 0 ? "E" : "W"}`;
  updateSunNow(lat, lon);
  fetchCurrentWeather(lat, lon);
}

/* ── Forecast payload ────────────────────────────────────────────── */
function payload() {
  return {
    lat: getLat(), lon: getLon(),
    panels: [...document.querySelectorAll(".panel-group")].map(panelGroupFromElement),
    battery: {
      capacity_kwh: value("battery-capacity"),
      dod: defaultConfig.dod,
      eta_c: defaultConfig.eta_c,
      eta_d: defaultConfig.eta_d,
      max_power_kw: value("battery-power"),
      initial_soc_kwh: value("initial-soc"),
    },
    load: { daily_kwh: value("daily-load") },
    days: value("days"),
  };
}

/* ── Sun position (simplified NOAA solar algorithm) ─────────────── */
function calcSunPosition(lat, lon, date) {
  const d = date || new Date();
  const rad = Math.PI / 180;
  const jd = d.getTime() / 86400000 + 2440587.5;
  const n = jd - 2451545.0;
  const L = ((280.46 + 0.9856474 * n) % 360 + 360) % 360;
  const g = ((357.528 + 0.9856003 * n) % 360 + 360) % 360;
  const lambda = L + 1.915 * Math.sin(g * rad) + 0.02 * Math.sin(2 * g * rad);
  const epsilon = 23.439 - 0.0000004 * n;
  const sinDec = Math.sin(epsilon * rad) * Math.sin(lambda * rad);
  const dec = Math.asin(Math.max(-1, Math.min(1, sinDec))) / rad;
  const ra = Math.atan2(Math.cos(epsilon * rad) * Math.sin(lambda * rad), Math.cos(lambda * rad)) / rad;
  const gmst = (6.697375 + 0.0657098242 * n + d.getUTCHours() + d.getUTCMinutes() / 60 + d.getUTCSeconds() / 3600) % 24;
  const lmst = ((gmst + lon / 15) % 24 + 24) % 24;
  const ha = (lmst - ((ra / 15 + 24) % 24) + 12 + 24) % 24 - 12;
  const sinAlt = Math.sin(lat * rad) * Math.sin(dec * rad) + Math.cos(lat * rad) * Math.cos(dec * rad) * Math.cos(ha * 15 * rad);
  const altitude = Math.asin(Math.max(-1, Math.min(1, sinAlt))) / rad;
  const cosAz = (Math.sin(dec * rad) - Math.sin(altitude * rad) * Math.sin(lat * rad)) / (Math.cos(altitude * rad) * Math.cos(lat * rad) + 1e-10);
  let azimuth = Math.acos(Math.max(-1, Math.min(1, cosAz))) / rad;
  if (ha > 0) azimuth = 360 - azimuth;
  return { altitude, azimuth };
}

function drawSunCompass(canvas, altitude, azimuth) {
  const ctx = canvas.getContext("2d");
  const w = canvas.width, h = canvas.height, cx = w / 2, cy = h / 2;
  const r = Math.min(cx, cy) - 5;
  const dark = document.body.classList.contains("dark-theme");
  const ringColor  = dark ? "rgba(255,255,255,.15)" : "rgba(20,40,55,.12)";
  const labelColor = dark ? "rgba(200,220,235,.5)"  : "rgba(20,40,55,.45)";

  ctx.clearRect(0, 0, w, h);

  // Outer ring
  ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2);
  ctx.strokeStyle = ringColor; ctx.lineWidth = 1; ctx.stroke();

  // Horizon ring (altitude = 0 → at r distance from center)
  ctx.beginPath(); ctx.arc(cx, cy, r * 0.5, 0, Math.PI * 2);
  ctx.strokeStyle = dark ? "rgba(100,160,200,.2)" : "rgba(20,80,140,.1)";
  ctx.lineWidth = 1; ctx.setLineDash([2, 3]); ctx.stroke(); ctx.setLineDash([]);

  // Cardinals
  ctx.fillStyle = labelColor;
  ctx.font = `bold 7px "DM Mono", monospace`;
  ctx.textAlign = "center"; ctx.textBaseline = "middle";
  ctx.fillText("N", cx, cy - r + 7);
  ctx.fillText("S", cx, cy + r - 7);
  ctx.fillText("E", cx + r - 7, cy);
  ctx.fillText("W", cx - r + 7, cy);

  if (altitude > -8) {
    const az = azimuth * Math.PI / 180;
    // altitude 90° → center, 0° → r*0.5 (horizon ring), below → beyond
    const dist = r * (1 - Math.max(0, altitude) / 90);
    const sx = cx + dist * Math.sin(az);
    const sy = cy - dist * Math.cos(az);

    if (altitude > 0) {
      // Glow
      const grd = ctx.createRadialGradient(sx, sy, 0, sx, sy, 13);
      grd.addColorStop(0, "rgba(255,200,30,.85)");
      grd.addColorStop(1, "rgba(255,150,0,0)");
      ctx.fillStyle = grd;
      ctx.beginPath(); ctx.arc(sx, sy, 13, 0, Math.PI * 2); ctx.fill();
      ctx.fillStyle = "#FFC020";
      ctx.beginPath(); ctx.arc(sx, sy, 4.5, 0, Math.PI * 2); ctx.fill();
    } else {
      // Below horizon — show dim crescent at last azimuth
      ctx.fillStyle = dark ? "rgba(90,130,180,.55)" : "rgba(60,100,160,.4)";
      ctx.beginPath(); ctx.arc(sx, sy, 3, 0, Math.PI * 2); ctx.fill();
    }
  } else {
    ctx.fillStyle = dark ? "rgba(100,150,200,.45)" : "rgba(60,100,160,.35)";
    ctx.font = `6px "DM Mono", monospace`;
    ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillText("NIGHT", cx, cy);
  }
}

function updateSunNow(lat, lon) {
  const { altitude, azimuth } = calcSunPosition(lat || getLat(), lon || getLon());
  const canvas = document.getElementById("sun-canvas");
  if (canvas) drawSunCompass(canvas, altitude, azimuth);
  const altEl = document.getElementById("sun-alt");
  const azEl  = document.getElementById("sun-az");
  if (altEl) altEl.textContent = `${altitude.toFixed(1)}°`;
  if (azEl)  azEl.textContent  = `${azimuth.toFixed(0)}°`;
}

/* ── Wind arrow (small canvas) ───────────────────────────────────── */
function drawWindArrow(canvas, direction) {
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  const cx = canvas.width / 2, cy = canvas.height / 2;
  const r = Math.min(cx, cy) - 1;
  const dark = document.body.classList.contains("dark-theme");
  const color = dark ? "#6aabff" : "#1478e8";

  ctx.save();
  ctx.translate(cx, cy);
  ctx.rotate(((direction + 180) % 360) * Math.PI / 180);
  ctx.strokeStyle = color; ctx.fillStyle = color; ctx.lineWidth = 1.5;
  ctx.beginPath(); ctx.moveTo(0, r - 1); ctx.lineTo(0, -(r - 5)); ctx.stroke();
  ctx.beginPath(); ctx.moveTo(0, -(r - 5)); ctx.lineTo(-3.5, -(r - 11)); ctx.lineTo(3.5, -(r - 11)); ctx.closePath(); ctx.fill();
  ctx.restore();
}

/* ── Current weather: Open-Meteo, no API key needed ─────────────── */
async function fetchCurrentWeather(lat, lon) {
  try {
    const url = `https://api.open-meteo.com/v1/forecast?latitude=${lat.toFixed(4)}&longitude=${lon.toFixed(4)}&current=temperature_2m,cloud_cover,wind_speed_10m,wind_direction_10m&timezone=auto&forecast_days=1`;
    const res = await fetch(url);
    if (!res.ok) return;
    const data = await res.json();
    if (!data.current) return;
    currentWeatherData = data.current;
    applyWeatherOverlays(data.current, lat, lon);
  } catch (_) { /* silent — overlays stay neutral */ }
}

function applyWeatherOverlays(cur, lat, lon) {
  const cloud = cur.cloud_cover ?? 0;
  const windSpeed = cur.wind_speed_10m ?? 0;
  const windDir   = cur.wind_direction_10m ?? 0;
  const temp      = cur.temperature_2m ?? "—";

  document.getElementById("chip-cloud-val").textContent = `${cloud}%`;
  document.getElementById("chip-wind-val").textContent  = `${windSpeed} m/s`;
  document.getElementById("chip-temp-val").textContent  = `${temp}°C`;

  drawWindArrow(document.getElementById("wind-canvas"), windDir);

  // Cloud tint: up to 30% opacity overlay on map
  const overlay = document.getElementById("cloud-overlay");
  if (overlay) overlay.style.background = `rgba(170,195,220,${(cloud / 100) * 0.30})`;

  // Animated wind arrow marker on map
  if (map) placeWindMarker(lat ?? getLat(), lon ?? getLon(), windSpeed, windDir);
}

function placeWindMarker(lat, lon, speed, direction) {
  if (windMarker) { windMarker.remove(); windMarker = null; }
  if (speed < 0.3) return;
  const dark = document.body.classList.contains("dark-theme");
  const color = dark ? "#6aabff" : "#1478e8";
  const arrow = `<div style="transform:rotate(${(direction + 180) % 360}deg);width:40px;height:40px;display:flex;align-items:center;justify-content:center;color:${color};font-size:26px;filter:drop-shadow(0 2px 6px rgba(0,0,0,.4));pointer-events:none;">↑</div>`;
  const icon = L.divIcon({ html: arrow, iconSize: [40, 40], iconAnchor: [20, 20], className: "" });
  windMarker = L.marker([lat, lon], { icon, interactive: false, zIndexOffset: -100 }).addTo(map);
}

/* ── Charts ──────────────────────────────────────────────────────── */
function destroyChart(name) {
  if (charts[name]) { charts[name].destroy(); delete charts[name]; }
}

function drawCharts(data) {
  const dark = document.body.classList.contains("dark-theme");
  const gridColor = dark ? "#22323c" : "#edf0f1";
  const tickColor = dark ? "#8fa0ac" : "#77818a";
  const labels = data.hourly.map((r) => new Date(r.time).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }));
  const opts = { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { x: { grid: { display: false }, ticks: { maxTicksLimit: 10, color: tickColor } }, y: { grid: { color: gridColor }, ticks: { color: tickColor } } } };

  destroyChart("hourly");
  charts.hourly = new Chart(document.getElementById("hourly-chart"), {
    type: "line",
    data: { labels, datasets: [
      { label: "Solar", data: data.hourly.map((r) => r.p_ac_w), borderColor: "#1478e8", backgroundColor: "rgba(20,120,232,.1)", fill: true, tension: .35, pointRadius: 0 },
      { label: "Load",  data: data.hourly.map((r) => r.load_w),  borderColor: "#f2a43a", borderDash: [5,5], tension: .35, pointRadius: 0 },
    ]}, options: opts,
  });
  destroyChart("daily");
  charts.daily = new Chart(document.getElementById("daily-chart"), {
    type: "bar",
    data: { labels: data.daily.map((r) => r.date), datasets: [{ data: data.daily.map((r) => r.kwh), backgroundColor: "#1478e8", borderRadius: 0 }] },
    options: opts,
  });
  destroyChart("soc");
  charts.soc = new Chart(document.getElementById("soc-chart"), {
    type: "line",
    data: { labels, datasets: [{ data: data.hourly.map((r) => r.soc_kwh), borderColor: dark ? "#6aabff" : "#23343e", backgroundColor: dark ? "rgba(106,171,255,.08)" : "rgba(35,52,62,.07)", fill: true, tension: .25, pointRadius: 0 }] },
    options: opts,
  });
}

/* ── Recommendations ─────────────────────────────────────────────── */
function renderRecommendations(items) {
  const list = document.getElementById("recommendation-list");
  list.innerHTML = items.length
    ? items.map((item) => `<article class="recommendation"><div class="recommendation-meta"><span class="badge">${item.subtopic}</span><span>${new Date(item.hour).toLocaleTimeString([], { hour:"2-digit", minute:"2-digit" })}</span></div><h3>${item.title}</h3><p>${item.reason}</p></article>`).join("")
    : '<div class="empty-state">No recommendations for this forecast.</div>';
}

function updateStatus(data) {
  document.getElementById("weather-status").textContent      = data.meta.data_sources.join(" / ").toUpperCase();
  document.getElementById("calibration-status").textContent  = data.meta.calibrated ? "CALIBRATED" : "NOT CALIBRATED";
  document.getElementById("result-badge").textContent        = "SIMULATED FORECAST";
  document.getElementById("last-run").textContent            = new Date().toLocaleTimeString([], { hour:"2-digit", minute:"2-digit" });
}

/* ── Error banner ────────────────────────────────────────────────── */
function showError(msg) { const b = document.getElementById("error-banner"); b.textContent = msg; b.hidden = false; }
function clearError()   { document.getElementById("error-banner").hidden = true; }

/* ── Forecast ────────────────────────────────────────────────────── */
async function runForecast(event) {
  event.preventDefault();
  clearError();
  const btn = document.getElementById("forecast-button");
  btn.disabled = true;
  btn.querySelector("span").textContent = "Calculating…";
  try {
    const res  = await fetch("/api/forecast", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload()) });
    const data = await res.json();
    if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : data.detail?.[0]?.msg || "Forecast request failed");
    drawCharts(data);
    renderRecommendations(data.recommendations);
    updateStatus(data);
    const hint = document.getElementById("scroll-hint");
    hint.hidden = false;
    document.getElementById("scroll-to-results").onclick = () =>
      document.querySelector(".results-wrap").scrollIntoView({ behavior: "smooth" });
  } catch (err) {
    showError(err.message || "Could not run forecast.");
  } finally {
    btn.disabled = false;
    btn.querySelector("span").textContent = "Run energy forecast";
  }
}

/* ── Map ─────────────────────────────────────────────────────────── */
function initMap() {
  map = L.map("map", { zoomControl: false }).setView([42.6977, 23.3219], 12);
  L.control.zoom({ position: "bottomright" }).addTo(map);
  const cartoKey = window.SOLARSIGHT_CONFIG?.cartoApiKey;
  const tileUrl = cartoKey
    ? `https://basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png?key=${encodeURIComponent(cartoKey)}`
    : "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png";
  L.tileLayer(tileUrl, { attribution: "&copy; OpenStreetMap &copy; CARTO", maxZoom: 20 }).addTo(map);
  siteMarker = L.marker([42.6977, 23.3219]).addTo(map);
  map.on("click", (e) => { siteMarker.setLatLng(e.latlng); setLocation(e.latlng.lat, e.latlng.lng); });
  setTimeout(() => map.invalidateSize(), 100);
  fetchCurrentWeather(42.6977, 23.3219);
}

/* ── View: determined by URL path ────────────────────────────────── */
function initView() {
  const isCalc = document.body.dataset.view === "calculator";
  document.getElementById("landing-wrap").hidden    = isCalc;
  document.getElementById("calculator-view").hidden = !isCalc;
  document.querySelectorAll(".view-tab").forEach((a) => {
    a.classList.toggle("is-active",
      (a.getAttribute("href") === "/" && !isCalc) ||
      (a.getAttribute("href") === "/calculator" && isCalc)
    );
  });
  if (isCalc) {
    initMap();
    updateSunNow(getLat(), getLon());
    sunTimer = setInterval(() => updateSunNow(getLat(), getLon()), 60000);
  }
}

/* ── Horizon label ───────────────────────────────────────────────── */
function updateHorizonLabel() {
  const map_ = { 1: "1 DAY", 3: "3 DAYS", 7: "7 DAYS", 30: "30 DAYS" };
  document.getElementById("horizon-label").textContent = map_[value("days")] || `${value("days")} DAYS`;
}

/* ── Theme ───────────────────────────────────────────────────────── */
function toggleTheme() {
  document.body.classList.toggle("dark-theme");
  localStorage.setItem("solarsight-theme", document.body.classList.contains("dark-theme") ? "dark" : "light");
  if (map) map.invalidateSize();
  updateSunNow(getLat(), getLon());
  if (currentWeatherData) {
    drawWindArrow(document.getElementById("wind-canvas"), currentWeatherData.wind_direction_10m ?? 0);
    placeWindMarker(getLat(), getLon(), currentWeatherData.wind_speed_10m ?? 0, currentWeatherData.wind_direction_10m ?? 0);
  }
}

/* ── Boot ────────────────────────────────────────────────────────── */
document.addEventListener("DOMContentLoaded", () => {
  addPanelGroup();
  if (localStorage.getItem("solarsight-theme") === "dark") document.body.classList.add("dark-theme");

  initView();

  const form = document.getElementById("forecast-form");
  if (form) form.addEventListener("submit", runForecast);
  const addBtn = document.getElementById("add-panel-group");
  if (addBtn) addBtn.addEventListener("click", () => addPanelGroup());
  document.getElementById("theme-toggle").addEventListener("click", toggleTheme);

  const daysEl = document.getElementById("days");
  if (daysEl) daysEl.addEventListener("change", updateHorizonLabel);

  // Manual lat/lon input re-syncs map + overlays
  ["lat", "lon"].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.addEventListener("change", () => {
      const lat = getLat(), lon = getLon();
      if (siteMarker) siteMarker.setLatLng([lat, lon]);
      if (map) map.panTo([lat, lon]);
      setLocation(lat, lon);
    });
  });
});
