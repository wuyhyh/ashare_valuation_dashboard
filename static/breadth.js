"use strict";
const elem=id=>document.getElementById(id);
const fmt=n=>n==null?"—":Number(n).toLocaleString("zh-CN",{maximumFractionDigits:2});
const fmtPct=n=>n==null?"—":(Number(n)*100).toFixed(1)+"%";
const numberPct=n=>n==null?"—":Number(n).toFixed(2)+"%";
const scopeBoards={
  SH_MAIN:["SH_MAIN"],SZ_MAIN:["SZ_MAIN"],CHINEXT:["CHINEXT"],STAR:["STAR"],BSE:["BSE"],
  MAIN_ALL:["SH_MAIN","SZ_MAIN"],SH_SZ_ALL:["SH_MAIN","SZ_MAIN","CHINEXT","STAR"],
  ALL_A:["SH_MAIN","SZ_MAIN","CHINEXT","STAR","BSE"]
};
let payload=null;
let trendSequence=0;
const SVG_NS="http://www.w3.org/2000/svg";
function svgTag(name,attrs,text){
  const node=document.createElementNS(SVG_NS,name);
  for(const [key,value] of Object.entries(attrs||{}))node.setAttribute(key,String(value));
  if(text!==undefined)node.textContent=text;
  return node;
}
function drawTrend(points){
  const svg=elem("breadth-trend-svg");
  svg.replaceChildren();
  const x=i=>50+i/(points.length-1)*796;
  const y=value=>205-Math.max(0,Math.min(1,value))*176;
  for(const value of [0,0.5,1]){
    const yy=y(value);
    svg.append(svgTag("line",{x1:50,x2:846,y1:yy,y2:yy,stroke:"#dce4ef","stroke-dasharray":"4 6"}));
    svg.append(svgTag("text",{x:40,y:yy+4,"text-anchor":"end",fill:"#7588a2","font-size":12},Math.round(value*100)+"%"));
  }
  let segment=[];
  const flush=()=>{
    if(segment.length>=2){
      svg.append(svgTag("path",{d:segment.join(" "),fill:"none",stroke:"#397ce0","stroke-width":3,"stroke-linejoin":"round","stroke-linecap":"round"}));
    }
    segment=[];
  };
  points.forEach((point,i)=>{
    if(i && (Date.parse(point.trading_date+"T00:00:00Z")-Date.parse(points[i-1].trading_date+"T00:00:00Z"))>7*86400000)flush();
    const xx=x(i),yy=y(point.advance_ratio);
    segment.push((segment.length?"L":"M")+xx.toFixed(1)+" "+yy.toFixed(1));
  });
  flush();
  points.forEach((point,i)=>{
    const dot=svgTag("circle",{cx:x(i),cy:y(point.advance_ratio),r:3,fill:"#397ce0"});
    dot.append(svgTag("title",{},point.trading_date+" · 上涨 "+(point.advance_ratio*100).toFixed(1)+"% · 成交 "+point.traded_count+"只"));
    svg.append(dot);
  });
  elem("breadth-trend-start").textContent=points[0].trading_date;
  elem("breadth-trend-end").textContent=points[points.length-1].trading_date;
}
async function loadTrend(){
  const request=++trendSequence;
  const scope=elem("breadth-exchange").value;
  const exclude=elem("breadth-no-st").checked?"1":"0";
  const empty=elem("breadth-trend-empty"),chart=elem("breadth-trend-chart");
  const count=elem("breadth-trend-count"),info=elem("breadth-trend-info");
  chart.hidden=true;empty.hidden=false;empty.textContent="正在检查经过验证的本地历史记录…";
  try{
    const query=new URLSearchParams({scope,exclude_st:exclude,limit:"90"});
    const response=await fetch("/api/breadth-trend?"+query,{cache:"no-store"});
    if(!response.ok)throw new Error("HTTP "+response.status);
    const data=await response.json();
    if(request!==trendSequence)return;
    count.textContent="有效交易日："+(data.point_count||0);
    info.textContent=data.message||"未提供经过核验的历史数据。";
    if(data.available && Array.isArray(data.observations) && data.observations.length>=2){
      drawTrend(data.observations);
      chart.hidden=false;empty.hidden=true;
    }else{
      empty.textContent=data.message||"没有足够的真实核验交易日，不显示趋势。";
    }
  }catch(error){
    if(request!==trendSequence)return;
    count.textContent="有效交易日：—";
    empty.textContent="历史数据读取失败："+error.message;
    info.textContent="读取失败不会启用演示或伪造历史曲线。";
  }
}


