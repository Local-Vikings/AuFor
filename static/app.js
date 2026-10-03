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
  const line = document.getElementById("tl-cloud");
  if (cloudField && line) line.textContent = siteLine(timeIndex);
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
    resolution: document.getElementById("resolution").value,
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

/* ── Current weather: through the server, which caches Open-Meteo ─ */
async function fetchCurrentWeather(lat, lon) {
  try {
    const res = await fetch(`/api/current-weather?lat=${lat.toFixed(4)}&lon=${lon.toFixed(4)}`);
    if (!res.ok) return;
    const current = await res.json();
    currentWeatherData = current;
    applyWeatherOverlays(current, lat, lon);
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
  if (overlay) overlay.style.background = cloudField ? "transparent" : `rgba(170,195,220,${(cloud / 100) * 0.30})`;

  // Single wind arrow at the site, only until the wind field layer is available
  if (map && !cloudField) placeWindMarker(lat ?? getLat(), lon ?? getLon(), windSpeed, windDir);
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

const fmt = (value, digits = 0) => Number(value).toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: digits });

function renderImpact(data) {
  const monthly = data.monthly.length > 0;
  const rows = monthly ? data.monthly : data.daily;
  const energy = rows.reduce((sum, r) => sum + r.kwh, 0);
  const clear = rows.reduce((sum, r) => sum + r.clear_sky_kwh, 0);
  const cloud = clear > 0 ? rows.reduce((sum, r) => sum + r.avg_cloud_cover * r.clear_sky_kwh, 0) / clear : 0;
  const lostKwh = Math.max(clear - energy, 0);
  const best = rows.reduce((top, r) => (r.kwh > top.kwh ? r : top), rows[0]);
  document.getElementById("kpi-energy").textContent = `${fmt(energy, 1)} kWh`;
  document.getElementById("kpi-energy-sub").textContent = `${fmt(clear, 1)} kWh with a clear sky`;
  document.getElementById("kpi-cloud").textContent = `${fmt(cloud)}%`;
  document.getElementById("kpi-cloud-sub").textContent = cloud < 20 ? "mostly clear" : cloud < 60 ? "partly cloudy" : "mostly cloudy";
  document.getElementById("kpi-loss").textContent = `${fmt(clear > 0 ? (lostKwh / clear) * 100 : 0)}%`;
  document.getElementById("kpi-loss-sub").textContent = `${fmt(lostKwh, 1)} kWh lost to clouds`;
  document.getElementById("kpi-best-label").textContent = monthly ? "BEST MONTH" : "BEST DAY";
  document.getElementById("kpi-best").textContent = `${fmt(best.kwh, 1)} kWh`;
  document.getElementById("kpi-best-sub").textContent = monthly ? best.month : best.date;
}

