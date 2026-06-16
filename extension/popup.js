function r(){ chrome.storage.local.get({status:"—"}, d=>{ document.getElementById("status").textContent=d.status; }); }
document.getElementById("go").onclick=function(){ document.getElementById("status").textContent="▶️ Запускаю збір…"; chrome.runtime.sendMessage({type:"manualHarvest"}); };
setInterval(r,1000); r();
