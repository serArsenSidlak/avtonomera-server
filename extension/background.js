// Accumulates parsed rows into a de-duplicated table (key = plate|tsc).
chrome.runtime.onMessage.addListener(function (msg) {
  if (msg && msg.type === "rows") {
    chrome.storage.local.get({ table: {} }, function (d) {
      const t = d.table;
      for (const r of msg.rows) t[r.plate + "|" + r.tsc] = r;
      chrome.storage.local.set({ table: t });
    });
  }
});
