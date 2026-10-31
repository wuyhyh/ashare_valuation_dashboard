# A 股市场估值观察站（v0.7）

本地运行的 A 股指数估值仪表盘：Python HTTP 服务 + SQLite + Plotly.js。v0.7 保留既有界面、统计与数据库结构，修复真实指数日线采集，并让月频估值可以与日频指数点位进行严格同日归因。

## v0.7 数据可用性

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

当前真实 SQLite 仅有沪深300 PE/PB，因此真实页面会显示 `1/4` 个指数可比较，其余三个明确标记为缺失。要完成真实四指数排名，必须先按同一已验证来源和口径采集另外三个指数；本版本没有虚构这些数据。

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
python app.py update --index 000300 --metric pe_ttm
python app.py update --index 000300 --metric pb
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

2026-10-31 本机联网验收数据库（SQLite 不随仓库分发）：

| 指数 | 指标 | 记录数 | 覆盖区间 | 频率 | 来源接口 |
| --- | --- | ---: | --- | --- | --- |
| 沪深300 | PE TTM | 259 | 2005-04-29 ～ 2026-10-30 | 月频 | `ak.stock_index_pe_lg` |
| 沪深300 | PB | 259 | 2005-04-29 ～ 2026-10-30 | 月频 | `ak.stock_index_pb_lg` |
| 沪深300 | 指数收盘 | 2628 | 2016-01-04 ～ 2026-10-30 | 日频 | `ak.stock_zh_index_daily` 与 `ak.stock_zh_index_hist_csindex` 均已独立核验 |
| 上证50 / 中证500 / 中证1000 | PE/PB | 0 | — | — | 尚无已验证真实序列 |

PE 严格使用 `滚动市盈率`，不使用静态、等权或中位数列；PB 严格使用 `市净率`，不使用等权或中位数列。不同 `series_id` 不会拼接。

2026-10-31 使用 AKShare `1.20.18` 实际验证：新浪 `ak.stock_zh_index_daily("sh000300")` 返回 6020 条（2002-01-04 至 2026-10-30），写库时按请求区间保存 2628 条；腾讯最近区间返回 17 条。中证官方接口同区间返回 2628 条，官方收盘与新浪共同日期最大绝对差 `0.005` 点。官方滚动 PE 与乐咕 PE 在共同月末最大相对差约 `25.22%`，因此官方 PE 只保留为独立研究候选，不自动激活。真实 SQLite 中乐咕 PE/PB 月频序列与指数日线的 1/3/5/10 年归因均可计算，覆盖率 100%。

## 数据安全

- 沿用 SQLite schema v3：`series`、`observations`、`active_series`、`update_runs`。
- v0.7 没有修改数据库结构，也没有覆盖、删除现有估值记录；更新前备份为 `valuation.sqlite3.backup-pre-v07-20261031`。
- 日期排序、去重、未来日期拒绝、空数据/列变化拒绝、缺口和突变标记逻辑保持不变。
- 候选指数历史明显短于当前活动历史时拒绝切换并记录失败，避免短期回退覆盖完整十年序列。
- 比较 API `/api/comparison` 只返回当前激活的真实 SQLite 序列，不调用 `demo_rows`。
- 四指数共同日期通过真实日期集合交集计算。
- 来源接口或 methodology 不一致时，`strict_comparison=false`，前端以警告色展示。
- 比较条形图仅绘制具有共同基准日、相同来源口径、足够样本且覆盖完整统计窗口的序列；被排除的序列仍保留在明细表并显示原因。
- 图表标题区提供“复位缩放”，可恢复当前 3月～全部历史观察范围；静态资源版本已提升至 `v0.7`，避免浏览器继续使用旧缓存。

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
