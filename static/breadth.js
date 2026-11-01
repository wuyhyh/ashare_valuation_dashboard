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
let linkageSequence=0;
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
  const drawAverage=(field,color,width)=>{
    let averageSegment=[];
    const flushAverage=()=>{
      if(averageSegment.length>=2)svg.append(svgTag("path",{
        d:averageSegment.join(" "),fill:"none",stroke:color,"stroke-width":width,
        "stroke-linejoin":"round","stroke-linecap":"round"
      }));
      averageSegment=[];
    };
    points.forEach((point,i)=>{
      if(point[field]==null){flushAverage();return;}
      if(i && (Date.parse(point.trading_date+"T00:00:00Z")-Date.parse(points[i-1].trading_date+"T00:00:00Z"))>7*86400000)flushAverage();
      averageSegment.push((averageSegment.length?"L":"M")+x(i).toFixed(1)+" "+y(point[field]).toFixed(1));
    });
    flushAverage();
  };
  drawAverage("advance_ratio_ma5","#f09b3d",2.2);
  drawAverage("advance_ratio_ma20","#845ec2",2.2);
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
    const averages=data.moving_averages||{};
    const averageNote=averages.ma20_ready?" · 已显示5/20个核验交易日均线":averages.ma5_ready?" · 已显示5个核验交易日均线；20日样本不足":"";
    info.textContent=(data.message||"未提供经过核验的历史数据。")+averageNote;
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

async function loadLinkage(){
  const request=++linkageSequence;
  const scope=elem("breadth-exchange").value;
  const exclude=elem("breadth-no-st").checked?"1":"0";
  const status=elem("breadth-linkage-status"),results=elem("breadth-linkage-results");
  status.textContent="正在检查严格同日样本…";results.hidden=true;results.replaceChildren();
  try{
    const query=new URLSearchParams({scope,exclude_st:exclude});
    const response=await fetch("/api/breadth-linkage?"+query,{cache:"no-store"});
    if(!response.ok)throw new Error("HTTP "+response.status);
    const data=await response.json();
    if(request!==linkageSequence)return;
    status.textContent=data.message||"没有达到联动门槛的严格同日样本。";
    const labels={"000300":"沪深300","000905":"中证500","000852":"中证1000"};
    const signed=(value,digits=2)=>value==null?"—":(Number(value)>=0?"+":"")+Number(value).toFixed(digits);
    const appendText=(parent,tag,className,value)=>{
      const node=document.createElement(tag);node.className=className;node.textContent=value;parent.append(node);return node;
    };
    for(const [code,item] of Object.entries(data.indices||{})){
      const card=document.createElement("article");card.className="breadth-linkage-card";
      appendText(card,"h3","",labels[code]||code);
      const divergence=item.divergence||{};
      const divergenceBlock=document.createElement("section");divergenceBlock.className="breadth-linkage-item";
      appendText(divergenceBlock,"h4","","20日指数探索性差值");
      if(divergence.available){
        appendText(divergenceBlock,"strong","linkage-value",signed(divergence.divergence_percentage_points)+" 个百分点");
        appendText(divergenceBlock,"p","","上涨比例变化 "+signed(divergence.breadth_change_percentage_points)+" 个百分点 − 指数收益 "+signed(divergence.index_return_pct)+"%");
        appendText(divergenceBlock,"small","",divergence.start_date+" → "+divergence.end_date+" · "+divergence.common_dates+"个严格同日样本");
      }else{
        appendText(divergenceBlock,"strong","linkage-unavailable","不可用");
        appendText(divergenceBlock,"p","",divergence.message||"指数同日样本不足");
        appendText(divergenceBlock,"small","",(divergence.common_dates||0)+" / "+(divergence.required_common_dates||20)+" 个所需共同日");
      }
      appendText(divergenceBlock,"small","","指数来源 "+(item.index_close_source||"未采集")+" · "+(item.index_close_frequency||"频率未知")+"；宽度来源 "+(data.breadth_source_interface||"尚无合格历史"));
      appendText(divergenceBlock,"small","linkage-method",divergence.calculation||"上涨比例百分点变化与指数百分比收益的探索性差值，不是预测信号");
      card.append(divergenceBlock);
      for(const [metric,relation] of Object.entries(item.valuation_relationship||{})){
        const block=document.createElement("section");block.className="breadth-linkage-item";
        const label=metric==="pe_ttm"?"PE TTM":"PB";
        appendText(block,"h4","",label+" 与上涨比例");
        if(relation.available){
          appendText(block,"strong","linkage-value","Pearson r = "+Number(relation.correlation).toFixed(3));
          appendText(block,"p","",relation.start_date+" → "+relation.end_date+" · "+relation.common_dates+"个严格同日样本");
        }else{
          appendText(block,"strong","linkage-unavailable","不可用");
          appendText(block,"p","",relation.message||"严格同日估值样本不足");
        }
        const monthDetail=relation.frequency==="monthly"
          ?" · "+(relation.common_months||0)+"个自然月 / 跨度"+(relation.calendar_month_span||0)+"月 · 缺月"+(relation.missing_month_count||0)
          :"";
        appendText(block,"small","",(relation.common_dates||0)+" / "+(relation.required_common_dates||12)+" 个所需共同日"+monthDetail);
        appendText(block,"small","","来源 "+(relation.source_interface||"未采集")+" · "+(relation.frequency||"频率未知"));
        appendText(block,"small","linkage-method",relation.calculation||"严格同日相关性，不填充、不插值");
        card.append(block);
      }
      results.append(card);
    }
    results.className="breadth-linkage-results";
    results.hidden=!results.childElementCount;
  }catch(error){
    if(request!==linkageSequence)return;
    status.textContent="联动状态读取失败："+error.message+"；不会生成替代结果。";
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
    payload=await response.json();render();await Promise.all([loadTrend(),loadLinkage()]);
  }catch(error){payload={available:false,message:"读取真实快照失败："+error.message};render();}
}
elem("breadth-exchange").addEventListener("change",()=>{render();loadTrend();loadLinkage();});
elem("breadth-no-st").addEventListener("change",()=>{render();loadTrend();loadLinkage();});
elem("breadth-reload").addEventListener("click",reload);
for(const button of document.querySelectorAll("#breadth-board-grid button"))button.addEventListener("click",()=>{selectScope(button.dataset.scope);loadTrend();loadLinkage();});
reload();
