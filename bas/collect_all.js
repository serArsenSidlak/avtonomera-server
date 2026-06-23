/* Автономера — макрос для BAS (Browser Automation Studio).
 *
 * Вставити у BAS в дію «Execute JavaScript» (Виконати JavaScript) на вкладці, де ВЖЕ відкрито
 * https://opendata.hsc.gov.ua/check-leisure-license-plates/ (Akamai пройдено, кукі є).
 *
 * Робить ОДИН повний цикл по всіх регіонах: для кожного — підставляє регіон + «Весь регіон» +
 * усі типи, забирає ПОВНИЙ список (а не видимі 10), і шле на наш сервер /collect-html
 * (сервер сам парсить, визначає тип по серії, прибирає дублі й оновлює саме цей регіон).
 *
 * Делікатно: пауза ~22 c між регіонами → повний цикл ~10 хв (тобто кожен регіон раз на ~10 хв).
 * BAS просто повторює цю дію в циклі (кожен потік — своя проксі/фінгерпринт).
 *
 * Повертає текстовий лог (BAS покаже його як результат дії).
 */
(async function () {
  var SERVER = "https://34.123.136.171.nip.io";
  var SECRET = "ing__0VaRFvnOC57sc7baY6H0dMzKx21PNJD";
  var GAP_MS = 22000; // пауза між регіонами (бережно до сайту)
  var sleep = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };
  var log = [];

  function setSelByValue(el, v) { if (el) { el.value = v; el.dispatchEvent(new Event("change", { bubbles: true })); } }
  function setSelByText(el, sub) {
    if (!el) return;
    for (var i = 0; i < el.options.length; i++) {
      if (el.options[i].text.trim().toLowerCase().indexOf(sub) >= 0) { el.selectedIndex = i; el.dispatchEvent(new Event("change", { bubbles: true })); return; }
    }
  }

  async function collectRegion(regionValue) {
    var rsel = document.querySelector("#region");
    if (!rsel) return "немає #region";
    setSelByValue(rsel, regionValue);
    var reg = rsel.options[rsel.selectedIndex] ? rsel.options[rsel.selectedIndex].text.trim() : "";
    await sleep(900); // дати підвантажитись списку ТСЦ
    setSelByText(document.querySelector("#tsc"), "весь регіон");
    var tv = document.querySelector("#type_venichle"); setSelByValue(tv, "all");
    await sleep(400);
    var form = rsel.closest("form") || document.querySelector("form");
    var p = [], es = form.querySelectorAll("input,select,textarea");
    for (var i = 0; i < es.length; i++) {
      var e = es[i];
      if (!e.name) continue;
      if ((e.type === "checkbox" || e.type === "radio") && !e.checked) continue;
      if (e.type === "submit" || e.type === "button") continue;
      p.push(encodeURIComponent(e.name) + "=" + encodeURIComponent(e.value));
    }
    var sb = form.querySelector("input[type=submit],button[type=submit]");
    if (sb && sb.name) p.push(encodeURIComponent(sb.name) + "=" + encodeURIComponent(sb.value || ""));
    var act = form.getAttribute("action") || location.href, m = (form.getAttribute("method") || "POST").toUpperCase();
    var url = act, opt = { method: m, credentials: "include" };
    if (m === "GET") { url = act + (act.indexOf("?") < 0 ? "?" : "&") + p.join("&"); }
    else { opt.headers = { "Content-Type": "application/x-www-form-urlencoded" }; opt.body = p.join("&"); }
    var html = await fetch(url, opt).then(function (r) { return r.text(); });
    var res = await fetch(SERVER + "/collect-html", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ secret: SECRET, region: reg, html: html }),
    }).then(function (r) { return r.json(); });
    return reg + ": +" + (res.new || 0) + " / -" + (res.removed || 0) + " (всього " + (res.scraped || 0) + ")";
  }

  var rsel = document.querySelector("#region");
  if (!rsel) return "ERROR: відкрий сторінку opendata спочатку";
  var values = [];
  for (var i = 0; i < rsel.options.length; i++) {
    var v = rsel.options[i].value;
    if (v && v !== "0" && v !== "-1") values.push(v);
  }
  for (var j = 0; j < values.length; j++) {
    try { log.push(await collectRegion(values[j])); }
    catch (e) { log.push("регіон[" + values[j] + "] помилка: " + e); }
    if (j < values.length - 1) await sleep(GAP_MS);
  }
  return log.join("\n");
})();
