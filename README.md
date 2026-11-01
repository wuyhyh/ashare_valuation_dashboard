# A 股市场估值观察站（main v0.8 / v0.9、v0.10 开发中）

本地运行的 A 股指数估值仪表盘：Python HTTP 服务 + SQLite + Plotly.js。v0.8 在保留既有界面、统计和 SQLite v3 结构的前提下，完成上证50、沪深300、中证500、中证1000的真实 PE TTM、PB 与指数收盘验证，并启用严格同日、同来源、同口径、同频率的四指数历史百分位比较。

## 为什么运行 `python app.py` 可能看不到 Codex 的真实数据？

`python app.py` **只启动本地 HTTP 服务，不自动联网采集**。代码在 Git 仓库中共享，但 `valuation.sqlite3`（含真实估值和行情）被 `.gitignore` 排除；Codex 本机已采集的 12/12 个真实序列不会随着 Git 推送进入另一台电脑。v0.8 已合并到主分支；尚未合并的 v0.9 开发分支与本地 `main` 仍可能不同。

```bash
git fetch origin
git switch main
git pull --ff-only origin main
python app.py doctor                       # 不联网：查看当前分支/提交、实际 SQLite 路径、12项覆盖
python app.py sync                         # 显式备份 SQLite 并联网更新全部12项（可能被限流）
python app.py                              # 启动页面，不联网更新
```

首次只想补缺失序列，可运行 `python app.py sync --only-missing`；**想获取最新数据不要使用 --only-missing**，因为该参数会跳过已有但可能过期的序列。可用 `--provider csindex_official|sina|auto` 指定指数点位数据源；默认显式使用已验证的中证官方接口，不改变 PE/PB 来源。

页面上方新增运行环境状态条，能显示当前代码分支/提交、实际数据库是否存在、真实序列数，并可点击“检查本机状态”。如果浏览器仍显示旧版，检查旧进程是否占用 `127.0.0.1:8765`（例如 `lsof -nP -iTCP:8765 -sTCP:LISTEN`）并从新服务地址重新打开。注意中国内地 A 股休市日没有新增交易日行情；最新完整交易日可能仍是此前的星期五。

**演示数据策略：** `/api/history` 默认只返回 SQLite 中已存在的真实历史；数据库为空时返回空数据和缺失状态，不再自动生成看似正常的估值曲线。仅显式请求 `/api/history?index=000300&metric=pe_ttm&demo=1` 时才输出带 `is_demo=true` 标记的合成示例。\n\n**安全规则：** `sync` 使用 SQLite 在线备份生成 `valuation.sqlite3.backup-before-sync-...`，逐项失败不会把其他来源静默伪装为成功。不会在页面启动时自动请求12个外部接口，以避免限流与启动卡顿。若各上游返回429/证书错误，先查看同步错误，再处理网络或证书环境。

## 体验 v0.9 开发分支（市场宽度 + 跨资产）

```bash
git fetch origin
git switch feat/v09-market-breadth
git pull --ff-only origin feat/v09-market-breadth

# 仅首次需主动联网采集；可分标的运行
python app.py breadth-update --provider auto
python app.py assets-update --asset 000688
python app.py assets-update --asset 399673
python app.py assets-update --asset 518880

python app.py
# 浏览器打开 /breadth.html 或 /assets.html
```

**目前属于 Draft**：2026-11-01 已完成腾讯主板快照与两交所名录核对、三项跨资产双源核验和本机真实 SQLite Playwright验收；东方财富快照仍受 `Push2RateLimitError` 影响，市场宽度交易日仍无法从即时接口独立确认。不要将无数据页面或 CI 模拟数据理解为真实行情。

## 体验 v0.10 开发分支（沪深全市场宽度）

```bash
git fetch origin
git switch feat/v010-all-a-share-breadth
git pull --ff-only origin feat/v010-all-a-share-breadth

python app.py breadth-update --provider tencent  # 显式联网并核对交易所名录
python app.py breadth-doctor                     # 不联网查看最新快照及分板块覆盖
python app.py serve --port 8765
# 浏览器打开 http://127.0.0.1:8765/breadth.html
```

