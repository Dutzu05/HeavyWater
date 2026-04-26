const presets = {
  cluj: { lat: 46.7712, lon: 23.6236, label: "Cluj-Napoca" },
};

const state = {
  previewUrl: null,
  previewLoadedUrl: null,
};

const form = document.querySelector("#generator-form");
const latInput = document.querySelector("#lat-input");
const lonInput = document.querySelector("#lon-input");
const sizeInput = document.querySelector("#size-input");
const waterSourceInput = document.querySelector("#water-source");
const rasterInput = document.querySelector("#communities-raster");
const thresholdInput = document.querySelector("#threshold-input");
const minAreaInput = document.querySelector("#min-area-input");
const communityMergeInput = document.querySelector("#community-merge-input");
const terrainToggle = document.querySelector("#terrain-toggle");
const terrainResolutionInput = document.querySelector("#terrain-resolution-input");
const metricsToggle = document.querySelector("#metrics-toggle");
const dischargeToggle = document.querySelector("#discharge-toggle");
const metricResInput = document.querySelector("#metric-res-input");
const lookbackInput = document.querySelector("#lookback-input");
const stabilityToggle = document.querySelector("#stability-toggle");
const stabilityBufferInput = document.querySelector("#stability-buffer-input");
const motionThresholdInput = document.querySelector("#motion-threshold-input");
const waterRiskToggle = document.querySelector("#water-risk-toggle");
const waterRiskModeInput = document.querySelector("#water-risk-mode");
const farmDemandInput = document.querySelector("#farm-demand-input");
const glofasDaysInput = document.querySelector("#glofas-days-input");

const previewFrame = document.querySelector("#preview-frame");
const previewLoader = document.querySelector("#preview-loader");
const statusText = document.querySelector("#status-text");
const generateBtn = document.querySelector("#generate-btn");
const metricWaterSource = document.querySelector("#metric-water-source");
const metricSize = document.querySelector("#metric-size");
const metricTerrain = document.querySelector("#metric-terrain");
const metricHydrology = document.createElement("article");
metricHydrology.className = "metric-card";
metricHydrology.innerHTML = "<span>Hydrology</span><strong id=\"metric-hydrology\">Off</strong>";
document.querySelector(".metric-grid").appendChild(metricHydrology);
const metricRisk = document.createElement("article");
metricRisk.className = "metric-card";
metricRisk.innerHTML = "<span>Water risk</span><strong id=\"metric-risk\">Off</strong>";
document.querySelector(".metric-grid").appendChild(metricRisk);

const insightWaterSource = document.querySelector("#insight-water-source");
const insightRaster = document.querySelector("#insight-raster");
const insightThreshold = document.querySelector("#insight-threshold");
const insightMinArea = document.querySelector("#insight-min-area");
const tabButtons = Array.from(document.querySelectorAll("[data-tab-target]"));
const tabPanels = Array.from(document.querySelectorAll(".tab-panel"));
const guidelineReportLink = document.querySelector("#guideline-report-link");
const caseStudyReportLink = document.querySelector("#case-study-report-link");

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
  if (!Number.isFinite(nextLat) || !Number.isFinite(nextLon)) {
    setStatus("Enter valid numeric latitude and longitude.", true);
    return;
  }
  if (nextLat < -90 || nextLat > 90 || nextLon < -180 || nextLon > 180) {
    setStatus("Latitude must be -90 to 90 and longitude must be -180 to 180.", true);
    return;
  }
  latInput.value = nextLat.toFixed(6);
  lonInput.value = nextLon.toFixed(6);
  marker.setLatLng([nextLat, nextLon]);
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
    ensurePreviewLoaded();
    document.querySelector("#preview-panel").scrollIntoView({ behavior: "smooth", block: "start" });
  }
}

function ensurePreviewLoaded(force = false) {
  if (!state.previewUrl) {
    return;
  }
  if (!force && state.previewLoadedUrl === state.previewUrl) {
    return;
  }
  previewFrame.src = `${state.previewUrl}?t=${Date.now()}`;
  state.previewLoadedUrl = state.previewUrl;
}

function syncSummary(payload) {
  const waterSourceLabel = formatWaterSource(payload.water_source);
  metricWaterSource.textContent = payload.water_source === "euhydro" ? "EuHydro" : "Overpass";
  metricSize.textContent = `${payload.size_km} km`;
  metricTerrain.textContent = payload.terrain ? "On" : "Off";
  document.querySelector("#metric-hydrology").textContent = payload.river_metrics ? "On" : "Off";
  document.querySelector("#metric-risk").textContent = payload.water_risk ? payload.water_risk_mode : "Off";

  insightWaterSource.textContent = waterSourceLabel;
  insightRaster.textContent = payload.communities_raster || "Not set";
  insightThreshold.textContent = String(payload.community_threshold);
  insightMinArea.textContent = `${payload.min_community_area_m2} mÂ²`;
}

function setReportLink(link, report, unavailableMessage) {
  const available = Boolean(report?.available && report?.url);
  link.classList.toggle("is-disabled", !available);
  link.setAttribute("aria-disabled", String(!available));
  link.href = available ? report.url : "#";
  link.target = available ? "_blank" : "";
  link.rel = available ? "noopener" : "";
  link.title = available ? `Open or download ${report.label}.` : unavailableMessage;
}

