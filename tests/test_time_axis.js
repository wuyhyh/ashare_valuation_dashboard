"use strict";
const assert=require("assert");
const fs=require("fs");
const vm=require("vm");

const elements=new Map();
const ids=[
  "analysis-card","analysis-layout","single-view","compare-view","drivers-view","chart-controls","display-window-row","insight-panel",
  "data-quality","quality-state","quality-source","quality-method","quality-range","quality-count","quality-fetched","quality-gaps","market-source","market-range",
  "chart-title","chart-subtitle","last-date","data-source","current","current-note","pct","pct-note","z","mean","mean-note","n-data","low","avg","high","std","signal-label","signal-text",
  "footer-status","demo-banner","error-banner","refresh","refresh-market","choose-file","file","file-name","export","idx","metric","analysis-mode","window","stats-window","chart","compare-chart",
  "stats-range","stats-count","ten-year-extremes","ten-year-deviation","date-match-status","comparison-warning","comparison-table-body","reset-zoom","maximize-chart",
  "drivers-window","drivers-readiness","drivers-title","drivers-date","drivers-price","drivers-multiple","drivers-implied","drivers-implied-label","drivers-warning","drivers-meta","drivers-series-chart","drivers-bars-chart"
];
function classList(){const values=new Set();return{toggle(name,on){if(on===undefined)on=!values.has(name);on?values.add(name):values.delete(name);return on;},contains(name){return values.has(name);},add(name){values.add(name);},remove(name){values.delete(name);}};}
function element(id){
  if(!elements.has(id))elements.set(id,{id,hidden:false,textContent:"",className:"",innerHTML:"",disabled:false,value:"",clientWidth:id==="chart"?1000:id==="compare-chart"?1000:0,clientHeight:id==="chart"?700:470,
    layout:null,data:null,handlers:{},attributes:{},classList:classList(),addEventListener(){},setAttribute(name,value){this.attributes[name]=value;},insertAdjacentHTML(){},closest(){return null;},click(){},on(name,handler){this.handlers[name]=handler;}});
  return elements.get(id);
}
ids.forEach(element);
const body={classList:classList()};
const plots={};let relayoutCalls=0,resizeCalls=0;
const context=vm.createContext({
  console,Date,Math,Number,Promise,Blob:function(){},URL:{createObjectURL(){return""},revokeObjectURL(){}},alert(){},clearTimeout(){},setTimeout(callback){callback();return 1;},requestAnimationFrame(callback){callback();},
  document:{body,getElementById:element,querySelectorAll(){return[]},querySelector(){return null},createElement(){return element("created")},addEventListener(){}},
  window:{addEventListener(){}},fetch(){return new Promise(()=>{})},
  Plotly:{
    react(target,traces,layout){const chart=element(target);chart.data=traces;chart.layout=layout;plots[target]={traces,layout};if(target==="chart")chart._fullLayout={xaxis:{...layout.xaxis,range:[...layout.xaxis.range]},xaxis2:{...layout.xaxis2,range:[...layout.xaxis2.range]}};else chart._fullLayout={xaxis:{...layout.xaxis}};return Promise.resolve(chart);},
    relayout(chart,update){relayoutCalls++;chart.lastRelayout=update;for(const axis of["xaxis","xaxis2"]){if(chart._fullLayout?.[axis]&&update[`${axis}.dtick`]!==undefined)chart._fullLayout[axis].dtick=update[`${axis}.dtick`];}return Promise.resolve(chart);},
    Plots:{resize(){resizeCalls++;}}
  }
});
vm.runInContext(fs.readFileSync("static/app.js","utf8"),context);
const run=source=>vm.runInContext(source,context);

