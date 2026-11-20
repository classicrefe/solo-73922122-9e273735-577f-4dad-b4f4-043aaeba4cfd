# 河流水质站日 CSV 发布与负荷核算服务

面向科研核验的不可改原件发布 API：原件原字节存共享卷，SQLite 只存目录、摘要与核算结果，
以 SHA-256 固定深度 Merkle 树出具检查点、单件纳入证明与检查点追加证明，
并提供完全离线的验证函数/CLI。流域分析员按发布单读取同站同日的**流量**与**总磷浓度**
两份 CSV 原件，核算各整点小时与全日**可达到的负荷上下界（克）**供日报入仓。
两个实例挂载同一卷并发发布/重算，不分叉、不覆盖、失败保留旧结果。

## 1. 目录结构

```
app/
  config.py     运行配置（DATA_DIR、1 MiB、128 件、树深度 7）
  storage.py    原件字节存储：内容寻址 + 临时文件 fsync + 原子改名
  db.py         SQLite 持久层：WAL、BEGIN IMMEDIATE 串行化、乐观并发、启动自检
  merkle.py     Merkle 树纯函数：根、纳入证明、追加证明、离线验证
  loadcalc.py   负荷核算纯函数：CSV 解析、时段校验、小时/全日负荷上下界（Fraction 精确运算）
  verify.py     离线核验函数与 CLI（python -m app.verify ...）
  schemas.py    HTTP 请求/响应模型
  main.py       FastAPI 路由（阻塞调用均走线程池）
tests/          pytest 用例（Merkle/API/故障路径 + 负荷算法/API，共 42 项）
scripts/
  demo.sh                 HTTP 端到端演示（含负荷核算、缺测拒绝、并发重算）
  dual_instance_check.py  双进程共卷并发/故障转移/负荷重算集成检查（26 项）
Dockerfile
docker-compose.yml        两个实例共卷，端口 8001/8002
```

## 2. 启动

### Docker Compose（交付目标形态）

```bash
docker compose up --build
# 实例 A：http://localhost:8001/docs
# 实例 B：http://localhost:8002/docs
```

两实例镜像相同、挂载同一命名卷 `shared-data`（`/data`），
`MAX_FILE_BYTES=1048576`、`MAX_RELEASES=128`（对应 Merkle 深度 7，128 个叶子槽）。
打开任一实例的 `/docs` 即可交互演示原件发布、证明下载与 `/load-results` 核算/查询。

### 本地直接运行

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
DATA_DIR=./data uvicorn app.main:app --port 8000
# 打开 http://localhost:8000/docs
```

## 3. API 一览（/docs 可交互演示）

### 3.1 原件发布与核验（原有能力）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/releases` | 表单 `station_id`、`sample_date`(YYYY-MM-DD)、`order_no`、`file`；同单同输入重试复用（`reused=true`），异输入 409，超 1 MiB 返回 413，超 128 件返回 409，写盘失败返回 507 |
| GET | `/releases` | 全部发布目录记录（不含字节） |
| GET | `/releases/{station}/{date}/{order}` | 单条目录记录 |
| GET | `/releases/{station}/{date}/{order}/download` | 下载已提交原件，文件名带站日单号 |
| GET | `/releases/{station}/{date}/{order}/inclusion-proof` | 单件纳入证明（恒 7 步路径） |
| GET | `/checkpoints` | 全部检查点（含 0 号空树检查点） |
| GET | `/checkpoints/latest` | 最新检查点（条数 + Merkle 根） |
| GET | `/checkpoints/{seq}` | 任意旧检查点 |
| GET | `/checkpoints/{old_seq}/append-proof` | 旧检查点 → 最新检查点的追加证明 |
| GET | `/health` | 启动自检结果（缺件/摘要不符时 `degraded`，并拒发证明、下载与核算） |

### 3.2 负荷核算（本次新增）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/load-results` | JSON 请求体按发布单读取两份原件并核算；`expected_id` 首次留空，重算携带要替换的现行编号；成功 201，校验失败 422（逐条问题时段），未发布单号 404，编号已变 409 |
| GET | `/load-results` | 各站日现行结果：现行编号、24 个整点小时与全日区间、两份原件发布单号与摘要 |
| GET | `/load-results/{station}/{date}` | 单站日现行结果 |
| GET | `/load-results/{station}/{date}/revisions` | 该站日全部历史版本（只追加，`is_current` 标记现行版本） |