function drawCharts(data) {
  const dark = document.body.classList.contains("dark-theme");
  const gridColor = dark ? "#22323c" : "#edf0f1";
  const tickColor = dark ? "#8fa0ac" : "#77818a";
  const blue = "#1478e8", orange = "#f2a43a", grey = dark ? "#5b6b76" : "#b4bec5";
  const baseScales = { x: { grid: { display: false }, ticks: { maxTicksLimit: 10, color: tickColor } }, y: { grid: { color: gridColor }, ticks: { color: tickColor } } };
  const opts = { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: baseScales };
  const legendOpts = { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: true, labels: { color: tickColor, boxWidth: 8, boxHeight: 8, font: { size: 10 } } } }, scales: baseScales };
  const percentAxis = { position: "right", min: 0, max: 100, grid: { drawOnChartArea: false }, ticks: { color: tickColor, callback: (v) => `${v}%` } };

  renderImpact(data);

  const hasHourly = data.hourly.length > 0;
  document.getElementById("hourly-card").hidden = !hasHourly;
  document.getElementById("soc-card").hidden = !hasHourly;
  document.querySelector(".chart-grid").classList.toggle("single", !hasHourly);

  destroyChart("hourly"); destroyChart("soc"); destroyChart("cloud");
  const monthly = data.monthly.length > 0;
  const rows = monthly ? data.monthly : data.daily;

  if (hasHourly) {
    const multiDay = data.hourly.length > 24;
    const labels = data.hourly.map((r) => new Date(r.time).toLocaleString([], multiDay ? { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" } : { hour: "2-digit", minute: "2-digit" }));
    charts.hourly = new Chart(document.getElementById("hourly-chart"), {
      type: "line",
      data: { labels, datasets: [
        { label: "Solar", data: data.hourly.map((r) => r.p_ac_w), borderColor: blue, backgroundColor: "rgba(20,120,232,.1)", fill: true, tension: .35, pointRadius: 0 },
        { label: "Load",  data: data.hourly.map((r) => r.load_w),  borderColor: orange, borderDash: [5,5], tension: .35, pointRadius: 0 },
      ]}, options: opts,
    });
    charts.soc = new Chart(document.getElementById("soc-chart"), {
      type: "line",
      data: { labels, datasets: [{ data: data.hourly.map((r) => r.soc_kwh), borderColor: dark ? "#6aabff" : "#23343e", backgroundColor: dark ? "rgba(106,171,255,.08)" : "rgba(35,52,62,.07)", fill: true, tension: .25, pointRadius: 0 }] },
      options: opts,
    });
    document.getElementById("cloud-title").textContent = "Cloud forecast and sunshine";
    document.getElementById("cloud-unit").textContent = "CLOUD % / SUN W/M²";
    charts.cloud = new Chart(document.getElementById("cloud-chart"), {
      type: "bar",
      data: { labels, datasets: [
        { type: "bar", label: "Cloud cover %", data: data.hourly.map((r) => r.cloud_cover), backgroundColor: dark ? "rgba(143,160,172,.28)" : "rgba(143,160,172,.32)", borderWidth: 0, yAxisID: "y1", order: 3 },
        { type: "line", label: "Clear-sky sun", data: data.hourly.map((r) => r.ghi_clear), borderColor: grey, borderDash: [5,5], pointRadius: 0, tension: .35, order: 2 },
        { type: "line", label: "Forecast sun", data: data.hourly.map((r) => r.ghi), borderColor: orange, backgroundColor: "rgba(242,164,58,.15)", fill: true, pointRadius: 0, tension: .35, order: 1 },
      ]}, options: { ...legendOpts, scales: { ...baseScales, y1: percentAxis } },
    });
  } else {
    document.getElementById("cloud-title").textContent = monthly ? "Cloud cover and losses by month" : "Cloud cover and losses by day";
    document.getElementById("cloud-unit").textContent = "CLOUD % / LOST TO CLOUDS %";
    const labels = rows.map((r) => (monthly ? r.month : r.date));
    charts.cloud = new Chart(document.getElementById("cloud-chart"), {
      type: "bar",
      data: { labels, datasets: [
        { type: "bar", label: "Average cloud cover %", data: rows.map((r) => r.avg_cloud_cover), backgroundColor: dark ? "rgba(143,160,172,.4)" : "rgba(143,160,172,.45)", borderWidth: 0, order: 2 },
        { type: "line", label: "Energy lost to clouds %", data: rows.map((r) => r.cloud_loss_pct), borderColor: orange, pointRadius: rows.length > 40 ? 0 : 3, tension: .25, order: 1 },
      ]}, options: { ...legendOpts, scales: { ...baseScales, y: { ...baseScales.y, min: 0, max: 100, ticks: { color: tickColor, callback: (v) => `${v}%` } } } },
    });
  }

  document.getElementById("daily-title").textContent = monthly ? "Monthly yield vs clear sky" : "Daily yield vs clear sky";
  destroyChart("daily");
  charts.daily = new Chart(document.getElementById("daily-chart"), {
    type: "bar",
    data: { labels: rows.map((r) => (monthly ? r.month : r.date)), datasets: [
      { label: "Forecast kWh", data: rows.map((r) => r.kwh), backgroundColor: blue, borderRadius: 0 },
      { label: "Clear-sky kWh", data: rows.map((r) => r.clear_sky_kwh), backgroundColor: grey, borderRadius: 0 },
    ]},
    options: legendOpts,
  });
  wireChartTime(data);
}

/* ── Recommendations ─────────────────────────────────────────────── */
function renderRecommendations(items) {
  const list = document.getElementById("recommendation-list");
  if (!items.length) {
    list.innerHTML = '<div class="empty-state">No recommendations for this forecast.</div>';
    return;
  }
  list.innerHTML = items.map((item) => {
    const yearly = item.subtopic === "optimize";
    const when = yearly ? "PER YEAR" : new Date(item.hour).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    const sign = item.kwh_effect > 0 ? "+" : "";
    const effect = `<div class="recommendation-effect"><small>KWH EFFECT${yearly ? " (CLEAR-SKY MAX)" : ""}</small><strong>${sign}${item.kwh_effect} kWh</strong></div>`;
    return `<article class="recommendation"><div class="recommendation-meta"><span class="badge badge-${item.subtopic}">${item.subtopic}</span><span>${when}</span></div><h3>${item.title}</h3><p>${item.reason}</p>${effect}</article>`;
  }).join("");
}

function calibrationLabel(meta) {
  const pr = Number(meta.pr_used).toFixed(2);
  if (!meta.calibrated) return `NOT CALIBRATED · PR ${pr}`;
  return `CALIBRATED · PR ${pr}${meta.calibration_source === "simulated" ? " · SIMULATED DATA" : ""}`;
}

function weatherStatus(sources) {
  if (sources.includes("mock weather")) return "MOCK (SIMULATED)";
  const cached = sources.find((x) => x.startsWith("open-meteo (cached"));
  if (cached) return `CACHED ${cached.match(/[\d.]+ h/)[0].toUpperCase()} AGO (OPEN-METEO DOWN)`;
  if (sources.some((x) => x.startsWith("climatology"))) return "LIVE + CLIMATE AVERAGE";
  return "LIVE (OPEN-METEO)";
}

function readingsLabel(summary) {
  if (!summary || summary.total === 0) return "NONE CONNECTED";
  if (summary.real === 0) return `SIMULATED (${summary.simulated})`;
  return summary.simulated === 0 ? `REAL (${summary.real})` : `REAL (${summary.real}) + SIMULATED (${summary.simulated})`;
}

async function refreshReadingsChip() {
  const chip = document.getElementById("readings-status");
  if (!chip) return;
  try {
    const response = await fetch("/api/readings/summary");
    chip.textContent = response.ok ? readingsLabel(await response.json()) : "UNAVAILABLE";
  } catch (error) {
    chip.textContent = "UNAVAILABLE";
  }
}

/* ── Sky camera: the latest measurement from the Pi ──────────────── */
const CAMERA_FRESH_MINUTES = 180;
let cameraTimer = null;

function cameraLabel(row, nowMs = Date.now()) {
  if (!row) return null;
  const minutes = Math.max(0, Math.round((nowMs - Date.parse(row.timestamp)) / 60000));
  if (minutes > CAMERA_FRESH_MINUTES) return null;
  const age = minutes < 1 ? "just now" : minutes < 60 ? `${minutes} min ago` : `${Math.round(minutes / 60)} h ago`;
  return `${Math.round(row.value * 100)}% cloud · ${age}`;
}

async function refreshCameraChip() {
  const chip = document.getElementById("chip-camera");
  if (!chip) return;
  try {
    const response = await fetch("/api/readings?type=cloud_fraction&source=camera&hours=3");
    const rows = response.ok ? await response.json() : [];
    const label = cameraLabel(rows[rows.length - 1]);
    chip.hidden = !label;
    if (label) document.getElementById("chip-camera-val").textContent = label;
  } catch (error) {
    chip.hidden = true;
  }
}

/* ── Live readings vs forecast ───────────────────────────────────── */
const READINGS_POLL_MS = 10000;
const READINGS_HOURS = 48;
let readingsChart = null, readingsTimer = null, lastForecast = null;

function readingsTag(rows) {
  const real = rows.filter((r) => r.source !== "simulated").length;
  const simulated = rows.length - real;
  const noun = (n) => `${n} reading${n === 1 ? "" : "s"}`;
  if (!rows.length) return { text: "NO READINGS", simulated: false };
  if (!real) return { text: `SIMULATED · ${noun(simulated)}`, simulated: true };
  return { text: simulated ? `REAL · ${real} + SIMULATED · ${simulated}` : `REAL · ${noun(real)}`, simulated: simulated > 0 };
}

function readingsDatasets(rows, forecast) {
  const point = (r) => ({ x: Date.parse(r.timestamp), y: r.value });
  const datasets = [];
  if (forecast && forecast.hourly && forecast.hourly.length) {
    datasets.push({ label: "Forecast power", type: "line", data: forecast.hourly.map((h) => ({ x: Date.parse(h.time), y: h.p_ac_w })), borderColor: "#1478e8", backgroundColor: "rgba(20,120,232,.08)", fill: true, pointRadius: 0, tension: .35, borderWidth: 2, order: 2 });
  }
  const simulated = rows.filter((r) => r.source === "simulated").map(point);
  const real = rows.filter((r) => r.source !== "simulated").map(point);
  if (simulated.length) datasets.push({ label: "Simulated readings", type: "scatter", data: simulated, backgroundColor: "rgba(242,164,58,.75)", borderColor: "#f2a43a", pointRadius: 3, order: 1 });
  if (real.length) datasets.push({ label: "Measured readings", type: "scatter", data: real, backgroundColor: "#1a9e5c", borderColor: "#1a9e5c", pointRadius: 3.5, order: 0 });
  return datasets;
}

async function refreshReadingsChart() {
  const canvas = document.getElementById("readings-chart");
  if (!canvas) return;
  let rows = [];
  try {
    const response = await fetch(`/api/readings?type=power_w&hours=${READINGS_HOURS}`);
    if (response.ok) rows = await response.json();
  } catch (error) { return; }  // keep what is on screen; the next poll tries again
  const tag = readingsTag(rows);
  const badge = document.getElementById("readings-tag");
  badge.textContent = tag.text;
  badge.classList.toggle("tag-sim", tag.simulated);
  document.getElementById("readings-empty").hidden = rows.length > 0;
  canvas.hidden = rows.length === 0;
  if (readingsChart) { readingsChart.destroy(); readingsChart = null; }
  if (!rows.length) return;
  const dark = document.body.classList.contains("dark-theme");
  const grid = dark ? "#22323c" : "#edf0f1", tick = dark ? "#8fa0ac" : "#77818a";
  const clock = (value) => new Date(value).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  readingsChart = new Chart(canvas, {
    type: "scatter",
    data: { datasets: readingsDatasets(rows, lastForecast) },
    options: { responsive: true, maintainAspectRatio: false, animation: false, interaction: { mode: "nearest", intersect: false },
      plugins: { legend: { display: true, labels: { color: tick, boxWidth: 8, boxHeight: 8, font: { size: 10 } } }, tooltip: { callbacks: { title: (items) => clock(items[0].parsed.x) } } },
      scales: { x: { type: "linear", grid: { display: false }, ticks: { color: tick, maxTicksLimit: 8, callback: clock } }, y: { grid: { color: grid }, ticks: { color: tick }, title: { display: true, text: "W", color: tick } } } },
  });
}

function startReadingsPolling() {
  refreshReadingsChart();
  if (!readingsTimer) readingsTimer = setInterval(refreshReadingsChart, READINGS_POLL_MS);
}

let explainToken = 0;

function setExplanation(text, source, note) {
  document.getElementById("explanation-text").textContent = text;
  const tag = document.getElementById("explanation-tag");
  tag.textContent = source === "llm" ? "AI-WRITTEN" : "TEMPLATE";
  tag.classList.toggle("tag-ai", source === "llm");
  document.getElementById("explanation-note").textContent = note;
}

async function showExplanation(data) {
  const token = ++explainToken;
  const card = document.querySelector(".explanation-card");
  card.classList.remove("is-loading");
  if (!data.explanation) return;
  const templateNote = "Built from the forecast numbers above by a fixed template.";
  setExplanation(data.explanation, "template", data.explain_id ? "Asking the AI to put this in its own words…" : templateNote);
  if (!data.explain_id) return;
  card.classList.add("is-loading");
  try {
    const response = await fetch("/api/explain", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ explain_id: data.explain_id }) });
    if (token !== explainToken) return;  // a newer forecast replaced this one
    card.classList.remove("is-loading");
    if (!response.ok) { setExplanation(data.explanation, "template", `${templateNote} (AI explanation unavailable.)`); return; }
    const answer = await response.json();
    if (token !== explainToken) return;
    setExplanation(answer.explanation, answer.explanation_source, answer.explanation_source === "llm"
      ? "Written by an AI model from the forecast numbers above. It was told not to invent numbers, and every number in it was checked against the data."
      : `${templateNote} (The AI could not be used, so this is the template.)`);
  } catch (error) {
    if (token !== explainToken) return;
    card.classList.remove("is-loading");
    setExplanation(data.explanation, "template", `${templateNote} (AI explanation unavailable.)`);
  }
}

