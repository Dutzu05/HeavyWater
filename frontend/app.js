const presets = {
  cluj: { lat: 46.7712, lon: 23.6236, label: "Cluj-Napoca" },
  danube: { lat: 45.1524, lon: 29.6531, label: "Danube Delta" },
  iasi: { lat: 47.1585, lon: 27.6014, label: "Iasi" },
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
const previewFrame = document.querySelector("#preview-frame");
const statusText = document.querySelector("#status-text");
const liveCoords = document.querySelector("#live-coords");
const generateBtn = document.querySelector("#generate-btn");

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
}

function setStatus(message, isError = false) {
  statusText.textContent = message;
  statusText.style.color = isError ? "#8f1d14" : "";
}

async function loadStatus() {
  const response = await fetch("/api/status");
  const payload = await response.json();

  sizeInput.value = payload.defaults.size_km;
  waterSourceInput.value = payload.defaults.water_source;
  thresholdInput.value = payload.defaults.community_threshold;
  minAreaInput.value = payload.defaults.min_community_area_m2;
  terrainResolutionInput.value = payload.defaults.terrain_resolution_m;

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
  };
}

async function generatePreview(event) {
  event.preventDefault();
  const payload = readPayload();
  generateBtn.disabled = true;
  generateBtn.textContent = "Generating...";
  setStatus(`Generating preview for ${payload.lat.toFixed(5)}, ${payload.lon.toFixed(5)}...`);

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
    state.previewUrl = result.index_url;
    previewFrame.src = `${result.index_url}?t=${Date.now()}`;
    setStatus(`Preview ready for ${result.lat.toFixed(5)}, ${result.lon.toFixed(5)}.`);
  } catch (error) {
    setStatus(error.message, true);
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
  setStatus("Planner reset to the default location.");
}

document.querySelectorAll("[data-preset]").forEach((button) => {
  button.addEventListener("click", () => {
    const preset = presets[button.dataset.preset];
    setCoordinates(preset.lat, preset.lon, { zoom: 10 });
    setStatus(`Preset loaded: ${preset.label}.`);
  });
});

document.querySelectorAll("[data-scroll-target]").forEach((button) => {
  button.addEventListener("click", () => {
    const target = document.querySelector(button.dataset.scrollTarget);
    target?.scrollIntoView({ behavior: "smooth", block: "start" });
  });
});

document.querySelector("#load-current-preview").addEventListener("click", () => {
  document.querySelector(".preview-card").scrollIntoView({ behavior: "smooth", block: "start" });
  if (state.previewUrl) {
    previewFrame.src = `${state.previewUrl}?t=${Date.now()}`;
    setStatus("Current preview refreshed.");
  }
});

document.querySelector("#use-map-center").addEventListener("click", () => {
  const center = map.getCenter();
  setCoordinates(center.lat, center.lng, { pan: false });
  setStatus("Map center applied to the coordinate inputs.");
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

setCoordinates(46.7712, 23.6236, { zoom: 8 });
loadStatus().catch((error) => setStatus(error.message, true));
