"use strict";

const SPECTRUM_BINS = 1024;
const SPECTRUM_FLOOR_DB = -120;
const SPECTRUM_CEILING_DB = 0;
const WATERFALL_COLOR_SPAN_DB = 100;
const WATERFALL_ROWS = 420;
const WATERFALL_COLUMNS = 1024;
const WATERFALL_INTERP_ROWS = 0;
const WATERFALL_ROW_MS = 280;
const WATERFALL_IDLE_MS = 700;
const POSITION_ORIGIN = { lat: 45.4642, lon: 9.19 };
const STATIONARY_COLOR = "#858b92";
const STATIONARY_SPEED_KT = 2.0;
const ALTITUDE_COLOR_STOPS = [
  { at: 0.0, rgb: [72, 118, 255] },
  { at: 0.28, rgb: [38, 224, 137] },
  { at: 0.68, rgb: [255, 68, 68] },
  { at: 1.0, rgb: [184, 88, 255] },
];

const state = {
  map: null,
  vectorRenderer: null,
  tracks: new Map(),
  selectedTrackKey: null,
  selectedTrack: null,
  aircraftDetailsDrawId: null,
  waterfalls: {
    adsb: { rows: [], lastRow: null, pendingRow: null, lastUpdateMs: 0, buffer: document.createElement("canvas") },
    flarm: { rows: [], lastRow: null, pendingRow: null, lastUpdateMs: 0, buffer: document.createElement("canvas") },
  },
  lastStats: null,
  gainSendTimer: null,
  lastIq: [],
};

const els = {};

document.addEventListener("DOMContentLoaded", () => {
  bindElements();
  initMap();
  initControls();
  initWaterfallTicker();
  initStream();
  requestInitialStatus();
  window.addEventListener("resize", () => {
    drawSpectrum([], els.spectrum);
    drawSpectrum([], els.flarmSpectrum);
    drawWaterfall("adsb");
    drawWaterfall("flarm");
    drawDecodeCircle(state.lastStats || {});
    drawIQScope(state.lastIq);
    scheduleAircraftDetailsDraw();
  });
  scheduleAircraftDetailsDraw(null);
  drawIQScope([]);
});

function bindElements() {
  [
    "rxState",
    "sourceName",
    "msgRate",
    "trackCount",
    "rfConfig",
    "noiseFloor",
    "spectrum",
    "waterfall",
    "flarmSpectrum",
    "flarmWaterfall",
    "decodeCircle",
    "aircraftDetails",
    "crcOk",
    "crcBad",
    "gainReadout",
    "clipReadout",
    "trackList",
    "lastUpdate",
    "mapFallback",
    "connectPlutoBtn",
    "simModeBtn",
    "controlStatus",
    "gainSlider",
    "gainSliderValue",
    "flarmRfConfig",
    "flarmNoiseFloor",
    "protoAdsb",
    "protoFlarm",
    "iqScope",
    "iqInfo",
  ].forEach((id) => {
    els[id] = document.getElementById(id);
  });
}

function initWaterfallTicker() {
  window.setInterval(() => {
    const now = Date.now();
    ["adsb", "flarm"].forEach((key) => {
      const wf = state.waterfalls[key];
      if (wf.pendingRow && now - wf.lastUpdateMs >= WATERFALL_ROW_MS) {
        commitWaterfallRow(key, wf.pendingRow, now);
        wf.pendingRow = null;
        return;
      }
      if (!wf.lastRow) return;
      if (now - wf.lastUpdateMs < WATERFALL_IDLE_MS) return;
      const faded = wf.lastRow.map((value) => Math.max(SPECTRUM_FLOOR_DB, value - 3));
      commitWaterfallRow(key, faded, now);
    });
  }, 80);
}

function initMap() {
  if (!window.L) {
    els.mapFallback.hidden = false;
    return;
  }

  state.vectorRenderer = L.svg({ padding: 0.5 });
  state.map = L.map("map", {
    dragging: true,
    scrollWheelZoom: true,
    doubleClickZoom: true,
    boxZoom: true,
    keyboard: true,
    touchZoom: true,
    zoomControl: true,
    preferCanvas: false,
    renderer: state.vectorRenderer,
  }).setView([45.4642, 9.19], 7);

  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 18,
    attribution: "&copy; OpenStreetMap",
  }).addTo(state.map);

  enableMapPointerInput();

}

function enableMapPointerInput() {
  if (!state.map) return;
  const container = state.map.getContainer();
  container.style.pointerEvents = "auto";
  container.style.cursor = "grab";

  ["mapPane", "tilePane"].forEach((name) => {
    const pane = state.map.getPane(name);
    if (pane) pane.style.pointerEvents = "auto";
  });

  ["overlayPane", "shadowPane", "markerPane", "tooltipPane", "popupPane"].forEach((name) => {
    const pane = state.map.getPane(name);
    if (pane) pane.style.pointerEvents = "none";
  });

  state.map.dragging.enable();
  state.map.scrollWheelZoom.enable();
  state.map.doubleClickZoom.enable();
  state.map.boxZoom.enable();
  state.map.keyboard.enable();
  state.map.touchZoom.enable();
}

function initControls() {
  els.connectPlutoBtn?.addEventListener("click", () => {
    startSource("pluto");
  });
  els.simModeBtn?.addEventListener("click", () => {
    startSource("sim");
  });
  els.gainSlider?.addEventListener("input", () => {
    const gain = Number(els.gainSlider.value);
    setGainLabel(gain);
    if (state.gainSendTimer) window.clearTimeout(state.gainSendTimer);
    state.gainSendTimer = window.setTimeout(() => sendGain(gain), 120);
  });
  [els.protoAdsb, els.protoFlarm].forEach((box) => {
    box?.addEventListener("change", onProtocolChange);
  });
  applyProtocolVisibility(readProtocolSelection());
}

