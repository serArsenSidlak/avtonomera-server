function r(){ chrome.storage.local.get({entries:[]}, d=>{ document.getElementById("c").textContent=d.entries.length; }); }
document.getElementById("dl").onclick=function(){
  chrome.storage.local.get({entries:[]}, d=>{
    const blob=new Blob([JSON.stringify(d.entries,null,2)],{type:"application/json"});
    chrome.downloads.download({url:URL.createObjectURL(blob), filename:"avto-traffic.json", saveAs:true});
  });
};
document.getElementById("clr").onclick=function(){ chrome.storage.local.set({entries:[]}, r); };
setInterval(r,1000); r();
