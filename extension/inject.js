// MAIN world: passively capture every fetch + XHR (request + response) and post to the page.
(function () {
  function send(e) { try { window.postMessage({ __avtoCap: true, e: e }, "*"); } catch (x) {} }
  function hdrs(h) {
    const o = {};
    try {
      if (!h) return o;
      if (h.forEach) { h.forEach((v, k) => (o[k] = v)); return o; }
      if (Array.isArray(h)) { h.forEach(([k, v]) => (o[k] = v)); return o; }
      if (typeof h === "object") for (const k in h) o[k] = h[k];
    } catch (x) {}
    return o;
  }
  const oFetch = window.fetch;
  if (oFetch) {
    window.fetch = async function (...args) {
      let url = args[0]; if (url && url.url) url = url.url;
      const init = args[1] || {};
      const method = (init.method) || (args[0] && args[0].method) || "GET";
      const reqHeaders = hdrs(init.headers || (args[0] && args[0].headers));
      const reqBody = (typeof init.body === "string") ? init.body : null;
      let res;
      try { res = await oFetch.apply(this, args); } catch (err) { send({ url: String(url), method, reqHeaders, reqBody, status: "ERR", body: String(err) }); throw err; }
      try {
        res.clone().text().then((body) => send({ url: String(url), method, reqHeaders, reqBody, status: res.status, body }));
      } catch (x) {}
      return res;
    };
  }
  const oOpen = XMLHttpRequest.prototype.open, oSend = XMLHttpRequest.prototype.send, oSet = XMLHttpRequest.prototype.setRequestHeader;
  XMLHttpRequest.prototype.open = function (m, u) { this.__c = { method: m, url: u, reqHeaders: {} }; return oOpen.apply(this, arguments); };
  XMLHttpRequest.prototype.setRequestHeader = function (k, v) { try { if (this.__c) this.__c.reqHeaders[k] = v; } catch (x) {} return oSet.apply(this, arguments); };
  XMLHttpRequest.prototype.send = function (b) {
    const x = this; if (x.__c) x.__c.reqBody = (typeof b === "string") ? b : null;
    x.addEventListener("load", function () {
      try { send({ url: (x.__c && x.__c.url) || "", method: (x.__c && x.__c.method) || "GET", reqHeaders: (x.__c && x.__c.reqHeaders) || {}, reqBody: (x.__c && x.__c.reqBody) || null, status: x.status, body: x.responseText }); } catch (e) {}
    });
    return oSend.apply(this, arguments);
  };
})();
