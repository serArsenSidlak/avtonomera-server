// Orchestrates: finds an e-driver tab and tells its content script to harvest; forwards each
// collected scope to our server's /ingest (background has host permission → no CORS).
const SERVER = "https://34.123.136.171.nip.io";
const INGEST_SECRET = "ing__0VaRFvnOC57sc7baY6H0dMzKx21PNJD";

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (msg && msg.type === "harvest") {
    chrome.tabs.query({ url: "*://e-driver.mvs.gov.ua/*" }, (tabs) => {
      if (!tabs || !tabs.length) {
        chrome.storage.local.set({ status: "Відкрий вкладку e-driver.mvs.gov.ua (залогінений) і тисни знову" });
        return;
      }
      chrome.tabs.sendMessage(tabs[0].id, { type: "harvestInPage" });
      chrome.storage.local.set({ status: "Запускаю збір на сторінці e-driver…" });
    });
    reply && reply({ started: true });
    return false;
  }
  if (msg && msg.type === "pushScope") {
    fetch(SERVER + "/ingest", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ secret: INGEST_SECRET, rows: msg.rows || [], ok_scopes: [[msg.rname, msg.label]] }),
    }).then((r) => reply({ status: r.status })).catch((e) => reply({ status: "err:" + e }));
    return true; // keep channel open for async reply
  }
  return false;
});