function updateStatus(data) {
  const sources = data.meta.data_sources;
  document.getElementById("weather-status").textContent      = weatherStatus(sources);
  document.getElementById("calibration-status").textContent  = calibrationLabel(data.meta);
  document.getElementById("engine-status").textContent       = data.meta.engine === "native" ? "NATIVE C++" : "PYTHON FALLBACK";
  document.getElementById("result-badge").textContent        = sources.some((x) => x.startsWith("climatology")) ? "FORECAST + CLIMATE AVERAGE" : "SIMULATED FORECAST";
  document.getElementById("last-run").textContent            = new Date().toLocaleTimeString([], { hour:"2-digit", minute:"2-digit" });
}

/* ── Error banner ────────────────────────────────────────────────── */
function showError(msg) {
  const b = document.getElementById("error-banner");
  b.textContent = msg; b.hidden = false;
  b.scrollIntoView({ block: "nearest", behavior: "smooth" });  // it sits under the Run button, maybe below the sidebar's fold
}
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
    lastForecast = data;
    drawCharts(data);
    renderRecommendations(data.recommendations);
    updateStatus(data);
    refreshReadingsChip();
    refreshReadingsChart();
    showExplanation(data);
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
  map = L.map("map", { zoomControl: false }).setView([42.6977, 23.3219], 8);
  const cloudPane = map.createPane("clouds");
  cloudPane.style.zIndex = 350;
  cloudPane.classList.add("cloud-pane");
  L.control.zoom({ position: "bottomright" }).addTo(map);
  const cartoKey = window.AUFOR_CONFIG?.cartoApiKey;
  const tileUrl = cartoKey
    ? `https://basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png?key=${encodeURIComponent(cartoKey)}`
    : "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png";
  L.tileLayer(tileUrl, { attribution: "&copy; OpenStreetMap &copy; CARTO", maxZoom: 20 }).addTo(map);
  siteMarker = L.marker([42.6977, 23.3219]).addTo(map);
  map.on("click", (e) => { siteMarker.setLatLng(e.latlng); setLocation(e.latlng.lat, e.latlng.lng); });
  setTimeout(() => map.invalidateSize(), 100);
  fetchCurrentWeather(42.6977, 23.3219);
  initTimeline();
  refreshReadingsChip();
  refreshCameraChip();
  startReadingsPolling();
  if (!cameraTimer) cameraTimer = setInterval(() => { refreshCameraChip(); refreshReadingsChip(); }, 30000);
  setTimeout(() => { map.invalidateSize(); ensureFieldForView(); }, 150);
}

