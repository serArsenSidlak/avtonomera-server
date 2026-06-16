// MAIN world: capture the Authorization header the app uses (so we can reuse the session).
(function () {
  function send(entry) { try { window.postMessage({ __avtoAuth: true, entry: entry }, "*"); } catch (e) {} }
  function authOf(h) {
    try {
      if (!h) return null;
      if (h.get) return h.get("authorization") || h.get("Authorization");
      if (typeof h === "object") for (const k in h) if (k.toLowerCase() === "authorization") return h[k];
    } catch (e) {}
    return null;
  }
  const oFetch = window.fetch;
  if (oFetch) {
    window.fetch = function (...args) {
      try {
        const init = args[1] || {};
        const a = authOf(init.headers) || (args[0] && args[0].headers ? authOf(args[0].headers) : null);
        if (a) send({ auth: a });
      } catch (e) {}
      return oFetch.apply(this, args);
    };
  }
  const oSet = XMLHttpRequest.prototype.setRequestHeader;
  XMLHttpRequest.prototype.setRequestHeader = function (k, v) {
    try { if (k && k.toLowerCase() === "authorization") send({ auth: v }); } catch (e) {}
    return oSet.apply(this, arguments);
  };
})();
