// Routes control commands (start/stop/send/clear) to the e-driver tab's content script, and
// forwards collected scopes to our server's /ingest (background has host permission → no CORS).
const SERVER = "https://34.123.136.171.nip.io";
const INGEST_SECRET = "ing__0VaRFvnOC57sc7baY6H0dMzKx21PNJD";

function toEdriverTab(payload) {
  chrome.tabs.query({ url: "*://e-driver.mvs.gov.ua/*" }, (tabs) => {
    if (!tabs || !tabs.length) {
      chrome.storage.local.set({ status: "Відкрий вкладку e-driver.mvs.gov.ua (залогінений) і повтори" });
      return;
    }
    chrome.tabs.sendMessage(tabs[0].id, payload);
  });
}

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (!msg) return false;
  if (msg.type === "ctrl") { toEdriverTab({ type: msg.cmd }); reply && reply({ ok: true }); return false; }
  if (msg.type === "pushScope") {
    fetch(SERVER + "/ingest", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ secret: INGEST_SECRET, rows: msg.rows || [], ok_scopes: [[msg.rname, msg.label]] }),
    }).then((r) => reply({ status: r.status })).catch((e) => reply({ status: "err:" + e }));
    return true;
  }
  return false;
});
