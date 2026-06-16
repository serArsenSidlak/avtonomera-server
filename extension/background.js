// Collects captured request/response entries into chrome.storage.local.
chrome.runtime.onMessage.addListener(function (msg, sender) {
  if (msg && msg.type === "capture") {
    chrome.storage.local.get({ entries: [] }, function (d) {
      const e = msg.entry || {};
      d.entries.push({
        url: e.url, method: e.method, status: e.status,
        reqBody: e.reqBody || null, body: e.body || "",
        page: (sender && sender.tab && sender.tab.url) || "", ts: Date.now()
      });
      if (d.entries.length > 4000) d.entries = d.entries.slice(-4000);
      chrome.storage.local.set({ entries: d.entries });
    });
  }
});