assert.strictEqual(run("timeScalePreset(['2017-01-01','2026-01-01']).majorDtick"),"M6");
assert.strictEqual(run("timeScalePreset(['2014-01-01','2026-01-01']).majorDtick"),"M12");
assert.strictEqual(run("timeScalePreset(['2026-01-01','2026-07-01']).majorDtick"),"M1");
assert.strictEqual(run("timeScalePreset(['2026-06-01','2026-07-01']).majorDtick"),7*86400000);
assert.strictEqual(run("timeScalePreset(['2024-01-01','2026-01-01']).majorDtick"),"M3");
assert.ok(run("timeAxisConfig(['2024-01-01','2026-01-01'],360).ticklabelstep")>run("timeAxisConfig(['2024-01-01','2026-01-01'],1000).ticklabelstep"));
assert.strictEqual(run("subtractCalendarMonths('2024-02-29',12)"),"2023-02-28");
assert.strictEqual(run("subtractCalendarMonths('2024-03-31',1)"),"2024-02-29");
const definitionStats=JSON.parse(run("JSON.stringify(statistics([{date:'2026-01-01',value:10},{date:'2026-01-02',value:12},{date:'2026-01-03',value:14}]))"));
assert.strictEqual(definitionStats.mean,12);
assert.ok(Math.abs(definitionStats.sd-Math.sqrt(8/3))<1e-12);
assert.ok(Math.abs(definitionStats.pct-83.33333333333334)<1e-12);

function rows(base=10){const result=[];for(let offset=0;offset<132;offset++){const month=offset%12+1,year=2016+Math.floor(offset/12);result.push({date:`${year}-${String(month).padStart(2,"0")}-28`,value:base+(offset%13)*.1,source:"test",fetched_at:"2026-12-28T00:00:00Z"});}return result;}
context.valuation=rows();context.market=rows(3000).map((row,index)=>({...row,value:3000+index*8}));
run("state.valuationRows=valuation;state.marketRows=market;state.demo=false;state.dataStatus={source_name:'test',source_interface:'fixture',methodology:'same',frequency:'monthly',count:132,first_date:valuation[0].date,last_date:valuation.at(-1).date,window_support:{'10':true}};state.marketStatus={source_name:'market',source_interface:'fixture',count:132,first_date:market[0].date,last_date:market.at(-1).date};state.displayWindow='1y';state.statsWindow='10y';renderSingle()");
assert.strictEqual(run("state.statsSnapshot.n"),121);
assert.strictEqual(plots.chart.layout.xaxis2.dtick,"M1");
assert.strictEqual(plots.chart.layout.xaxis.matches,"x2");
assert.strictEqual(plots.chart.layout.xaxis2.rangeslider.visible,true);
assert.ok(plots.chart.traces.some(trace=>trace.name==="指数收盘"));
assert.ok(plots.chart.traces.some(trace=>trace.name==="市盈率 TTM"));
const statsBefore=run("JSON.stringify(state.statsSnapshot)");
run("handleTimeRelayout({'xaxis2.range[0]':'2026-07-01','xaxis2.range[1]':'2027-01-01'})");
assert.strictEqual(element("chart").lastRelayout["xaxis2.dtick"],"M1");
assert.strictEqual(run("JSON.stringify(state.statsSnapshot)"),statsBefore);
run("state.axisUpdateInFlight=false;handleTimeRelayout({'xaxis2.range[0]':'2026-12-01','xaxis2.range[1]':'2027-01-01'})");
assert.strictEqual(element("chart").lastRelayout["xaxis2.dtick"],7*86400000);
assert.strictEqual(run("JSON.stringify(state.statsSnapshot)"),statsBefore);

context.series=["000016","000300","000905","000852"].map((code,index)=>({index:code,name:{"000016":"上证50","000300":"沪深300","000905":"中证500","000852":"中证1000"}[code],available:true,rows:rows(10+index*5),status:{source_name:"test",source_interface:"same",methodology:"same"}}));
run("state.mode='compare';state.comparisonData={metric:'pe_ttm',common_date:'2026-12-28',strict_comparison:true,source_consistent:true,warnings:[],series};renderComparison()");
assert.strictEqual(run("state.comparisonResults.length"),4);
assert.strictEqual(plots["compare-chart"].traces[0].x.length,4);
assert.deepStrictEqual(Array.from(plots["compare-chart"].layout.xaxis.range),[0,100]);
assert.match(element("comparison-table-body").innerHTML,/中证1000/);
assert.strictEqual(element("comparison-warning").hidden,true);