请求体示例：

```json
{
  "station_id": "ST-LOAD",
  "sample_date": "2026-11-20",
  "flow_order_no": "FLOW-001",
  "conc_order_no": "CONC-001",
  "expected_id": null
}
```

## 4. 原件 CSV 格式（UTC 日内左闭右开时段）

两份原件都必须先经 `/releases` 发布，核算时只按发布单读取卷上已提交字节，
并复核 SHA-256 摘要，杜绝“核算用的字节”与“入仓原件”不一致。

### 4.1 流量表 `start,end,volume_m3,max_flow_m3s`

每段给出左闭右开时段、段总水量 V（m³）与瞬时流量上限 q（m³/s）。

```csv
start,end,volume_m3,max_flow_m3s
00:00,12:00,1800,1
12:00,24:00,1800,1
```

### 4.2 浓度表 `start,end,conc_low_mg_l,conc_high_mg_l`

每段给出左闭右开时段与总磷浓度上下界（mg/L）。

```csv
start,end,conc_low_mg_l,conc_high_mg_l
00:00,24:00,2,4
```

* 时间接受 `HH:MM`、`HH:MM:SS` 与带 `Z` 的 UTC 时间戳（如 `2026-11-20T08:00:00Z`），
  右端点允许 `24:00` 或次日 `00:00:00Z` 表示日末；
* 两表表内时段互不重叠，数值必须有限且非负（拒绝负数、NaN、Inf）；
* 流量表必须连续覆盖全天 `[00:00, 24:00)`，缺口即拒绝并指出缺口时段；
* 段总水量与上限矛盾（`V > q × 段长秒数`）即拒绝并指出问题时段；
* 有水量（V>0）的时段若缺浓度覆盖即拒绝并指出缺失区间；零水量段不要求浓度。

## 5. 负荷上下界核算模型

### 5.1 单位与目标

恒等式 `1 mg/L × 1 m³ = 1 g`（1 m³ = 1000 L，毫克与升的千倍因子相消），
故任意子区间负荷（克）= 浓度(mg/L) × 水量(m³)。段内水量的时间分布未知，
仅受两个约束：段总水量固定为 V；任一瞬时流量不超过 q，即时长 d 秒的子区间
最多容纳 `q·d` 立方米。分别求每个整点小时窗口与全日窗口内**可达到**的负荷上下界。

### 5.2 窗口可取水量

对窗口 W（整点小时或全日）与流量段 i：

```
w_max_i(W) = min(V_i, q_i·|F_i∩W|)       # 窗内容量上限
w_min_i(W) = max(0, V_i − q_i·|F_i\\W|)   # 段外放不下的水量必在窗内
```

把 `F_i∩W` 按浓度段切成原子片 k（容量 `cap_k = q_i·片长秒数`、浓度区间 `[a_k, b_k]`）：

* **上界**：把可取水量 `w_max` 按浓度上界 `b_k` 从高到低贪心灌满；
* **下界**：把必取水量 `w_min` 按浓度下界 `a_k` 从低到高贪心灌起。

线性规划的极值在贪心极点取得，上下界均可由满足全部约束的具体分段流量分配实际达到
（已用全天 6 槽离散暴力枚举对随机实例逐窗口验证，见 `tests/test_loadcalc.py`）。
内部全程使用 `fractions.Fraction` 有理精确运算，避免倒数往返的浮点残差，
仅在 JSON 输出时转为 int/float。

### 5.3 响应片段

```json
{
  "id": 2,
  "station_id": "ST-LOAD",
  "sample_date": "2026-11-20",
  "flow_order_no": "FLOW-001",
  "conc_order_no": "CONC-001",
  "flow_blob_sha256": "…",
  "conc_blob_sha256": "…",
  "hours": [
    {"hour": 0, "window_start": "00:00:00", "window_end": "01:00:00",
     "load_low_g": 0.0, "load_high_g": 7200.0}
  ],
  "day": {"load_low_g": 7200.0, "load_high_g": 14400.0},
  "created_at": "2026-11-20T02:20:33Z"
}
```

## 6. 崩溃安全与并发不变量

1. **先落盘后入库**：原件写临时文件 → `fsync` → `os.replace` 原子改名 → `fsync` 目录，
   成功后才开数据库事务；写盘失败无库记录、临时文件清理、可原样重试。
