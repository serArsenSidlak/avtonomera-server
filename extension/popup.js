function refresh() {
  chrome.storage.local.get({ status: "—" }, (d) => { document.getElementById("status").textContent = d.status; });
}
document.getElementById("go").onclick = function () {
  document.getElementById("status").textContent = "Запускаю збір…";
  chrome.runtime.sendMessage({ type: "harvest" });
};
setInterval(refresh, 1000); refresh();
