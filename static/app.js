"use strict";

const $ = id => document.getElementById(id);
const DAY_MS = 86400000;
const labels = {pe_ttm:"市盈率 TTM",pb:"市净率 PB"};
const indexNames = {"000016":"上证50","000300":"沪深300","000905":"中证500","000852":"中证1000"};
const chartColors = {market:"#263b59",series:"#3b74ef",mean:"#d99a22",high:"#df6d67",low:"#319985",current:"#245fd5"};
const comparisonColors = {"000016":"#6683c3","000300":"#3674ee","000905":"#2e9a87","000852":"#d08b38"};
const displayWindowOptions = [
  {key:"3m",label:"近3月",months:3},{key:"6m",label:"近6月",months:6},{key:"1y",label:"近1年",months:12},
  {key:"3y",label:"近3年",months:36},{key:"5y",label:"近5年",months:60},{key:"8y",label:"近8年",months:96},
  {key:"10y",label:"近10年",months:120},{key:"all",label:"全部",months:null}
];
const statsWindowOptions = [
  {key:"3y",label:"3年",months:36},{key:"5y",label:"5年",months:60},{key:"8y",label:"8年",months:96},
  {key:"10y",label:"10年",months:120},{key:"all",label:"全部",months:null}
];
const state = {
  index:"000300",metric:"pe_ttm",mode:"single",displayWindow:"8y",statsWindow:"10y",isMaximized:false,
  valuationRows:[],marketRows:[],demo:true,name:"沪深300",dataStatus:{},marketStatus:{},comparisonData:null,
  comparisonResults:[],busy:false,axisUpdateInFlight:false,visibleRange:null,pendingRange:null,statsSnapshot:null,requestToken:0
};

