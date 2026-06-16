function refresh() {
  chrome.storage.local.get({ entries: [] }, function (d) {
    document.getElementById("count").textContent = d.entries.length;
  });
}
document.getElementById("dl").onclick = function () {
  chrome.storage.local.get({ entries: [] }, function (d) {
    const blob = new Blob([JSON.stringify(d.entries, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    chrome.downloads.download({ url: url, filename: "avto-capture.json", saveAs: true });
  });
};
document.getElementById("clr").onclick = function () {
  chrome.storage.local.set({ entries: [] }, refresh);
};
refresh();