/* ── Cloud forecast field: map overlay, timeline, chart sync ─────── */
const TRANSPARENT_PIXEL = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7";
let cloudField = null, cloudImage = null, timeIndex = 0, playTimer = null;
let cloudsOn = true, windOn = false, flowOn = true, fieldTimer = null, pillTimer = null;  // default map layers: clouds and flow
let fieldView = null, fieldRequest = 0, uvCache = new Map();
let cursorEpoch = null;  // follows the mouse on the charts, even past the end of the cloud field
const CLOUD_FIELD_HOURS = 168;  // 7 days: each map view costs one Open-Meteo call per grid point (135) of the free 10,000 a day

function bilinear(grid, row, col) {
  const r0 = Math.max(0, Math.min(grid.length - 2, Math.floor(row)));
  const c0 = Math.max(0, Math.min(grid[0].length - 2, Math.floor(col)));
  const fr = Math.max(0, Math.min(1, row - r0)), fc = Math.max(0, Math.min(1, col - c0));
  const top = grid[r0][c0] * (1 - fc) + grid[r0][c0 + 1] * fc;
  const bottom = grid[r0 + 1][c0] * (1 - fc) + grid[r0 + 1][c0 + 1] * fc;
  return top * (1 - fr) + bottom * fr;
}

const smoothstep = (edge0, edge1, x) => { const t = Math.max(0, Math.min(1, (x - edge0) / (edge1 - edge0))); return t * t * (3 - 2 * t); };

const mercY = (lat) => Math.log(Math.tan(Math.PI / 4 + (lat * Math.PI) / 360));
const invMercY = (y) => (Math.atan(Math.sinh(y)) * 180) / Math.PI;

// Pixels of the cloud image. The map shows the image stretched in Web Mercator, so each image row
// is looked up at its true latitude, not evenly between the grid's first and last latitude.
function cloudPixels(grid, size, dark, lats = null) {
  const rows = grid.length, cols = grid[0].length;
  const [red, green, blue] = dark ? [236, 241, 249] : [214, 224, 238];
  const pixels = new Uint8ClampedArray(size * size * 4);
  const south = lats ? lats[0] : 0, north = lats ? lats[lats.length - 1] : 1;
  const yNorth = lats ? mercY(north) : 0, ySouth = lats ? mercY(south) : 0;
  for (let py = 0; py < size; py++) {
    const row = lats
      ? ((invMercY(yNorth + (ySouth - yNorth) * (py / (size - 1))) - south) / (north - south)) * (rows - 1)
      : (1 - py / (size - 1)) * (rows - 1);
    for (let px = 0; px < size; px++) {
      const cover = bilinear(grid, row, (px / (size - 1)) * (cols - 1));
      const edge = Math.min(px, py, size - 1 - px, size - 1 - py) / size;
      const alpha = Math.pow(Math.max(0, Math.min(1, cover / 100)), 1.25) * 0.8 * smoothstep(0, 0.1, edge);
      const at = (py * size + px) * 4;
      pixels[at] = red; pixels[at + 1] = green; pixels[at + 2] = blue; pixels[at + 3] = Math.round(alpha * 255);
    }
  }
  return pixels;
}

function renderCloudFrame() {
  if (!cloudField || !cloudImage) return;
  const dark = document.body.classList.contains("dark-theme");
  const size = 192;
  const raw = document.createElement("canvas");
  raw.width = raw.height = size;
  const rawContext = raw.getContext("2d");
  const image = rawContext.createImageData(size, size);
  image.data.set(cloudPixels(cloudField.cloud[timeIndex], size, dark, cloudField.lats));
  rawContext.putImageData(image, 0, 0);
  const soft = document.createElement("canvas");
  soft.width = soft.height = 384;
  const softContext = soft.getContext("2d");
  softContext.filter = "blur(2px)";
  softContext.imageSmoothingQuality = "high";
  softContext.drawImage(raw, 0, 0, 384, 384);
  cloudImage.setUrl(soft.toDataURL("image/png"));
  refreshWind();
}

