const defaultConfig = {
  noct: 45,
  gamma: -0.004,
  inverter_max_w: 4000,
  dod: 0.9,
  eta_c: 0.95,
  eta_d: 0.95,
};

let map;
let marker;
const charts = {};
let panelGroupCount = 0;

function value(id) {
  return Number(document.getElementById(id).value);
}

function panelGroupFromElement(element) {
  return {
    count: Number(element.querySelector("[data-field='count']").value),
    watt_peak: Number(element.querySelector("[data-field='watt_peak']").value),
    tilt: Number(element.querySelector("[data-field='tilt']").value),
    azimuth: Number(element.querySelector("[data-field='azimuth']").value),
    noct: 45,
    gamma: defaultConfig.gamma,
    inverter_max_w: Number(element.querySelector("[data-field='inverter_max_w']").value),
  };
}

function addPanelGroup(config = { count: 10, watt_peak: 400, tilt: 35, azimuth: 180, inverter_max_w: 4000 }) {
  panelGroupCount += 1;
  const group = document.createElement("div");
  group.className = "panel-group";
  group.innerHTML = `<div class="panel-group-head"><strong>Group ${panelGroupCount}</strong><button type="button" class="remove-group" aria-label="Remove panel group">×</button></div><div class="field-grid three"><label>Panels <input data-field="count" type="number" min="1" value="${config.count}"></label><label>Watt peak <input data-field="watt_peak" type="number" min="1" value="${config.watt_peak}"></label><label>Tilt <input data-field="tilt" type="number" min="0" max="90" value="${config.tilt}"><small>degrees</small></label></div><div class="field-grid two"><label>Azimuth <input data-field="azimuth" type="number" min="0" max="360" value="${config.azimuth}"><small>south = 180</small></label><label>Inverter <input data-field="inverter_max_w" type="number" min="1" value="${config.inverter_max_w}"><small>watts</small></label></div>`;
  group.querySelector(".remove-group").addEventListener("click", () => { if (document.querySelectorAll(".panel-group").length > 1) group.remove(); });
  document.getElementById("panel-groups").appendChild(group);
}

function setLocation(lat, lon) {
  document.getElementById("lat").value = lat.toFixed(4);
  document.getElementById("lon").value = lon.toFixed(4);
}

function payload() {
  const panels = [...document.querySelectorAll(".panel-group")].map(panelGroupFromElement);
  return {
    lat: value("lat"),
    lon: value("lon"),
    panels,
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

function showError(message) {
  const banner = document.getElementById("error-banner");
  banner.textContent = message;
  banner.hidden = false;
}

function clearError() {
  document.getElementById("error-banner").hidden = true;
}

function destroyChart(name) {
  if (charts[name]) charts[name].destroy();
}

function drawCharts(data) {
  const labels = data.hourly.map((row) => new Date(row.time).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }));
  const chartOptions = { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { x: { grid: { display: false }, ticks: { maxTicksLimit: 10, color: "#77818a" } }, y: { grid: { color: "#edf0f1" }, ticks: { color: "#77818a" } } } };
  destroyChart("hourly");
  charts.hourly = new Chart(document.getElementById("hourly-chart"), { type: "line", data: { labels, datasets: [{ label: "Solar", data: data.hourly.map((row) => row.p_ac_w), borderColor: "#1478e8", backgroundColor: "rgba(20,120,232,.1)", fill: true, tension: .35, pointRadius: 0 }, { label: "Load", data: data.hourly.map((row) => row.load_w), borderColor: "#f2a43a", borderDash: [5, 5], tension: .35, pointRadius: 0 }] }, options: chartOptions });
  destroyChart("daily");
  charts.daily = new Chart(document.getElementById("daily-chart"), { type: "bar", data: { labels: data.daily.map((row) => row.date), datasets: [{ data: data.daily.map((row) => row.kwh), backgroundColor: "#1478e8", borderRadius: 0 }] }, options: chartOptions });
  destroyChart("soc");
  charts.soc = new Chart(document.getElementById("soc-chart"), { type: "line", data: { labels, datasets: [{ data: data.hourly.map((row) => row.soc_kwh), borderColor: "#23343e", backgroundColor: "rgba(35,52,62,.08)", fill: true, tension: .25, pointRadius: 0 }] }, options: chartOptions });
}