async function loadStatus() {
  const response = await fetch("/api/status");
  const payload = await response.json();

  sizeInput.value = payload.defaults.size_km;
  waterSourceInput.value = payload.defaults.water_source;
  thresholdInput.value = payload.defaults.community_threshold;
  minAreaInput.value = payload.defaults.min_community_area_m2;
  communityMergeInput.value = payload.defaults.community_merge_distance_m;
  terrainResolutionInput.value = payload.defaults.terrain_resolution_m;

  metricsToggle.checked = payload.defaults.river_metrics;
  dischargeToggle.checked = payload.defaults.river_discharge;
  metricResInput.value = payload.defaults.river_metric_resolution_m;
  lookbackInput.value = payload.defaults.river_metric_lookback_days;

  stabilityToggle.checked = payload.defaults.stability;
  stabilityBufferInput.value = payload.defaults.stability_buffer_m;
  motionThresholdInput.value = payload.defaults.differential_motion_threshold;
  waterRiskToggle.checked = payload.defaults.water_risk;
  waterRiskModeInput.value = payload.defaults.water_risk_mode;
  farmDemandInput.value = payload.defaults.farm_demand_m3_day;
  glofasDaysInput.value = payload.defaults.glofas_days_back;

  syncSummary(readPayload());
  setReportLink(
    guidelineReportLink,
    payload.documents?.guideline,
    "The Romania legal guideline has not been generated yet."
  );
  setReportLink(
    caseStudyReportLink,
    payload.documents?.case_study,
    "The feasibility report will be available after processing."
  );

  if (payload.has_preview && payload.preview_url) {
    state.previewUrl = payload.preview_url;
    state.previewLoadedUrl = null;
    setStatus("Current preview is ready. Open the Preview tab when you want to load it.");
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
    community_merge_distance_m: Number(communityMergeInput.value),
    terrain: terrainToggle.checked,
    terrain_resolution_m: Number(terrainResolutionInput.value),
    river_metrics: metricsToggle.checked,
    river_discharge: dischargeToggle.checked,
    river_metric_resolution_m: Number(metricResInput.value),
    river_metric_lookback_days: Number(lookbackInput.value),
    stability: stabilityToggle.checked,
    stability_buffer_m: Number(stabilityBufferInput.value),
    differential_motion_threshold: Number(motionThresholdInput.value),
    water_risk: waterRiskToggle.checked,
    water_risk_mode: waterRiskModeInput.value,
    farm_demand_m3_day: Number(farmDemandInput.value),
    glofas_days_back: Number(glofasDaysInput.value),
  };
}

function validatePayload(payload) {
  if (!Number.isFinite(payload.lat) || !Number.isFinite(payload.lon)) {
    throw new Error("Enter valid numeric latitude and longitude.");
  }
  if (payload.lat < -90 || payload.lat > 90 || payload.lon < -180 || payload.lon > 180) {
    throw new Error("Latitude must be -90 to 90 and longitude must be -180 to 180.");
  }
  if (!Number.isFinite(payload.size_km) || payload.size_km <= 0) {
    throw new Error("AOI size must be a positive number.");
  }
  if (
    payload.water_risk &&
    payload.water_risk_mode === "farm" &&
    (!Number.isFinite(payload.farm_demand_m3_day) || payload.farm_demand_m3_day <= 0)
  ) {
    throw new Error("Farm demand must be greater than zero for farm risk mode.");
  }
}

async function generatePreview(event) {
  event.preventDefault();
  const payload = readPayload();
  try {
    validatePayload(payload);
  } catch (error) {
    setStatus(error.message, true);
    selectTab("map-panel");
    return;
  }
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
      state.previewLoadedUrl = result.index_url;
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
    ensurePreviewLoaded(true);
    setStatus("Current preview refreshed.");
  }
});
});

document.querySelector("#open-preview-tab").addEventListener("click", openCurrentPreview);
document.querySelector("#refresh-preview").addEventListener("click", () => {
  if (!state.previewUrl) {
    setStatus("There is no preview to refresh yet.", true);
    return;
  }
  ensurePreviewLoaded(true);
  setStatus("Preview frame refreshed.");
});
document.querySelector("#reset-btn").addEventListener("click", resetForm);
[guidelineReportLink, caseStudyReportLink].forEach((link) => {
  link.addEventListener("click", (event) => {
    if (link.getAttribute("aria-disabled") === "true") {
      event.preventDefault();
      setStatus(link.title, true);
    }
  });
});
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

[sizeInput, waterSourceInput, rasterInput, thresholdInput, minAreaInput, communityMergeInput, terrainToggle, metricsToggle, dischargeToggle, metricResInput, lookbackInput, stabilityToggle, stabilityBufferInput, motionThresholdInput, waterRiskToggle, waterRiskModeInput, farmDemandInput, glofasDaysInput].forEach((input) => {
  input.addEventListener("change", () => syncSummary(readPayload()));
});

setCoordinates(46.7712, 23.6236, { zoom: 8 });
loadStatus().catch((error) => setStatus(error.message, true));
