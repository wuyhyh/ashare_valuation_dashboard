# A 股市场估值观察站（v0.5）

本地运行的 A 股指数估值仪表盘：Python HTTP 服务 + SQLite + Plotly.js。v0.5 保留 v0.3 的真实估值数据管线和 v0.4 的共享时间轴双图，重点统一图表操作区域并增加多指数估值比较。

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

估值诊断与更新：

```bash
python app.py diagnose --index 000300 --metric all
python app.py update --index 000300 --metric pe_ttm
python app.py update --index 000300 --metric pb
```

指数点位诊断与更新：

```bash
python app.py diagnose-market --index 000300 --start 20160101 --end 20261030
python app.py update-market --index 000300 --start 20160101 --end 20261030
```

命令行更新失败返回非零退出码。网页更新仍调用原有 `/api/update` 和 `/api/update-market`，失败时保留原始完整历史。

## 数据覆盖与来源

2026-10-31 当前真实数据库：

| 指数 | 指标 | 记录数 | 覆盖区间 | 频率 | 来源接口 |
| --- | --- | ---: | --- | --- | --- |
| 沪深300 | PE TTM | 259 | 2005-04-29 ～ 2026-10-30 | 月频 | `ak.stock_index_pe_lg` |
| 沪深300 | PB | 259 | 2005-04-29 ～ 2026-10-30 | 月频 | `ak.stock_index_pb_lg` |
| 上证50 / 中证500 / 中证1000 | PE/PB | 0 | — | — | 尚无已验证真实序列 |

PE 严格使用 `滚动市盈率`，不使用静态、等权或中位数列；PB 严格使用 `市净率`，不使用等权或中位数列。不同 `series_id` 不会拼接。

指数收盘目标接口为 `ak.index_zh_a_hist`，存储指数自身不复权日线收盘点位，不使用 ETF。2026-10-31 现场联网请求仍遭东方财富 `Push2RateLimitError`，当前真实 SQLite 没有指数点位；页面会明确提示缺失。Playwright 中的点位和四指数序列均标记为测试模拟响应，只验证交互且不写入 SQLite。

## 数据安全

- 沿用 SQLite schema v3：`series`、`observations`、`active_series`、`update_runs`。
- v0.5 没有修改数据库结构，也没有覆盖、删除现有估值记录。
- 日期排序、去重、未来日期拒绝、空数据/列变化拒绝、缺口和突变标记逻辑保持不变。
- 比较 API `/api/comparison` 只返回当前激活的真实 SQLite 序列，不调用 `demo_rows`。
- 四指数共同日期通过真实日期集合交集计算。
- 来源接口或 methodology 不一致时，`strict_comparison=false`，前端以警告色展示。
- 比较条形图仅绘制具有共同基准日、相同来源口径、足够样本且覆盖完整统计窗口的序列；被排除的序列仍保留在明细表并显示原因。
- 图表标题区提供“复位缩放”，可恢复当前 3月～全部历史观察范围；脚本版本已提升至 `v0.5.1`，避免浏览器继续使用旧缓存。

## 测试

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
node tests/test_time_axis.js
node --check static/app.js
python -m py_compile app.py data_sources/*.py
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

截图：

- `artifacts/v0.5-single-controls.png`
- `artifacts/v0.5-comparison.png`
- `artifacts/v0.5-maximized.png`
- `artifacts/v0.5-mobile-controls.png`

## 文件结构

- `app.py`：HTTP/API、比较序列查询、CLI 和更新入口。
- `data_sources/history.py`：估值采集、验证、持久化和状态查询。
- `data_sources/market.py`：指数收盘采集、验证和更新。
- `data_sources/storage.py`：SQLite v3 schema 与安全迁移。
- `static/index.html`：分析卡片、工具栏、双图和比较表结构。
- `static/styles.css`：响应式工具栏、比较表和最大化布局。
- `static/app.js`：统一状态、Plotly 配置、单指数与比较交互。
- `tests/test_comparison_api.py`：共同日期、缺失数据和来源一致性测试。
- `tests/test_time_axis.js`：前端状态、时间轴、比较和最大化单元测试。
- `tests/browser_time_axis.py`：Playwright 真实交互验收与截图。

本工具仅用于历史估值研究，不构成投资建议或交易指令。
