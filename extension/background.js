// Store every captured entry in chrome.storage.local (bounded).
chrome.runtime.onMessage.addListener(function (msg) {
  if (msg && msg.type === "cap") {
    chrome.storage.local.get({ entries: [] }, function (d) {
      d.entries.push(msg.e);
      if (d.entries.length > 6000) d.entries = d.entries.slice(-6000);
      chrome.storage.local.set({ entries: d.entries });
    });
  }
});
