const presets = {
  cluj: { lat: 46.7712, lon: 23.6236, label: "Cluj-Napoca" },
};

const state = {
  previewUrl: null,
};

const form = document.querySelector("#generator-form");
const latInput = document.querySelector("#lat-input");
const lonInput = document.querySelector("#lon-input");
const sizeInput = document.querySelector("#size-input");
const waterSourceInput = document.querySelector("#water-source");
const rasterInput = document.querySelector("#communities-raster");
const thresholdInput = document.querySelector("#threshold-input");
const minAreaInput = document.querySelector("#min-area-input");
const terrainToggle = document.querySelector("#terrain-toggle");
const terrainResolutionInput = document.querySelector("#terrain-resolution-input");
const metricsToggle = document.querySelector("#metrics-toggle");
const dischargeToggle = document.querySelector("#discharge-toggle");
const metricResInput = document.querySelector("#metric-res-input");
const lookbackInput = document.querySelector("#lookback-input");
const stabilityToggle = document.querySelector("#stability-toggle");
const stabilityBufferInput = document.querySelector("#stability-buffer-input");
const motionThresholdInput = document.querySelector("#motion-threshold-input");

const previewFrame = document.querySelector("#preview-frame");
const previewLoader = document.querySelector("#preview-loader");
const statusText = document.querySelector("#status-text");
const liveCoords = document.querySelector("#live-coords");
const generateBtn = document.querySelector("#generate-btn");
const metricWaterSource = document.querySelector("#metric-water-source");
const metricSize = document.querySelector("#metric-size");
const metricTerrain = document.querySelector("#metric-terrain");
const metricHydrology = document.createElement("article");
metricHydrology.className = "metric-card";
metricHydrology.innerHTML = "<span>Hydrology</span><strong id=\"metric-hydrology\">Off</strong>";
document.querySelector(".metric-grid").appendChild(metricHydrology);

const insightWaterSource = document.querySelector("#insight-water-source");
const insightRaster = document.querySelector("#insight-raster");
const insightThreshold = document.querySelector("#insight-threshold");
const insightMinArea = document.querySelector("#insight-min-area");
const tabButtons = Array.from(document.querySelectorAll("[data-tab-target]"));
const tabPanels = Array.from(document.querySelectorAll(".tab-panel"));

const map = L.map("selection-map", {
  zoomControl: true,
  preferCanvas: true,
}).setView([46.7712, 23.6236], 8);

L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: "&copy; OpenStreetMap contributors",
}).addTo(map);

const marker = L.marker([46.7712, 23.6236], { draggable: true }).addTo(map);

function setCoordinates(lat, lon, options = {}) {
  const nextLat = Number(lat);
  const nextLon = Number(lon);
  latInput.value = nextLat.toFixed(6);
  lonInput.value = nextLon.toFixed(6);
  marker.setLatLng([nextLat, nextLon]);
  liveCoords.textContent = `${nextLat.toFixed(5)}, ${nextLon.toFixed(5)}`;
  if (options.pan !== false) {
    map.setView([nextLat, nextLon], options.zoom ?? Math.max(map.getZoom(), 10), { animate: true });
  }
  syncSummary(readPayload());
}

function setStatus(message, isError = false) {
  statusText.textContent = message;
  statusText.style.color = isError ? "#8f1d14" : "";
}

function formatWaterSource(value) {
  return value === "euhydro" ? "Local EuHydro" : "OpenStreetMap Overpass";
}

function selectTab(targetId) {
  tabButtons.forEach((button) => {
    button.classList.toggle("is-active", button.dataset.tabTarget === targetId);
  });
  tabPanels.forEach((panel) => {
    panel.classList.toggle("is-active", panel.id === targetId);
  });
  if (targetId === "preview-panel") {
    document.querySelector("#preview-panel").scrollIntoView({ behavior: "smooth", block: "start" });
  }
}

function syncSummary(payload) {
  const waterSourceLabel = formatWaterSource(payload.water_source);
  metricWaterSource.textContent = payload.water_source === "euhydro" ? "EuHydro" : "Overpass";
  metricSize.textContent = `${payload.size_km} km`;
  metricTerrain.textContent = payload.terrain ? "On" : "Off";
  document.querySelector("#metric-hydrology").textContent = payload.river_metrics ? "On" : "Off";

  insightWaterSource.textContent = waterSourceLabel;
  insightRaster.textContent = payload.communities_raster || "Not set";
  insightThreshold.textContent = String(payload.community_threshold);
  insightMinArea.textContent = `${payload.min_community_area_m2} m²`;
}

async function loadStatus() {
  const response = await fetch("/api/status");
  const payload = await response.json();

  sizeInput.value = payload.defaults.size_km;
  waterSourceInput.value = payload.defaults.water_source;
  thresholdInput.value = payload.defaults.community_threshold;
  minAreaInput.value = payload.defaults.min_community_area_m2;
  terrainResolutionInput.value = payload.defaults.terrain_resolution_m;
  
  metricsToggle.checked = payload.defaults.river_metrics;
  dischargeToggle.checked = payload.defaults.river_discharge;
  metricResInput.value = payload.defaults.river_metric_resolution_m;
  lookbackInput.value = payload.defaults.river_metric_lookback_days;
  
  stabilityToggle.checked = payload.defaults.stability;
  stabilityBufferInput.value = payload.defaults.stability_buffer_m;
  motionThresholdInput.value = payload.defaults.differential_motion_threshold;

  syncSummary(readPayload());

  if (payload.has_preview && payload.preview_url) {
    state.previewUrl = payload.preview_url;
    previewFrame.src = `${payload.preview_url}?t=${Date.now()}`;
    setStatus("Current preview loaded. Adjust the coordinates or click Generate View for a fresh result.");
  } else {
    setStatus("No generated preview yet. Choose a place and generate a new view.");
  }
}