run("state.comparisonData.series[3]={index:'000852',name:'中证1000',available:false,rows:[],status:null};state.comparisonData.strict_comparison=false;state.comparisonData.common_date=null;state.comparisonData.warnings=['缺少真实序列'];renderComparison()");
assert.strictEqual(plots["compare-chart"].traces.length,0);
assert.match(element("comparison-warning").textContent,/缺少真实序列/);
assert.match(element("comparison-warning").textContent,/比较条形图已排除/);
assert.match(element("comparison-table-body").innerHTML,/不参与比较/);

context.driverPayload={index:"000300",name:"沪深300",metric:"pe_ttm",horizon:"3y",label:"隐含盈利代理",start:"2023-10-30",end:"2026-10-30",frequency:"monthly",matched_count:37,expected_periods:37,coverage_ratio:1,max_missing_consecutive_periods:0,lag_days:1,price_source:"fixture.index_close",valuation_source:"fixture.pe_ttm",method:"log-return exact",warnings:["隐含代理不是实际利润"],context:{readiness:[{horizon:"1y",status:"computable",matched_count:13},{horizon:"3y",status:"computable",matched_count:37},{horizon:"5y",status:"history_insufficient",message:"历史不足"},{horizon:"10y",status:"collection_failed",message:"采集失败"}]},changes_pct:{close:20,multiple:50,implied:-20},log_contributions_pp:{close:18.232155679,multiple:40.546510811,implied:-22.314355132},series:[{date:"2023-10-30",index:100,multiple:100,implied:100},{date:"2026-10-30",index:120,multiple:150,implied:80}]};
run("state.mode='drivers';state.driverData=driverPayload;renderDrivers()");
assert.strictEqual(element("drivers-date").textContent,"2023-10-30 — 2026-10-30 · 37 个同日样本");
assert.match(element("drivers-meta").textContent,/月频归因/);
assert.match(element("drivers-readiness").innerHTML,/采集失败/);
assert.strictEqual(element("drivers-price").textContent,"+20.00%");
assert.strictEqual(element("drivers-multiple").textContent,"+50.00%");
assert.strictEqual(element("drivers-implied").textContent,"-20.00%");
assert.strictEqual(plots["drivers-series-chart"].traces.length,3);
assert.strictEqual(plots["drivers-bars-chart"].traces.length,1);
assert.match(element("drivers-warning").textContent,/不是实际利润/);
run("state.driverData=null;state.driverError='无同日数据';renderDrivers()");
assert.strictEqual(plots["drivers-series-chart"].traces.length,0);
assert.match(element("drivers-warning").textContent,/无同日数据/);

run("state.mode='single';state.displayWindow='8y';state.visibleRange=['2026-12-01','2026-12-28'];resetZoom()");
assert.notStrictEqual(run("JSON.stringify(state.visibleRange)"),'["2026-12-01","2026-12-28"]');

run("state.mode='single';state.visibleRange=['2026-09-01','2026-12-28'];toggleMaximize()");
assert.strictEqual(run("state.isMaximized"),true);
assert.strictEqual(run("JSON.stringify(state.visibleRange)"),'["2026-09-01","2026-12-28"]');
assert.ok(element("analysis-card").classList.contains("analysis-maximized"));
run("toggleMaximize()");
assert.strictEqual(run("state.isMaximized"),false);
assert.ok(resizeCalls>=2);
console.log("v0.7 state, time-axis, comparison and monthly decomposition tests: OK");