/* ── Wind: arrows and animated flow, drawn in screen space ───────── */
const WIND_CELL = 20;      // px between nodes of the screen wind grid
const ARROW_SPACING = 54;  // px between arrows
const ARROW_OPACITY = 0.6;
let windCanvas = null, flowCanvas = null, windGrid = null, particles = [], flowFrame = null, windRefresh = null;

function fieldPosition(lat, lon) {
  const { lats, lons } = cloudField;
  const row = ((lat - lats[0]) / (lats[lats.length - 1] - lats[0])) * (lats.length - 1);
  const col = ((lon - lons[0]) / (lons[lons.length - 1] - lons[0])) * (lons.length - 1);
  return row < 0 || col < 0 || row > lats.length - 1 || col > lons.length - 1 ? null : [row, col];
}

function frameUV(index) {
  if (!uvCache.has(index)) {
    const speeds = cloudField.wind_speed[index], dirs = cloudField.wind_dir[index];
    const component = (fn) => speeds.map((row, i) => row.map((s, j) => fn(s, (dirs[i][j] * Math.PI) / 180)));
    // wind_dir is where the wind blows FROM, so it flows towards dir + 180
    uvCache.set(index, { u: component((s, a) => -s * Math.sin(a)), v: component((s, a) => -s * Math.cos(a)) });
  }
  return uvCache.get(index);
}

function speedColor(speed) {
  const stops = [[0, [120, 175, 255]], [5, [60, 205, 190]], [10, [250, 205, 70]], [17, [240, 85, 70]]];
  if (speed >= stops[stops.length - 1][0]) return `rgb(${stops[stops.length - 1][1].join(",")})`;
  for (let i = 1; i < stops.length; i++) {
    if (speed <= stops[i][0]) {
      const [s0, c0] = stops[i - 1], [s1, c1] = stops[i], t = (speed - s0) / (s1 - s0);
      return `rgb(${c0.map((c, k) => Math.round(c + (c1[k] - c) * t)).join(",")})`;
    }
  }
  return "rgb(120,175,255)";
}

function prepareCanvas(canvas, className) {
  if (!canvas) {
    canvas = document.createElement("canvas");
    canvas.className = className;
    map.getContainer().appendChild(canvas);
  }
  const { x, y } = map.getSize(), ratio = window.devicePixelRatio || 1;
  canvas.width = Math.round(x * ratio); canvas.height = Math.round(y * ratio);
  canvas.style.width = `${x}px`; canvas.style.height = `${y}px`;
  const context = canvas.getContext("2d");
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, x, y);
  return canvas;
}

function buildWindGrid() {
  const { x: width, y: height } = map.getSize();
  const cols = Math.ceil(width / WIND_CELL) + 1, rows = Math.ceil(height / WIND_CELL) + 1;
  const grid = { cols, rows, u: new Float32Array(cols * rows), v: new Float32Array(cols * rows), valid: new Uint8Array(cols * rows) };
  const uv = frameUV(timeIndex);
  for (let j = 0; j < rows; j++) {
    for (let i = 0; i < cols; i++) {
      const point = map.containerPointToLatLng([i * WIND_CELL, j * WIND_CELL]);
      const at = fieldPosition(point.lat, point.lng);
      if (!at) continue;
      grid.u[j * cols + i] = bilinear(uv.u, at[0], at[1]);
      grid.v[j * cols + i] = bilinear(uv.v, at[0], at[1]);
      grid.valid[j * cols + i] = 1;
    }
  }
  windGrid = grid;
}

function sampleWindGrid(x, y) {
  const g = windGrid;
  const fx = x / WIND_CELL, fy = y / WIND_CELL;
  const i = Math.floor(fx), j = Math.floor(fy);
  if (!g || i < 0 || j < 0 || i >= g.cols - 1 || j >= g.rows - 1) return null;
  const at = (a, b) => b * g.cols + a;
  if (!(g.valid[at(i, j)] && g.valid[at(i + 1, j)] && g.valid[at(i, j + 1)] && g.valid[at(i + 1, j + 1)])) return null;
  const tx = fx - i, ty = fy - j;
  const mix = (arr) => (arr[at(i, j)] * (1 - tx) + arr[at(i + 1, j)] * tx) * (1 - ty) + (arr[at(i, j + 1)] * (1 - tx) + arr[at(i + 1, j + 1)] * tx) * ty;
  return [mix(g.u), mix(g.v)];
}

function drawArrow(context, x, y, u, v) {
  const speed = Math.hypot(u, v);
  if (speed < 0.3) { context.globalAlpha = ARROW_OPACITY; context.fillStyle = speedColor(0); context.beginPath(); context.arc(x, y, 1.4, 0, 6.3); context.fill(); context.globalAlpha = 1; return; }
  const angle = Math.atan2(-v, u);  // screen y points down
  const length = 12 + Math.min(speed, 18) * 1.5, head = 4.5 + Math.min(speed, 18) * 0.16;
  const tipX = x + (Math.cos(angle) * length) / 2, tipY = y + (Math.sin(angle) * length) / 2;
  const tailX = x - (Math.cos(angle) * length) / 2, tailY = y - (Math.sin(angle) * length) / 2;
  const path = () => {
    context.beginPath();
    context.moveTo(tailX, tailY); context.lineTo(tipX, tipY);
    context.moveTo(tipX - Math.cos(angle - 0.5) * head, tipY - Math.sin(angle - 0.5) * head);
    context.lineTo(tipX, tipY);
    context.lineTo(tipX - Math.cos(angle + 0.5) * head, tipY - Math.sin(angle + 0.5) * head);
    context.stroke();
  };
  context.lineCap = "round"; context.lineJoin = "round";
  context.globalAlpha = ARROW_OPACITY;
  context.strokeStyle = "rgba(15,30,45,.45)"; context.lineWidth = 2.2; path();   // thin outline, readable on any map
  context.strokeStyle = speedColor(speed); context.lineWidth = 1; path();
  context.globalAlpha = 1;
}

