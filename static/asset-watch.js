"use strict";
const byId=id=>document.getElementById(id);
const state={asset:"000688",window:"1y",data:null,token:0};
const WINDOWS=["3m","6m","1y","3y","5y","8y","10y","all"];
function selectControls(){
 document.querySelectorAll("#asset-tabs button").forEach(b=>b.classList.toggle("selected",b.dataset.asset===state.asset));
 document.querySelectorAll("#asset-windows button").forEach(b=>b.classList.toggle("selected",b.dataset.window===state.window));
}
function fmt(n,dp=2){return Number(n).toLocaleString("zh-CN",{maximumFractionDigits:dp,minimumFractionDigits:dp});}
function pct(n){return (n>=0?"+":"")+fmt(n)+"%";}
function axisStyle(windowKey,width=window.innerWidth){
 if(windowKey==="3m")return {dtick:width<500?14*86400000:7*86400000,minor:7*86400000,tickformat:"%m-%d"};
 if(["6m","1y"].includes(windowKey))return {dtick:"M1",minor:7*86400000,tickformat:"%Y-%m"};
 if(["3y","5y"].includes(windowKey))return {dtick:"M3",minor:"M1",tickformat:"%Y-%m"};
 if(["8y","10y"].includes(windowKey))return {dtick:"M6",minor:"M3",tickformat:"%Y-%m"};
 return {dtick:"M12",minor:"M3",tickformat:"%Y"};
}
function styleFromVisibleRange(range,width=window.innerWidth){
 if(!Array.isArray(range)||range.length!==2)return axisStyle("all",width);
 const from=Date.parse(range[0]),to=Date.parse(range[1]);
 if(!Number.isFinite(from)||!Number.isFinite(to)||to<=from)return axisStyle("all",width);
 const days=(to-from)/86400000;
 if(days<=95)return {dtick:width<500?14*86400000:7*86400000,minor:7*86400000,tickformat:"%m-%d"};
 if(days<=370)return axisStyle("1y",width);
 if(days<=5*366)return axisStyle("5y",width);
 if(days<=10*366)return axisStyle("10y",width);
 return axisStyle("all",width);
}
function bindAdaptiveZoom(){
 const gd=byId("asset-chart");
 if(gd.__assetAdaptiveZoomBound)return;
 gd.__assetAdaptiveZoomBound=true;
 const update=()=>{
  const range=gd._fullLayout?.xaxis?.range;
  if(!range)return;
  const wanted=styleFromVisibleRange(range,gd.clientWidth),xaxis=gd.layout?.xaxis||{};
  if(wanted.dtick!==xaxis.dtick||wanted.tickformat!==xaxis.tickformat||wanted.minor!==xaxis.minor?.dtick){
   Promise.resolve(Plotly.relayout(gd,{"xaxis.dtick":wanted.dtick,"xaxis.tickformat":wanted.tickformat,"xaxis.minor.dtick":wanted.minor}))
      .catch(err=>console.warn("Date ticks could not be updated",err));
  }
 };
 gd.on("plotly_relayout",event=>{
  if(!event||!(event["xaxis.range[0]"]||event["xaxis.range[1]"]||event["xaxis.autorange"]))return;
  update();
 });
 if(!window.__assetAxisResizeBound){
  window.__assetAxisResizeBound=true;
  let timer;
  window.addEventListener("resize",()=>{clearTimeout(timer);timer=setTimeout(update,120);});
 }
}
function showEmpty(msg){
 byId("asset-quality").hidden=true;
 byId("asset-empty").textContent=msg;
 byId("asset-empty").hidden=false;
 byId("asset-chart").hidden=true;
 byId("asset-stats").hidden=true;
 byId("asset-verification").textContent="";
}
function render(){
 selectControls();
 const p=state.data;
 byId("asset-title").textContent=(p?.meta?.name||"跨资产观察")+" · 历史收盘";
 if(!p||!p.available){byId("asset-status").textContent="无真实日线";byId("asset-meta").textContent="缺少本机独立行情序列";showEmpty("当前标的尚无真实日线数据。请执行 python app.py assets-update --asset "+state.asset+" 后刷新。");return;}
 const stats=p.stats?.[state.window];
 if(!stats?.available){byId("asset-status").textContent="历史不足";byId("asset-meta").textContent=p.source?.first_date+" → "+p.source?.last_date+" · "+p.source?.source_interface;showEmpty("所选区间没有足够的真实数据："+(stats?.reason||"未知原因")+"。可以选择更短区间或全部历史。");return;}
 byId("asset-empty").hidden=true;byId("asset-chart").hidden=false;byId("asset-stats").hidden=false;
 const selected=p.rows.filter(r=>r.date>=stats.start&&r.date<=stats.end);
 const unit=p.meta.unit;
 byId("asset-status").textContent="最新真实收盘 "+p.source.last_date;
 byId("asset-close").textContent=fmt(stats.last_close,p.meta.type==="gold_etf"?3:2)+" "+unit;
 byId("asset-return").textContent=pct(stats.return_pct);
 byId("asset-drawdown").textContent=pct(stats.max_drawdown_pct);
 byId("asset-samples").textContent=String(stats.samples)+" 条";
 const source=p.source;
 byId("asset-meta").textContent="日期 "+stats.start+" → "+stats.end+" · 来源 "+source.source_name+" ("+source.source_interface+") · AKShare "+source.source_version+" · 全部历史 "+source.first_date+" → "+source.last_date+" · "+source.method+" · 采集时间 "+source.fetched_at;
 const warnings=p.quality?.warnings||[];
 byId("asset-quality").hidden=!warnings.length;
 byId("asset-quality").textContent=warnings.join("；");
 const verification=source.verification||{};
 const verificationNode=byId("asset-verification");
 verificationNode.classList.toggle("warn",verification.status!=="verified");
 verificationNode.textContent=verification.status==="verified"
   ?"独立同日交叉核验通过 · "+verification.source_interface+" · 共同日期 "+verification.common_dates+" 条 · 最近30日最大相对收盘差 "+(Number(verification.max_relative_close_difference)*100).toFixed(4)+"%"
   :"独立交叉核验未完成："+(verification.reason||"无可用结果");
 const axis=axisStyle(state.window,byId("asset-chart").clientWidth);
 Plotly.react("asset-chart",[{
   x:selected.map(x=>x.date),y:selected.map(x=>x.close),type:"scattergl",mode:"lines",
   line:{color:"#3f75f0",width:2.2},name:p.meta.name,
   hovertemplate:"日期 %{x|%Y-%m-%d}<br>收盘 %{y:.3f} "+unit+"<extra></extra>"
 }],{
   margin:{l:64,r:23,t:15,b:66},paper_bgcolor:"#fff",plot_bgcolor:"#fff",
   hovermode:"closest",showlegend:false,
   xaxis:{type:"date",tickformat:axis.tickformat,dtick:axis.dtick,showgrid:true,gridcolor:"#e6edf6",minor:{dtick:axis.minor,showgrid:true,gridcolor:"#f3f6fa"},rangeslider:{visible:true,thickness:.08}},
   yaxis:{title:{text:unit},tickformat:".2f",showgrid:true,gridcolor:"#edf2f7"},
   font:{family:"-apple-system, PingFang SC, sans-serif",color:"#60718a",size:11},
   uirevision:state.asset+":"+state.window
 },{responsive:true,displaylogo:false,scrollZoom:true}).then(bindAdaptiveZoom);
}
async function load(){
 const token=++state.token;
 byId("asset-status").textContent="正在读取本地真实数据";
 try{
  const response=await fetch("/api/assets?asset="+encodeURIComponent(state.asset),{cache:"no-store"});
  if(!response.ok)throw new Error("HTTP "+response.status);
  const payload=await response.json();if(token!==state.token)return;
  state.data=payload;render();
 }catch(e){if(token!==state.token)return;state.data=null;showEmpty("读取本地数据失败："+e.message);byId("asset-status").textContent="读取失败";}
}
byId("asset-tabs").addEventListener("click",e=>{const b=e.target.closest("button[data-asset]");if(!b||b.dataset.asset===state.asset)return;state.asset=b.dataset.asset;state.data=null;load();});
byId("asset-windows").addEventListener("click",e=>{const b=e.target.closest("button[data-window]");if(!b)return;state.window=b.dataset.window;render();});
byId("asset-reload").addEventListener("click",load);
load();
