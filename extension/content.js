// In-page gentle harvest (≤9/min) → pushes each scope to the server staging queue via background.
window.addEventListener("message", function (e) {
  if (e.source === window && e.data && e.data.__avtoAuth && e.data.auth) {
    try { chrome.storage.local.set({ authToken: e.data.auth }); } catch (x) {}
  }
});

const BASE = "https://e-driver.mvs.gov.ua";
const TYPE_GROUPS = [
  { codes: [1000428, 3], label: "Легковий, вантажний" },
  { codes: [1000436], label: "Електромобіль" },
];
const REQ_GAP_MS = 7000; // ~8.5 req/min, under the ≤9/min limit
function titleUA(s) { return (s || "").toLowerCase().replace(/(^|[\s\-.])([a-zа-яіїєґ'])/g, (m, p, c) => p + c.toUpperCase()); }
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const getToken = () => new Promise((r) => chrome.storage.local.get({ authToken: null }, (d) => r(d.authToken)));
const setStatus = (s) => chrome.storage.local.set({ status: s, statusTs: Date.now() });
const stage = (rows, reset, done) => new Promise((res) => chrome.runtime.sendMessage({ type: "stage", rows, reset, done }, (r) => res(r || {})));

let busy = false;
async function api(path, method, body) {
  const t = await getToken();
  const headers = { "Content-Type": "application/json", "Accept": "application/json" };
  if (t) headers["Authorization"] = t;
  return fetch(BASE + path, { method: method || "GET", headers, body: body || undefined, credentials: "include" });
}

async function harvest() {
  if (busy) return; busy = true;
  try {
    setStatus("▶️ Збір… читаю регіони");
    let res;
    try { res = await api("/api/dictionaries/location/regions", "GET"); }
    catch (e) { setStatus("Помилка мережі: " + e); return; }
    if (res.status === 401) { setStatus("⛔ 401 — відкрий кабінет, зроби 1 пошук, потім знову"); return; }
    if (!res.ok) { setStatus("Регіони HTTP " + res.status); return; }
    const regions = await res.json();
    await stage([], true, false); // reset the queue → fresh snapshot
    let done = 0, grand = 0; const total = regions.length * TYPE_GROUPS.length;
    for (const reg of regions) {
      const rname = titleUA(reg.name);
      for (const g of TYPE_GROUPS) {
        try {
          const r = await api("/api/plate/reserve", "POST", JSON.stringify({ reg: String(reg.id), type: g.codes, num: "" }));
          if (r.ok) {
            const arr = await r.json();
            const rows = (Array.isArray(arr) ? arr : []).map((p) => ({
              plate_number: p.plate, region: rname, tsc: p.depName || null,
              vehicle_type: g.label, price: p.cost ? parseFloat(p.cost) : null,
            }));
            if (rows.length) { await stage(rows, false, false); grand += rows.length; }
          }
        } catch (e) {}
        done++;
        setStatus(`🔎 Зібрано ${grand} · ${done}/${total} (${rname})`);
        await sleep(REQ_GAP_MS + Math.random() * 1500);
      }
    }
    await stage([], false, true); // done → server marks pending + notifies admin
    setStatus(`✅ Зібрано ${grand}. Надіслано в чергу — адмін підтвердить у боті.`);
  } finally { busy = false; }
}

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (msg && (msg.type === "harvest" || msg.type === "autoharvest")) {
    harvest().catch((e) => setStatus("Помилка: " + e));
    reply && reply({ started: true });
  }
  return false;
});