- 默认范围为沪深 A 股合计：上海主板、深圳主板、创业板、科创板；可分别查看主板合计和四个板块。
- 北交所使用独立范围，不计入默认沪深 A 股分母；只有官方名录核验通过时才启用。本轮真实验证为 `354/354`。
- 股票池以交易所官方名录决定，不受个人账户交易权限或自选股影响。科创板 `689009` 为 CDR，单独标记且不计入默认 A 股分母；创业板 `302132` 依官方板块身份纳入。
- v0.9 已保存的主板快照迁移后明确标为 `mainboard_only`，不会被解释为全市场历史。即时源仍未给出可独立核验的交易日期，因此本版继续只显示时点快照，不伪造历史宽度曲线。
- 真实联网覆盖、字段和各板块样本详见 `docs/V010_BREADTH_AUDIT.md`；SQLite 与备份不进入 Git。

## v0.9（开发中）：科创50、创业板50与华安黄金ETF价格观察
- `/assets.html` 提供三对象真实收盘价走势图，可切换3个月、6个月、1/3/5/8/10年和全部历史；日期轴缩放自动细化。
- `python app.py assets-update` 显式更新全部三项，或以 `--asset 000688|399673|518880` 单项更新；`python app.py assets-doctor` 查看本机数据覆盖。行情存入独立 `asset_prices.sqlite3`，不随Git推送，不在启动时自动采集。
- **科创50与创业板50只添加指数点位研究；PE/PB历史来源待核验。** 黄金采用518880场内非复权价，不能误当Au99.99现货或基金净值，不提供PE/PB。三项主源均已与独立交叉源核验，覆盖和差异参见 `docs/V09_ASSETS_AUDIT.md`。
- 10年数据不足时明确拒绝短历史冒充，按钮和时间轴直接放在图表上方。

## v0.9（开发中）：沪深主板市场宽度
- 新增 `/breadth.html` 市场宽度页面，显示可审核的沪深主板**行情快照**，支持上海/深圳/合计切换和 ST 过滤；涨跌数、上涨比例、无成交和缺失报价分别统计。
- `python app.py breadth-doctor`：不联网查看本机快照；`python app.py breadth-update --provider auto|eastmoney|tencent|sina`：主动调用所选数据源，校验字段、两交所主板股票池覆盖和同源快照完整性后存入独立 `breadth.sqlite3`。只浏览页面或运行 `python app.py` 不会自动抓取。
- `GET /api/breadth` 只读，严格不提供演示行情或伪造历史；“采集日”不等于“实际交易日”，接口尚未可靠提供并验证交易日，故尚不启用历史走势。
- **重要**：2026-11-01 腾讯快照实测 `5,577` 只，其中沪深主板 `3,194` 只，与上交所 `1,700` 只和深交所主板 `1,494` 只代码完全一致；东方财富调用本次限流失败。由于在周日采集且即时接口不提供可独立核实的报价交易日，仍不启用历史宽度曲线。详见 `docs/V09_BREADTH_AUDIT.md`。

## v0.8 数据可用性

- 四指数乐咕 PE 固定读取 `滚动市盈率`，PB 固定读取 `市净率`；不混入静态、等权或中位数列。
- 四指数估值均为月频，最新共同日期为 `2026-10-30`；本机真实数据库的 PE/PB 比较均为 `strict_comparison=true`。
- 乐咕数值型“指数”列不是代码或名称；系统将其与独立官方收盘按真实同日交叉核验，并把结果写入质量元数据。
- 中证官方日频滚动 PE 与乐咕月频 PE 的频率、缺失结构和数值存在差异，继续独立隔离，不替换、不拼接。
- 批量估值更新在请求之间主动节流，HTTP 429 使用较长退避；失败记录原因且不破坏原活动历史。

