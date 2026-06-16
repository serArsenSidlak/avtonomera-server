// Isolated content script: inject the MAIN-world hook, relay captured entries to background.
(function () {
  try {
    const s = document.createElement("script");
    s.src = chrome.runtime.getURL("inject.js");
    s.onload = function () { this.remove(); };
    (document.head || document.documentElement).appendChild(s);
  } catch (e) {}
  window.addEventListener("message", function (e) {
    if (e.source !== window || !e.data || !e.data.__avtoCapture) return;
    const en = e.data.entry || {};
    // Keep payloads bounded.
    if (en.body && en.body.length > 800000) en.body = en.body.slice(0, 800000);
    chrome.runtime.sendMessage({ type: "capture", entry: en });
  });
})();
