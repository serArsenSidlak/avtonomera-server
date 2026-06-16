// Isolated content script: relay captured entries (from the MAIN-world inject.js) to background.
window.addEventListener("message", function (e) {
  if (e.source !== window || !e.data || !e.data.__avtoCapture) return;
  const en = e.data.entry || {};
  if (en.body && en.body.length > 800000) en.body = en.body.slice(0, 800000);
  try { chrome.runtime.sendMessage({ type: "capture", entry: en }); } catch (x) {}
});
