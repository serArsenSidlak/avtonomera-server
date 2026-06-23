// ISOLATED world: отримує перехоплений HTML, визначає регіон, парсить номери,
// визначає тип ТЗ по серії (Додаток 5) і накопичує в chrome.storage.local по регіонах.
// Жодних автозапитів — лише пасивний збір того, що ти сам відкриваєш на сайті.

const LAT = { A: "А", B: "В", C: "С", E: "Е", H: "Н", I: "І", K: "К", M: "М", O: "О", P: "Р", T: "Т", X: "Х" };
function norm(p) {
  p = (p || "").replace(/[\s\-]/g, "").toUpperCase();
  let o = ""; for (let i = 0; i < p.length; i++) o += (LAT[p[i]] || p[i]); return o;
}

// Додаток 5 — серія (кінцеві 2 літери) → тип ТЗ. Блоки дослівно з наказу МВС.
const D5 = {
  "Легковий, вантажний": "ААВАСАЕАНАІА КАМАРАТАХАОО АВВВСВЕВНВ ІВ КВМВРВ ТВХВОР АСВСССЕСНС ІС КСМСРС ТСХСОТ АЕ ВЕ СЕ ЕЕ НЕ ІЕ КЕМЕРЕ ТЕ ХЕОХ АНВНСНЕНННІН КНМНРНТНХН АІ ВІ СІ ЕІ НІ ІІ КІ МІ РІ ТІ ХІ АКВКСКЕКНК ІК ККМКРК ТКХК АМВМСМЕМНМІМКМММРМТМХМ АОВОСОЕОНОІО КОМОРОТОХО АР ВР СР ЕР НР ІР КР МР РР ТР ХР АТВТ СТ ЕТ НТ ІТ КТМТРТ ТТ ХТ АХВХСХЕХНХІХ КХМХРХТХХХ ОА ОВ ОС ОЕ ОН ОІ ОК ОМ",
  "Причіп": "XFXGXJXLXNXRXSXUXVXYXZ FF FR FSFUFVFYFZ СFСGСJ СLСNСRСSСUСY FG FJ FL FN",
  "Електромобіль": "UAUFUGUHUIUJUKULUMUNUOUP URUSUTUUUХUY QAQBQCQDQEQFQGQHQIQJQKQL QMQNQOQPQQQRQSQTQUQХQY ZAZBZCZDZEZFZGZHZI ZJZKZL ZMZNZOZPZRZSZTZUZVZXZYZZ YAYBYCYDYEYFYGYHYIYJYKYL YMYNYOYPYRYSYTYUYVYXYYYZ UB UC UD UE",
  "Мотоцикл": "JAJBJCJDJE JFJGJH JI JJ JKJL JMJNJOJPJRJS JTJUJVJXJYJZ LELFLGLHLI LJLKLLLMLNLOLP LRLSLTLULVLXLYLZ",
  "Електромотоцикл": "RARFRGRHRIRJRKRLRMRNRORP RRRSRTRURVRXRYRZ SASBSCSDSESFSGSHSI SJSKSL SMSNSOSPSRSSSTSUSVSXSYSZ",
};
const SER = {};
for (const t in D5) { const s = D5[t].replace(/\s+/g, ""); for (let i = 0; i + 1 < s.length; i += 2) SER[norm(s.substr(i, 2))] = t; }
function vtype(plate) {
  const s = plate.slice(-2);
  if (SER[s]) return SER[s];
  const a = s[0], b = s[1];
  if (a === "F" || (a === "Х" && "FGJLNRSUV".indexOf(b) >= 0) || (a === "С" && "FGJLNRSUVY".indexOf(b) >= 0)) return "Причіп";
  if (a === "J" || a === "L") return "Мотоцикл";
  if (a === "R" || a === "S") return "Електромотоцикл";
  if (a === "U" || a === "Y" || a === "Z" || a === "Q") return "Електромобіль";
  return "Легковий, вантажний";
}

function regionName() {
  const el = document.querySelector("#region");
  if (el && el.selectedIndex >= 0) {
    let t = (el.options[el.selectedIndex].text || "").trim().replace(/\s*область$/i, "").trim();
    if (/київ/i.test(t)) t = "м. Київ";
    return t;
  }
  return "";
}

function parseRows(html) {
  const doc = new DOMParser().parseFromString(html, "text/html");
  let tbl = null; const tabs = doc.getElementsByTagName("table");
  for (let i = 0; i < tabs.length; i++) { const h = tabs[i].querySelector("tr"); if (h && h.textContent.indexOf("Номерний") >= 0) { tbl = tabs[i]; break; } }
  if (!tbl) return [];
  const trs = tbl.querySelectorAll("tr"), rows = [], seen = {};
  for (let j = 1; j < trs.length; j++) {
    const td = trs[j].querySelectorAll("td"); if (td.length < 3) continue;
    const raw = td[0].textContent.trim(); if (!raw || raw.indexOf("Номерний") >= 0) continue;
    const n = norm(raw); if (!/^\D{2}\d{4}\D{2}$/.test(n)) continue;
    const tsc = td[2].textContent.trim() || null;
    const k = n + "|" + (tsc || ""); if (seen[k]) continue; seen[k] = 1;
    const pm = td[1].textContent.replace(/\s/g, "").match(/[0-9.,]+/);
    rows.push({ plate_number: n, price: pm ? parseFloat(pm[0].replace(",", ".")) : null, tsc: tsc, vehicle_type: vtype(n) });
  }
  return rows;
}

function capture(html) {
  const rows = parseRows(html);
  if (!rows.length) return;
  const reg = regionName();
  chrome.storage.local.get({ captures: {} }, function (d) {
    const c = d.captures || {};
    const key = reg || ("Невідомо · " + new Date().toLocaleTimeString());
    // latest snapshot per region wins (повний знімок регіону за один запит)
    c[key] = { region: reg || key, count: rows.length, rows: rows, time: Date.now() };
    chrome.storage.local.set({ captures: c });
  });
}

window.addEventListener("message", function (e) {
  if (e.source === window && e.data && e.data.__avtoCap && e.data.html) capture(e.data.html);
});

// Запасний варіант: якщо сайт віддав результат як звичайну сторінку (без fetch/XHR),
// беремо таблицю з готового DOM.
window.addEventListener("load", function () {
  try { const h = document.documentElement.outerHTML; if (h.indexOf("Номерний") >= 0) capture(h); } catch (e) {}
});