2. **串行化发布与核算**：所有写事务 `BEGIN IMMEDIATE`，双实例写操作被 SQLite 锁串行化，
   后到者等待后重检唯一键/现行编号；`busy_timeout` 之外另有 3 次有限重试。
3. **只增不改**：发布同键同字节复用、同键异字节 409；负荷结果每站日只插入新版本，
   历史版本可经 `/revisions` 回溯，无 UPDATE/DELETE 路径。
4. **乐观并发重算**：提交携带要替换的 `expected_id`（首次留空）。事务内复核该站日
   现行编号，已被另一实例改写即回滚并返回 409（回传 `current_id`），旧结果原样保留。
5. **失败保留旧结果**：原件定位（404）、读取/摘要复核（503）、CSV 校验与核算（422）
   全部发生在写事务之前或事务回滚路径上，任何失败都不会产生半截新版本。
6. **启动自检**：重算全部叶子与根，逐件校验卷上原件摘要并检查序号稠密、检查点连续；
   发现缺件即 `degraded`，拒绝发布/证明/下载/核算。
7. 阻塞式磁盘/DB 调用均在任意线程池执行，锁等待不阻塞事件循环。

## 7. 测试结果（本环境实测）

* `pytest tests/`：**42 项全部通过**
  - Merkle（原）：根稳定性、纳入/追加证明、篡改/重放/重叠/缺口拒绝；
  - API/故障（原）：发布、幂等、409/413/422、128 上限、下载、离线核验、写盘失败、
    缺件自检与 503 拒服、孤立文件无害、重启后检查点可复算；
  - 负荷算法（新）：多格式时间解析、表头/数值/负值/NaN 拒绝、未覆盖全天/重叠/容量矛盾/
    有水缺浓度四类拒绝、恒定与分段浓度的小时/全日数值、干枯小时为 0、分数精确性、
    以及 25 组随机实例 ×9 窗口的离散暴力枚举紧界验证；
  - 负荷 API（新）：首次核算与查询形态、乐观并发与版本历史、双线程并发重算恰一胜一冲突、
    缺测/容量矛盾/缺浓度三类 422 后旧结果保留、保存阶段磁盘故障事务回滚不留半截行、
    未发布单号 404、入参 422、多段小时回归。
* `scripts/dual_instance_check.py`（两个独立 uvicorn 进程共卷）：**26 项全部通过**，
  覆盖并发同单、10 件并发异单序号稠密、跨实例取件/检查点/证明，以及新增的跨实例核算、
  重复首次 409、**两实例并发重算一胜一冲突且只前进一个编号**、缺测 422 保留旧结果、
  第三进程挂同卷的故障转移视角与继续追加。
* `scripts/demo.sh`（对真实 uvicorn 实例）：退出码 0，13 段演示全部符合预期。
* 手工冒烟：`24:00`/次日 `00:00:00Z` 日末、`HH:MM:SS`、科学计数法数值均可解析；
  `/docs` 与 `/openapi.json` 含全部 12 条路径与请求/响应模型。

## 8. 剩余工作与已知限制（据实说明）

1. **本环境无 Docker 守护进程**，`docker compose up --build` 未实际起容器验证；
   已用两个独立 uvicorn 进程共享同一 `DATA_DIR` 做等价并发/故障转移/重算测试，
   compose YAML 已通过语法校验，需在具备 Docker 的环境执行一次复核。
2. 沙箱以 seccomp 禁止 `kill` 系统调用，集成脚本无法终止演示进程，改为随机端口 +
   每次独立临时目录；脚本中的“重启”以第三进程挂同卷模拟（故障转移语义等价）。
3. 负荷数值内部为精确有理数，JSON 输出为 IEEE-754 double（整值输出整数）；
   日报量级（m³ × mg/L = g）下精度充裕，若未来需要严格十进制口径可改为字符串输出。
4. 窗口极值是“各自可达到”的上下界：某小时的上界与下界一般对应不同的水量分配，
   不保证存在同一分配同时达到所有小时的极值（全日上下界另由独立窗口保证可达）。
5. 128 件发布上限在实现上是固定深度树的叶子槽位数（2 的幂，可经 `MAX_RELEASES` 调整）；
   负荷结果编号独立计数、不受该上限约束。
6. 未引入鉴权/传输加密/TLS，定位为内网或网关后的服务；对外暴露需前置网关。
7. 共享卷自身的备份与异地容灾未包含在本服务内；服务能检出卷上缺件与篡改，
   但不会自动恢复字节。
