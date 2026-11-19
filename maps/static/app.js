"use strict";

const REFRESH_MS = 20000;
const COLOR = { off: "var(--off)", on: "var(--on)", unknown: "var(--unk)" };
const ORDER = { off: 0, unknown: 1, on: 2 };
const LABEL = { off: "Без света", on: "Со светом", unknown: "Нет ответа сегодня" };

const wide = () => matchMedia("(min-width:761px)").matches;
const sheetH = () => Math.round(innerHeight * 0.45);
const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};

const map = L.map("map", { zoomControl: false }).setView([44.95, 34.1], 9);
L.control.zoom({ position: "bottomright", zoomInTitle: "Приблизить", zoomOutTitle: "Отдалить" }).addTo(map);
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 18, attribution: "© OpenStreetMap" }).addTo(map);

const markers = new Map(); // code -> { marker, status }
let shops = [];
let filter = "all";
let query = "";
let selected = null;
let firstLoad = true;

function duration(minutes) {
  const min = Math.max(0, Number(minutes) || 0);
  if (min < 60) return `${min} мин`;
  const h = Math.floor(min / 60), r = min % 60;
  return r ? `${h} ч ${r} мин` : `${h} ч`;
}

function shortDuration(minutes) {
  const min = Math.max(0, Number(minutes) || 0);
  return min < 60 ? `${min} мин` : min < 1440 ? `${Math.floor(min / 60)} ч` : `${Math.floor(min / 1440)} дн`;
}

