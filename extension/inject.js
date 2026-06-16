// MAIN world: capture the Authorization token so the harvest can reuse the session.
(function () {
  function send(a) { try { window.postMessage({ __avtoAuth: true, auth: a }, "*"); } catch (e) {} }
  function authOf(h) {
    try {
      if (!h) return null;
      if (h.get) return h.get("authorization") || h.get("Authorization");
      if (typeof h === "object") for (const k in h) if (k.toLowerCase() === "authorization") return h[k];
    } catch (e) {}
    return null;
  }
  const oF = window.fetch;
  if (oF) window.fetch = function (...a) {
    try { const x = authOf((a[1] || {}).headers) || (a[0] && a[0].headers ? authOf(a[0].headers) : null); if (x) send(x); } catch (e) {}
    return oF.apply(this, a);
  };
  const oS = XMLHttpRequest.prototype.setRequestHeader;
  XMLHttpRequest.prototype.setRequestHeader = function (k, v) {
    try { if (k && k.toLowerCase() === "authorization") send(v); } catch (e) {}
    return oS.apply(this, arguments);
  };
})();