- 指数行情提供方层支持 `sina`、`tencent`、`eastmoney_daily`、`eastmoney_legacy` 和按顺序有界回退的 `auto`。
- 每个提供方拥有独立 `series_id`、来源接口与方法说明；不同来源不会静默拼接，ETF 价格不会替代指数点位。
- 首次启用或切换来源时，优先用另一独立接口核验至少 5 个共同交易日；核验结果写入质量状态。
- 修复大批量行情通过子进程队列时的等待死锁；请求有超时、显式重试上限，失败不会切换或破坏当前活动序列。
- 支持带来源名称与核验说明的受控指数 CSV 导入，明确标记为用户数据而非官方数据。
- 归因频率由估值活动序列的 `frequency` 决定：支持日频与月频，拒绝未知频率或把月频误作日频。
- 月频归因要求至少 75% 同日覆盖、起点不晚于目标日 45 天、最多连续缺失 2 个自然月；不插值、不前向填充。
- 归因 API 返回频率、共同样本数、期望期数、覆盖率、最大连续缺失周期和 1/3/5/10 年窗口准备状态。

## v0.6 行情涨跌归因

- 支持 PE TTM / PB 与 1、3、5、10 年日历窗口。
- 只匹配同一指数的真实同日正数点位与估值，不插值、不前向填充、不使用演示数据。
- 定义 `X=P/M` 为隐含盈利或净资产代理，使用 `ln(P1/P0)=ln(M1/M0)+ln(X1/X0)` 精确拆分。
- 趋势图以起点 100 归一化；贡献图单位为对数百分点，不能与普通百分比收益直接混加。
- 混合来源、共同样本过少、起点覆盖不足或超过 30 天共同日期缺口时拒绝计算。
- `X` 不是实际利润、EPS 或账面净资产；指数换样、权重、除数和第三方估值口径都会限制解释。

接口：`GET /api/decomposition?index=000300&metric=pe_ttm&horizon=3y`。失败返回 `422` 和明确原因。

## v0.5 主要变化

### 统一分析卡片

- 指数、PE/PB、分析模式和图表时间范围均位于图表正上方。
- 统计基准、数据截止日、数据来源、质量状态和更新按钮位于图表正下方。
- 单指数模式继续使用同一卡片内的指数收盘点位 + PE/PB 上下双图。
- 窄屏工具栏折行，时间范围按钮可横向滚动，不再与图表分离。
- 最大化使用同一分析卡片，保留全部控件、缩放范围和统计基准；按 Esc 或「还原」退出。

### 多指数比较

默认比较上证50、沪深300、中证500和中证1000，支持 PE TTM / PB 以及 3、5、8、10 年和全部历史统计基准。

- 主图为固定 0%—100% 横轴的历史估值百分位横向条形图。
- 明细表展示当前值、均值、标准差、百分位、Z-Score、均值偏离、数据日期、历史长度、来源和口径。
- 优先使用四个指数均存在的最新共同估值日期。
- 若缺少序列、没有共同日期、来源或口径不同，则取消“严格比较”状态并显示警告。
- 缺失指数不会用演示数据补入比较，也不会把不同日期伪装成同一时点。
- 点击条形或表格行进入对应单指数分析，并保留指标和统计基准。

新 clone 不包含 SQLite 市场数据，因此会明确显示缺失且不会用演示序列参与比较。完成真实更新并通过质量核验后，页面可显示四指数严格比较；本项目不会提交或虚构数据库内容。

## 时间轴与统计

- 图表显示范围：3个月、6个月、1年、3年、5年、8年、10年、全部历史。
- 估值统计基准：3年、5年、8年、10年、全部历史；与图表视窗独立。
- 3个月以内按周，3个月至1年按月，1年至5年按季度，5年至10年根据容器宽度采用半年或年度主刻度，并保留季度次级网格。
- `plotly_relayout` 只调整 `dtick`、`minor.dtick`、标签密度和参考线位置，不重算统计样本。
- 双图通过 Plotly `matches` 共用日期范围；数据日期保持真实，不插值、不前向填充。
- 10年回溯使用日历月份计算，不以3650天代替。

统计定义保持不变：

- 均值：窗口内有效估值的算术平均。
- 标准差：总体标准差 `sqrt(sum((x - mean)^2) / N)`。
- 历史百分位：`100% × (小于当前值的样本数 + 0.5 × 等于当前值的样本数) / 样本数`。
- Z-Score：`(当前估值 − 均值) / 标准差`。
- 均值 ±1σ 仅为统计参考，不是确定性的合理价值边界。

## 运行

Python 3.10+：

```bash
python app.py
```

浏览器打开 <http://127.0.0.1:8765>。