function drawWindArrows() {
  if (!windCanvas) return;
  const context = windCanvas.getContext("2d");
  const { x: width, y: height } = map.getSize();
  context.clearRect(0, 0, width, height);
  if (!windOn || !windGrid) return;
  const offset = ARROW_SPACING / 2;
  for (let y = offset; y < height; y += ARROW_SPACING) {
    for (let x = offset; x < width; x += ARROW_SPACING) {
      const wind = sampleWindGrid(x, y);
      if (wind) drawArrow(context, x, y, wind[0], wind[1]);
    }
  }
}

function newParticle(width, height, fresh) {
  return { x: Math.random() * width, y: Math.random() * height, age: fresh ? 0 : Math.floor(Math.random() * 80), max: 70 + Math.floor(Math.random() * 60) };
}

function stepFlow() {
  flowFrame = null;
  if (!flowOn || !flowCanvas || !windGrid) return;
  const context = flowCanvas.getContext("2d");
  const { x: width, y: height } = map.getSize();
  context.globalCompositeOperation = "destination-out";
  context.fillStyle = "rgba(0,0,0,0.10)";
  context.fillRect(0, 0, width, height);
  context.globalCompositeOperation = "source-over";
  context.lineWidth = 1.4;
  for (let k = 0; k < particles.length; k++) {
    const p = particles[k];
    const wind = sampleWindGrid(p.x, p.y);
    if (!wind || p.age++ > p.max) { particles[k] = newParticle(width, height, true); continue; }
    const nx = p.x + wind[0] * 0.5, ny = p.y - wind[1] * 0.5;
    context.strokeStyle = speedColor(Math.hypot(wind[0], wind[1]));
    context.globalAlpha = 0.85;
    context.beginPath(); context.moveTo(p.x, p.y); context.lineTo(nx, ny); context.stroke();
    p.x = nx; p.y = ny;
  }
  context.globalAlpha = 1;
  flowFrame = requestAnimationFrame(stepFlow);
}

function startFlow() {
  if (flowFrame) cancelAnimationFrame(flowFrame);
  flowFrame = null;
  if (!flowOn || !cloudField) return;
  flowCanvas = prepareCanvas(flowCanvas, "wind-canvas flow-canvas");
  const { x: width, y: height } = map.getSize();
  particles = Array.from({ length: Math.min(2600, Math.round((width * height) / 900)) }, () => newParticle(width, height, false));
  flowFrame = requestAnimationFrame(stepFlow);
}

function hideWind() {
  if (flowFrame) { cancelAnimationFrame(flowFrame); flowFrame = null; }
  [windCanvas, flowCanvas].forEach((canvas) => { if (canvas) canvas.style.opacity = 0; });
}

// Rebuild the screen-space wind from the current map view and forecast hour
function refreshWind() {
  if (!map || !cloudField) return;
  windCanvas = prepareCanvas(windCanvas, "wind-canvas");
  buildWindGrid();
  drawWindArrows();
  [windCanvas, flowCanvas].forEach((canvas) => { if (canvas) canvas.style.opacity = 1; });
  if (flowOn && !flowFrame) startFlow();
}

function scheduleWindRefresh() {
  cancelAnimationFrame(windRefresh);
  windRefresh = requestAnimationFrame(() => { refreshWind(); if (flowOn) startFlow(); });
}

/* ── Timeline ───────────────────────────────────────────────────── */
const fieldTimeLabel = (epoch) => new Date(epoch * 1000).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" });

// Cloud % and wind speed at the site marker for a forecast hour; null if the site is outside the field
function conditionsAtSite(index) {
  if (!cloudField || !siteMarker) return null;
  const { lat, lng } = siteMarker.getLatLng();
  const at = fieldPosition(lat, lng);
  if (!at) return null;
  return { cloud: Math.round(bilinear(cloudField.cloud[index], at[0], at[1])), wind: Math.round(bilinear(cloudField.wind_speed[index], at[0], at[1]) * 10) / 10 };
}

function siteLine(index) {
  const site = conditionsAtSite(index);
  return site ? `☁ ${site.cloud}% · ${site.wind} m/s wind` : "site is outside this map view";
}

function setTimeIndex(index) {
  if (!cloudField) return;
  timeIndex = Math.max(0, Math.min(cloudField.times.length - 1, Math.round(index)));
  cursorEpoch = cloudField.times[timeIndex];
  document.getElementById("tl-range").value = timeIndex;
  document.getElementById("tl-time").textContent = fieldTimeLabel(cloudField.times[timeIndex]);
  document.getElementById("tl-cloud").textContent = siteLine(timeIndex);
  renderCloudFrame();
  Object.values(charts).forEach((chart) => chart.draw());
}

function nearestFieldIndex(epoch, toleranceSeconds) {
  if (!cloudField) return -1;
  let best = -1, bestGap = Infinity;
  cloudField.times.forEach((t, i) => { const gap = Math.abs(t - epoch); if (gap < bestGap) { best = i; bestGap = gap; } });
  return bestGap <= toleranceSeconds ? best : -1;
}

function currentEpoch() { return cloudField ? cloudField.times[timeIndex] : null; }

function showHoverPill(text, ms = 2200) {
  const pill = document.getElementById("hover-pill");
  pill.textContent = text;
  pill.hidden = false;
  clearTimeout(pillTimer);
  pillTimer = setTimeout(() => { pill.hidden = true; }, ms);
}

