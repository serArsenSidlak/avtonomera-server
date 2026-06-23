// Відправляє накопичені по регіонах номери у свою базу (/collect), по одному регіону.
const SERVER = "https://34.123.136.171.nip.io";
const INGEST_SECRET = "ing__0VaRFvnOC57sc7baY6H0dMzKx21PNJD";

function sendRegion(region, entry) {
  const rows = (entry && entry.rows) || [];
  rows.forEach((r) => { r.region = region; });
  const types = {};
  rows.forEach((r) => { types[r.vehicle_type] = 1; });
  const scopes = Object.keys(types).map((t) => [region, t]);
  return fetch(SERVER + "/collect", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ secret: INGEST_SECRET, rows: rows, ok_scopes: scopes }),
  }).then((r) => r.json());
}

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (!msg) return false;

  if (msg.type === "send") {
    chrome.storage.local.get({ captures: {} }, (d) => {
      const entry = (d.captures || {})[msg.region];
      if (!entry) { reply({ error: "немає даних" }); return; }
      sendRegion(msg.region, entry)
        .then((res) => reply({ region: msg.region, result: res }))
        .catch((e) => reply({ region: msg.region, error: String(e) }));
    });
    return true;
  }

  if (msg.type === "sendAll") {
    chrome.storage.local.get({ captures: {} }, async (d) => {
      const caps = d.captures || {};
      const out = [];
      for (const region of Object.keys(caps)) {
        try { const res = await sendRegion(region, caps[region]); out.push({ region, result: res }); }
        catch (e) { out.push({ region, error: String(e) }); }
      }
      reply({ sent: out });
    });
    return true;
  }

  return false;
});
