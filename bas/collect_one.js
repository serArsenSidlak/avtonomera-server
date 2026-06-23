/* Автономера — BAS макрос для ОДНОГО регіону.
 *
 * Збирає лише один регіон і шле на /collect-html. Зручно, коли кожному регіону —
 * свій потік/проксі в BAS (запускаєш окрему копію цієї дії на регіон).
 *
 * Налаштування: впиши REGION нижче.
 *   • "" (порожньо) → збирає той регіон, що ЗАРАЗ вибраний у списку #region на сторінці
 *     (тоді обери регіон кроком BAS «Select» перед цією дією);
 *   • назва, напр. "Вінницька" → скрипт сам обере цей регіон;
 *   • або числове значення опції, напр. "2".
 */
(async function () {
  var REGION = "";  // <- ВПИШИ ТУТ (назва / значення / порожньо = поточний)

  var SERVER = "https://34.123.136.171.nip.io";
  var SECRET = "ing__0VaRFvnOC57sc7baY6H0dMzKx21PNJD";
  var sleep = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };

  function setSelByText(el, sub) {
    if (!el) return;
    for (var i = 0; i < el.options.length; i++) {
      if (el.options[i].text.trim().toLowerCase().indexOf(sub) >= 0) { el.selectedIndex = i; el.dispatchEvent(new Event("change", { bubbles: true })); return; }
    }
  }

  var rsel = document.querySelector("#region");
  if (!rsel) return "ERROR: відкрий сторінку opendata спочатку";

  if (REGION) {
    var done = false;
    for (var i = 0; i < rsel.options.length; i++) {
      var o = rsel.options[i];
      if (o.value === REGION || o.text.trim().toLowerCase().indexOf(REGION.toLowerCase()) >= 0) {
        rsel.selectedIndex = i; rsel.dispatchEvent(new Event("change", { bubbles: true })); done = true; break;
      }
    }
    if (!done) return "ERROR: регіон не знайдено: " + REGION;
    await sleep(900);
  }
  var reg = rsel.options[rsel.selectedIndex] ? rsel.options[rsel.selectedIndex].text.trim() : "";
  if (!reg) return "ERROR: регіон не вибрано";

  setSelByText(document.querySelector("#tsc"), "весь регіон");
  var tv = document.querySelector("#type_venichle"); if (tv) { tv.value = "all"; tv.dispatchEvent(new Event("change", { bubbles: true })); }
  await sleep(400);

  var form = rsel.closest("form") || document.querySelector("form");
  var p = [], es = form.querySelectorAll("input,select,textarea");
  for (var k = 0; k < es.length; k++) {
    var e = es[k];
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

  try {
    var html = await fetch(url, opt).then(function (r) { return r.text(); });
    var res = await fetch(SERVER + "/collect-html", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ secret: SECRET, region: reg, html: html }),
    }).then(function (r) { return r.json(); });
    return reg + ": +" + (res.new || 0) + " / -" + (res.removed || 0) + " (всього " + (res.scraped || 0) + ")";
  } catch (e) { return reg + ": помилка " + e; }
})();
