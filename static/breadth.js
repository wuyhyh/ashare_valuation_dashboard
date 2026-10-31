"use strict";
const elem=id=>document.getElementById(id);
const fmt=n=>n==null?"—":Number(n).toLocaleString("zh-CN",{maximumFractionDigits:2});
const fmtPct=n=>n==null?"—":(Number(n)*100).toFixed(1)+"%";
const numberPct=n=>n==null?"—":Number(n).toFixed(2)+"%";
let payload=null;
function render(){
  if(!payload?.available){elem("breadth-report").hidden=true;elem("breadth-empty").hidden=false;
    elem("breadth-empty").textContent=payload?.message||"无真实主板行情快照。请在终端执行 python app.py breadth-update。";return;}
  elem("breadth-empty").hidden=true;elem("breadth-report").hidden=false;
  const ex=elem("breadth-exchange").value,st=elem("breadth-no-st").checked,
    data=payload.groups[ex+"_"+(st?"ex_st":"all")];
  if(!data){elem("breadth-empty").hidden=false;elem("breadth-report").hidden=true;return;}
  elem("breadth-captured").textContent="采集时间（北京时间） "+new Date(payload.capture.captured_at_utc).toLocaleString("zh-CN",{timeZone:"Asia/Shanghai",hour12:false});
  elem("breadth-source").textContent=payload.capture.source_interface+" · "+payload.capture.source_version;
  elem("breadth-date-trust").textContent=payload.capture.trading_date_verified?"已核验交易日 "+payload.capture.trading_date:"交易日期尚未验证（非确认收盘）";
  elem("b-up").textContent=fmt(data.advances);
  elem("b-down").textContent=fmt(data.declines);
  elem("b-flat").textContent=fmt(data.unchanged);
  elem("b-ratio").textContent=fmtPct(data.advance_ratio);
  const total=data.traded_count||0;
  for(const [id,value] of [["b-up-bar",data.advances],["b-flat-bar",data.unchanged],["b-down-bar",data.declines]]){
    elem(id).style.width=(total?value/total*100:0)+"%";
  }
  elem("b-identified").textContent=fmt(data.identified_mainboard);
  elem("b-selected").textContent=fmt(data.selected_count);
  elem("b-traded").textContent=fmt(data.traded_count);
  elem("b-untraded").textContent=fmt(data.zero_turnover_count)+" / "+fmt(data.missing_quote_count);
  elem("b-risk").textContent=fmt(data.risk_warning);
  elem("b-median").textContent=numberPct(data.median_change_pct);
  elem("b-weighted").textContent=numberPct(data.turnover_weighted_change_pct);
  elem("b-raw").textContent=fmt(payload.capture.quality.raw_count);
  elem("breadth-history").textContent=payload.historical_trend_reason;
}
async function reload(){
  try{
    const response=await fetch("/api/breadth",{cache:"no-store"});
    if(!response.ok)throw new Error("HTTP "+response.status);
    payload=await response.json();render();
  }catch(error){payload={available:false,message:"读取真实快照失败："+error.message};render();}
}
elem("breadth-exchange").addEventListener("change",render);
elem("breadth-no-st").addEventListener("change",render);
elem("breadth-reload").addEventListener("click",reload);
reload();