function readPayload() {
  return {
    lat: Number(latInput.value),
    lon: Number(lonInput.value),
    size_km: Number(sizeInput.value),
    water_source: waterSourceInput.value,
    communities_raster: rasterInput.value.trim(),
    community_threshold: Number(thresholdInput.value),
    min_community_area_m2: Number(minAreaInput.value),
    terrain: terrainToggle.checked,
    terrain_resolution_m: Number(terrainResolutionInput.value),
    river_metrics: metricsToggle.checked,
    river_discharge: dischargeToggle.checked,
    river_metric_resolution_m: Number(metricResInput.value),
    river_metric_lookback_days: Number(lookbackInput.value),
    stability: stabilityToggle.checked,
    stability_buffer_m: Number(stabilityBufferInput.value),
    differential_motion_threshold: Number(motionThresholdInput.value),
  };
}

async function generatePreview(event) {
  event.preventDefault();
  const payload = readPayload();
  syncSummary(payload);
  
  // Show loading state
  selectTab("preview-panel");
  if (previewLoader) previewLoader.style.display = "flex";
  generateBtn.disabled = true;
  generateBtn.textContent = "Generating...";
  setStatus(`Generating geospatial preview for ${payload.lat.toFixed(5)}, ${payload.lon.toFixed(5)}... This may take up to a minute.`);

  try {
    const response = await fetch("/api/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const result = await response.json();
    if (!response.ok || !result.ok) {
      throw new Error(result.error || "Preview generation failed.");
    }
    
    // Update state and iframe
    state.previewUrl = result.index_url;
    // Force reload by setting src to blank then the new URL with timestamp
    previewFrame.src = "about:blank";
    setTimeout(() => {
      previewFrame.src = `${result.index_url}?t=${Date.now()}`;
      if (previewLoader) previewLoader.style.display = "none";
    }, 50);
    
    setStatus(`Success! Preview ready for ${result.lat.toFixed(5)}, ${result.lon.toFixed(5)}.`);
  } catch (error) {
    if (previewLoader) previewLoader.style.display = "none";
    setStatus(error.message, true);
    // Switch back to map if error
    selectTab("map-panel");
  } finally {
    generateBtn.disabled = false;
    generateBtn.textContent = "Generate View";
  }
}

function openCurrentPreview() {
  if (!state.previewUrl) {
    setStatus("There is no generated preview to open yet.", true);
    return;
  }
  window.open(state.previewUrl, "_blank", "noopener");
}

function copyCoordinates() {
  const text = `${latInput.value}, ${lonInput.value}`;
  navigator.clipboard.writeText(text)
    .then(() => setStatus(`Copied coordinates: ${text}`))
    .catch(() => setStatus("Clipboard access failed.", true));
}

function resetForm() {
  const preset = presets.cluj;
  setCoordinates(preset.lat, preset.lon, { zoom: 10 });
  rasterInput.value = "";
  terrainToggle.checked = false;
  syncSummary(readPayload());
  setStatus("Planner reset to the default location.");
}

document.querySelectorAll("[data-scroll-target]").forEach((button) => {
  button.addEventListener("click", () => {
    const target = document.querySelector(button.dataset.scrollTarget);
    target?.scrollIntoView({ behavior: "smooth", block: "start" });
  });
});

document.querySelectorAll("[data-action=\"load-current-preview\"]").forEach((button) => {
  button.addEventListener("click", () => {
  document.querySelector("#preview-panel").scrollIntoView({ behavior: "smooth", block: "start" });
  if (state.previewUrl) {
    previewFrame.src = `${state.previewUrl}?t=${Date.now()}`;
    setStatus("Current preview refreshed.");
  }
});
});

document.querySelector("#copy-coords").addEventListener("click", copyCoordinates);
document.querySelector("#open-preview-tab").addEventListener("click", openCurrentPreview);
document.querySelector("#refresh-preview").addEventListener("click", () => {
  if (!state.previewUrl) {
    setStatus("There is no preview to refresh yet.", true);
    return;
  }
  previewFrame.src = `${state.previewUrl}?t=${Date.now()}`;
  setStatus("Preview frame refreshed.");
});
document.querySelector("#reset-btn").addEventListener("click", resetForm);
tabButtons.forEach((button) => {
  button.addEventListener("click", () => selectTab(button.dataset.tabTarget));
});

form.addEventListener("submit", generatePreview);

map.on("click", (event) => {
  setCoordinates(event.latlng.lat, event.latlng.lng, { pan: false });
  setStatus("Coordinates updated from the selection map.");
});

marker.on("dragend", (event) => {
  const position = event.target.getLatLng();
  setCoordinates(position.lat, position.lng, { pan: false });
  setStatus("Coordinates updated from the draggable marker.");
});

[latInput, lonInput].forEach((input) => {
  input.addEventListener("change", () => {
    const lat = Number(latInput.value);
    const lon = Number(lonInput.value);
    if (Number.isFinite(lat) && Number.isFinite(lon)) {
      setCoordinates(lat, lon, { zoom: map.getZoom() });
    }
  });
});

[sizeInput, waterSourceInput, rasterInput, thresholdInput, minAreaInput, terrainToggle, metricsToggle, dischargeToggle, metricResInput, lookbackInput, stabilityToggle, stabilityBufferInput, motionThresholdInput].forEach((input) => {
  input.addEventListener("change", () => syncSummary(readPayload()));
});

setCoordinates(46.7712, 23.6236, { zoom: 8 });
loadStatus().catch((error) => setStatus(error.message, true));
