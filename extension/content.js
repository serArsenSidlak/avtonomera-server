// Token capture + controllable harvest (Start/Stop) that ACCUMULATES into a buffer; sending to
// the server is a separate explicit action. Runs in-page (same-origin authenticated context).
window.addEventListener("message", function (e) {
  if (e.source !== window || !e.data || !e.data.__avtoAuth) return;
  const a = e.data.entry && e.data.entry.auth;
  if (a) { try { chrome.storage.local.set({ authToken: a }); } catch (x) {} }
});

const BASE = "https://e-driver.mvs.gov.ua";
// For now: only cars/trucks + electric cars (per product decision).
const TYPE_GROUPS = [
  { codes: [1000428, 3], label: "Легковий, вантажний" },
  { codes: [1000436], label: "Електромобіль" },
];
const NUMS = [""]; // gentle: single request per scope (empty filter returns all)
function titleUA(s) {
  return (s || "").toLowerCase().replace(/(^|[\s\-.])([a-zа-яіїєґ'])/g, (m, p, c) => p + c.toUpperCase());
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const getToken = () => new Promise((r) => chrome.storage.local.get({ authToken: null }, (d) => r(d.authToken)));
const setStatus = (s) => chrome.storage.local.set({ status: s, statusTs: Date.now() });
const loadBuffer = () => new Promise((r) => chrome.storage.local.get({ buffer: {} }, (d) => r(d.buffer)));
const saveBuffer = (b) => new Promise((r) => chrome.storage.local.set({ buffer: b }, r));
const pushScope = (rname, label, rows) =>
  new Promise((res) => chrome.runtime.sendMessage({ type: "pushScope", rname, label, rows }, (r) => res(r || {})));
const countBuf = (b) => Object.values(b).reduce((n, a) => n + a.length, 0);

let collecting = false;

async function api(path, method, body) {
  const t = await getToken();
  const headers = { "Content-Type": "application/json", "Accept": "application/json" };
  if (t) headers["Authorization"] = t;
  return fetch(BASE + path, { method: method || "GET", headers, body: body || undefined, credentials: "include" });
}

async function scan() {
  collecting = true;
  setStatus("▶️ Старт… читаю регіони");
  let res;
  try { res = await api("/api/dictionaries/location/regions", "GET"); }
  catch (e) { setStatus("Помилка мережі: " + e); collecting = false; return; }
  if (res.status === 401) { setStatus("⛔ 401 — зроби 1 пошук на цій сторінці, потім «Старт» знову"); collecting = false; return; }
  if (!res.ok) { setStatus("Помилка регіонів: HTTP " + res.status); collecting = false; return; }
  const regions = await res.json();
  const buffer = await loadBuffer();
  let done = 0; const total = regions.length * TYPE_GROUPS.length;
  for (const reg of regions) {
    if (!collecting) { setStatus("⏹ Зупинено. Зібрано " + countBuf(buffer) + ". Натисни «Відправити»."); return; }
    const rname = titleUA(reg.name);
    for (const g of TYPE_GROUPS) {
      if (!collecting) { setStatus("⏹ Зупинено. Зібрано " + countBuf(buffer) + "."); return; }
      const rows = [], seen = new Set(); let ok = false;
      for (const num of NUMS) {
        if (!collecting) break;
        try {
          const r = await api("/api/plate/reserve", "POST", JSON.stringify({ reg: String(reg.id), type: g.codes, num }));
          if (r.ok) {
            const arr = await r.json(); ok = true;
            if (Array.isArray(arr)) {
              for (const p of arr) {
                const k = p.plate + "|" + (p.depName || "");
                if (seen.has(k)) continue; seen.add(k);
                rows.push({ plate_number: p.plate, region: rname, tsc: p.depName || null,
                            vehicle_type: g.label, price: p.cost ? parseFloat(p.cost) : null });
              }
              if (num === "" && arr.length > 0) break;
            }
          }
        } catch (e) {}
        await sleep(800 + Math.random() * 700);
      }
      done++;
      await sleep(3000 + Math.random() * 4000); // gentle pacing between scopes (avoid block)
      if (ok) {
        buffer[rname + "|||" + g.label] = rows;
        await saveBuffer(buffer);
        setStatus(`🔎 Зібрано ${countBuf(buffer)} · ${done}/${total} (${rname}/${g.label})`);
      }
    }
  }
  collecting = false;
  setStatus(`✅ Скан завершено. Зібрано ${countBuf(buffer)}. Тисни «Відправити на сервер».`);
}

async function sendAll() {
  const buffer = await loadBuffer();
  const keys = Object.keys(buffer);
  if (!keys.length) { setStatus("Немає зібраного. Спершу «Старт»."); return; }
  let sent = 0, i = 0;
  for (const k of keys) {
    const idx = k.indexOf("|||");
    const rname = k.slice(0, idx), label = k.slice(idx + 3);
    const resp = await pushScope(rname, label, buffer[k]);
    sent += buffer[k].length; i++;
    setStatus(`📤 Відправляю ${sent} (${i}/${keys.length}) · сервер ${resp.status}`);
    await sleep(150);
  }
  setStatus(`✅ Відправлено ${sent} номерів на сервер.`);
}

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (!msg) return false;
  if (msg.type === "start") { if (!collecting) scan().catch((e) => setStatus("Помилка: " + e)); }
  else if (msg.type === "stop") { collecting = false; setStatus("⏹ Зупиняю…"); }
  else if (msg.type === "send") { sendAll().catch((e) => setStatus("Помилка відправки: " + e)); }
  else if (msg.type === "clear") { chrome.storage.local.set({ buffer: {} }, () => setStatus("🗑 Буфер очищено.")); }
  reply && reply({ ok: true });
  return false;
});
