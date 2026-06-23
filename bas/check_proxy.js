/* Автономера — BAS перевірка проксі для opendata.
 *
 * Запускати на ЗАВАНТАЖЕНІЙ сторінці opendata (через проксі, який зараз стоїть у потоці BAS).
 * Пробує реально отримати список номерів одного регіону. Повертає:
 *   "OK:<рядків>"  → проксі підходить (пройшов Akamai і віддав таблицю)
 *   "FAIL:..."     → проксі не годиться (бан/капча/таймаут/сторінка не завантажилась)
 *
 * BAS-флоу: цикл по проксі → Set proxy → Load page → Wait 5–8c → ця дія →
 *   якщо результат починається з "OK" → допиши проксі у файл good_proxies.txt.
 */
(async function () {
  var rsel = document.querySelector("#region");
  if (!rsel) return "FAIL: сторінка не завантажилась (Akamai/проксі)";
  var idx = -1;
  for (var i = 0; i < rsel.options.length; i++) { var v = rsel.options[i].value; if (v && v !== "0" && v !== "-1") { idx = i; break; } }
  if (idx < 0) return "FAIL: немає регіонів у списку";
  rsel.selectedIndex = idx; rsel.dispatchEvent(new Event("change", { bubbles: true }));
  await new Promise(function (r) { setTimeout(r, 900); });
  function setSelByText(el, sub) { if (!el) return; for (var i = 0; i < el.options.length; i++) { if (el.options[i].text.trim().toLowerCase().indexOf(sub) >= 0) { el.selectedIndex = i; el.dispatchEvent(new Event("change", { bubbles: true })); return; } } }
  setSelByText(document.querySelector("#tsc"), "весь регіон");
  var tv = document.querySelector("#type_venichle"); if (tv) { tv.value = "all"; tv.dispatchEvent(new Event("change", { bubbles: true })); }
  await new Promise(function (r) { setTimeout(r, 300); });
  var form = rsel.closest("form") || document.querySelector("form");
  var p = [], es = form.querySelectorAll("input,select,textarea");
  for (var k = 0; k < es.length; k++) { var e = es[k]; if (!e.name) continue; if ((e.type === "checkbox" || e.type === "radio") && !e.checked) continue; if (e.type === "submit" || e.type === "button") continue; p.push(encodeURIComponent(e.name) + "=" + encodeURIComponent(e.value)); }
  var sb = form.querySelector("input[type=submit],button[type=submit]"); if (sb && sb.name) p.push(encodeURIComponent(sb.name) + "=" + encodeURIComponent(sb.value || ""));
  var act = form.getAttribute("action") || location.href, m = (form.getAttribute("method") || "POST").toUpperCase();
  var url = act, opt = { method: m, credentials: "include" };
  if (m === "GET") { url = act + (act.indexOf("?") < 0 ? "?" : "&") + p.join("&"); }
  else { opt.headers = { "Content-Type": "application/x-www-form-urlencoded" }; opt.body = p.join("&"); }
  try {
    var t = await fetch(url, opt).then(function (r) { return r.text(); });
    if (t.indexOf("Номерний") >= 0) return "OK:" + ((t.match(/<tr/g) || []).length);
    return "FAIL: відповідь без таблиці (бан/капча)";
  } catch (e) { return "FAIL: " + e; }
})();