function togglePlay() {
  const button = document.getElementById("cloud-play");
  if (playTimer) { clearInterval(playTimer); playTimer = null; button.textContent = "▶"; return; }
  button.textContent = "❚❚";
  const step = Math.max(1, Math.round(cloudField.times.length / 96));  // a long field plays faster per frame
  playTimer = setInterval(() => setTimeIndex(timeIndex + step >= cloudField.times.length ? 0 : timeIndex + step), 450);
}

function initTimeline() {
  document.getElementById("tl-range").addEventListener("input", (event) => setTimeIndex(Number(event.target.value)));
  document.getElementById("cloud-play").addEventListener("click", () => { if (cloudField) togglePlay(); });
  const chip = (id, getter, setter) => document.getElementById(id).addEventListener("click", (event) => {
    setter(!getter());
    event.currentTarget.classList.toggle("is-active", getter());
  });
  chip("toggle-clouds", () => cloudsOn, (v) => { cloudsOn = v; if (cloudImage) cloudImage.setOpacity(v ? 1 : 0); });
  chip("toggle-wind", () => windOn, (v) => { windOn = v; drawWindArrows(); });
  chip("toggle-flow", () => flowOn, (v) => {
    flowOn = v;
    if (v) startFlow();
    else { if (flowFrame) cancelAnimationFrame(flowFrame); flowFrame = null; if (flowCanvas) flowCanvas.getContext("2d").clearRect(0, 0, flowCanvas.width, flowCanvas.height); }
  });
  map.on("movestart zoomstart", hideWind);
  map.on("moveend zoomend resize", () => { scheduleWindRefresh(); scheduleFieldCheck(); });
}

/* ── The field follows the map view ──────────────────────────────── */
function viewBounds(pad) {
  const b = map.getBounds();
  const dLat = (b.getNorth() - b.getSouth()) * pad, dLon = (b.getEast() - b.getWest()) * pad;
  return { south: Math.max(-85, b.getSouth() - dLat), north: Math.min(85, b.getNorth() + dLat), west: Math.max(-180, b.getWest() - dLon), east: Math.min(180, b.getEast() + dLon) };
}

function fieldCoversView() {
  if (!cloudField || !fieldView) return false;
  const b = map.getBounds();
  // Compare with the area we asked for, not with what came back: a clamped grid (near the poles or the
  // date line) must not trigger a new request on every move.
  const inside = Math.max(b.getSouth(), -85) >= fieldView.south && Math.min(b.getNorth(), 85) <= fieldView.north
    && Math.max(b.getWest(), -180) >= fieldView.west && Math.min(b.getEast(), 180) <= fieldView.east;
  return inside && map.getZoom() - fieldView.zoom < 1;  // zoomed in a level or more: the grid is too coarse, refetch
}

function scheduleCloudField() { scheduleFieldCheck(); }

function scheduleFieldCheck() {
  clearTimeout(fieldTimer);
  fieldTimer = setTimeout(ensureFieldForView, 600);
}

function ensureFieldForView() {
  if (!map || fieldCoversView()) return;
  loadCloudField(viewBounds(0.3));
}

async function loadCloudField(view) {
  const timeline = document.getElementById("map-timeline");
  const request = ++fieldRequest;
  const query = `south=${view.south.toFixed(3)}&west=${view.west.toFixed(3)}&north=${view.north.toFixed(3)}&east=${view.east.toFixed(3)}&hours=${CLOUD_FIELD_HOURS}`;
  let payload;
  try {
    const response = await fetch(`/api/cloud-field?${query}`);
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || "cloud field request failed");
    payload = body;
  } catch (error) {
    if (request !== fieldRequest) return;
    const reason = String(error.message || "");
    showHoverPill(reason.includes("zoom in") ? "Zoom in to see the cloud forecast" : `Cloud map unavailable: ${reason.slice(0, 160)}`, 9000);
    if (!cloudField) timeline.hidden = true;
    return;
  }
  if (request !== fieldRequest) return;  // a newer view was requested meanwhile
  const keepEpoch = cursorEpoch ?? Date.now() / 1000;
  cloudField = payload;
  fieldView = { ...view, zoom: map.getZoom() };
  uvCache = new Map();
  const bounds = [[cloudField.lats[0], cloudField.lons[0]], [cloudField.lats[cloudField.lats.length - 1], cloudField.lons[cloudField.lons.length - 1]]];
  if (cloudImage) cloudImage.remove();
  cloudImage = L.imageOverlay(TRANSPARENT_PIXEL, bounds, { pane: "clouds", interactive: false, opacity: cloudsOn ? 1 : 0 }).addTo(map);
  document.getElementById("tl-range").max = cloudField.times.length - 1;
  document.getElementById("tl-start").textContent = fieldTimeLabel(cloudField.times[0]);
  document.getElementById("tl-end").textContent = fieldTimeLabel(cloudField.times[cloudField.times.length - 1]);
  timeline.hidden = false;
  const overlay = document.getElementById("cloud-overlay");
  if (overlay) overlay.style.background = "transparent";
  if (windMarker) { windMarker.remove(); windMarker = null; }
  setTimeIndex(Math.max(0, nearestFieldIndex(keepEpoch, Infinity)));
  if (String(cloudField.source).startsWith("open-meteo (cached")) showHoverPill("Open-Meteo is unreachable: showing the last cloud forecast saved on the server", 9000);
}

const crosshairPlugin = {
  id: "timeCrosshair",
  afterDatasetsDraw(chart) {
    const epoch = cursorEpoch;
    if (epoch === null || !chart.$epochs || !chart.scales.x) return;
    let best = 0;
    chart.$epochs.forEach((e, i) => { if (Math.abs(e - epoch) < Math.abs(chart.$epochs[best] - epoch)) best = i; });
    const x = chart.scales.x.getPixelForValue(best);
    const { top, bottom } = chart.chartArea;
    const context = chart.ctx;
    context.save();
    context.strokeStyle = "rgba(20,120,232,.65)";
    context.lineWidth = 1.5;
    context.setLineDash([4, 3]);
    context.beginPath(); context.moveTo(x, top); context.lineTo(x, bottom); context.stroke();
    context.restore();
  },
};
if (typeof Chart !== "undefined" && Chart.register) Chart.register(crosshairPlugin);

