# 指标口径双算平台（Caliber Platform）

同一指标按**旧口径**与**候选口径**同时计算，多个报表通过统一登记服务依赖同一口径，
不允许各自复制公式后私改；修改维度过滤或时间归属时，系统自动找出需要重算的报表产物，
并显式标注哪些报表可逐行比较、哪些输入已经不同。所有数字均由系统对**虚构订单**做
真实加工得到（逐订单评估、逐行贡献落库），不是公式编辑器或版本列表演示。

## 技术栈

- Python 3.11 标准库（`sqlite3` / `http.server` / `unittest`），零第三方依赖
- 前端原生 HTML/CSS/JS，无 CDN、无构建步骤，离线可用
- SQLite 单文件库：`data/metrics.db`

## 启动

```bash
bash scripts/reset_db.sh     # 可选：清空并重建虚构订单与基线产物（841 单 / 8 份产物）
bash scripts/run_server.sh 8080
# 打开 http://localhost:8080
```

测试：

```bash
python3 -m unittest tests.test_e2e -v   # 11 个端到端用例，全部使用临时库
```

## 领域模型与表结构（`app/db.py`）

| 表 | 作用 |
| --- | --- |
| `metrics` | 指标（gmv、order_count），声明默认公式 |
| `calibers` | 口径版本：`candidate / approved / superseded / withdrawn`，含时间归属、维度过滤、公式、父版本、revision |
| `engines` / `engine_calibers` | 计算引擎与可服务口径的映射，支撑“计算器退出”演练 |
| `reports` | 报表消费端：依赖指标、维度拆分、升级策略（dual 双算 / whole 整版替换 / delayed 延后） |
| `products` | 报表 × 期间 × 口径的计算产物（合计、状态、引擎、inputs_differ 标记） |
| `contributions` | **逐订单贡献**：每个订单对每个产物留一行，含是否计入、贡献值、排除原因 |
| `impacts` | 口径变化自动登记的受影响报表/期间/产物，含 comparable 与 inputs_differ 判定 |
| `audit_log` | 登记、修订、审批、撤回、重算、引擎退出全部留痕 |
| `orders` | 虚构原始订单（固定随机种子 `20261117`，含跨月支付、月末下单、全额退款、赠品等边界单） |

## 公式唯一来源（`app/rules.py`）

`evaluate_order(order, spec, period)` 是所有口径计算的唯一入口，评估顺序固定：

1. **时间归属**：`order_time`（下单时间）或 `pay_time`（支付时间），为空或不在期间则排除；
2. **维度过滤**：仅允许 `status/channel/region/category/is_gift` 五个登记字段，
   支持 `in/not_in/eq/ne`，登记时校验，非法口径无法入库；
3. **公式取值**：`net_paid`（支付−退款）、`gross_paid`（支付额）、`row_count`（每单 1）。

口径登记服务、计算器、报告端都只调用这里，报表侧没有任何可私改的公式副本。

## 预置演示场景

**指标与口径**

- `gmv`（成交总额）：v1 按下单时间 + 实付净额；候选 v2 改**按支付时间归属**
  → 订单进入的期间不同，**输入已不同、总额不可直接比较**，但逐行可解释。
- `order_count`（有效订单数）：v1 仅过滤有效状态；候选 v2 增加**排除赠品、排除线下门店**
  → 同窗口同公式，**可比较**，差异可逐行对账。

**报表（4 份，覆盖三种升级策略）**

| 报表 | 指标/拆分 | 策略 | 行为 |
| --- | --- | --- | --- |
| GMV 总览月报 | gmv / 不拆分 | dual 双算过渡 | 新旧产物并存，保留对比 |
| 渠道 GMV 月报 | gmv / channel | whole 整版替换 | 批准后旧产物 superseded，只展示新口径 |
| 大区 GMV 月报 | gmv / region | delayed 延后至 2026-12-31 | 批准后仍用 v1 出数，可在网页结束延后 |
| 渠道有效订单数 | order_count / channel | dual 双算过渡 | 过滤变化的可比较对照 |

## 可比较性判定规则（`app/registry.py`）

- `comparable=1`：时间归属与公式都未变，同窗口下两口径差异可逐行对账（桥接闭合）；
- `inputs_differ=1`：时间归属或维度过滤变化，进入计算的订单集合已不同；
- 时间归属改变时 `comparable=0 且 inputs_differ=1`，网页明确提示“总额不能直接比较”，
  并在逐行差异中给出每单在两口径下的归属/排除原因。

## 关键端到端流程

1. **登记候选口径** → 与父口径做规则 diff（`diff_rule_sets`）→ 向 `impacts` 登记所有
   受影响报表×期间 → 候选计算器自动双算，逐订单贡献落库 → impact 标记 recomputed。
2. **修订候选口径**（仅 candidate）→ revision 自增、影响重新登记、全部候选产物先删后建重算。
3. **批准** → 候选转 approved、父版本转 superseded；按报表策略处理旧产物；
   delayed 报表保持旧口径并登记，可单独“结束延后”切换。
4. **撤回**（仅 candidate）→ 产物作废、引擎解绑、影响关闭；已批准口径拒绝撤回（须走新版替换）。
5. **计算器退出模拟**（不落库）→ 检查引擎独占口径与在役产物；有阻塞时给出具体阻塞清单，
   真实退出在条件不满足时直接报错；撤回候选后候选引擎变为可安全退出。
6. **逐行差异**（`app/diff.py`）→ 两产物逐订单并排，差异分 same/added/removed/changed，
   桥接式对账：`旧合计 − 移除 + 新增 + 公式差 = 新合计`，并有闭合校验；
   存储合计与逐行重建合计互相印证。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/overview` `/api/metrics` `/api/calibers` `/api/reports` `/api/engines` | 基础登记数据 |
| GET | `/api/products` `/api/report-detail?report=` | 产物与维度拆分汇总 |
| GET | `/api/impacts?caliber=` | 影响登记与可比较性标注 |
| GET | `/api/diff?a=&b=` | 逐行贡献差异 + 桥接对账 |
| GET | `/api/orders?...` | 虚构订单多维查询（可按下单/支付时间） |
| GET | `/api/simulate/withdraw?caliber=` `/api/simulate/retire?engine=` | 撤回 / 退出模拟（不落库） |
| GET | `/api/audit` | 审计日志 |
| POST | `/api/calibers` | 登记候选（自动影响分析 + 双算） |
| POST | `/api/calibers/amend` `/approve` `/withdraw` | 修订 / 批准 / 撤回 |
| POST | `/api/reports/delay` `/lift-delay` | 延后升级 / 结束延后 |
| POST | `/api/engines/retire` `/reactivate` `/api/compute` `/recompute` | 引擎与重算操作 |

## 网页功能（七个页签）

总览 KPI、口径登记（含过滤条件编辑器/修订/批准/撤回）、报表与逐行差异（桥接图、
差异类型着色、可只看差异行）、影响与重算（可比较性与输入差异说明）、
撤回/退出模拟（安全判定与阻塞清单，模拟与真实执行分离）、订单查询、审计日志。

## 目录

```
app/         后端：db / rules(唯一公式) / orders(虚构数据) / calculator / registry / diff / server / bootstrap
web/         前端：index.html / styles.css / app.js
tests/       端到端测试（临时库，不污染正式数据）
scripts/     重建数据库 / 启动服务
data/        SQLite 库（.gitignore 已忽略）
```
