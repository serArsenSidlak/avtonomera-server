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

function parseRowsRoot(root) {
  let tbl = null; const tabs = root.getElementsByTagName("table");
  for (let i = 0; i < tabs.length; i++) { const h = tabs[i].querySelector("tr"); if (h && h.textContent.indexOf("Номерний") >= 0) { tbl = tabs[i]; break; } }
  if (!tbl) return [];
  const trs = tbl.querySelectorAll("tr"), rows = [], seen = {};
  for (let j = 0; j < trs.length; j++) {
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

function parseRows(html) { return parseRowsRoot(new DOMParser().parseFromString(html, "text/html")); }

function storeRows(rows) {
  if (!rows.length) return;
  const reg = regionName();
  chrome.storage.local.get({ captures: {} }, function (d) {
    const c = d.captures || {};
    const key = reg || ("Невідомо · " + new Date().toLocaleTimeString());
    c[key] = { region: reg || key, count: rows.length, rows: rows, time: Date.now() };  // latest snapshot per region wins
    chrome.storage.local.set({ captures: c });
  });
}

function capture(html) { storeRows(parseRows(html)); }

// 1) Перехоплення мережевих відповідей (fetch/XHR з готовою таблицею).
window.addEventListener("message", function (e) {
  if (e.source === window && e.data && e.data.__avtoCap && e.data.html) capture(e.data.html);
});

// 2) НАДІЙНО: періодично скануємо таблицю прямо в DOM (DataTables/AJAX/звичайна сторінка),
//    бо сайт може вантажити результати динамічно — тоді мережевий хук їх не бачить.
let _lastSig = "";
function scanDom() {
  try {
    const rows = parseRowsRoot(document);
    if (!rows.length) return;
    const reg = regionName();
    const sig = (reg || "?") + ":" + rows.length + ":" + (rows[0] && rows[0].plate_number) + ":" + (rows[rows.length - 1] && rows[rows.length - 1].plate_number);
    if (sig === _lastSig) return;  // та сама таблиця — не перезаписуємо
    _lastSig = sig;
    storeRows(rows);
  } catch (e) {}
}
setInterval(scanDom, 1500);
window.addEventListener("load", scanDom);