function available(scope){return (payload?.available_scopes||[]).includes(scope);}
function group(scope){
  const suffix=elem("breadth-no-st").checked?"ex_st":"all";
  return payload?.groups?.[scope+"_"+suffix];
}
function coverage(scope){
  const audit=payload?.capture?.quality?.official_pool;
  if(!audit)return null;
  if(!audit.boards){
    return scope==="MAIN_ALL"?{official:audit.official_count,matched:audit.matched_count,ratio:audit.coverage_ratio}:null;
  }
  const items=(scopeBoards[scope]||[]).map(board=>audit.boards[board]).filter(Boolean);
  if(!items.length)return null;
  const official=items.reduce((sum,item)=>sum+item.official_count,0);
  const matched=items.reduce((sum,item)=>sum+item.matched_count,0);
  return {official,matched,ratio:official?matched/official:null};
}
function selectScope(scope){
  if(!available(scope))return;
  elem("breadth-exchange").value=scope;
  render();
}
function configureScopes(){
  const select=elem("breadth-exchange");
  for(const option of select.options)option.disabled=!available(option.value);
  if(!available(select.value))select.value=payload.default_scope||payload.available_scopes?.[0]||"MAIN_ALL";
  for(const button of document.querySelectorAll("#breadth-board-grid button")){
    const scope=button.dataset.scope;
    const data=group(scope),audit=coverage(scope);
    button.disabled=!available(scope);
    button.classList.toggle("active",scope===select.value);
    button.querySelector("strong").textContent=data?fmtPct(data.advance_ratio):"未覆盖";
    button.querySelector("small").textContent=data
      ?fmt(data.identified_count??data.identified_mainboard)+"只 · 名录"+(audit?fmtPct(audit.ratio):"未核验")
      :"本快照无该板块";
  }
}
function render(){
  if(!payload?.available){
    elem("breadth-report").hidden=true;elem("breadth-empty").hidden=false;
    elem("breadth-empty").textContent=payload?.message||"无真实市场宽度快照。请在终端执行 python app.py breadth-update。";return;
  }
  elem("breadth-empty").hidden=true;elem("breadth-report").hidden=false;
  configureScopes();
  const scope=elem("breadth-exchange").value,data=group(scope);
  if(!data){elem("breadth-empty").hidden=false;elem("breadth-report").hidden=true;return;}
  const warning=elem("breadth-scope-warning");
  warning.hidden=!payload.scope_warning;warning.textContent=payload.scope_warning||"";
  elem("breadth-captured").textContent="采集时间（北京时间） "+new Date(payload.capture.captured_at_utc).toLocaleString("zh-CN",{timeZone:"Asia/Shanghai",hour12:false});
  elem("breadth-source").textContent=payload.capture.source_interface+" · "+payload.capture.source_version+" · "+payload.capture.coverage_scope;
  const evidence=payload.capture.quality?.close_verification;
  const verifiedBoards=new Set(evidence?.checked_boards||[]);
  const selectedBoards=scopeBoards[scope]||[];
  const hasVerifiedClose=Boolean(payload.capture.trading_date_verified && evidence?.status==="passed"
    && selectedBoards.length && selectedBoards.every(board=>verifiedBoards.has(board)));
  elem("breadth-date-trust").textContent=hasVerifiedClose
    ?"已核验收盘日 "+payload.capture.trading_date+"（独立抽样）"
    :payload.capture.trading_date_verified
      ?"仅部分板块经过收盘抽样核验；当前范围未全部核验"
      :"交易日期尚未验证（非确认收盘）";
  elem("b-up").textContent=fmt(data.advances);
  elem("b-down").textContent=fmt(data.declines);
  elem("b-flat").textContent=fmt(data.unchanged);
  elem("b-ratio").textContent=fmtPct(data.advance_ratio);
  const total=data.traded_count||0;
  for(const [id,value] of [["b-up-bar",data.advances],["b-flat-bar",data.unchanged],["b-down-bar",data.declines]]){
    elem(id).style.width=(total?value/total*100:0)+"%";
  }
  elem("b-identified").textContent=fmt(data.identified_count??data.identified_mainboard);
  elem("b-selected").textContent=fmt(data.selected_count);
  elem("b-traded").textContent=fmt(data.traded_count);
  elem("b-untraded").textContent=fmt(data.zero_turnover_count)+" / "+fmt(data.missing_quote_count);
  elem("b-risk").textContent=fmt(data.risk_warning);
  elem("b-median").textContent=numberPct(data.median_change_pct);
  elem("b-weighted").textContent=numberPct(data.turnover_weighted_change_pct);
  elem("b-raw").textContent=fmt(payload.capture.quality.raw_count);
  const audit=coverage(scope);
  elem("b-pool").textContent=audit?fmt(audit.matched)+" / "+fmt(audit.official):"未核验";
  elem("b-coverage").textContent=audit?fmtPct(audit.ratio):"未核验";
  elem("breadth-history").textContent=payload.historical_trend_reason;
  configureScopes();
}
async function reload(){
  try{
    const response=await fetch("/api/breadth",{cache:"no-store"});
    if(!response.ok)throw new Error("HTTP "+response.status);
    payload=await response.json();render();await loadTrend();
  }catch(error){payload={available:false,message:"读取真实快照失败："+error.message};render();}
}
elem("breadth-exchange").addEventListener("change",()=>{render();loadTrend();});
elem("breadth-no-st").addEventListener("change",()=>{render();loadTrend();});
elem("breadth-reload").addEventListener("click",reload);
for(const button of document.querySelectorAll("#breadth-board-grid button"))button.addEventListener("click",()=>{selectScope(button.dataset.scope);loadTrend();});
reload();