安装可选 AKShare 依赖：

```bash
python -m pip install akshare
```

安装测试依赖：

```bash
python -m pip install -r requirements-dev.txt
python -m playwright install chromium
```

估值诊断与更新：

```bash
python app.py diagnose --index 000300 --metric all
python app.py diagnose --index 000016 --metric all
python app.py diagnose --index 000905 --metric all
python app.py diagnose --index 000852 --metric all
python app.py update --index all --metric all
```

指数点位诊断与更新：

```bash
python app.py diagnose-market --index 000300 --provider sina --cross-check tencent --start 20261001 --end 20261030
python app.py update-market --index 000300 --provider auto --start 20160101 --end 20261030
python app.py diagnose-market --index 000300 --provider csindex_official --cross-check sina --start 20160101 --end 20261030
python app.py update-market --index 000300 --provider csindex_official --start 20160101 --end 20261030
python app.py import-market --index 000300 --file index.csv \
  --source-name "文件供应方" --verification-notes "已核对指数代码、日期与收盘字段"
```

命令行更新失败返回非零退出码。网页更新仍调用原有 `/api/update` 和 `/api/update-market`，失败时保留原始完整历史。

## 数据覆盖与来源

2026-11-01 本机联网验收数据库，AKShare `1.20.18`（SQLite 不随仓库分发）：

| 指数 | 指标 | 记录数 | 覆盖区间 | 频率 | 来源接口 |
| --- | --- | ---: | --- | --- | --- |
| 沪深300 | PE TTM | 259 | 2005-04-29 ～ 2026-10-30 | 月频 | `ak.stock_index_pe_lg` |
| 沪深300 | PB | 259 | 2005-04-29 ～ 2026-10-30 | 月频 | `ak.stock_index_pb_lg` |
| 沪深300 | 指数收盘 | 2628 | 2016-01-04 ～ 2026-10-30 | 日频 | `ak.stock_zh_index_hist_csindex`，已与新浪核验 |
| 上证50 | PE TTM / PB | 各 262 | 2005-01-31 ～ 2026-10-30 | 月频 | 乐咕对应 PE/PB 接口 |
| 上证50 | 指数收盘 | 5300 | 2005-01-04 ～ 2026-10-30 | 日频 | `ak.stock_zh_index_hist_csindex`，已与新浪核验 |
| 中证500 | PE TTM / PB | 各 238 | 2007-01-31 ～ 2026-10-30 | 月频 | 乐咕对应 PE/PB 接口 |
| 中证500 | 指数收盘 | 4817 | 2007-01-04 ～ 2026-10-30 | 日频 | `ak.stock_zh_index_hist_csindex`，已与新浪核验 |
| 中证1000 | PE TTM / PB | 各 145 | 2014-10-31 ～ 2026-10-30 | 月频 | 乐咕对应 PE/PB 接口 |
| 中证1000 | 指数收盘 | 3118 | 2014-01-02 ～ 2026-10-30 | 日频 | `ak.stock_zh_index_hist_csindex`，已与新浪核验 |

PE 严格使用 `滚动市盈率`，不使用静态、等权或中位数列；PB 严格使用 `市净率`，不使用等权或中位数列。不同 `series_id` 不会拼接。

中证官方接口返回了可验证的指数代码和中文名称。上证50、中证500、中证1000官方收盘与新浪最近 5 个共同交易日的最大相对差均小于 `0.0002%`；乐咕“指数”数值与官方收盘最近 5 个共同月末完全一致。四指数真实 PE/PB 与指数日线的 10 年归因均为月频 `121/121` 个同日样本，覆盖率 100%。详细审计见 `docs/V08_DATA_AUDIT.md`。

## 数据安全

