// Parses captured e-driver responses into a clean table (plate, price, tsc, region, type).
// Region id→name is learned dynamically from the regions dictionary response.
const TYPE_LABEL = {
  "1000428": "Легковий, вантажний", "3": "Легковий, вантажний",
  "1000436": "Електромобіль", "1000437": "Причіп",
  "1000457": "Мотоцикл", "1000458": "Електромотоцикл",
  "1000443": "Мопед", "1000444": "Електромопед",
};
function titleUA(s) {
  return (s || "").toLowerCase().replace(/(^|[\s\-.])([a-zа-яіїєґ'])/g, (m, p, c) => p + c.toUpperCase());
}
let regionMap = {};
chrome.storage.local.get({ regionMap: {} }, (d) => { regionMap = d.regionMap || {}; });

window.addEventListener("message", function (e) {
  if (e.source !== window || !e.data || !e.data.__avtoCap) return;
  const en = e.data.e || {};
  const url = en.url || "";
  // Learn region id → name.
  if (url.indexOf("/api/dictionaries/location/regions") >= 0 && en.body) {
    try {
      const arr = JSON.parse(en.body), m = {};
      arr.forEach((r) => (m[String(r.id)] = titleUA(r.name)));
      regionMap = m; chrome.storage.local.set({ regionMap: m });
    } catch (x) {}
    return;
  }
  // Parse plate lists into table rows.
  if (url.indexOf("/api/plate/reserve") >= 0 && (en.method || "").toUpperCase() === "POST" && en.body) {
    let reg = "";
    try { reg = String((JSON.parse(en.reqBody || "{}") || {}).reg || ""); } catch (x) {}
    const region = regionMap[reg] || (reg ? "рег " + reg : "");
    let arr; try { arr = JSON.parse(en.body); } catch (x) { return; }
    if (!Array.isArray(arr)) return;
    const rows = arr.map((p) => ({
      plate: p.plate, price: p.cost, tsc: p.depName || "",
      region: region, type: TYPE_LABEL[String(p.type)] || String(p.type),
    }));
    if (rows.length) chrome.runtime.sendMessage({ type: "rows", rows: rows });
  }
});