const fmt = value => Number.isFinite(value)?value.toFixed(2):"—";
const escapeHtml = value => String(value??"").replace(/[&<>'"]/g,char=>({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]));
function utcDate(value){return new Date(`${String(value).slice(0,10)}T00:00:00Z`);}
function isoDate(value){return new Date(value).toISOString().slice(0,10);}
function axisDate(value){const parsed=new Date(value);return Number.isFinite(parsed.getTime())?parsed:utcDate(value);}
function rangeSpanDays(range){return !range||range.length<2?0:Math.abs(axisDate(range[1])-axisDate(range[0]))/DAY_MS;}
function subtractCalendarMonths(value,months){
  const source=utcDate(value),absoluteMonth=source.getUTCFullYear()*12+source.getUTCMonth()-months;
  const year=Math.floor(absoluteMonth/12),month=absoluteMonth-year*12,lastDay=new Date(Date.UTC(year,month+1,0)).getUTCDate();
  return isoDate(Date.UTC(year,month,Math.min(source.getUTCDate(),lastDay)));
}
function optionFor(options,key){return options.find(option=>option.key===key);}
function filterWindow(rows,key,options,anchorDate=null){
  const option=optionFor(options,key);if(!rows.length||!option||option.months===null)return rows.filter(row=>!anchorDate||row.date<=anchorDate);
  const end=anchorDate||rows.at(-1).date,cutoff=subtractCalendarMonths(end,option.months);
  return rows.filter(row=>row.date>=cutoff&&row.date<=end);
}
function latestDate(rowsList){return rowsList.flatMap(rows=>rows.length?[rows.at(-1).date]:[]).sort().at(-1)||null;}
function earliestDate(rowsList){return rowsList.flatMap(rows=>rows.length?[rows[0].date]:[]).sort()[0]||null;}
function displayDateBounds(){
  const end=latestDate([state.valuationRows,state.marketRows]);if(!end)return null;
  const option=optionFor(displayWindowOptions,state.displayWindow);
  return [option?.months===null?earliestDate([state.valuationRows,state.marketRows]):subtractCalendarMonths(end,option.months),end];
}
function rowsWithinBounds(rows,bounds){return bounds?rows.filter(row=>row.date>=bounds[0]&&row.date<=bounds[1]):[];}

function timeScalePreset(range){
  const days=rangeSpanDays(range);
  if(days<=93)return{name:"weekly",majorDtick:7*DAY_MS,minorDtick:DAY_MS,tick0:"2000-01-03",tickformat:"%Y-%m-%d",labelWidth:82,majorDays:7};
  if(days<460)return{name:"monthly",majorDtick:"M1",minorDtick:7*DAY_MS,tick0:"2000-01-01",minorTick0:"2000-01-03",tickformat:"%Y-%m",labelWidth:66,majorDays:30.44};
  if(days<5*366)return{name:"quarterly",majorDtick:"M3",minorDtick:"M1",tick0:"2000-01-01",tickformat:"%Y-%m",labelWidth:66,majorDays:91.31};
  if(days<=10*366+62)return{name:"halfyear",majorDtick:"M6",minorDtick:"M3",tick0:"2000-01-01",tickformat:"%Y-%m",labelWidth:66,majorDays:182.63};
  return{name:"yearly",majorDtick:"M12",minorDtick:"M3",tick0:"2000-01-01",tickformat:"%Y",labelWidth:48,majorDays:365.25};
}
function timeAxisConfig(range,width,{slider=false,labelsVisible=true}={}){
  const preset=timeScalePreset(range);let majorDtick=preset.majorDtick,majorDays=preset.majorDays,tickformat=preset.tickformat;
  if(width<520){
    if(preset.name==="halfyear"){majorDtick="M12";majorDays=365.25;tickformat="%Y";}
    else if(preset.name==="quarterly"){majorDtick="M6";majorDays=182.63;}
    else if(preset.name==="monthly"){majorDtick="M2";majorDays=60.88;}
    else if(preset.name==="weekly"&&rangeSpanDays(range)>45){majorDtick=14*DAY_MS;majorDays=14;}
  }
  const count=Math.max(1,Math.ceil(rangeSpanDays(range)/majorDays)+1),maxLabels=Math.max(3,Math.floor(Math.max(width,260)/preset.labelWidth));
  return{type:"date",range:[...range],tick0:preset.tick0,dtick:majorDtick,tickformat,ticklabelstep:Math.max(1,Math.ceil(count/maxLabels)),tickangle:width<520?-35:0,
    showticklabels:labelsVisible,showgrid:true,gridcolor:"#dfe6ef",gridwidth:1,minor:{tick0:preset.minorTick0||preset.tick0,dtick:preset.minorDtick,showgrid:true,gridcolor:"#f2f5f9",gridwidth:1,ticks:""},
    zeroline:false,showline:true,linecolor:"#dfe5ee",linewidth:1,automargin:true,fixedrange:false,hoverformat:"%Y-%m-%d",showspikes:true,spikemode:"across",spikesnap:"cursor",spikecolor:"#8fa0b6",spikethickness:1,spikedash:"dot",
    rangeslider:{visible:slider,thickness:.08,bgcolor:"#f8fafc",bordercolor:"#e3e9f1",borderwidth:1}};
}
function niceStep(span,targetTicks=6){const rough=Math.max(span,Number.EPSILON)/targetTicks,power=10**Math.floor(Math.log10(rough)),fraction=rough/power;return(fraction<=1?1:fraction<=2?2:fraction<=5?5:10)*power;}
function minorStep(step){const power=10**Math.floor(Math.log10(step));return step/((step/power)>=5?5:2);}
function tickFormat(step){return step>=.1?".1f":step>=.01?".2f":".3f";}
function numericAxisConfig(values,title,{valuation=false,stats=null}={}){
  const included=values.slice();if(stats)included.push(stats.low,stats.high);let minimum=Math.min(...included),maximum=Math.max(...included);
  if(!Number.isFinite(minimum)||!Number.isFinite(maximum))return{visible:false};
  const span=maximum-minimum||Math.max(Math.abs(maximum)*.08,.02),padding=span*.1;let lower=minimum-padding,upper=maximum+padding;
  if(minimum>=0&&lower<=0)lower=minimum*.5;const major=niceStep(upper-lower);
  return{range:[lower,upper],tick0:Math.floor(lower/major)*major,dtick:major,tickformat:valuation?tickFormat(major):",.0f",title:{text:title,standoff:9,font:{size:12,color:"#53657c"}},showgrid:true,gridcolor:"#dfe6ef",gridwidth:1,
    minor:{dtick:minorStep(major),showgrid:true,gridcolor:"#f2f5f9",gridwidth:1,ticks:""},zeroline:false,showline:false,automargin:true,fixedrange:false,showspikes:true,spikemode:"across",spikesnap:"cursor",spikecolor:"#8fa0b6",spikethickness:1,spikedash:"dot"};
}
function paddedDateRange(bounds){const start=utcDate(bounds[0]).getTime(),end=utcDate(bounds[1]).getTime(),span=Math.max(end-start,30*DAY_MS),left=Math.min(span*.015,7*DAY_MS),right=Math.min(span*.075,30*DAY_MS);return{range:[isoDate(start-left),isoDate(end+right)],rightRatio:right/span};}
function statistics(rows){
  const values=rows.map(row=>row.value),n=values.length;if(!n)return null;
  const mean=values.reduce((sum,value)=>sum+value,0)/n,variance=values.reduce((sum,value)=>sum+(value-mean)**2,0)/n,sd=Math.sqrt(variance),current=values.at(-1);
  const below=values.filter(value=>value<current).length,equal=values.filter(value=>value===current).length;
  return{n,current,mean,sd,pct:(below+equal/2)/n*100,z:sd<1e-9?0:(current-mean)/sd,low:mean-sd,high:mean+sd,min:Math.min(...values),max:Math.max(...values),firstDate:rows[0].date,lastDate:rows.at(-1).date};
}
function selectedStatsRows(){return filterWindow(state.valuationRows,state.statsWindow,statsWindowOptions);}
function hasCoverage(rows,windowKey,asOf){
  const option=optionFor(statsWindowOptions,windowKey);if(!rows.length||!option||option.months===null)return Boolean(rows.length);
  return rows[0].date<=subtractCalendarMonths(asOf||rows.at(-1).date,option.months);
}
function decide(stats){
  if(stats.n<30)return{status:"样本不足，暂不判断",tone:"neutral",text:"当前统计基准不足 30 个有效观测点。"};
  if(stats.z<=-1)return{status:"处于历史相对低估区",tone:"cold",text:"当前指标低于所选统计基准均值超过 1 个标准差，但不是自动买入指令。"};
  if(stats.z<0)return{status:"处于历史中低估值区",tone:"cold",text:"当前估值低于所选统计基准均值，仍需结合盈利周期。"};
  if(stats.z<1)return{status:"处于历史中高估值区",tone:"warm",text:"当前估值高于所选统计基准均值，但尚未超过 +1σ。"};
  return{status:"处于历史相对高估区",tone:"hot",text:"当前估值高于所选统计基准均值超过 1 个标准差。"};
}

function annotationOffsets(items,range,height){
  const usable=Math.max(height,180),spacing=24,edge=12,span=range[1]-range[0]||1,placed=items.map((item,index)=>({index,desired:(item.value-range[0])/span*usable})).sort((a,b)=>a.desired-b.desired);
  placed.forEach((item,index)=>{item.position=Math.max(item.desired,index?placed[index-1].position+spacing:edge);});
  for(let index=placed.length-1;index>=0;index--){const ceiling=index===placed.length-1?usable-edge:placed[index+1].position-spacing;placed[index].position=Math.min(placed[index].position,ceiling);}
  const offsets=Array(items.length).fill(0);placed.forEach(item=>{offsets[item.index]=item.position-item.desired;});return offsets;
}
function valueAnnotations(stats,lastDate,xEnd,yRange,height,width,rightRatio){
  const items=[{label:"−1σ",value:stats.low,color:chartColors.low,x:xEnd},{label:"均值",value:stats.mean,color:chartColors.mean,x:xEnd},{label:"+1σ",value:stats.high,color:chartColors.high,x:xEnd},{label:"最新",value:stats.current,color:chartColors.current,x:lastDate,current:true}],offsets=annotationOffsets(items,yRange,height),gap=Math.max(24,width*rightRatio/(1+.015+rightRatio)+9);
  return items.map((item,index)=>({xref:"x2",yref:"y2",x:item.x,y:item.value,text:`<b>${item.label}</b> ${fmt(item.value)}`,showarrow:true,arrowhead:0,arrowwidth:1,arrowcolor:item.color,ax:item.current?gap:9,ay:-offsets[index],xanchor:"left",bgcolor:"rgba(255,255,255,.95)",bordercolor:item.color,borderwidth:1,borderpad:3,font:{size:10,color:item.color},captureevents:false}));
}
function plotConfig(filename,height=1050){return{responsive:true,displaylogo:false,doubleClick:"reset+autosize",scrollZoom:false,toImageButtonOptions:{format:"png",filename,height,width:1400,scale:2},modeBarButtonsToRemove:["lasso2d","select2d","zoomIn2d","zoomOut2d","autoScale2d","toggleSpikelines"]};}
function adaptiveAxisUpdate(range,width){
  const axis=timeAxisConfig(range,width),update={};["xaxis","xaxis2"].forEach(name=>{
    update[`${name}.tick0`]=axis.tick0;update[`${name}.dtick`]=axis.dtick;update[`${name}.tickformat`]=axis.tickformat;update[`${name}.ticklabelstep`]=axis.ticklabelstep;update[`${name}.tickangle`]=axis.tickangle;
    update[`${name}.minor.tick0`]=axis.minor.tick0;update[`${name}.minor.dtick`]=axis.minor.dtick;update[`${name}.minor.showgrid`]=true;update[`${name}.minor.gridcolor`]=axis.minor.gridcolor;update[`${name}.minor.gridwidth`]=axis.minor.gridwidth;
  });return update;
}
function relayoutRange(eventData){
  if(!eventData)return null;for(const axis of["xaxis2","xaxis"]){if(eventData[`${axis}.range[0]`]!==undefined&&eventData[`${axis}.range[1]`]!==undefined)return[eventData[`${axis}.range[0]`],eventData[`${axis}.range[1]`]];if(Array.isArray(eventData[`${axis}.range`]))return eventData[`${axis}.range`];}return null;
}
function applyAdaptiveTimeAxis(range){
  const chart=$("chart");if(!chart||!chart._fullLayout||!range||state.axisUpdateInFlight)return Promise.resolve();state.visibleRange=[...range];state.axisUpdateInFlight=true;
  try{
    const update=adaptiveAxisUpdate(range,chart.clientWidth||800);(chart.layout?.shapes||[]).forEach((shape,index)=>{if(shape.xref==="x2"){update[`shapes[${index}].x0`]=range[0];update[`shapes[${index}].x1`]=range[1];}});(chart.layout?.annotations||[]).forEach((annotation,index)=>{if(index<3&&annotation.xref==="x2")update[`annotations[${index}].x`]=range[1];});
    return Promise.resolve(Plotly.relayout(chart,update)).finally(()=>setTimeout(()=>{state.axisUpdateInFlight=false;},0));
  }catch(error){state.axisUpdateInFlight=false;throw error;}
}
let axisRelayoutTimer;
function handleTimeRelayout(eventData){
  if(state.axisUpdateInFlight)return;const requested=relayoutRange(eventData),reset=eventData?.["xaxis.autorange"]===true||eventData?.["xaxis2.autorange"]===true;if(!requested&&!reset)return;
  clearTimeout(axisRelayoutTimer);axisRelayoutTimer=setTimeout(()=>{const chart=$("chart"),range=requested||chart?._fullLayout?.xaxis2?.range||chart?._fullLayout?.xaxis?.range;if(range)applyAdaptiveTimeAxis(range);},45);
}
function bindSingleChart(initialRange){const chart=$("chart");state.visibleRange=[...initialRange];if(!chart||typeof chart.on!=="function"||chart.__axisBound)return;chart.on("plotly_relayout",handleTimeRelayout);chart.__axisBound=true;}

function syncControls(){
  $("idx").value=state.index;
  document.querySelectorAll("#metric button").forEach(button=>button.classList.toggle("selected",button.dataset.metric===state.metric));
  document.querySelectorAll("#analysis-mode button").forEach(button=>button.classList.toggle("selected",button.dataset.mode===state.mode));
  document.querySelectorAll("#window button").forEach(button=>button.classList.toggle("selected",button.dataset.window===state.displayWindow));
  document.querySelectorAll("#stats-window button").forEach(button=>button.classList.toggle("selected",button.dataset.statsWindow===state.statsWindow));
  $("analysis-layout").classList.toggle("compare-mode",state.mode==="compare");
  $("single-view").hidden=state.mode!=="single";$("compare-view").hidden=state.mode!=="compare";
  $("reset-zoom").hidden=state.mode!=="single";
  $("maximize-chart").textContent=state.isMaximized?"⛶ 还原":"⛶ 最大化";$("maximize-chart").setAttribute("aria-pressed",String(state.isMaximized));
}
function setupControls(){
  $("window").innerHTML=displayWindowOptions.map(option=>`<button data-window="${option.key}">${option.label}</button>`).join("");
  $("stats-window").innerHTML=statsWindowOptions.map(option=>`<button data-stats-window="${option.key}">${option.label}</button>`).join("");syncControls();
}
function setError(message){const box=$("error-banner");box.hidden=!message;box.textContent=message||"";}
function setBusy(busy,kind="valuation"){
  state.busy=busy;["refresh","refresh-market","choose-file"].forEach(id=>{$(id).disabled=busy;});
  $("refresh").textContent=busy&&kind==="valuation"?"正在更新估值…":"↻ 更新估值";$("refresh-market").textContent=busy&&kind==="market"?"正在更新点位…":"↻ 更新指数点位";
}
function localizedFrequency(value){return{daily:"日频",weekly:"周频",monthly:"月频",quarterly:"季频",irregular:"不规则",unknown:"待识别"}[value]||"待识别";}
function renderSingleQuality(){
  const status=state.dataStatus||{},market=state.marketStatus||{},rows=selectedStatsRows(),supported=state.statsWindow==="all"||hasCoverage(state.valuationRows,state.statsWindow,state.valuationRows.at(-1)?.date);
  let label="真实数据",tone="real";if(state.demo){label=status.last_update_status==="failed"?"演示估值 / 更新失败":"演示估值";tone="demo";}else if(status.last_update_status==="failed"){label="更新失败 · 已保留原估值";tone="error";}else if(status.is_stale){label="真实估值 · 已过期";tone="warn";}else if(!supported||rows.length<30){label="真实估值 · 样本不足";tone="warn";}else if(!state.marketRows.length){label="真实估值 · 点位缺失";tone="warn";}
  $("quality-state").className=`quality-badge ${tone}`;$("quality-state").textContent=label;
  $("quality-source").textContent=`${status.source_name||"—"}${status.source_interface?` · ${status.source_interface}`:""}`;$("quality-method").textContent=status.methodology||"—";
  $("quality-range").textContent=status.first_date&&status.last_date?`${status.first_date} → ${status.last_date}`:"—";$("quality-count").textContent=`${status.count??state.valuationRows.length} 条 · ${localizedFrequency(status.frequency)}`;
  $("quality-fetched").textContent=status.fetched_at?new Date(status.fetched_at).toLocaleString("zh-CN",{hour12:false}):state.demo?"演示数据未采集":"—";$("quality-gaps").textContent=status.has_gaps?`有 · 缺失 ${status.missing_periods||0} 个周期`:"未发现明显缺口";
  $("market-source").textContent=state.marketRows.length?`${market.source_name||"—"}${market.source_interface?` · ${market.source_interface}`:""}`:(market.last_error?`更新失败：${market.last_error}`:"暂无指数点位数据");$("market-range").textContent=market.first_date&&market.last_date?`${market.first_date} → ${market.last_date} · ${market.count} 条`:"—";
}
function dateMatchingSummary(){
  if(!state.marketRows.length)return"指数点位缺失，未做日期匹配";const marketDates=new Set(state.marketRows.map(row=>row.date)),matching=state.valuationRows.filter(row=>marketDates.has(row.date)).length,valuationLast=state.valuationRows.at(-1)?.date,marketLast=state.marketRows.at(-1)?.date;
  if(!valuationLast)return"共同交易日 0；估值缺失";const lag=Math.round((utcDate(marketLast)-utcDate(valuationLast))/DAY_MS),text=lag>0?`估值滞后指数 ${lag} 天`:lag<0?`指数滞后估值 ${-lag} 天`:"两者同日截止";return`共同交易日 ${matching}；${text}`;
}
function renderStatsSummary(stats,tenYearStats,selectedRows){
  const full=stats&&hasCoverage(state.valuationRows,state.statsWindow,stats.lastDate);$("stats-range").textContent=stats?`${stats.firstDate} → ${stats.lastDate}${full||state.statsWindow==="all"?"":"（覆盖不足）"}`:"—";$("stats-count").textContent=stats?`${stats.n} 条 · ${optionFor(statsWindowOptions,state.statsWindow)?.label||state.statsWindow}基准`:"—";
  const fullTen=tenYearStats&&hasCoverage(state.valuationRows,"10y",tenYearStats.lastDate);$("ten-year-extremes").textContent=tenYearStats?`${fullTen?"":"覆盖不足 · "}${fmt(tenYearStats.max)} / ${fmt(tenYearStats.min)}`:"—";const deviation=tenYearStats&&Math.abs(tenYearStats.mean)>1e-12?(tenYearStats.current/tenYearStats.mean-1)*100:null;$("ten-year-deviation").textContent=deviation===null?"—":`${fullTen?"":"实际覆盖 · "}${deviation>=0?"+":""}${fmt(deviation)}%`;$("date-match-status").textContent=dateMatchingSummary();
}

async function loadSingle({preserveRange=false}={}){
  const token=++state.requestToken;setError("");
  try{const response=await fetch(`/api/history?index=${encodeURIComponent(state.index)}&metric=${encodeURIComponent(state.metric)}`,{cache:"no-store"});if(!response.ok)throw new Error(`HTTP ${response.status}`);const data=await response.json();if(token!==state.requestToken)return;
    state.valuationRows=data.rows||[];state.marketRows=data.market_rows||[];state.demo=data.is_demo;state.name=data.name;state.dataStatus=data.status||{};state.marketStatus=data.market_status||{};$("demo-banner").hidden=!state.demo;if(!preserveRange)state.visibleRange=null;renderSingle({preserveVisible:preserveRange});
  }catch(error){if(token===state.requestToken)setError(`加载数据失败：${error.message}`);}
}
async function loadComparison(){
  const token=++state.requestToken;setError("");$("comparison-warning").hidden=false;$("comparison-warning").textContent="正在读取四个指数的真实估值序列…";
  try{const response=await fetch(`/api/comparison?metric=${encodeURIComponent(state.metric)}`,{cache:"no-store"});if(!response.ok)throw new Error(`HTTP ${response.status}`);const data=await response.json();if(token!==state.requestToken)return;state.comparisonData=data;renderComparison();}
  catch(error){if(token===state.requestToken){setError(`比较数据加载失败：${error.message}`);renderComparisonEmpty();}}
}
function renderSingleEmpty(){
  $("chart-title").textContent=`${state.name} · 指数点位与${labels[state.metric]}`;$("chart-subtitle").textContent="当前没有可用于分析的估值记录";$("last-date").textContent="—";$("data-source").textContent="暂无有效估值数据";
  ["current","pct","z","mean","low","avg","high","std","stats-range","stats-count","ten-year-extremes","ten-year-deviation","date-match-status"].forEach(id=>{$(id).textContent="—";});$("n-data").textContent="0 个估值点";$("signal-label").className="signal neutral";$("signal-label").textContent="暂无数据";$("signal-text").textContent="当前指数和指标没有有效估值数据。";
  Plotly.react("chart",[],{margin:{l:58,r:24,t:18,b:42},paper_bgcolor:"#fff",plot_bgcolor:"#fff",xaxis:{visible:false},yaxis:{visible:false},annotations:[{xref:"paper",yref:"paper",x:.5,y:.5,text:"暂无有效估值数据",showarrow:false}]},plotConfig("empty"));
}
function renderSingle({preserveVisible=false}={}){
  syncControls();renderSingleQuality();const selected=selectedStatsRows(),stats=statistics(selected),tenYear=statistics(filterWindow(state.valuationRows,"10y",statsWindowOptions));if(!stats){renderSingleEmpty();return;}
  state.statsSnapshot={window:state.statsWindow,n:stats.n,current:stats.current,mean:stats.mean,sd:stats.sd,pct:stats.pct,firstDate:stats.firstDate,lastDate:stats.lastDate};
  const bounds=displayDateBounds(),valuation=rowsWithinBounds(state.valuationRows,bounds),market=rowsWithinBounds(state.marketRows,bounds),last=state.valuationRows.at(-1);
  $("chart-title").textContent=`${state.name} · 指数点位与${labels[state.metric]}`;$("chart-subtitle").textContent="上图为指数自身收盘点位，下图为估值；共享真实日期轴，不插值、不前向填充";$("last-date").textContent=last.date;$("data-source").textContent=state.dataStatus.source_name||last.source;
  $("current").textContent=`${fmt(stats.current)} 倍`;$("current-note").textContent=state.demo?"合成示例 · 非真实估值":"最近一个已存储估值观察值";$("pct").textContent=`${fmt(stats.pct)}%`;$("pct-note").textContent=`${state.statsWindow==="all"?"全部历史":optionFor(statsWindowOptions,state.statsWindow).label}样本 ${stats.n} 条`;$("z").textContent=`${stats.z>=0?"+":""}${fmt(stats.z)}σ`;$("mean").textContent=fmt(stats.mean);$("mean-note").textContent=`${stats.firstDate} → ${stats.lastDate}`;$("n-data").textContent=`显示 ${valuation.length} 个估值点 / 统计 ${stats.n} 个样本`;
  $("low").textContent=fmt(stats.low);$("avg").textContent=fmt(stats.mean);$("high").textContent=fmt(stats.high);$("std").textContent=fmt(stats.sd);const verdict=decide(stats);$("signal-label").className=`signal ${verdict.tone}`;$("signal-label").textContent=verdict.status;$("signal-text").textContent=`${state.demo?"【演示估值】":""}${verdict.text}`;$("footer-status").textContent=`估值截至 ${last.date}${state.marketRows.length?` · 指数截至 ${state.marketRows.at(-1).date}`:" · 指数点位缺失"}`;renderStatsSummary(stats,tenYear,selected);
  const chart=$("chart"),width=chart.clientWidth||800,height=chart.clientHeight||700,padded=paddedDateRange(bounds);let visible=padded.range,requested=state.pendingRange||(preserveVisible?state.visibleRange:null);if(requested&&axisDate(requested[1])>=axisDate(padded.range[0])&&axisDate(requested[0])<=axisDate(padded.range[1]))visible=[...requested];state.pendingRange=null;
  const xaxis=timeAxisConfig(visible,width,{slider:false,labelsVisible:false}),xaxis2=timeAxisConfig(visible,width,{slider:true,labelsVisible:true});xaxis.anchor="y";xaxis.matches="x2";xaxis.domain=[0,1];xaxis2.anchor="y2";xaxis2.domain=[0,1];const marketAxis=numericAxisConfig(market.map(row=>row.value),"指数收盘（点）");marketAxis.domain=[.58,1];const valuationAxis=numericAxisConfig(valuation.map(row=>row.value),`${labels[state.metric]}（倍）`,{valuation:true,stats});valuationAxis.domain=[.08,.45];const traces=[];
  if(market.length)traces.push({x:market.map(row=>row.date),y:market.map(row=>row.value),customdata:market.map(row=>row.date),xaxis:"x",yaxis:"y",type:"scattergl",mode:"lines",name:"指数收盘",connectgaps:false,line:{color:chartColors.market,width:1.8},hovertemplate:"日期：%{customdata}<br>指数收盘：%{y:,.2f} 点<extra></extra>"});
  if(valuation.length)traces.push({x:valuation.map(row=>row.date),y:valuation.map(row=>row.value),customdata:valuation.map(row=>row.date),xaxis:"x2",yaxis:"y2",type:"scatter",mode:"lines",name:labels[state.metric],connectgaps:false,line:{color:chartColors.series,width:2.4},hovertemplate:`日期：%{customdata}<br>${labels[state.metric]}：%{y:.2f} 倍<extra></extra>`});
  if(last.date>=bounds[0]&&last.date<=bounds[1])traces.push({x:[last.date],y:[last.value],customdata:[last.date],xaxis:"x2",yaxis:"y2",type:"scatter",mode:"markers",name:"最新估值",marker:{color:chartColors.current,size:9,line:{color:"#fff",width:2}},hovertemplate:`日期：%{customdata}<br>${labels[state.metric]}：%{y:.2f} 倍（最新）<extra></extra>`});
  const shapes=[{value:stats.mean,color:chartColors.mean,width:1.7},{value:stats.high,color:chartColors.high,width:1.5},{value:stats.low,color:chartColors.low,width:1.5}].map(item=>({type:"line",xref:"x2",yref:"y2",x0:visible[0],x1:visible[1],y0:item.value,y1:item.value,layer:"below",line:{color:item.color,width:item.width,dash:"dash"}})),annotations=valueAnnotations(stats,last.date,visible[1],valuationAxis.range,height*.37-20,width-180,padded.rightRatio);if(!market.length)annotations.push({xref:"paper",yref:"paper",x:.5,y:.79,text:"指数点位暂无真实数据，请先更新指数行情",showarrow:false,font:{size:12,color:"#ad7a2d"},bgcolor:"rgba(255,248,232,.9)"});
  const layout={margin:{l:74,r:102,t:25,b:55},paper_bgcolor:"#fff",plot_bgcolor:"#fff",uirevision:`single:${state.index}:${state.displayWindow}`,font:{family:'-apple-system,"PingFang SC","Microsoft YaHei",sans-serif',color:"#718198",size:11},hovermode:"closest",hoverdistance:35,spikedistance:-1,showlegend:false,dragmode:"zoom",xaxis,xaxis2,yaxis:marketAxis,yaxis2:valuationAxis,shapes,annotations,hoverlabel:{bgcolor:"#17263d",bordercolor:"#17263d",font:{color:"#fff",size:11}}};Promise.resolve(Plotly.react("chart",traces,layout,plotConfig(`${state.name}_${state.metric}_${state.displayWindow}`))).then(()=>bindSingleChart(visible));
}

function comparisonStatistics(item){
  if(!item.available||!item.rows.length)return{index:item.index,name:item.name,available:false,status:item.status,warnings:["无真实估值序列"]};
  const asOf=state.comparisonData.common_date||item.rows.at(-1).date,windowRows=filterWindow(item.rows,state.statsWindow,statsWindowOptions,asOf),stats=statistics(windowRows),warnings=[];
  const coverage=hasCoverage(item.rows,state.statsWindow,asOf);if(state.statsWindow!=="all"&&!coverage)warnings.push(`${optionFor(statsWindowOptions,state.statsWindow).label}覆盖不足`);if(!stats||stats.n<30)warnings.push("有效样本不足30条");if(item.status?.last_update_status==="failed")warnings.push("最近更新失败，使用原有历史");if(stats&&state.metric==="pe_ttm"&&(stats.current>100||Math.abs(stats.z)>4))warnings.push("PE为极端值，已保留未删除");
  return{index:item.index,name:item.name,available:Boolean(stats),eligible:Boolean(stats&&coverage&&stats.n>=30),asOf,coverage,stats,status:item.status,warnings,deviation:stats&&Math.abs(stats.mean)>1e-12?(stats.current/stats.mean-1)*100:null,historyStart:item.rows[0].date};
}
function renderComparisonQuality(){
  const data=state.comparisonData,available=data.series.filter(item=>item.available),sources=[...new Set(available.map(item=>item.status?.source_interface).filter(Boolean))];
  $("quality-state").className=`quality-badge ${data.strict_comparison?"real":"warn"}`;$("quality-state").textContent=data.strict_comparison?"真实数据 · 严格同日":"非严格同日 / 数据不全";$("quality-source").textContent=sources.join("；")||"无可比较的真实来源";$("quality-method").textContent=data.source_consistent?"可用序列来源与口径一致":"存在来源或口径差异，禁止无说明排名";$("quality-range").textContent=data.common_date?`共同基准日 ${data.common_date}`:"没有四指数共同日期";$("quality-count").textContent=`${available.length}/4 个指数有真实序列`;$("quality-fetched").textContent="各指数采集时间见比较表";$("quality-gaps").textContent=data.warnings.join("；")||"未发现比较级警告";$("market-source").textContent="比较模式不使用指数点位参与百分位排名";$("market-range").textContent="点击任一指数可进入单指数双图";
}
function renderComparisonEmpty(){
  state.comparisonResults=[];$("chart-title").textContent=`四指数 · ${labels[state.metric]}历史百分位比较`;$("chart-subtitle").textContent="没有可用于比较的真实估值序列";$("comparison-table-body").innerHTML="<tr><td colspan='10' class='unavailable'>暂无真实比较数据</td></tr>";Plotly.react("compare-chart",[],{paper_bgcolor:"#fff",plot_bgcolor:"#fff",xaxis:{visible:false},yaxis:{visible:false},annotations:[{xref:"paper",yref:"paper",x:.5,y:.5,text:"暂无真实比较数据",showarrow:false}]},plotConfig("comparison-empty",700));
}
function renderComparison(){
  syncControls();const data=state.comparisonData;if(!data){renderComparisonEmpty();return;}state.comparisonResults=data.series.map(comparisonStatistics);const available=state.comparisonResults.filter(item=>item.available),eligible=data.common_date&&data.source_consistent?state.comparisonResults.filter(item=>item.eligible):[],warnings=[...(data.warnings||[])];state.comparisonResults.forEach(item=>item.warnings.forEach(warning=>warnings.push(`${item.name}：${warning}`)));if(available.length&&!eligible.length)warnings.push("没有同时满足共同日期、相同来源口径和历史覆盖要求的序列，比较条形图已排除这些数据");
  $("comparison-warning").hidden=!warnings.length;$("comparison-warning").className=`comparison-warning ${data.strict_comparison?"":"error"}`;$("comparison-warning").textContent=warnings.join("；");$("chart-title").textContent=`四指数 · ${labels[state.metric]}历史百分位比较`;$("chart-subtitle").textContent=data.common_date?`比较基准日 ${data.common_date}；横轴固定为 0%—100%`:`无法严格同日比较；每个指数按表中实际截止日展示，未伪装为同一时点`;$("last-date").textContent=data.common_date||"非严格同日";$("data-source").textContent=data.source_consistent?"来源与口径见明细":"存在来源 / 口径差异";$("n-data").textContent=`${eligible.length}/4 严格可绘制 · ${available.length}/4 有数据`;renderComparisonQuality();
  const trace={type:"bar",orientation:"h",x:eligible.map(item=>item.stats.pct),y:eligible.map(item=>item.name),customdata:eligible.map(item=>[item.index,item.stats.current,item.stats.mean,item.stats.z,item.asOf,item.stats.n]),marker:{color:eligible.map(item=>comparisonColors[item.index])},text:eligible.map(item=>`${fmt(item.stats.pct)}%`),textposition:"outside",cliponaxis:false,hovertemplate:"%{y}<br>历史百分位：%{x:.2f}%<br>当前值：%{customdata[1]:.2f} 倍<br>均值：%{customdata[2]:.2f} 倍<br>Z-Score：%{customdata[3]:.2f}σ<br>截止：%{customdata[4]}<extra>点击进入单指数分析</extra>"};
  const layout={margin:{l:100,r:75,t:34,b:55},paper_bgcolor:"#fff",plot_bgcolor:"#fff",uirevision:`compare:${state.metric}:${state.statsWindow}`,font:{family:'-apple-system,"PingFang SC","Microsoft YaHei",sans-serif',color:"#63758c",size:11},showlegend:false,bargap:.42,xaxis:{range:[0,100],dtick:20,ticksuffix:"%",title:{text:"历史估值百分位（0%—100%）"},showgrid:true,gridcolor:"#e3e9f1",zeroline:false,fixedrange:true},yaxis:{autorange:"reversed",fixedrange:true},annotations:eligible.length?[]:[{xref:"paper",yref:"paper",x:.5,y:.5,text:"暂无满足同日、同口径与历史覆盖要求的可比序列",showarrow:false,font:{size:12,color:"#9b6d28"}}],hoverlabel:{bgcolor:"#17263d",font:{color:"#fff"}}};Promise.resolve(Plotly.react("compare-chart",eligible.length?[trace]:[],layout,plotConfig(`四指数_${state.metric}_${state.statsWindow}`,700))).then(bindComparisonClick);
  $("comparison-table-body").innerHTML=state.comparisonResults.map(item=>{
    if(!item.available)return`<tr><td><b>${escapeHtml(item.name)}</b><br>${item.index}</td><td colspan="9" class="unavailable">无真实估值序列，不参与比较</td></tr>`;
    const source=`${item.status?.source_name||"—"} · ${item.status?.source_interface||"—"}<br>${item.status?.methodology||"—"}`,history=`${item.historyStart} → ${item.asOf}<br>${item.stats.n} 条${item.coverage?"":" · 覆盖不足"}`,warning=item.warnings.length?`<br><span class="warning-cell">${escapeHtml(item.warnings.join("；"))}</span>`:"";
    return`<tr data-index="${item.index}"><td><b>${escapeHtml(item.name)}</b><br>${item.index}</td><td>${fmt(item.stats.current)} 倍</td><td>${fmt(item.stats.mean)}</td><td>${fmt(item.stats.sd)}</td><td><b>${fmt(item.stats.pct)}%</b></td><td>${item.stats.z>=0?"+":""}${fmt(item.stats.z)}σ</td><td>${item.deviation>=0?"+":""}${fmt(item.deviation)}%</td><td>${item.asOf}</td><td>${history}</td><td>${source}${warning}</td></tr>`;
  }).join("");
  ["current","pct","z","mean","low","avg","high","std","ten-year-extremes","ten-year-deviation"].forEach(id=>{$(id).textContent="—";});$("current-note").textContent="各指数见比较表";$("pct-note").textContent="横轴固定 0%—100%";$("mean-note").textContent="各指数独立计算";$("stats-range").textContent=data.common_date?`共同基准日 ${data.common_date}`:"各指数实际截止日";$("stats-count").textContent=`${available.length}/4 个指数 · ${optionFor(statsWindowOptions,state.statsWindow).label}基准`;$("date-match-status").textContent=data.strict_comparison?"严格同日且来源口径一致":"不满足严格同日同口径比较";$("signal-label").className=`signal ${data.strict_comparison?"neutral":"warm"}`;$("signal-label").textContent=data.strict_comparison?"严格同日比较":"谨慎解读";$("signal-text").textContent=data.strict_comparison?"四个指数使用共同日期、各自独立历史样本计算百分位。":"缺失、日期或口径警告已显示；未把不同日期伪装成同一时点。";$("footer-status").textContent=data.common_date?`比较基准日 ${data.common_date}`:"比较数据不满足严格同日条件";
}
function bindComparisonClick(){const chart=$("compare-chart");if(!chart||typeof chart.on!=="function"||chart.__clickBound)return;chart.on("plotly_click",event=>{const code=event.points?.[0]?.customdata?.[0];if(code)enterSingleIndex(code);});chart.__clickBound=true;}
function enterSingleIndex(indexCode){state.index=indexCode;state.name=indexNames[indexCode];state.mode="single";state.visibleRange=null;state.pendingRange=null;syncControls();loadSingle();}
function renderActive(){state.mode==="single"?renderSingle({preserveVisible:true}):renderComparison();}
function resetZoom(){if(state.mode!=="single")return;state.visibleRange=null;state.pendingRange=null;renderSingle();}
function toggleMaximize(){state.isMaximized=!state.isMaximized;$("analysis-card").classList.toggle("analysis-maximized",state.isMaximized);document.body.classList.toggle("chart-maximized",state.isMaximized);syncControls();requestAnimationFrame(()=>{const chart=state.mode==="single"?$("chart"):$("compare-chart");if(chart&&Plotly.Plots?.resize)Plotly.Plots.resize(chart);if(state.mode==="single"&&state.visibleRange)applyAdaptiveTimeAxis(state.visibleRange);});}

async function updateReal(){if(state.busy)return;setBusy(true,"valuation");setError("");try{const response=await fetch(`/api/update?index=${encodeURIComponent(state.index)}&metric=${encodeURIComponent(state.metric)}`,{method:"POST"}),data=await response.json();if(!response.ok)throw new Error(data.error||`HTTP ${response.status}`);if(state.mode==="compare")await loadComparison();else await loadSingle({preserveRange:true});alert(`已更新 ${data.count} 条历史估值，数据截至 ${data.last_date}。`);}catch(error){state.mode==="compare"?await loadComparison():await loadSingle({preserveRange:true});setError(`估值更新失败：${error.message}`);}finally{setBusy(false);}}
async function updateMarket(){if(state.busy)return;setBusy(true,"market");setError("");try{const response=await fetch(`/api/update-market?index=${encodeURIComponent(state.index)}`,{method:"POST"}),data=await response.json();if(!response.ok)throw new Error(data.error||`HTTP ${response.status}`);if(state.mode==="single")await loadSingle({preserveRange:true});alert(`已更新 ${data.count} 条指数点位，数据截至 ${data.last_date}。`);}catch(error){if(state.mode==="single")await loadSingle({preserveRange:true});setError(`指数点位更新失败：${error.message}`);}finally{setBusy(false);}}
async function importFile(file){if(state.busy||!file)return;if(file.size>3_000_000){setError("CSV不得超过3MB");return;}setBusy(true,"valuation");setError("");try{const response=await fetch(`/api/import?index=${encodeURIComponent(state.index)}&metric=${encodeURIComponent(state.metric)}`,{method:"POST",headers:{"Content-Type":"text/csv; charset=utf-8"},body:await file.text()}),data=await response.json();if(!response.ok)throw new Error(data.error||`HTTP ${response.status}`);await loadSingle({preserveRange:true});alert(`${data.message}。请复核口径。`);}catch(error){setError(`导入失败：${error.message}`);}finally{setBusy(false);}}
function exportCsv(){
  if(state.mode==="compare"){if(!state.comparisonResults.length)return;const header="index,name,current,mean,sd,percentile,z_score,deviation_pct,as_of,sample_count,source_interface\r\n",lines=state.comparisonResults.filter(item=>item.available).map(item=>[item.index,item.name,item.stats.current,item.stats.mean,item.stats.sd,item.stats.pct,item.stats.z,item.deviation,item.asOf,item.stats.n,item.status?.source_interface||""].map(value=>`"${String(value).replaceAll('"','""')}"`).join(","));downloadCsv(`comparison_${state.metric}_${state.statsWindow}.csv`,header+lines.join("\r\n"));return;}
  const rows=rowsWithinBounds(state.valuationRows,displayDateBounds());if(!rows.length)return;downloadCsv(`${state.demo?"DEMO_":""}${state.index}_${state.metric}_${state.displayWindow}.csv`,"date,value,source\r\n"+rows.map(row=>`${row.date},${row.value},"${String(row.source).replaceAll('"','""')}"`).join("\r\n"));
}
function downloadCsv(filename,text){const blob=new Blob(["\ufeff"+text],{type:"text/csv;charset=utf-8"}),anchor=document.createElement("a");anchor.href=URL.createObjectURL(blob);anchor.download=filename;anchor.click();setTimeout(()=>URL.revokeObjectURL(anchor.href),1000);}

$("idx").addEventListener("change",event=>enterSingleIndex(event.target.value));
$("metric").addEventListener("click",event=>{const button=event.target.closest("button[data-metric]");if(!button||button.dataset.metric===state.metric)return;state.pendingRange=state.visibleRange?[...state.visibleRange]:null;state.metric=button.dataset.metric;syncControls();state.mode==="single"?loadSingle({preserveRange:true}):loadComparison();});
$("analysis-mode").addEventListener("click",event=>{const button=event.target.closest("button[data-mode]");if(!button||button.dataset.mode===state.mode)return;state.mode=button.dataset.mode;syncControls();state.mode==="compare"?loadComparison():renderSingle({preserveVisible:true});requestAnimationFrame(()=>{const chart=state.mode==="single"?$("chart"):$("compare-chart");if(chart&&Plotly.Plots?.resize)Plotly.Plots.resize(chart);});});
$("window").addEventListener("click",event=>{const button=event.target.closest("button[data-window]");if(!button)return;state.displayWindow=button.dataset.window;state.visibleRange=null;state.pendingRange=null;syncControls();renderSingle();});
$("stats-window").addEventListener("click",event=>{const button=event.target.closest("button[data-stats-window]");if(!button)return;state.statsWindow=button.dataset.statsWindow;syncControls();renderActive();});
$("comparison-table-body").addEventListener("click",event=>{const row=event.target.closest("tr[data-index]");if(row)enterSingleIndex(row.dataset.index);});
$("reset-zoom").addEventListener("click",resetZoom);$("maximize-chart").addEventListener("click",toggleMaximize);document.addEventListener("keydown",event=>{if(event.key==="Escape"&&state.isMaximized)toggleMaximize();});
$("refresh").addEventListener("click",updateReal);$("refresh-market").addEventListener("click",updateMarket);$("export").addEventListener("click",exportCsv);$("choose-file").addEventListener("click",()=>$("file").click());$("file").addEventListener("change",event=>{const file=event.target.files[0];if(file){$("file-name").textContent=file.name;importFile(file);}event.target.value="";});
let resizeTimer;window.addEventListener("resize",()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(()=>{const chart=state.mode==="single"?$("chart"):$("compare-chart");if(chart&&Plotly.Plots?.resize)Plotly.Plots.resize(chart);if(state.mode==="single"&&state.visibleRange)applyAdaptiveTimeAxis(state.visibleRange);},140);});

setupControls();
loadSingle();
