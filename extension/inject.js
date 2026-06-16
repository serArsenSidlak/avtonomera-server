// Runs in the PAGE (MAIN) context: patches fetch + XMLHttpRequest to capture responses,
// then forwards them to the content script via window.postMessage.
(function () {
  function send(entry) {
    try { window.postMessage({ __avtoCapture: true, entry: entry }, "*"); } catch (e) {}
  }
  const origFetch = window.fetch;
  if (origFetch) {
    window.fetch = async function (...args) {
      const res = await origFetch.apply(this, args);
      try {
        let url = args[0]; if (url && url.url) url = url.url;
        const method = (args[1] && args[1].method) || (args[0] && args[0].method) || "GET";
        const reqBody = (args[1] && typeof args[1].body === "string") ? args[1].body : null;
        res.clone().text().then(function (body) {
          send({ kind: "fetch", url: String(url), method: method, status: res.status,
                 reqBody: reqBody, body: body });
        }).catch(function () {});
      } catch (e) {}
      return res;
    };
  }
  const oOpen = XMLHttpRequest.prototype.open;
  const oSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (m, u) {
    this.__avto = { method: m, url: u }; return oOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function (b) {
    const xhr = this;
    xhr.addEventListener("load", function () {
      try {
        send({ kind: "xhr", url: (xhr.__avto && xhr.__avto.url) || "", 
               method: (xhr.__avto && xhr.__avto.method) || "GET", status: xhr.status,
               reqBody: (typeof b === "string") ? b : null, body: xhr.responseText });
      } catch (e) {}
    });
    return oSend.apply(this, arguments);
  };
})();
