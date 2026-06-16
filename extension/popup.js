function csvCell(v){ v = (v==null?"":String(v)); return /[",\n;]/.test(v) ? '"'+v.replace(/"/g,'""')+'"' : v; }
function refresh(){ chrome.storage.local.get({table:{}}, d=>{ document.getElementById("c").textContent = Object.keys(d.table).length; }); }
function dl(name, text, mime){
  const blob = new Blob([text], {type: mime});
  chrome.downloads.download({ url: URL.createObjectURL(blob), filename: name, saveAs: true });
}
document.getElementById("csv").onclick = function(){
  chrome.storage.local.get({table:{}}, d=>{
    const rows = Object.values(d.table);
    const head = "Номер;Ціна;Сервісний центр;Регіон;Тип ТЗ\n";
    const body = rows.map(r=>[r.plate,r.price,r.tsc,r.region,r.type].map(csvCell).join(";")).join("\n");
    dl("avtonomera.csv", "﻿"+head+body, "text/csv;charset=utf-8");
  });
};
document.getElementById("json").onclick = function(){
  chrome.storage.local.get({table:{}}, d=>dl("avtonomera.json", JSON.stringify(Object.values(d.table),null,2), "application/json"));
};
document.getElementById("clr").onclick = function(){ chrome.storage.local.set({table:{}}, refresh); };
setInterval(refresh,1000); refresh();
