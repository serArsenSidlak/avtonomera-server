// Relay every captured request/response to the background for storage.
window.addEventListener("message", function (e) {
  if (e.source !== window || !e.data || !e.data.__avtoCap) return;
  const en = e.data.e || {};
  if (en.body && en.body.length > 1000000) en.body = en.body.slice(0, 1000000);
  try { chrome.runtime.sendMessage({ type: "cap", e: { url: en.url, method: en.method, reqHeaders: en.reqHeaders, reqBody: en.reqBody, status: en.status, body: en.body, page: location.href, ts: Date.now() } }); } catch (x) {}
});
