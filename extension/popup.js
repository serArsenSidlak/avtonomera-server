function log(s) { document.getElementById("log").textContent = s; }
function fmtTime(ts) { try { return new Date(ts).toLocaleString("uk-UA"); } catch (e) { return ""; } }

function render() {
  chrome.storage.local.get({ captures: {} }, (d) => {
    const caps = d.captures || {};
    const keys = Object.keys(caps).sort();
    const list = document.getElementById("list");
    list.innerHTML = "";
    let total = 0;
    for (const k of keys) {
      const e = caps[k]; total += e.count || 0;
      const div = document.createElement("div"); div.className = "item";
      div.innerHTML = "<div class='meta'><div class='reg'>✅ " + k + "</div>" +
        "<div class='sub'>" + (e.count || 0) + " номерів · " + fmtTime(e.time) + "</div></div>";
      const btn = document.createElement("button"); btn.className = "g"; btn.textContent = "📤";
      btn.onclick = () => {
        log("Відправляю " + k + "…");
        chrome.runtime.sendMessage({ type: "send", region: k }, (r) => {
          if (!r) { log("Немає відповіді"); return; }
          if (r.error) { log("❌ " + k + ": " + r.error); return; }
          const res = r.result || {};
          log("✅ " + k + ": нових " + (res.new || 0) + ", знято " + (res.removed || 0) + ", всього " + (res.scraped || 0));
        });
      };
      div.appendChild(btn);
      list.appendChild(div);
    }
    document.getElementById("total").textContent =
      keys.length ? ("Спіймано регіонів: " + keys.length + " · номерів: " + total) : "Поки нічого не спіймано. Відкрий регіон на opendata.";
  });
}

document.getElementById("sendAll").onclick = () => {
  log("Заливаю всі регіони…");
  chrome.runtime.sendMessage({ type: "sendAll" }, (r) => {
    if (!r || !r.sent) { log("Немає відповіді"); return; }
    const lines = r.sent.map((x) => x.error
      ? ("❌ " + x.region + ": " + x.error)
      : ("✅ " + x.region + ": +" + ((x.result || {}).new || 0) + " / −" + ((x.result || {}).removed || 0)));
    log(lines.join("\n"));
  });
};

document.getElementById("export").onclick = () => {
  chrome.storage.local.get({ captures: {} }, (d) => {
    const caps = d.captures || {};
    const out = ["region;plate;vehicle_type;price;tsc"];
    for (const k of Object.keys(caps)) {
      for (const r of (caps[k].rows || [])) {
        out.push([caps[k].region || k, r.plate_number, r.vehicle_type || "", r.price == null ? "" : r.price, (r.tsc || "").replace(/;/g, ",")].join(";"));
      }
    }
    const blob = new Blob(["﻿" + out.join("\n")], { type: "text/csv;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "avtonomera_opendata.csv";
    a.click();
    log("Експортовано " + (out.length - 1) + " рядків у CSV");
  });
};

document.getElementById("clear").onclick = () => {
  if (!confirm("Очистити всі спіймані дані?")) return;
  chrome.storage.local.set({ captures: {} }, () => { log("Очищено"); render(); });
};

setInterval(render, 1500);
render();
