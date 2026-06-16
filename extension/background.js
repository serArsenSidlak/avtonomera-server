// Harvests available plates via the authenticated e-driver API and pushes them to the server,
// SCOPE BY SCOPE (region × type) so each request is bounded and data lands incrementally.
const SERVER = "https://34.123.136.171.nip.io";
const INGEST_SECRET = "ing__0VaRFvnOC57sc7baY6H0dMzKx21PNJD";
const BASE = "https://e-driver.mvs.gov.ua";
const TYPE_GROUPS = [
  { codes: [1000428, 3], label: "Легковий, вантажний" },
  { codes: [1000436], label: "Електромобіль" },
  { codes: [1000437], label: "Причіп" },
  { codes: [1000457], label: "Мотоцикл" },
  { codes: [1000458], label: "Електромотоцикл" },
  { codes: [1000443], label: "Мопед" },
  { codes: [1000444], label: "Електромопед" },
];
const NUMS = ["", "0", "1", "2", "3", "4", "5", "6", "7", "8", "9"];

function titleUA(s) {
  return (s || "").toLowerCase().replace(/(^|[\s\-.])([a-zа-яіїєґ'])/g, (m, p, c) => p + c.toUpperCase());
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const getToken = () => new Promise((r) => chrome.storage.local.get({ authToken: null }, (d) => r(d.authToken)));
const setStatus = (s) => chrome.storage.local.set({ status: s, statusTs: Date.now() });

async function api(path, method, body) {
  const t = await getToken();
  const headers = { "Content-Type": "application/json", "Accept": "application/json" };
  if (t) headers["Authorization"] = t;
  return fetch(BASE + path, { method: method || "GET", headers, body: body || undefined, credentials: "include" });
}

async function harvest() {
  setStatus("Старт… читаю регіони");
  let res;
  try { res = await api("/api/dictionaries/location/regions", "GET"); }
  catch (e) { setStatus("Помилка мережі: " + e); return; }
  if (res.status === 401) { setStatus("⛔ 401 — спершу залогінься на e-driver.mvs.gov.ua, відкрий «Бронювання НЗ», потім тисни тут знову"); return; }
  if (!res.ok) { setStatus("Помилка регіонів: HTTP " + res.status); return; }
  const regions = await res.json();
  let done = 0, grandTotal = 0; const total = regions.length * TYPE_GROUPS.length;
  for (const reg of regions) {
    const rname = titleUA(reg.name);
    for (const g of TYPE_GROUPS) {
      const scopeRows = [], seen = new Set();
      let scopeOk = false;
      for (const num of NUMS) {
        try {
          const r = await api("/api/plate/reserve", "POST", JSON.stringify({ reg: String(reg.id), type: g.codes, num }));
          if (r.ok) {
            const arr = await r.json();
            scopeOk = true;
            if (Array.isArray(arr)) {
              for (const p of arr) {
                const key = p.plate + "|" + (p.depName || "");
                if (seen.has(key)) continue; seen.add(key);
                scopeRows.push({ plate_number: p.plate, region: rname, tsc: p.depName || null,
                                 vehicle_type: g.label, price: p.cost ? parseFloat(p.cost) : null });
              }
              if (num === "" && arr.length > 0) break; // empty filter returned everything
            }
          }
        } catch (e) {}
        await sleep(250);
      }
      done++;
      if (scopeOk) {
        try {
          const pr = await fetch(SERVER + "/ingest", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ secret: INGEST_SECRET, rows: scopeRows, ok_scopes: [[rname, g.label]] }),
          });
          grandTotal += scopeRows.length;
          setStatus(`📤 ${rname} / ${g.label}: ${scopeRows.length} → сервер ${pr.status}. Всього ${grandTotal} · ${done}/${total}`);
        } catch (e) {
          setStatus(`⚠️ ${rname}/${g.label}: не надіслалось (${e}). ${done}/${total}`);
        }
      } else {
        setStatus(`• ${rname}/${g.label}: скоуп не вдався. ${done}/${total}`);
      }
    }
  }
  setStatus(`✅ Готово. Надіслано ~${grandTotal} номерів на сервер.`);
}

let running = false;
chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (msg && msg.type === "harvest") {
    if (running) { reply && reply({ already: true }); return false; }
    running = true;
    harvest().catch((e) => setStatus("Помилка: " + e)).finally(() => { running = false; });
    reply && reply({ started: true });
  }
  return false;
});