function sinceText(iso) {
  const m = /(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/.exec(iso || "");
  return m ? `с ${m[3]}.${m[2]} в ${m[4]}:${m[5]}` : "";
}

const validCoordinates = (s) =>
  Number.isFinite(+s.lat) && Number.isFinite(+s.lon) && Math.abs(+s.lat) <= 90 && Math.abs(+s.lon) <= 180;

const matches = (s) =>
  (filter === "all" || s.status === filter) &&
  (!query || `${s.code} ${s.address || ""}`.toLowerCase().includes(query));

// ---------- маркеры и попапы ----------

function pinIcon(status) {
  const n = status === "off" ? 26 : 16;
  const pin = el("span", `pin ${status}`);
  pin.style.setProperty("--c", COLOR[status]);
  return L.divIcon({ html: pin, className: "", iconSize: [n, n], iconAnchor: [n / 2, n / 2] });
}

function popupNode(s) {
  const box = el("div", "pop");
  box.style.setProperty("--c", COLOR[s.status]);
  const st = el("div", "pop-st");
  st.append(el("span", "pop-lbl", LABEL[s.status]));
  if (s.status === "off") {
    st.append(el("b", "pop-big", duration(s.minutes)));
    const t = sinceText(s.since);
    if (t) st.append(el("small", "", t));
  }
  box.append(el("div", "pop-code", `Магазин №${s.code}`), el("div", "pop-addr", s.address || "Адрес не указан"), st);
  return box;
}

function popupOptions() {
  return {
    maxWidth: 280,
    autoPanPaddingTopLeft: wide() ? [400, 20] : [20, 20],
    autoPanPaddingBottomRight: [20, wide() ? 20 : sheetH()],
  };
}

function setSelected(code, scroll = false) {
  selected = code;
  document.querySelectorAll(".shop").forEach((b) => {
    const on = b.dataset.code === code;
    b.classList.toggle("sel", on);
    if (on && scroll) b.scrollIntoView({ block: "nearest" });
  });
}

function createMarker(s) {
  const marker = L.marker([+s.lat, +s.lon], { icon: pinIcon(s.status), title: `№${s.code}` });
  // Клик по метке открывает/закрывает попап сам Leaflet (bindPopup), свой click не нужен.
  marker.bindPopup(popupNode(s), popupOptions());
  marker.on("popupopen", () => setSelected(s.code, true));
  marker.on("popupclose", () => { if (selected === s.code) setSelected(null); });
  return { marker, status: s.status };
}

function updateMarker(item, s) {
  item.marker.setLatLng([+s.lat, +s.lon]);
  if (item.status !== s.status) {
    item.marker.setIcon(pinIcon(s.status));
    item.status = s.status;
  }
  item.marker.setPopupContent(popupNode(s));
}

// ---------- карта и панель ----------

function fitAll() {
  const pts = shops.filter(matches).map((s) => [+s.lat, +s.lon]);
  if (!pts.length) return;
  map.fitBounds(L.latLngBounds(pts), {
    paddingTopLeft: wide() ? [400, 40] : [30, 40],
    paddingBottomRight: [40, wide() ? 40 : sheetH()],
    maxZoom: 13,
  });
}

function focusShop(code) {
  const item = markers.get(code);
  if (!item) return;
  if (!map.hasLayer(item.marker)) item.marker.addTo(map);
  const z = Math.max(map.getZoom(), 14);
  // сдвигаем центр так, чтобы точка оказалась в видимой части карты, а не под панелью
  const shift = wide() ? [190, 0] : [0, -Math.round(sheetH() / 2)];
  map.setView(map.unproject(map.project(item.marker.getLatLng(), z).subtract(shift), z), z);
  item.marker.openPopup();
  if (!wide()) $("panel").classList.remove("tall");
}

function renderList() {
  const list = $("list");
  const rows = shops.filter(matches);
  if (!rows.length) {
    list.replaceChildren(el("p", "empty", shops.length ? "Ничего не найдено" : "Нет данных"));
    return;
  }
  list.replaceChildren(
    ...rows.map((s) => {
      const btn = el("button", `shop ${s.status}` + (s.code === selected ? " sel" : ""));
      btn.type = "button";
      btn.dataset.code = s.code;
      btn.style.setProperty("--c", COLOR[s.status]);
      const body = el("div");
      body.append(el("strong", "", `№${s.code}`), el("small", "", s.address || "Адрес не указан"));
      btn.append(el("i", "dot"), body, el("span", "dur", s.status === "off" ? shortDuration(s.minutes) : ""));
      btn.addEventListener("click", () => focusShop(s.code));
      return btn;
    })
  );
}

function refresh() {
  for (const s of shops) {
    const item = markers.get(s.code);
    const visible = matches(s);
    if (visible && !map.hasLayer(item.marker)) item.marker.addTo(map);
    if (!visible && map.hasLayer(item.marker)) map.removeLayer(item.marker);
  }
  document.querySelectorAll("#filters button").forEach((b) =>
    b.setAttribute("aria-pressed", String(b.dataset.f === filter))
  );
  renderList();
}

function render(data) {
  shops = (Array.isArray(data.shops) ? data.shops : [])
    .map((s) => ({ ...s, code: String(s.code), status: COLOR[s.status] ? s.status : "unknown" }))
    .filter(validCoordinates)
    .sort(
      (a, b) =>
        ORDER[a.status] - ORDER[b.status] ||
        (Number(b.minutes) || 0) - (Number(a.minutes) || 0) ||
        a.code.localeCompare(b.code, "ru")
    );

  const count = { off: 0, on: 0, unknown: 0 };
  const seen = new Set();
  for (const s of shops) {
    count[s.status]++;
    seen.add(s.code);
    const item = markers.get(s.code);
    if (item) updateMarker(item, s);
    else markers.set(s.code, createMarker(s));
  }
  for (const [code, item] of markers) {
    if (!seen.has(code)) {
      map.removeLayer(item.marker);
      markers.delete(code);
      if (selected === code) selected = null;
    }
  }

  $("c-all").textContent = shops.length;
  $("c-off").textContent = count.off;
  $("c-on").textContent = count.on;
  $("c-unk").textContent = count.unknown;
  for (const i of $("bar").children) {
    const n = count[i.className];
    i.hidden = n === 0;
    i.style.setProperty("--n", n);
  }

  const date = new Date(data.updated);
  $("updated").textContent = Number.isNaN(date.getTime())
    ? "Обновлено"
    : "Обновлено в " + date.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  $("live").classList.remove("err");

  const noCoords = Array.isArray(data.no_coords) ? data.no_coords : [];
  $("note").hidden = noCoords.length === 0;
  $("note").textContent = noCoords.length ? `Нет координат у магазинов: ${noCoords.join(", ")}` : "";

  refresh();

  if (firstLoad && shops.length) {
    fitAll();
    firstLoad = false;
  }
}

async function load() {
  try {
    const res = await fetch("/api/shops", { cache: "no-store", credentials: "same-origin" });
    if (res.status === 401) { location.href = "/login"; return; }
    if (!res.ok) throw new Error(res.status);
    render(await res.json());
  } catch (e) {
    console.error("Ошибка загрузки магазинов:", e);
    $("updated").textContent = "Нет связи, повтор через 20 с";
    $("live").classList.add("err");
  }
}

// ---------- управление ----------

$("filters").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  filter = b.dataset.f;
  refresh();
});
$("q").addEventListener("input", (e) => {
  query = e.target.value.trim().toLowerCase();
  refresh();
});
$("fit").addEventListener("click", fitAll);
$("grip").addEventListener("click", () => $("panel").classList.toggle("tall"));

load();
setInterval(load, REFRESH_MS);