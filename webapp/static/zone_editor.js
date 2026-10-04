/* Roof-type zone editor (MapLibre) */

(function () {
  const COLORS = [
    "#ef4444",
    "#f59e0b",
    "#84cc16",
    "#14b8a6",
    "#3b82f6",
    "#a855f7",
    "#ec4899",
    "#22c55e",
  ];

  function colorForType(t) {
    const n = Number(t) || 0;
    return COLORS[Math.abs(n) % COLORS.length];
  }

  function el(id) {
    return document.getElementById(id);
  }

  const state = {
    map: null,
    drawVerts: [],
    zones: { type: "FeatureCollection", features: [] },
    buildingsPath: null,
    imageryPath: null,
    nextId: 1,
    selectedZoneId: null,
    legendTypes: new Set(),
    cursorLngLat: null,
  };

  function status(msg) {
    const s = el("ze-status");
    if (s) s.textContent = msg || "";
  }

  function currentRoofType() {
    const v = parseInt(el("ze-roof-type").value, 10);
    return Number.isFinite(v) ? v : 1;
  }

  function rememberLegendType(t) {
    const n = Number(t);
    if (!Number.isFinite(n)) return;
    if (!state.legendTypes.has(n)) {
      state.legendTypes.add(n);
      refreshLegend();
    }
  }

  function refreshLegend() {
    const box = el("ze-legend");
    if (!box) return;
    const types = Array.from(state.legendTypes).sort((a, b) => a - b);
    box.innerHTML = "";
    box.classList.toggle("ze-legend-empty", types.length === 0);
    types.forEach((t) => {
      const row = document.createElement("div");
      row.className = "ze-legend-row";
      row.innerHTML =
        `<span class="ze-swatch" style="background:${colorForType(t)}"></span>` +
        `<span class="ze-legend-label">${t}</span>`;
      box.appendChild(row);
    });
  }

  function refreshZoneList() {
    const list = el("ze-zone-list");
    if (!list) return;
    list.innerHTML = "";
    state.zones.features.forEach((f, idx) => {
      const t = f.properties.roof_type;
      const id = f.properties.id;
      const selected =
        state.selectedZoneId != null &&
        Number(state.selectedZoneId) === Number(id);
      const row = document.createElement("div");
      row.className = "ze-zone-row" + (selected ? " ze-zone-row-selected" : "");
      row.dataset.id = String(id);
      row.innerHTML =
        `<span class="ze-swatch" style="background:${colorForType(t)}"></span>` +
        `<button type="button" class="ze-zone-pick" data-id="${id}">` +
        `zone ${idx + 1} · type ${t}</button>` +
        `<button type="button" data-id="${id}" class="ze-del" title="Remove zone">Remove</button>`;
      list.appendChild(row);
    });
    const count = el("ze-zone-count");
    if (count) count.textContent = String(state.zones.features.length);
  }

  function zonesGeoJSON() {
    return {
      type: "FeatureCollection",
      features: state.zones.features.slice(),
    };
  }

  function syncZonesToMap() {
    const src = state.map && state.map.getSource("zones");
    // New object each time — MapLibre can skip updates on same-reference mutate.
    if (src) src.setData(zonesGeoJSON());
    clearDraft();
    highlightSelectedZone();
  }

  function removeZoneById(id) {
    if (id == null || id === "") return false;
    const nid = Number(id);
    const before = state.zones.features.length;
    state.zones.features = state.zones.features.filter(
      (f) => Number(f.properties.id) !== nid
    );
    if (state.zones.features.length === before) return false;
    if (Number(state.selectedZoneId) === nid) state.selectedZoneId = null;
    syncZonesToMap();
    refreshZoneList();
    status(`Removed zone id=${nid}`);
    return true;
  }

  function selectZone(id) {
    if (id == null || id === "") {
      state.selectedZoneId = null;
    } else {
      state.selectedZoneId = Number(id);
    }
    refreshZoneList();
    highlightSelectedZone();
    if (state.selectedZoneId == null) status("Zone selection cleared");
    else
      status(
        `Selected zone id=${state.selectedZoneId} — press Delete or Remove`
      );
  }

  function highlightSelectedZone() {
    if (!state.map || !state.map.getLayer("zones-selected")) return;
    const id = state.selectedZoneId;
    if (id == null) {
      state.map.setFilter("zones-selected", ["==", ["get", "id"], -999999]);
      return;
    }
    state.map.setFilter("zones-selected", ["==", ["get", "id"], id]);
  }

  function removeSelectedZone() {
    if (state.selectedZoneId == null) {
      status("Select a zone in the list (or click it on the map) first");
      return;
    }
    removeZoneById(state.selectedZoneId);
  }

  function clearAllZones() {
    if (!state.zones.features.length) {
      status("No zones to clear");
      return;
    }
    state.zones.features = [];
    state.selectedZoneId = null;
    state.legendTypes.clear();
    refreshLegend();
    syncZonesToMap();
    refreshZoneList();
    status("All zones cleared");
  }

  function clearDraft() {
    state.drawVerts = [];
    state.cursorLngLat = null;
    if (state.map && state.map.getSource("draft")) {
      state.map.getSource("draft").setData({
        type: "FeatureCollection",
        features: [],
      });
    }
  }

  function updateDraft() {
    const verts = state.drawVerts;
    const cursor = state.cursorLngLat;
    const features = [];
    if (!state.map || !state.map.getSource("draft")) return;

    verts.forEach((c) => {
      features.push({
        type: "Feature",
        properties: { role: "vertex" },
        geometry: { type: "Point", coordinates: c },
      });
    });

    if (verts.length >= 2) {
      features.push({
        type: "Feature",
        properties: { role: "edge" },
        geometry: { type: "LineString", coordinates: verts },
      });
    }

    // Rubber-band preview toward the live cursor.
    if (verts.length >= 1 && cursor) {
      const last = verts[verts.length - 1];
      features.push({
        type: "Feature",
        properties: { role: "rubber" },
        geometry: {
          type: "LineString",
          coordinates: [last, cursor],
        },
      });
      if (verts.length >= 2) {
        features.push({
          type: "Feature",
          properties: { role: "rubber" },
          geometry: {
            type: "LineString",
            coordinates: [cursor, verts[0]],
          },
        });
        const ring = verts.concat([cursor, verts[0]]);
        features.push({
          type: "Feature",
          properties: { role: "preview" },
          geometry: { type: "Polygon", coordinates: [ring] },
        });
      }
    } else if (verts.length >= 3) {
      const ring = verts.concat([verts[0]]);
      features.push({
        type: "Feature",
        properties: { role: "preview" },
        geometry: { type: "Polygon", coordinates: [ring] },
      });
    }

    state.map.getSource("draft").setData({
      type: "FeatureCollection",
      features,
    });
  }

  function finishPolygon() {
    if (state.drawVerts.length < 3) {
      status("Need at least 3 vertices (click map).");
      return;
    }
    const ring = state.drawVerts.concat([state.drawVerts[0]]);
    const roof = currentRoofType();
    state.zones.features.push({
      type: "Feature",
      geometry: { type: "Polygon", coordinates: [ring] },
      properties: {
        id: state.nextId++,
        roof_type: roof,
        name: "",
        color: colorForType(roof),
      },
    });
    syncZonesToMap();
    refreshZoneList();
    rememberLegendType(roof);
    status(`Added zone with roof_type=${roof}`);
  }

  async function fetchJSON(url) {
    const r = await fetch(url);
    if (!r.ok) {
      let detail = r.statusText;
      try {
        const j = await r.json();
        detail = j.detail || JSON.stringify(j);
      } catch (_) {
        detail = await r.text();
      }
      throw new Error(detail || r.statusText);
    }
    return r.json();
  }

  function featureLngLat(f) {
    try {
      const g = f.geometry;
      if (!g) return null;
      if (g.type === "Point") return g.coordinates;
      if (g.type === "Polygon") return g.coordinates[0][0];
      if (g.type === "MultiPolygon") return g.coordinates[0][0][0];
    } catch (_) {}
    return null;
  }

  /** Prefer a dense local view — fitting the full AOI often zooms out until footprints vanish. */
  function focusBuildings(geojson, bbox) {
    const feats = (geojson && geojson.features) || [];
    if (!feats.length) return;
    const step = 0.008;
    const cells = new Map();
    for (let i = 0; i < feats.length; i++) {
      const c = featureLngLat(feats[i]);
      if (!c) continue;
      const key =
        Math.floor(c[0] / step) + "," + Math.floor(c[1] / step);
      let cell = cells.get(key);
      if (!cell) {
        cell = { n: 0, sx: 0, sy: 0 };
        cells.set(key, cell);
      }
      cell.n += 1;
      cell.sx += c[0];
      cell.sy += c[1];
    }
    let best = null;
    cells.forEach((cell) => {
      if (!best || cell.n > best.n) best = cell;
    });
    const span =
      bbox &&
      Math.max(bbox[2] - bbox[0], bbox[3] - bbox[1]);
    if (best && (span == null || span > 0.04 || state.map.getZoom() < 13)) {
      state.map.easeTo({
        center: [best.sx / best.n, best.sy / best.n],
        zoom: 15.5,
        duration: 800,
      });
      return;
    }
    if (bbox) {
      state.map.fitBounds(
        [
          [bbox[0], bbox[1]],
          [bbox[2], bbox[3]],
        ],
        { padding: 48, maxZoom: 17, duration: 700 }
      );
    }
  }

  async function loadBuildings(path) {
    if (!path) {
      if (state.map && state.map.getSource("buildings")) {
        state.map.getSource("buildings").setData({
          type: "FeatureCollection",
          features: [],
        });
      }
      status("No buildings selected");
      return;
    }
    if (!state.map || !state.map.getSource("buildings")) {
      status("Map not ready yet — pick buildings again in a moment");
      return;
    }
    status("Loading buildings…");
    try {
      // Full-resolution rings (no server simplify). MapLibre source tolerance
      // is also 0 so zoom-out does not dissolve footprints into mush.
      const q = new URLSearchParams({
        path,
        simplify: "0",
        properties: "false",
      });
      const data = await fetchJSON("/api/zones/geojson?" + q.toString());
      state.buildingsPath = path;
      state.map.getSource("buildings").setData(data.geojson);
      state.map.resize();
      focusBuildings(data.geojson, data.bbox);
      status(
        `Buildings: ${data.count} polygons (full res) — click map to draw`
      );
    } catch (err) {
      status("Buildings failed: " + String(err));
    }
  }

  async function loadZones(path) {
    if (!path) {
      state.zones = { type: "FeatureCollection", features: [] };
      state.selectedZoneId = null;
      syncZonesToMap();
      refreshZoneList();
      return;
    }
    status("Loading zones…");
    const q = new URLSearchParams({ path, properties: "true" });
    const data = await fetchJSON("/api/zones/geojson?" + q.toString());
    const feats = (data.geojson.features || []).map((f, i) => {
      const p = f.properties || {};
      let roof = p.roof_type;
      if (roof === undefined || roof === null || roof === "") {
        for (const k of Object.keys(p)) {
          if (k.toLowerCase() === "roof_type") {
            roof = p[k];
            break;
          }
        }
      }
      const rt = parseInt(roof, 10) || 1;
      return {
        type: "Feature",
        geometry: f.geometry,
        properties: {
          id: p.id != null ? p.id : i + 1,
          roof_type: rt,
          name: p.name || "",
          color: colorForType(rt),
        },
      };
    });
    state.zones = { type: "FeatureCollection", features: feats };
    state.nextId =
      feats.reduce((m, f) => Math.max(m, Number(f.properties.id) || 0), 0) + 1;
    state.selectedZoneId = null;
    feats.forEach((f) => rememberLegendType(f.properties.roof_type));
    syncZonesToMap();
    refreshZoneList();
    status(`Zones: ${feats.length}`);
  }

  function setImagery(path) {
    state.imageryPath = path || null;
    const srcId = "imagery";
    if (state.map.getLayer("imagery")) state.map.removeLayer("imagery");
    if (state.map.getSource(srcId)) state.map.removeSource(srcId);
    if (!path) {
      status("Imagery off (OSM basemap only)");
      return;
    }
    const url =
      "/api/zones/tiles/{z}/{x}/{y}.png?path=" + encodeURIComponent(path);
    state.map.addSource(srcId, {
      type: "raster",
      tiles: [url],
      tileSize: 256,
      attribution: "Local GeoTIFF",
    });
    state.map.addLayer(
      {
        id: "imagery",
        type: "raster",
        source: srcId,
        paint: { "raster-opacity": 0.92 },
      },
      "buildings-fill"
    );
    status("Imagery tiles streaming for current view");
  }

  async function saveZones() {
    if (!state.zones.features.length) {
      status("No zones to save");
      return;
    }
    const name = (el("ze-save-name").value || "roof_zones").trim();
    status("Saving…");
    const r = await fetch("/api/zones/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        geojson: state.zones,
        name,
        tier: el("ze-save-tier").value || "final",
      }),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || JSON.stringify(data));
    status(`Saved ${data.count} zones → ${data.path}`);
    el("ze-saved-path").textContent = data.path;
  }

  function fillSelect(sel, choices, selectedPath) {
    sel.innerHTML = "";
    const real = choices || [];
    if (!real.length) {
      const none = document.createElement("option");
      none.value = "";
      none.textContent = "(none)";
      sel.appendChild(none);
      return;
    }
    // Keep "(none)" as the closed value, but hide it from the open list.
    const none = document.createElement("option");
    none.value = "";
    none.textContent = "(none)";
    none.hidden = true;
    sel.appendChild(none);
    real.forEach((c) => {
      const opt = document.createElement("option");
      opt.value = c.path;
      opt.textContent = c.label;
      if (selectedPath && c.path === selectedPath) opt.selected = true;
      sel.appendChild(opt);
    });
    if (!(selectedPath && real.some((c) => c.path === selectedPath))) {
      sel.value = "";
    }
  }

  function waitForMapLibre(timeoutMs) {
    return new Promise((resolve, reject) => {
      const start = Date.now();
      (function tick() {
        if (typeof maplibregl !== "undefined") return resolve();
        if (Date.now() - start > timeoutMs) {
          return reject(new Error("MapLibre failed to load (CDN blocked?)"));
        }
        setTimeout(tick, 50);
      })();
    });
  }

  async function init() {
    try {
      status("Starting map…");
      await waitForMapLibre(10000);
      const layers = await fetchJSON("/api/zones/layers");
      fillSelect(el("ze-buildings"), layers.buildings, null);
      fillSelect(el("ze-zones"), layers.zones, null);
      const imageryChoices = []
        .concat(layers.imagery_folders || [])
        .concat(layers.imagery_files || []);
      fillSelect(el("ze-imagery"), imageryChoices, null);
      if (!layers.rasterio) {
        status("rasterio missing — imagery tiles unavailable");
      }

      state.map = new maplibregl.Map({
        container: "ze-map",
        style: {
          version: 8,
          sources: {
            osm: {
              type: "raster",
              tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
              tileSize: 256,
              attribution: "© OpenStreetMap",
            },
          },
          layers: [
            {
              id: "osm",
              type: "raster",
              source: "osm",
              paint: { "raster-opacity": 0.55 },
            },
          ],
        },
        center: [35.26, 32.23],
        zoom: 13,
      });
      window.__zeMap = state.map;
      state.map.addControl(new maplibregl.NavigationControl(), "top-right");

      state.map.on("load", async () => {
        state.map.resize();
        state.map.addSource("buildings", {
          type: "geojson",
          data: { type: "FeatureCollection", features: [] },
          // Keep ring detail across zooms (geojson-vt defaults are aggressive).
          tolerance: 0,
          buffer: 128,
          maxzoom: 20,
        });
        state.map.addLayer({
          id: "buildings-fill",
          type: "fill",
          source: "buildings",
          paint: {
            "fill-color": "#f97316",
            "fill-opacity": [
              "interpolate",
              ["linear"],
              ["zoom"],
              8,
              0.85,
              12,
              0.65,
              16,
              0.5,
              18,
              0.4,
            ],
            "fill-outline-color": "#fdba74",
          },
        });
        state.map.addLayer({
          id: "buildings-line",
          type: "line",
          source: "buildings",
          paint: {
            "line-color": "#fff7ed",
            "line-opacity": 0.95,
            // Thicker when zoomed out so footprints stay readable.
            "line-width": [
              "interpolate",
              ["linear"],
              ["zoom"],
              8,
              2.8,
              11,
              2.2,
              14,
              1.6,
              17,
              1.2,
            ],
          },
        });

        state.map.addSource("zones", {
          type: "geojson",
          data: state.zones,
        });
        state.map.addLayer({
          id: "zones-fill",
          type: "fill",
          source: "zones",
          paint: {
            "fill-color": ["coalesce", ["get", "color"], "#f59e0b"],
            "fill-opacity": 0.35,
          },
        });
        state.map.addLayer({
          id: "zones-line",
          type: "line",
          source: "zones",
          paint: { "line-color": "#f8fafc", "line-width": 2 },
        });
        state.map.addLayer({
          id: "zones-selected",
          type: "line",
          source: "zones",
          filter: ["==", ["get", "id"], -999999],
          paint: {
            "line-color": "#f472b6",
            "line-width": 3.5,
          },
        });

        state.map.addSource("draft", {
          type: "geojson",
          data: { type: "FeatureCollection", features: [] },
        });
        state.map.addLayer({
          id: "draft-fill",
          type: "fill",
          source: "draft",
          filter: ["==", ["geometry-type"], "Polygon"],
          paint: { "fill-color": "#fbbf24", "fill-opacity": 0.22 },
        });
        state.map.addLayer({
          id: "draft-line",
          type: "line",
          source: "draft",
          filter: [
            "all",
            ["==", ["geometry-type"], "LineString"],
            ["!=", ["get", "role"], "rubber"],
          ],
          paint: {
            "line-color": "#fbbf24",
            "line-width": 2,
            "line-dasharray": [2, 1],
          },
        });
        state.map.addLayer({
          id: "draft-rubber",
          type: "line",
          source: "draft",
          filter: ["==", ["get", "role"], "rubber"],
          paint: {
            "line-color": "#fb7185",
            "line-width": 1.8,
            "line-dasharray": [1.5, 1.5],
          },
        });
        state.map.addLayer({
          id: "draft-pts",
          type: "circle",
          source: "draft",
          filter: ["==", ["geometry-type"], "Point"],
          paint: {
            "circle-radius": 4,
            "circle-color": "#fbbf24",
            "circle-stroke-width": 1,
            "circle-stroke-color": "#111",
          },
        });

        state.map.on("click", (e) => {
          // While drawing, always place vertices (don't steal clicks for select).
          if (!state.drawVerts.length) {
            const hits = state.map.queryRenderedFeatures(e.point, {
              layers: ["zones-fill", "zones-line", "zones-selected"],
            });
            if (hits.length) {
              const id = hits[0].properties && hits[0].properties.id;
              selectZone(id);
              return;
            }
          }
          state.cursorLngLat = [e.lngLat.lng, e.lngLat.lat];
          state.drawVerts.push([e.lngLat.lng, e.lngLat.lat]);
          updateDraft();
          status(
            `Vertex ${state.drawVerts.length} — Enter/Finish to close (≥3)`
          );
        });

        state.map.on("mousemove", (e) => {
          if (!state.drawVerts.length) return;
          state.cursorLngLat = [e.lngLat.lng, e.lngLat.lat];
          updateDraft();
        });
        state.map.on("mouseout", () => {
          if (!state.drawVerts.length) return;
          state.cursorLngLat = null;
          updateDraft();
        });

        const redCursor =
          "url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='24' height='24' viewBox='0 0 24 24'%3E%3Cpath d='M12 1v8M12 15v8M1 12h8M15 12h8' stroke='%23ef4444' stroke-width='2.2' stroke-linecap='round'/%3E%3Ccircle cx='12' cy='12' r='2.2' fill='%23ef4444'/%3E%3C/svg%3E\") 12 12, crosshair";
        state.map.getCanvas().style.cursor = redCursor;
        window.addEventListener("resize", () => {
          if (state.map) state.map.resize();
        });

        status("");
      });

      const zoneList = el("ze-zone-list");
      if (zoneList) {
        zoneList.addEventListener("click", (ev) => {
          const t = ev.target;
          if (!(t instanceof Element)) return;
          const del = t.closest(".ze-del");
          if (del) {
            ev.preventDefault();
            ev.stopPropagation();
            removeZoneById(del.getAttribute("data-id"));
            return;
          }
          const pick = t.closest(".ze-zone-pick");
          if (pick) {
            ev.preventDefault();
            selectZone(pick.getAttribute("data-id"));
          }
        });
      }
      const clearBtn = el("ze-clear-zones");
      if (clearBtn) {
        clearBtn.addEventListener("click", (ev) => {
          ev.preventDefault();
          clearAllZones();
        });
      }
      const removeSelBtn = el("ze-remove-selected");
      if (removeSelBtn) {
        removeSelBtn.addEventListener("click", (ev) => {
          ev.preventDefault();
          removeSelectedZone();
        });
      }

      el("ze-buildings").addEventListener("change", (e) => {
        loadBuildings(e.target.value);
      });
      el("ze-zones").addEventListener("change", (e) => {
        loadZones(e.target.value).catch((err) => status(String(err)));
      });
      el("ze-imagery").addEventListener("change", (e) => {
        try {
          setImagery(e.target.value);
        } catch (err) {
          status(String(err));
        }
      });
      el("ze-finish").addEventListener("click", finishPolygon);
      el("ze-cancel").addEventListener("click", () => {
        clearDraft();
        status("Draft cleared");
      });
      el("ze-save").addEventListener("click", () => {
        saveZones().catch((err) => status(String(err)));
      });
      window.addEventListener("keydown", (ev) => {
        const tag = (ev.target && ev.target.tagName) || "";
        const typing =
          tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
        if (ev.key === "Enter" && !typing) finishPolygon();
        else if (ev.key === "Escape") {
          if (state.selectedZoneId != null) {
            selectZone(null);
          } else {
            clearDraft();
            status("Draft cleared");
          }
        } else if (
          !typing &&
          (ev.key === "Delete" || ev.key === "Backspace")
        ) {
          if (state.selectedZoneId != null) {
            ev.preventDefault();
            removeSelectedZone();
          }
        }
      });
    } catch (err) {
      status("Init failed: " + String(err));
      console.error(err);
    }
  }

  window.qtZoneEditor = { init };
})();