- 沿用 SQLite schema v3：`series`、`observations`、`active_series`、`update_runs`。
- v0.8 没有修改数据库结构，也没有删除既有估值记录；真实导入前先创建本地备份，数据库及备份均由 `.gitignore` 排除。
- 日期排序、去重、未来日期拒绝、空数据/列变化拒绝、缺口和突变标记逻辑保持不变。
- 候选指数历史明显短于当前活动历史时拒绝切换并记录失败，避免短期回退覆盖完整十年序列。
- 比较 API `/api/comparison` 只返回当前激活的真实 SQLite 序列，不调用 `demo_rows`。
- 四指数共同日期通过真实日期集合交集计算。
- 来源接口、methodology、频率或已记录的身份核验不一致时，`strict_comparison=false`，前端以警告色展示。
- 比较条形图仅绘制具有共同基准日、相同来源口径、足够样本且覆盖完整统计窗口的序列；被排除的序列仍保留在明细表并显示原因。
- 图表标题区提供“复位缩放”，可恢复当前 3月～全部历史观察范围；静态资源版本已提升至 `v0.8`，避免浏览器继续使用旧缓存。

## 测试

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
python -m pytest tests -q
node tests/test_time_axis.js
node --check static/app.js
python -m py_compile app.py decomposition.py data_sources/*.py
```

浏览器验收：

```bash
python app.py serve --port 8766
DASHBOARD_URL=http://127.0.0.1:8766 python tests/browser_time_axis.py
```

自动化覆盖：

- 四指数最新共同日期和严格同日比较。
- 缺失指数不使用演示数据。
- 来源/口径不一致时取消严格排名。
- 单指数显示范围与统计基准独立。
- 10年图缩放至整3个月后切换为周级刻度，双图范围同步且统计不变。
- PE/PB、统计窗口和分析模式切换。
- 点击比较条形/表格进入单指数并保留指标与统计基准。
- 最大化/还原保留缩放范围和控件。
- 桌面和390px窄屏工具栏、横向滚动及刻度标签。
- 原有采集幂等、网络失败保护、口径隔离、SQLite迁移和质量验证。
- 归因恒等式、同日匹配、历史不足、稀疏数据、混源和大缺口拒绝。
- 月频 1/3/5/10 年归因、75% 覆盖阈值、连续缺失 3 月拒绝和频率误分类拒绝。
- 指数提供方回退、五日交叉核验、受控 CSV 导入、重复更新幂等和失败保留。
- 归因模式 PE/PB、区间、指数、最大化和窄屏真实浏览器交互。
- 四指数×PE/PB 采集字段、独立持久化和数值指数点位身份交叉核验。

2026-11-01 本地结果：61 项 pytest 与 24 个子测试、36 项 unittest、Node 逻辑/语法、Python 编译和 Playwright 桌面/390px 回归全部通过。

截图：

- `artifacts/v0.5-single-controls.png`
- `artifacts/v0.5-comparison.png`
- `artifacts/v0.5-maximized.png`
- `artifacts/v0.5-mobile-controls.png`
- `artifacts/v0.6-decomposition.png`
- `artifacts/v0.6-decomposition-mobile.png`
- `artifacts/v0.7-monthly-attribution.png`
- `artifacts/v0.7-monthly-attribution-mobile.png`
- `artifacts/v0.7-live-monthly-attribution.png`（当前真实 SQLite：新浪点位 + 乐咕月频估值）
- `artifacts/v0.8-real-pe-comparison.png`（本机四指数真实 PE，10 年统计基准）
- `artifacts/v0.8-real-pb-comparison.png`（本机四指数真实 PB，10 年统计基准）

## 文件结构

- `app.py`：HTTP/API、比较序列查询、CLI 和更新入口。
- `decomposition.py`：严格同日的指数估值对数归因纯算法。
- `data_sources/history.py`：估值采集、验证、持久化和状态查询。
- `data_sources/market.py`：指数收盘采集、验证和更新。
- `data_sources/storage.py`：SQLite v3 schema 与安全迁移。
- `static/index.html`：分析卡片、工具栏、双图和比较表结构。
- `static/styles.css`：响应式工具栏、比较表和最大化布局。
- `static/app.js`：统一状态、Plotly 配置、单指数、比较与涨跌归因交互。
- `docs/`：ChatGPT/Codex 共享的状态、决策与任务事实记录。
- `tests/test_comparison_api.py`：共同日期、缺失数据和来源一致性测试。
- `tests/test_time_axis.js`：前端状态、时间轴、比较和最大化单元测试。
- `tests/browser_time_axis.py`：Playwright 真实交互验收与截图。

本工具仅用于历史估值研究，不构成投资建议或交易指令。
