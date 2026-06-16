// Schedules a gentle harvest every 3h (alarms) and forwards staged scopes to the server.
const SERVER = "https://34.123.136.171.nip.io";
const INGEST_SECRET = "ing__0VaRFvnOC57sc7baY6H0dMzKx21PNJD";

chrome.runtime.onInstalled.addListener(() => chrome.alarms.create("autoscan", { periodInMinutes: 180 }));
chrome.runtime.onStartup.addListener(() => chrome.alarms.create("autoscan", { periodInMinutes: 180 }));

function tellTab(payload) {
  chrome.tabs.query({ url: "*://e-driver.mvs.gov.ua/*" }, (tabs) => {
    if (!tabs || !tabs.length) { chrome.storage.local.set({ status: "Відкрий вкладку e-driver.mvs.gov.ua (залогінений) для збору" }); return; }
    chrome.tabs.sendMessage(tabs[0].id, payload);
  });
}

chrome.alarms.onAlarm.addListener((a) => { if (a.name === "autoscan") tellTab({ type: "autoharvest" }); });

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (!msg) return false;
  if (msg.type === "manualHarvest") { tellTab({ type: "harvest" }); reply && reply({ ok: true }); return false; }
  if (msg.type === "stage") {
    fetch(SERVER + "/stage", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ secret: INGEST_SECRET, rows: msg.rows || [], reset: !!msg.reset, done: !!msg.done, scopes: msg.scopes || [] }),
    }).then((r) => reply({ status: r.status })).catch((e) => reply({ status: "err:" + e }));
    return true;
  }
  return false;
});
