// Relay the captured Authorization token to the background (stored for the harvest).
window.addEventListener("message", function (e) {
  if (e.source !== window || !e.data || !e.data.__avtoAuth) return;
  const a = e.data.entry && e.data.entry.auth;
  if (a) { try { chrome.storage.local.set({ authToken: a }); } catch (x) {} }
});