function wireChartTime(data) {
  const hourly = data.hourly.length > 0;
  const monthly = data.monthly.length > 0;
  const rows = monthly ? data.monthly : data.daily;
  const epochs = {
    hourly: data.hourly.map((r) => Date.parse(r.time) / 1000),
    daily: rows.map((r) => Date.parse(monthly ? `${r.month}-15T12:00:00` : `${r.date}T12:00:00`) / 1000),
  };
  const attach = (name, values, tolerance) => {
    const chart = charts[name];
    if (!chart) return;
    chart.$epochs = values;
    chart.options.interaction = { mode: "index", intersect: false };
    chart.options.onHover = (event, active) => {
      if (!active.length || !cloudField) return;
      const epoch = values[active[0].index];
      const index = nearestFieldIndex(epoch, tolerance);
      if (index < 0) {
        // Past the end of the cloud forecast: the cursor still follows the mouse, the map stays put.
        if (cursorEpoch === epoch) return;
        cursorEpoch = epoch;
        Object.values(charts).forEach((c) => c.draw());
        showHoverPill(`No cloud forecast after ${fieldTimeLabel(cloudField.times[cloudField.times.length - 1])}`);
        return;
      }
      if (index === timeIndex && cursorEpoch === cloudField.times[index]) return;
      setTimeIndex(index);
      showHoverPill(`${fieldTimeLabel(cloudField.times[index])} · ${siteLine(index)} (map follows)`);
    };
    chart.update("none");
  };
  if (hourly) { attach("hourly", epochs.hourly, 1800); attach("soc", epochs.hourly, 1800); attach("cloud", epochs.hourly, 1800); }
  else attach("cloud", epochs.daily, 12 * 3600);
  attach("daily", epochs.daily, 12 * 3600);
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

/* ── Forecast period ─────────────────────────────────────────────── */
const OPEN_METEO_DAYS = 16;
const MAX_HOURLY_DAYS = 31;

function syncPeriodControls() {
  const resolution = document.getElementById("resolution").value;
  const daysInput = document.getElementById("days");
  daysInput.max = resolution === "hourly" ? MAX_HOURLY_DAYS : 365;
  if (Number(daysInput.value) > Number(daysInput.max)) daysInput.value = daysInput.max;
  const days = Math.max(1, Math.round(value("days")) || 1);
  document.getElementById("horizon-label").textContent = `${days} ${days === 1 ? "DAY" : "DAYS"} · ${resolution.toUpperCase()}`;
  const note = document.getElementById("horizon-note");
  note.hidden = days <= OPEN_METEO_DAYS;
  note.textContent = `Weather forecasts reach ${OPEN_METEO_DAYS} days. Days after that use the average weather of the same dates last year, so treat them as an estimate, not a forecast.`;
  document.querySelectorAll(".preset").forEach((b) => b.classList.toggle("is-active", Number(b.dataset.days) === days && b.dataset.resolution === resolution));
}

function applyPreset(button) {
  document.getElementById("resolution").value = button.dataset.resolution;
  document.getElementById("days").value = button.dataset.days;
  syncPeriodControls();
}

/* ── Theme ───────────────────────────────────────────────────────── */
/* The theme follows the system setting until the toggle is used; then the saved choice wins. */
function systemPrefersDark() {
  return !!(window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);
}

function savedTheme() {
  try {
    const saved = localStorage.getItem("aufor-theme");
    return saved === "dark" || saved === "light" ? saved : null;
  } catch (error) { return null; }
}

function wantsDark() {
  const saved = savedTheme();
  return saved ? saved === "dark" : systemPrefersDark();
}

function applyTheme(dark) {
  document.body.classList.toggle("dark-theme", dark);
  if (map) map.invalidateSize();
  updateSunNow(getLat(), getLon());
  renderCloudFrame();
  if (lastForecast) { drawCharts(lastForecast); refreshReadingsChart(); }  // charts pick their colours when drawn
  if (currentWeatherData && !cloudField) {
    drawWindArrow(document.getElementById("wind-canvas"), currentWeatherData.wind_direction_10m ?? 0);
    placeWindMarker(getLat(), getLon(), currentWeatherData.wind_speed_10m ?? 0, currentWeatherData.wind_direction_10m ?? 0);
  }
}

function toggleTheme() {
  const dark = !document.body.classList.contains("dark-theme");
  try { localStorage.setItem("aufor-theme", dark ? "dark" : "light"); } catch (error) { /* private mode: the choice just is not kept */ }
  applyTheme(dark);
}

/* ── Boot ────────────────────────────────────────────────────────── */
document.addEventListener("DOMContentLoaded", () => {
  addPanelGroup();
  applyTheme(wantsDark());
  const scheme = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)");
  if (scheme && scheme.addEventListener) scheme.addEventListener("change", () => { if (!savedTheme()) applyTheme(systemPrefersDark()); });

  initView();

  const form = document.getElementById("forecast-form");
  if (form) form.addEventListener("submit", runForecast);
  const addBtn = document.getElementById("add-panel-group");
  if (addBtn) addBtn.addEventListener("click", () => addPanelGroup());
  document.getElementById("theme-toggle").addEventListener("click", toggleTheme);

  const daysEl = document.getElementById("days");
  if (daysEl) {
    daysEl.addEventListener("input", syncPeriodControls);
    document.getElementById("resolution").addEventListener("change", syncPeriodControls);
    document.querySelectorAll(".preset").forEach((b) => b.addEventListener("click", () => applyPreset(b)));
    syncPeriodControls();
  }

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
