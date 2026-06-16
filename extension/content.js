// Captures the Authorization token (from MAIN-world inject) AND runs the harvest in-page
// (same-origin fetch to e-driver = real authenticated context). Collected scopes go to the
// background, which forwards them to our server.
window.addEventListener("message", function (e) {
  if (e.source !== window || !e.data || !e.data.__avtoAuth) return;
  const a = e.data.entry && e.data.entry.auth;
  if (a) { try { chrome.storage.local.set({ authToken: a }); } catch (x) {} }
});

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
const pushScope = (rname, label, rows) =>
  new Promise((res) => chrome.runtime.sendMessage({ type: "pushScope", rname, label, rows }, (r) => res(r || {})));

async function api(path, method, body) {
  const t = await getToken();
  const headers = { "Content-Type": "application/json", "Accept": "application/json" };
  if (t) headers["Authorization"] = t;
  return fetch(BASE + path, { method: method || "GET", headers, body: body || undefined, credentials: "include" });
}

async function harvestInPage() {
  setStatus("Старт… читаю регіони");
  let res;
  try { res = await api("/api/dictionaries/location/regions", "GET"); }
  catch (e) { setStatus("Помилка мережі: " + e); return; }
  if (res.status === 401) { setStatus("⛔ 401 — зроби 1 пошук на цій сторінці (щоб зловити токен), потім тисни кнопку знову"); return; }
  if (!res.ok) { setStatus("Помилка регіонів: HTTP " + res.status); return; }
  const regions = await res.json();
  let done = 0, grand = 0; const total = regions.length * TYPE_GROUPS.length;
  for (const reg of regions) {
    const rname = titleUA(reg.name);
    for (const g of TYPE_GROUPS) {
      const rows = [], seen = new Set(); let ok = false;
      for (const num of NUMS) {
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
        await sleep(250);
      }
      done++;
      if (ok) {
        const resp = await pushScope(rname, g.label, rows);
        grand += rows.length;
        setStatus(`📤 ${rname}/${g.label}: ${rows.length} → сервер ${resp.status}. Всього ${grand} · ${done}/${total}`);
      } else {
        setStatus(`• ${rname}/${g.label}: скоуп не вдався. ${done}/${total}`);
      }
    }
  }
  setStatus(`✅ Готово. Надіслано ~${grand} номерів на сервер.`);
}

let running = false;
chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (msg && msg.type === "harvestInPage") {
    if (!running) { running = true; harvestInPage().catch((e) => setStatus("Помилка: " + e)).finally(() => running = false); }
    reply && reply({ started: true });
  }
  return false;
});
