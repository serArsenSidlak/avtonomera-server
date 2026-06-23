// MAIN world: тихо перехоплює відповіді opendata (fetch + XHR), у яких є таблиця номерів,
// і передає сирий HTML у content.js. Працює, поки ти вручну ходиш по сайту.
(function () {
  function post(html, url) {
    try { if (html && html.indexOf("Номерний") >= 0) window.postMessage({ __avtoCap: true, html: html, url: url || "" }, "*"); }
    catch (e) {}
  }
  const oF = window.fetch;
  if (oF) {
    window.fetch = function (...a) {
      return oF.apply(this, a).then(function (res) {
        try { res.clone().text().then(function (t) { post(t, res.url); }).catch(function () {}); } catch (e) {}
        return res;
      });
    };
  }
  const oOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (m, u) { try { this.__au = u; } catch (e) {} return oOpen.apply(this, arguments); };
  const oSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.send = function () {
    try {
      this.addEventListener("load", function () {
        try { if (this.responseType === "" || this.responseType === "text") post(this.responseText, this.__au); } catch (e) {}
      });
    } catch (e) {}
    return oSend.apply(this, arguments);
  };
})();