function renderRecommendations(items) {
  const list = document.getElementById("recommendation-list");
  list.innerHTML = items.length ? items.map((item) => `<article class="recommendation"><div class="recommendation-meta"><span class="badge">${item.subtopic}</span><span>${new Date(item.hour).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span></div><h3>${item.title}</h3><p>${item.reason}</p></article>`).join("") : '<div class="empty-state">No recommendations for this forecast.</div>';
}

function updateStatus(data) {
  document.getElementById("weather-status").textContent = data.meta.data_sources.join(" / ").toUpperCase();
  document.getElementById("calibration-status").textContent = data.meta.calibrated ? "CALIBRATED" : "NOT CALIBRATED";
  document.getElementById("result-badge").textContent = "SIMULATED FORECAST";
  document.getElementById("last-run").textContent = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function setView(view) {
  const landing = document.getElementById("landing-view");
  const calculator = document.getElementById("calculator-view");
  landing.hidden = view !== "landing";
  calculator.hidden = view !== "calculator";
  document.querySelectorAll(".view-tab").forEach((tab) => {
    tab.classList.toggle("is-active", tab.dataset.view === view);
  });
  if (view === "calculator" && !map) { initMap(); }
  if (view === "calculator" && map) { setTimeout(() => map.invalidateSize(), 50); }
}

function updateHorizonLabel() {
  const days = value("days");
  document.getElementById("horizon-label").textContent = `${days} DAYS`;
}

function toggleTheme() {
  document.body.classList.toggle("dark-theme");
  localStorage.setItem("solarsight-theme", document.body.classList.contains("dark-theme") ? "dark" : "light");
  if (map) map.invalidateSize();
}

async function runForecast(event) {
  event.preventDefault();
  clearError();
  const button = document.getElementById("forecast-button");
  button.disabled = true;
  button.querySelector("span").textContent = "Calculating energy flow...";
  try {
    const response = await fetch("/api/forecast", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload()) });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail?.[0]?.msg || "Forecast request failed");
    drawCharts(data);
    renderRecommendations(data.recommendations);
    updateStatus(data);
    document.getElementById("results").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    showError(error.message || "Could not run forecast.");
  } finally {
    button.disabled = false;
    button.querySelector("span").textContent = "Run energy forecast";
  }
}

function initMap() {
  map = L.map("map", { zoomControl: false }).setView([42.6977, 23.3219], 12);
  L.control.zoom({ position: "bottomright" }).addTo(map);
  const cartoKey = window.SOLARSIGHT_CONFIG?.cartoApiKey;
  const tileUrl = cartoKey
    ? "https://basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png?key=" + encodeURIComponent(cartoKey)
    : "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png";
  L.tileLayer(tileUrl, { attribution: "&copy; OpenStreetMap &copy; CARTO", maxZoom: 20 }).addTo(map);
  marker = L.marker([42.6977, 23.3219]).addTo(map);
  map.on("click", (event) => { marker.setLatLng(event.latlng); setLocation(event.latlng.lat, event.latlng.lng); });
}

document.addEventListener("DOMContentLoaded", () => {
  addPanelGroup();
  if (localStorage.getItem("solarsight-theme") === "dark") document.body.classList.add("dark-theme");
  document.getElementById("forecast-form").addEventListener("submit", runForecast);
  document.getElementById("add-panel-group").addEventListener("click", () => addPanelGroup());
  document.getElementById("open-calculator").addEventListener("click", () => setView("calculator"));
  document.getElementById("back-to-intro").addEventListener("click", () => setView("landing"));
  document.querySelectorAll(".view-tab").forEach((tab) => tab.addEventListener("click", () => setView(tab.dataset.view)));
  document.getElementById("theme-toggle").addEventListener("click", toggleTheme);
  document.getElementById("days").addEventListener("change", updateHorizonLabel);
});