function readProtocolSelection() {
  const protocols = [];
  if (els.protoAdsb?.checked) protocols.push("adsb");
  if (els.protoFlarm?.checked) protocols.push("flarm");
  return protocols;
}

function onProtocolChange(event) {
  let protocols = readProtocolSelection();
  // Almeno un protocollo deve restare attivo.
  if (protocols.length === 0) {
    if (event?.target) event.target.checked = true;
    protocols = readProtocolSelection();
  }
  applyProtocolVisibility(protocols);
  sendProtocols(protocols);
}

function applyProtocolVisibility(protocols) {
  const enabled = new Set(protocols);
  document.querySelectorAll(".instrument[data-protocol]").forEach((panel) => {
    panel.hidden = !enabled.has(panel.dataset.protocol);
  });
}

async function sendProtocols(protocols) {
  try {
    const response = await fetch("/api/protocol", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      cache: "no-store",
      body: JSON.stringify({ protocols }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || payload.ok === false) {
      throw new Error(payload.error || `Errore HTTP ${response.status}`);
    }
    if (payload.stats) updateStats(payload.stats);
  } catch (error) {
    if (els.controlStatus) els.controlStatus.textContent = String(error.message || error);
  }
}

function syncProtocolControls(stats) {
  const enabled = Array.isArray(stats.enabled_protocols) ? stats.enabled_protocols : null;
  if (!enabled) return;
  const set = new Set(enabled);
  // Non sovrascrivere mentre l'utente sta interagendo con la checkbox.
  if (els.protoAdsb && document.activeElement !== els.protoAdsb) els.protoAdsb.checked = set.has("adsb");
  if (els.protoFlarm && document.activeElement !== els.protoFlarm) els.protoFlarm.checked = set.has("flarm");
  applyProtocolVisibility(enabled);
}

async function sendGain(gain) {
  try {
    const response = await fetch("/api/gain", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      cache: "no-store",
      body: JSON.stringify({ gain }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || payload.ok === false) {
      throw new Error(payload.error || `Errore HTTP ${response.status}`);
    }
    if (payload.stats) updateStats(payload.stats);
    if (payload.gain_db !== undefined) setGainLabel(Number(payload.gain_db));
  } catch (error) {
    if (els.controlStatus) els.controlStatus.textContent = String(error.message || error);
  }
}

function setGainLabel(gain) {
  if (els.gainSliderValue) els.gainSliderValue.textContent = `${formatNumber(gain, 1)} dB`;
}

async function startSource(source) {
  const isReconnect = source === "pluto" && String(state.lastStats?.source || "") === "pluto";
  const label = source === "pluto" ? "PlutoSDR" : "simulazione";
  setControlsBusy(true);
  els.controlStatus.textContent = `${isReconnect ? "Riconnessione" : "Connessione"} ${label}`;

  try {
    const response = await fetch(`/api/connect/${source}`, {
      method: "POST",
      cache: "no-store",
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || payload.ok === false) {
      throw new Error(payload.error || `Errore HTTP ${response.status}`);
    }
    if (payload.stats) updateStats(payload.stats);
    els.controlStatus.textContent = payload.message || `${label} attiva`;
  } catch (error) {
    els.controlStatus.textContent = String(error.message || error);
  } finally {
    setControlsBusy(false);
    updateSourceControls(state.lastStats || {});
  }
}

function setControlsBusy(isBusy) {
  if (els.connectPlutoBtn) els.connectPlutoBtn.disabled = isBusy;
  if (els.simModeBtn) els.simModeBtn.disabled = isBusy;
}

function initStream() {
  const events = new EventSource("/events");

  events.onopen = () => {
    els.rxState.textContent = "LINK";
  };

  events.onerror = () => {
    els.rxState.textContent = "WAIT";
  };

  events.onmessage = (event) => {
    try {
      const payload = JSON.parse(event.data);
      handleSnapshot(payload);
    } catch (error) {
      console.error("Snapshot non valido", error);
    }
  };
}

async function requestInitialStatus() {
  try {
    const response = await fetch("/api/status", { cache: "no-store" });
    if (!response.ok) return;
    const payload = await response.json();
    updateStats(payload.stats || {});
    drawSpectrum(payload.spectrum || [], els.spectrum);
    pushWaterfall(payload.waterfall || [], "adsb");
    drawSpectrum(payload.flarm_spectrum || [], els.flarmSpectrum);
    pushWaterfall(payload.flarm_waterfall || [], "flarm");
    drawIQScope(payload.iq || []);
  } catch (_error) {
    // Lo stream SSE aggiornera lo stato appena disponibile.
  }
}

function handleSnapshot(payload) {
  if (!payload || payload.type !== "snapshot") return;
  updateStats(payload.stats || {});
  updateTracks(payload.tracks || []);
  drawSpectrum(payload.spectrum || [], els.spectrum);
  pushWaterfall(payload.waterfall || [], "adsb");
  drawSpectrum(payload.flarm_spectrum || [], els.flarmSpectrum);
  pushWaterfall(payload.flarm_waterfall || [], "flarm");
  drawIQScope(payload.iq || []);
  drawDecodeCircle(payload.stats || {});
  els.lastUpdate.textContent = formatTime(payload.time);
}

function updateStats(stats) {
  state.lastStats = stats;
  els.rxState.textContent = String(stats.rx_state || "RUN").toUpperCase();
  els.sourceName.textContent = String(stats.source || "-").toUpperCase();
  els.msgRate.textContent = String(stats.messages_per_sec ?? 0);
  els.trackCount.textContent = String(stats.active_tracks ?? 0);
  els.crcOk.textContent = String(stats.crc_ok ?? 0);
  els.crcBad.textContent = String(stats.crc_bad ?? 0);
  els.noiseFloor.textContent = `NF ${formatNumber(stats.noise_floor, 1)}`;

  const sampleRate = Number(stats.sample_rate || 0) / 1_000_000;
  const bandwidth = Number(stats.rf_bandwidth || 0) / 1_000_000;
  const activeFrequency = stats.active_frequency ? ` · ${formatFrequency(stats.active_frequency)}` : "";
  els.rfConfig.textContent = `${formatNumber(sampleRate, 1)} MS/s · ${formatNumber(bandwidth, 1)} MHz${activeFrequency}`;

  const gain = stats.gain_db;
  const gainMode = stats.gain_mode || "-";
  els.gainReadout.textContent = gain === null || gain === undefined ? gainMode : `${formatNumber(gain, 1)} dB ${gainMode}`;
  if (gain !== null && gain !== undefined && els.gainSlider && document.activeElement !== els.gainSlider) {
    els.gainSlider.value = String(gain);
    setGainLabel(Number(gain));
  }
  els.clipReadout.textContent = `${formatNumber((stats.clip_ratio || 0) * 100, 3)}%`;
  if (els.flarmRfConfig) {
    const active = String(stats.active_protocol || "").toUpperCase();
    const flarmFrequency = stats.flarm_frequency ? ` · ${formatFrequency(stats.flarm_frequency)}` : "";
    let rxLabel = stats.rx1_active ? "RX1 LIVE" : "RX1 OFF";
    if (stats.rx0_scan) rxLabel = `RX0 SCAN ${active || "-"}`;
    els.flarmRfConfig.textContent = `${rxLabel}${flarmFrequency} · BURST ${stats.flarm_bursts_total ?? 0}`;
  }
  if (els.flarmNoiseFloor) els.flarmNoiseFloor.textContent = `FLARM ${stats.flarm_messages_total ?? 0}`;
  syncProtocolControls(stats);
  updateSourceControls(stats);
}

function updateSourceControls(stats) {
  const source = String(stats.source || "");
  const rxState = String(stats.rx_state || "");
  const isConnecting = rxState === "connecting" || rxState === "starting";
  if (els.connectPlutoBtn) {
    els.connectPlutoBtn.classList.toggle("active", source === "pluto");
    els.connectPlutoBtn.textContent = source === "pluto" ? "RICONNETTI PLUTO" : "CONNETTI PLUTO";
    els.connectPlutoBtn.disabled = isConnecting;
  }
  if (els.simModeBtn) {
    els.simModeBtn.classList.toggle("active", source === "sim");
    els.simModeBtn.disabled = isConnecting || source === "sim";
  }
  if (!els.controlStatus) return;
  if (stats.last_error) {
    els.controlStatus.textContent = stats.last_error;
  } else if (rxState === "running" && source === "pluto") {
    els.controlStatus.textContent = "Pluto connessa";
  } else if (rxState === "running" && source === "sim") {
    els.controlStatus.textContent = "Simulazione attiva";
  } else {
    els.controlStatus.textContent = rxState || "Pronto";
  }
}

function updateTracks(tracks) {
  const seen = new Set();

  tracks.forEach((track) => {
    if (!Number.isFinite(track.lat) || !Number.isFinite(track.lon)) return;
    const key = trackKey(track);
    seen.add(key);

    const existing = state.tracks.get(key);
    if (existing) {
      updateTrackLayer(existing, track);
    } else {
      state.tracks.set(key, createTrackLayer(track));
    }
  });

  for (const [icao, layer] of state.tracks.entries()) {
    if (!seen.has(icao)) {
      removeTrackLayer(layer);
      state.tracks.delete(icao);
    }
  }

  if (state.selectedTrackKey && !seen.has(state.selectedTrackKey)) {
    selectTrack(null);
  }
}

function trackKey(track) {
  return `${track.protocol || "adsb"}:${track.icao}`;
}

function createTrackLayer(track) {
  const position = L.latLng(track.lat, track.lon);
  const trailPoints = normalizeTrail(track);
  const trail = L.layerGroup().addTo(state.map);
  updateTrailLayer(trail, track, trailPoints);

  const target = L.circleMarker(position, {
    ...targetStyle(track, state.selectedTrackKey === trackKey(track), trailPoints),
    className: "aircraft-target",
    renderer: state.vectorRenderer,
    interactive: true,
    bubblingMouseEvents: false,
  }).addTo(state.map);

  target.bindTooltip(trackLabelHtml(track), {
    permanent: true,
    direction: "right",
    offset: [8, 0],
    className: trackLabelClass(track),
    interactive: false,
  });

  const layer = { target, trail, offsets: null, track };
  target.on("click", (event) => {
    if (event.originalEvent) {
      L.DomEvent.stopPropagation(event.originalEvent);
      L.DomEvent.preventDefault(event.originalEvent);
    }
    selectTrack(layer.track);
  });
  updateTargetClass(layer);
  evaluateDisplayOffsets(layer, position, trailPoints);
  return layer;
}

function updateTrackLayer(layer, track) {
  const position = L.latLng(track.lat, track.lon);
  const trailPoints = normalizeTrail(track);
  layer.track = track;
  updateTrailLayer(layer.trail, track, trailPoints);
  layer.target.setLatLng(position);
  layer.target.setStyle(targetStyle(track, state.selectedTrackKey === trackKey(track), trailPoints));
  if (layer.target.getTooltip()) layer.target.setTooltipContent(trackLabelHtml(track));
  updateTargetClass(layer);
  if (state.selectedTrackKey === trackKey(track)) {
    state.selectedTrack = track;
    scheduleAircraftDetailsDraw(track);
  }
  evaluateDisplayOffsets(layer, position, trailPoints);
}

function removeTrackLayer(layer) {
  if (!state.map) return;
  state.map.removeLayer(layer.trail);
  state.map.removeLayer(layer.target);
}

function trackLabelHtml(track) {
  const callsign = String(track.callsign || track.icao || "-").trim().toUpperCase() || "-";
  const altitude = formatAltitudeMeters(track.altitude_ft);
  return `<strong>${escapeHtml(callsign)}</strong><span>${escapeHtml(altitude)}</span>`;
}

function trackLabelClass(track) {
  return `aircraft-label-tooltip${String(track.protocol || "adsb") === "flarm" ? " flarm" : ""}`;
}

function targetStyle(track, selected = false, trailPoints = null) {
  const color = trackColor(track, trailPoints);
  return {
    radius: selected ? 8 : 5,
    color,
    weight: selected ? 2.5 : 1.5,
    fillColor: color,
    fillOpacity: selected ? 0.48 : 0.28,
    opacity: 0.95,
  };
}

function updateTargetClass(layer) {
  const element = layer.target.getElement?.();
  if (!element || !layer.track) return;
  element.classList.toggle("selected", state.selectedTrackKey === trackKey(layer.track));
}

function selectTrack(track) {
  const previousKey = state.selectedTrackKey;
  if (!track) {
    state.selectedTrackKey = null;
    state.selectedTrack = null;
    const previousLayer = previousKey ? state.tracks.get(previousKey) : null;
    if (previousLayer) {
      previousLayer.target.setStyle(targetStyle(previousLayer.track, false));
      updateTargetClass(previousLayer);
    }
    scheduleAircraftDetailsDraw(null);
    return;
  }

  const key = trackKey(track);
  state.selectedTrackKey = key;
  state.selectedTrack = track;

  if (previousKey && previousKey !== key) {
    const previousLayer = state.tracks.get(previousKey);
    if (previousLayer) {
      previousLayer.target.setStyle(targetStyle(previousLayer.track, false));
      updateTargetClass(previousLayer);
    }
  }

  const currentLayer = state.tracks.get(key);
  if (currentLayer) {
    currentLayer.target.setStyle(targetStyle(track, true));
    updateTargetClass(currentLayer);
  }
  scheduleAircraftDetailsDraw(track);
}

function updateTrailLayer(layerGroup, track, trailPoints = normalizeTrail(track)) {
  layerGroup.clearLayers();
  if (!state.map || trailPoints.length < 2) return;
  for (let index = 1; index < trailPoints.length; index += 1) {
    const previous = trailPoints[index - 1];
    const point = trailPoints[index];
    const color = trailSegmentColor(track, previous, point);
    L.polyline([previous.latLng, point.latLng], {
      color,
      weight: 2.4,
      opacity: 0.82,
      smoothFactor: 0,
      noClip: true,
      interactive: false,
      renderer: state.vectorRenderer,
      lineCap: "round",
      lineJoin: "round",
    }).addTo(layerGroup);
  }
}

function normalizeTrail(track) {
  const current = L.latLng(track.lat, track.lon);
  const currentAltitude = normalizeAltitude(track.altitude_ft);
  const points = [];
  (track.trail || []).forEach((rawPoint) => {
    const point = normalizeTrailPoint(rawPoint, current);
    if (!point) return;
    const previous = points[points.length - 1];
    if (!previous || previous.latLng.distanceTo(point.latLng) > 0.5) points.push(point);
    else if (Number.isFinite(point.altitude_ft)) previous.altitude_ft = point.altitude_ft;
  });

  const last = points[points.length - 1];
  if (!last || last.latLng.distanceTo(current) > 0.5) {
    points.push({ latLng: current, altitude_ft: currentAltitude });
  } else if (Number.isFinite(currentAltitude)) {
    last.altitude_ft = currentAltitude;
  }
  return points;
}

function normalizeTrailPoint(rawPoint, current) {
  if (!rawPoint) return null;
  const values = Array.isArray(rawPoint)
    ? rawPoint
    : [
        rawPoint.lat ?? rawPoint.latitude,
        rawPoint.lng ?? rawPoint.lon ?? rawPoint.longitude,
        rawPoint.altitude_ft ?? rawPoint.altitude ?? rawPoint.alt,
      ];
  if (values.length < 2) return null;

  const first = Number(values[0]);
  const second = Number(values[1]);
  const altitude = normalizeAltitude(values[2]);
  if (!Number.isFinite(first) || !Number.isFinite(second)) return null;

  const normal = isValidLatLng(first, second) ? L.latLng(first, second) : null;
  const swapped = isValidLatLng(second, first) ? L.latLng(second, first) : null;
  if (normal && swapped && current) {
    const latLng = swapped.distanceTo(current) + 1 < normal.distanceTo(current) ? swapped : normal;
    return { latLng, altitude_ft: altitude };
  }
  const latLng = normal || swapped;
  return latLng ? { latLng, altitude_ft: altitude } : null;
}

function isValidLatLng(lat, lon) {
  return lat >= -90 && lat <= 90 && lon >= -180 && lon <= 180;
}

function normalizeAltitude(value) {
  if (value === null || value === undefined || value === "") return null;
  const altitude = Number(value);
  return Number.isFinite(altitude) ? altitude : null;
}

function trackColor(track, trailPoints = null) {
  if (isStationaryTrack(track, trailPoints)) return STATIONARY_COLOR;
  return altitudeColor(track.altitude_ft);
}

function trailSegmentColor(track, previous, point) {
  if (isStationaryTrack(track, [previous, point])) return STATIONARY_COLOR;
  return altitudeColor(point.altitude_ft ?? track.altitude_ft);
}

function isStationaryTrack(track, trailPoints = null) {
  const speed = Number(track?.speed_kt);
  if (Number.isFinite(speed)) return speed <= STATIONARY_SPEED_KT;

  const points = trailPoints || normalizeTrail(track);
  if (!points || points.length < 2) return false;
  const recent = points.slice(-4);
  const first = recent[0].latLng;
  const last = recent[recent.length - 1].latLng;
  return first.distanceTo(last) < 15;
}

function altitudeColor(value) {
  const altitude = normalizeAltitude(value);
  if (!Number.isFinite(altitude)) return STATIONARY_COLOR;
  const t = Math.max(0, Math.min(1, altitude / 45000));
  for (let index = 1; index < ALTITUDE_COLOR_STOPS.length; index += 1) {
    const previous = ALTITUDE_COLOR_STOPS[index - 1];
    const next = ALTITUDE_COLOR_STOPS[index];
    if (t <= next.at) {
      const mix = (t - previous.at) / Math.max(0.0001, next.at - previous.at);
      return rgbToHex(interpolateRgb(previous.rgb, next.rgb, mix));
    }
  }
  return rgbToHex(ALTITUDE_COLOR_STOPS[ALTITUDE_COLOR_STOPS.length - 1].rgb);
}

function interpolateRgb(a, b, mix) {
  return [
    Math.round(a[0] * (1 - mix) + b[0] * mix),
    Math.round(a[1] * (1 - mix) + b[1] * mix),
    Math.round(a[2] * (1 - mix) + b[2] * mix),
  ];
}

function rgbToHex(rgb) {
  return `#${rgb.map((value) => value.toString(16).padStart(2, "0")).join("")}`;
}

function evaluateDisplayOffsets(layer, position, trailPoints) {
  if (!state.map || !trailPoints.length) return;
  const targetPoint = state.map.latLngToLayerPoint(position);
  const trailEndPoint = state.map.latLngToLayerPoint(trailPoints[trailPoints.length - 1].latLng);
  const targetLatLng = layer.target.getLatLng();
  const targetVsTrailPx = targetPoint.distanceTo(trailEndPoint);
  const targetVsLayerPx = targetPoint.distanceTo(state.map.latLngToLayerPoint(targetLatLng));
  layer.offsets = {
    targetVsTrailPx,
    targetVsLayerPx,
    trailPoints: trailPoints.length,
  };
  if (targetVsTrailPx > 1.5 || targetVsLayerPx > 1.5) {
    console.warn("Offset mappa", layer.offsets);
  }
}

function renderTrackList(tracks) {
  const sorted = [...tracks].sort((a, b) => String(a.icao).localeCompare(String(b.icao)));
  if (sorted.length === 0) {
    els.trackList.innerHTML = `<div class="track-row"><span>Nessun contatto</span><span></span><span class="muted">-</span></div>`;
    return;
  }

  els.trackList.innerHTML = sorted
    .map((track) => {
      const callsign = track.callsign || "-";
      const age = Number(track.last_position_age || 0);
      return `<div class="track-row">
        <strong>${escapeHtml(track.icao || "-")}</strong>
        <span>${escapeHtml(callsign)} · ${escapeHtml(formatAltitude(track.altitude_ft))}</span>
        <span class="muted">${formatNumber(age, 0)}s</span>
      </div>`;
    })
    .join("");
}

function drawSpectrum(values, canvas = els.spectrum) {
  if (!canvas) return;
  const ctx = setupCanvas(canvas);
  const width = canvas.width;
  const height = canvas.height;
  const row = values.length ? values : new Array(SPECTRUM_BINS).fill(SPECTRUM_FLOOR_DB);

  ctx.clearRect(0, 0, width, height);
  drawGrid(ctx, width, height);

  ctx.beginPath();
  row.forEach((db, index) => {
    const x = (index / Math.max(1, row.length - 1)) * width;
    const y = mapDbToY(db, height);
    if (index === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.strokeStyle = "#39ff88";
  ctx.lineWidth = 2;
  ctx.stroke();

  ctx.fillStyle = "rgba(242, 245, 248, 0.72)";
  ctx.font = `${12 * devicePixelRatio}px ui-monospace, SFMono-Regular, Menlo, monospace`;
  ctx.fillText(`${SPECTRUM_FLOOR_DB} dB`, 10 * devicePixelRatio, height - 10 * devicePixelRatio);
  ctx.fillText(`${SPECTRUM_CEILING_DB} dB`, 10 * devicePixelRatio, 18 * devicePixelRatio);
}

function pushWaterfall(values, key = "adsb") {
  if (!values.length) return;
  const wf = state.waterfalls[key] || state.waterfalls.adsb;
  const row = resampleRow(values, WATERFALL_COLUMNS);
  const now = Date.now();
  wf.pendingRow = row;
  if (now - wf.lastUpdateMs < WATERFALL_ROW_MS) return;
  commitWaterfallRow(key, row, now);
  wf.pendingRow = null;
}

function commitWaterfallRow(key, row, nowMs = Date.now()) {
  const wf = state.waterfalls[key] || state.waterfalls.adsb;
  if (wf.lastRow) {
    for (let step = 1; step <= WATERFALL_INTERP_ROWS; step += 1) {
      const mix = step / (WATERFALL_INTERP_ROWS + 1);
      wf.rows.unshift(interpolateRows(wf.lastRow, row, mix));
    }
  }
  wf.rows.unshift(row);
  wf.lastRow = row;
  wf.lastUpdateMs = nowMs;
  if (wf.rows.length > WATERFALL_ROWS) wf.rows.length = WATERFALL_ROWS;
  drawWaterfall(key);
}

function drawWaterfall(key = "adsb") {
  const canvas = key === "flarm" ? els.flarmWaterfall : els.waterfall;
  if (!canvas) return;
  const wf = state.waterfalls[key] || state.waterfalls.adsb;
  const ctx = setupCanvas(canvas);
  const width = canvas.width;
  const height = canvas.height;
  ctx.fillStyle = "#06080a";
  ctx.fillRect(0, 0, width, height);

  const rows = wf.rows;
  if (!rows.length) return;

  const image = ctx.createImageData(WATERFALL_COLUMNS, WATERFALL_ROWS);
  const data = image.data;
  for (let y = 0; y < WATERFALL_ROWS; y += 1) {
    const row = rows[y] || rows[rows.length - 1];
    for (let x = 0; x < WATERFALL_COLUMNS; x += 1) {
      const [r, g, b] = colorForDb(row[x]);
      const offset = (y * WATERFALL_COLUMNS + x) * 4;
      data[offset] = r;
      data[offset + 1] = g;
      data[offset + 2] = b;
      data[offset + 3] = 255;
    }
  }

  const offscreen = wf.buffer;
  if (offscreen.width !== WATERFALL_COLUMNS) offscreen.width = WATERFALL_COLUMNS;
  if (offscreen.height !== WATERFALL_ROWS) offscreen.height = WATERFALL_ROWS;
  offscreen.getContext("2d").putImageData(image, 0, 0);
  // Upscale bilineare verso il canvas ad alta densita: niente blocchi "clippati".
  ctx.imageSmoothingEnabled = true;
  ctx.imageSmoothingQuality = "high";
  ctx.drawImage(offscreen, 0, 0, width, height);
}

function resampleRow(values, size) {
  if (values.length === size) return values.map(Number);
  const output = new Array(size);
  const maxInput = Math.max(1, values.length - 1);
  const maxOutput = Math.max(1, size - 1);
  for (let index = 0; index < size; index += 1) {
    const position = (index / maxOutput) * maxInput;
    const left = Math.floor(position);
    const right = Math.min(values.length - 1, left + 1);
    const mix = position - left;
    output[index] = Number(values[left]) * (1 - mix) + Number(values[right]) * mix;
  }
  return output;
}

function interpolateRows(a, b, mix) {
  const output = new Array(Math.min(a.length, b.length));
  for (let index = 0; index < output.length; index += 1) {
    output[index] = a[index] * (1 - mix) + b[index] * mix;
  }
  return output;
}

function drawDecodeCircle(stats) {
  const canvas = els.decodeCircle;
  if (!canvas) return;
  const ctx = setupCanvas(canvas);
  const width = canvas.width;
  const height = canvas.height;
  const cx = width / 2;
  const cy = height / 2;
  const radius = Math.min(width, height) * 0.39;
  const rate = Number(stats.messages_per_sec || 0);
  const ratio = Math.max(0, Math.min(1, rate / 35));
  const crcRatio = Number(stats.crc_ratio || 0);

  ctx.clearRect(0, 0, width, height);

  ctx.strokeStyle = "rgba(139, 148, 158, 0.26)";
  ctx.lineWidth = 2 * devicePixelRatio;
  ctx.beginPath();
  ctx.arc(cx, cy, radius, 0, Math.PI * 2);
  ctx.stroke();

  ctx.strokeStyle = "#71d9ff";
  ctx.lineWidth = 8 * devicePixelRatio;
  ctx.lineCap = "round";
  ctx.beginPath();
  ctx.arc(cx, cy, radius, -Math.PI / 2, -Math.PI / 2 + ratio * Math.PI * 2);
  ctx.stroke();

  ctx.strokeStyle = crcRatio > 0.3 ? "#8dffcb" : "#ffb86b";
  ctx.lineWidth = 2 * devicePixelRatio;
  ctx.beginPath();
  ctx.arc(cx, cy, radius * 0.72, -Math.PI / 2, -Math.PI / 2 + Math.max(0.03, crcRatio) * Math.PI * 2);
  ctx.stroke();

  ctx.fillStyle = "#f2f5f8";
  ctx.textAlign = "center";
  ctx.font = `${24 * devicePixelRatio}px ui-monospace, SFMono-Regular, Menlo, monospace`;
  ctx.fillText(String(rate), cx, cy + 3 * devicePixelRatio);
  ctx.fillStyle = "rgba(139, 148, 158, 0.95)";
  ctx.font = `${10 * devicePixelRatio}px ui-monospace, SFMono-Regular, Menlo, monospace`;
  ctx.fillText("MSG/S", cx, cy + 24 * devicePixelRatio);
}

function drawIQScope(points) {
  const canvas = els.iqScope;
  if (!canvas) return;
  if (Array.isArray(points) && points.length) state.lastIq = points;
  const samples = state.lastIq;
  const ctx = setupCanvas(canvas);
  const width = canvas.width;
  const height = canvas.height;
  const ratio = window.devicePixelRatio || 1;

  ctx.fillStyle = "#06080a";
  ctx.fillRect(0, 0, width, height);

  const cx = width / 2;
  const cy = height / 2;
  const radius = Math.min(width, height) * 0.46;

  // Griglia: assi I/Q e cerchi di ampiezza.
  ctx.strokeStyle = "rgba(139, 148, 158, 0.18)";
  ctx.lineWidth = 1 * ratio;
  ctx.beginPath();
  ctx.moveTo(cx - radius, cy);
  ctx.lineTo(cx + radius, cy);
  ctx.moveTo(cx, cy - radius);
  ctx.lineTo(cx, cy + radius);
  ctx.stroke();
  [0.5, 1.0].forEach((scale) => {
    ctx.beginPath();
    ctx.arc(cx, cy, radius * scale, 0, Math.PI * 2);
    ctx.stroke();
  });

  if (samples && samples.length) {
    ctx.fillStyle = "rgba(57, 255, 136, 0.62)";
    const dot = 1.4 * ratio;
    for (let index = 0; index < samples.length; index += 1) {
      const point = samples[index];
      const i = Number(point[0]);
      const q = Number(point[1]);
      if (!Number.isFinite(i) || !Number.isFinite(q)) continue;
      const x = cx + i * radius;
      const y = cy - q * radius;
      ctx.beginPath();
      ctx.arc(x, y, dot, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  ctx.fillStyle = "rgba(139, 148, 158, 0.95)";
  ctx.font = `${10 * ratio}px ui-monospace, SFMono-Regular, Menlo, monospace`;
  ctx.textAlign = "left";
  ctx.textBaseline = "alphabetic";
  ctx.fillText("I", cx + radius - 10 * ratio, cy - 6 * ratio);
  ctx.fillText("Q", cx + 6 * ratio, cy - radius + 12 * ratio);

  if (els.iqInfo) els.iqInfo.textContent = `${samples ? samples.length : 0} pt`;
}

function scheduleAircraftDetailsDraw(track = state.selectedTrack) {
  if (state.aircraftDetailsDrawId) {
    window.cancelAnimationFrame(state.aircraftDetailsDrawId);
  }
  state.aircraftDetailsDrawId = window.requestAnimationFrame(() => {
    state.aircraftDetailsDrawId = null;
    drawAircraftDetails(track);
  });
}

function drawAircraftDetails(track) {
  const canvas = els.aircraftDetails;
  if (!canvas) return;
  const ctx = setupCanvas(canvas);
  const width = canvas.width;
  const height = canvas.height;
  const ratio = window.devicePixelRatio || 1;
  const pad = 18 * ratio;
  const top = 18 * ratio;
  const bottom = height - 16 * ratio;

  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = "rgba(6, 8, 10, 0.98)";
  ctx.fillRect(0, 0, width, height);

  ctx.strokeStyle = "rgba(42, 48, 54, 0.95)";
  ctx.lineWidth = 1 * ratio;
  ctx.beginPath();
  ctx.moveTo(0, 0.5 * ratio);
  ctx.lineTo(width, 0.5 * ratio);
  ctx.stroke();

  if (!track) {
    drawAircraftPlaceholder(ctx, width, height, ratio, pad);
    return;
  }

  const accent = trackColor(track);
  const fields = aircraftDetailFields(track);
  const usableWidth = Math.max(1, width - pad * 2);
  const cellWidth = usableWidth / fields.length;

  ctx.fillStyle = accent;
  ctx.fillRect(pad, top, 3 * ratio, bottom - top);

  fields.forEach((field, index) => {
    const x = pad + 14 * ratio + index * cellWidth;
    const maxWidth = Math.max(48 * ratio, cellWidth - 18 * ratio);

    if (index > 0) {
      ctx.strokeStyle = "rgba(42, 48, 54, 0.78)";
      ctx.beginPath();
      ctx.moveTo(pad + index * cellWidth, top);
      ctx.lineTo(pad + index * cellWidth, bottom);
      ctx.stroke();
    }

    ctx.fillStyle = "rgba(139, 148, 158, 0.98)";
    ctx.font = `${10 * ratio}px ui-monospace, SFMono-Regular, Menlo, monospace`;
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    drawFittedText(ctx, field.label, x, top + 14 * ratio, maxWidth);

    ctx.fillStyle = field.strong ? accent : "#f2f5f8";
    ctx.font = `${(field.strong ? 20 : 17) * ratio}px ui-monospace, SFMono-Regular, Menlo, monospace`;
    drawFittedText(ctx, field.value, x, top + 48 * ratio, maxWidth);
  });
}

function drawAircraftPlaceholder(ctx, width, height, ratio, pad) {
  ctx.fillStyle = "rgba(139, 148, 158, 0.72)";
  ctx.font = `${13 * ratio}px ui-monospace, SFMono-Regular, Menlo, monospace`;
  ctx.textAlign = "left";
  ctx.textBaseline = "middle";
  ctx.fillText("NESSUN AEREO", pad, height / 2);
}

function aircraftDetailFields(track) {
  const protocol = String(track.protocol || "adsb").toUpperCase();
  const callsign = String(track.callsign || "-").trim() || "-";
  const rssi = track.rssi === null || track.rssi === undefined ? "-" : `${formatNumber(track.rssi, 1)} dB`;
  const lastSeen = `${formatNumber(track.last_seen_age, 1)} S`;
  const lastPosition = `${formatNumber(track.last_position_age, 1)} S`;
  const trailPoints = Array.isArray(track.trail) ? track.trail.length : 0;
  const position = positionMeters(track);
  const speed = formatSpeedMetersPerSecond(track.speed_kt, track.speed_source);
  const vertical = formatVerticalMetersPerSecond(track.vertical_ft_min, track.vertical_source);
  return [
    { label: "PROT", value: protocol },
    { label: "ICAO", value: String(track.icao || "-").toUpperCase(), strong: true },
    { label: "CALL", value: callsign.toUpperCase() },
    { label: "QUOTA M", value: formatAltitudeMeters(track.altitude_ft) },
    { label: "POS M", value: position ? `E${formatSignedMeters(position.east)} N${formatSignedMeters(position.north)}` : "-" },
    { label: "VEL M/S", value: speed },
    { label: "V VERT", value: vertical },
    { label: "RSSI", value: rssi },
    { label: "MSG", value: lastSeen },
    { label: "ETA POS", value: lastPosition },
    { label: "SCIA", value: `${trailPoints} PT` },
  ];
}

function positionMeters(track) {
  const lat = Number(track?.lat);
  const lon = Number(track?.lon);
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) return null;
  const originLatRad = (POSITION_ORIGIN.lat * Math.PI) / 180;
  const latRad = (lat * Math.PI) / 180;
  const metersPerDegLat = 111132.92 - 559.82 * Math.cos(2 * latRad) + 1.175 * Math.cos(4 * latRad);
  const metersPerDegLon = 111412.84 * Math.cos(originLatRad) - 93.5 * Math.cos(3 * originLatRad);
  return {
    east: (lon - POSITION_ORIGIN.lon) * metersPerDegLon,
    north: (lat - POSITION_ORIGIN.lat) * metersPerDegLat,
  };
}

function formatSignedMeters(value) {
  const meters = Number(value);
  if (!Number.isFinite(meters)) return "-";
  const rounded = Math.round(meters);
  return `${rounded >= 0 ? "+" : ""}${rounded}`;
}

function formatSpeedMetersPerSecond(speedKt, source = "") {
  const value = Number(speedKt);
  if (!Number.isFinite(value)) return "-";
  return `${source === "estimated" ? "~" : ""}${formatNumber(value * 0.514444, 1)} M/S`;
}

function formatVerticalMetersPerSecond(verticalFtMin, source = "") {
  const value = Number(verticalFtMin);
  if (!Number.isFinite(value)) return "-";
  return `${source === "estimated" ? "~" : ""}${formatNumber(value * 0.00508, 1)} M/S`;
}

function drawFittedText(ctx, text, x, y, maxWidth) {
  const value = String(text ?? "-");
  if (ctx.measureText(value).width <= maxWidth) {
    ctx.fillText(value, x, y);
    return;
  }

  const ellipsis = "...";
  let output = value;
  while (output.length > 0 && ctx.measureText(`${output}${ellipsis}`).width > maxWidth) {
    output = output.slice(0, -1);
  }
  ctx.fillText(output ? `${output}${ellipsis}` : ellipsis, x, y);
}

function setupCanvas(canvas) {
  const ratio = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  const attrWidth = Number(canvas.getAttribute("width")) || 300;
  const attrHeight = Number(canvas.getAttribute("height")) || 120;
  const cssWidth = rect.width || canvas.clientWidth || attrWidth / ratio;
  const cssHeight = rect.height || canvas.clientHeight || attrHeight / ratio;
  const width = Math.max(1, Math.round(cssWidth * ratio));
  const height = Math.max(1, Math.round(cssHeight * ratio));
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
  }
  return canvas.getContext("2d");
}

function drawGrid(ctx, width, height) {
  ctx.strokeStyle = "rgba(139, 148, 158, 0.16)";
  ctx.lineWidth = 1;
  for (let i = 1; i < 4; i += 1) {
    const y = (height / 4) * i;
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(width, y);
    ctx.stroke();
  }
  for (let i = 1; i < 6; i += 1) {
    const x = (width / 6) * i;
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, height);
    ctx.stroke();
  }
}

function mapDbToY(db, height) {
  const span = SPECTRUM_CEILING_DB - SPECTRUM_FLOOR_DB;
  const value = Math.max(SPECTRUM_FLOOR_DB, Math.min(SPECTRUM_CEILING_DB, Number(db)));
  return height - ((value - SPECTRUM_FLOOR_DB) / span) * height;
}

function colorForDb(db) {
  // Mappa assoluta: floor (-120 dBFS) -> nero, ~-20 dBFS -> rosso pieno.
  const value = Math.max(0, Math.min(1, (Number(db) - SPECTRUM_FLOOR_DB) / WATERFALL_COLOR_SPAN_DB));
  const stops = [
    [0.0, 0, 0, 24],
    [0.25, 0, 28, 150],
    [0.48, 0, 190, 255],
    [0.68, 255, 230, 0],
    [0.84, 255, 78, 0],
    [1.0, 255, 0, 0],
  ];

  for (let index = 1; index < stops.length; index += 1) {
    const prev = stops[index - 1];
    const next = stops[index];
    if (value <= next[0]) {
      const mix = (value - prev[0]) / Math.max(0.0001, next[0] - prev[0]);
      return [
        Math.round(prev[1] * (1 - mix) + next[1] * mix),
        Math.round(prev[2] * (1 - mix) + next[2] * mix),
        Math.round(prev[3] * (1 - mix) + next[3] * mix),
      ];
    }
  }
  return [255, 0, 0];
}

function formatAltitude(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "ALT -";
  return `${Math.round(Number(value))} FT`;
}

function formatAltitudeMeters(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  return `${Math.round(Number(value) * 0.3048)} M`;
}

function formatNumber(value, digits) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "-";
  return number.toFixed(digits);
}

function formatTime(epochSeconds) {
  const value = Number(epochSeconds);
  if (!Number.isFinite(value)) return "-";
  return new Date(value * 1000).toLocaleTimeString();
}

function formatFrequency(value) {
  const hz = Number(value);
  if (!Number.isFinite(hz)) return "-";
  return `${formatNumber(hz / 1_000_000, 3)} MHz`;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
