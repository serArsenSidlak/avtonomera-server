function refresh() {
  chrome.storage.local.get({ status: "—" }, (d) => { document.getElementById("status").textContent = d.status; });
}
function ctrl(cmd, note) {
  document.getElementById("status").textContent = note;
  chrome.runtime.sendMessage({ type: "ctrl", cmd: cmd });
}
document.getElementById("start").onclick = () => ctrl("start", "▶️ Запускаю збір…");
document.getElementById("stop").onclick = () => ctrl("stop", "⏹ Зупиняю…");
document.getElementById("send").onclick = () => ctrl("send", "📤 Відправляю зібране…");
document.getElementById("clear").onclick = () => ctrl("clear", "🗑 Очищаю…");
setInterval(refresh, 1000); refresh();
