# 退役电池包收货与安全放行系统

面向拆解中心的收货、隔离、拆解、检测、安全放行、仓库拣货与下游签收一体化应用。
技术栈：**Vue 3 + Vite（前端）**、**FastAPI + SQLAlchemy（后端）**、**PostgreSQL（数据库）**。

---

## 1. 业务能力与规则落地

| 需求点 | 实现位置/行为 |
| --- | --- |
| 每包唯一序列号；重复扫码只返回原收货单 | `services/receiving.py::receive_pack`，响应字段 `duplicated=true` |
| 收货单显示隔离位、责任班组、未完成前置项 | 收货单与包列表均返回 `pending_preconditions`（碰撞/浸水未复检、热事件未结案、缺隔离位/班组） |
| 整批隔离 vs 单包隔离 | `freeze_batch` / `quarantine_pack`；整批冻结挡住同车所有包，单包隔离仅冻结风险包 |
| 双方见证扫码建立包→模组父子关系 | `services/dismantling.py::link_module`，两名见证者必须不同 |
| 重贴标签保留旧号、操作者、时间，两人分别确认 | `label_history` 表 + `relabel` 两阶段接口（发起→第二人确认生效） |
| 无法确认父子关系的模组隔离待核 | `register_unknown_module`：`pack_id=NULL、unknown_parent=true`，工单挂起，**绝不自动归到最相似序列号** |
| BMS 正常不能抵消碰撞/浸水历史 | `services/safety.py::echelon_blockers` 明确提示该原则，必须有复检合格回执 |
| 校准过期 / 绝缘失败 / 热事件未结案禁止梯次利用 | 同上函数的硬门禁，任一命中即 409 拒绝 |
| 8 种状态互不混淆 | `enums.PackStatus`：接收、隔离、拆解、待检、待审批、可复用、待回收、已出库 |
| 退回补检后旧放行单失效 | `_void_active_approvals`：旧单 `voided`，待拣货单 `corrected`，已通知买家收到更正通知 |
| 仓库只能按已批准去向拣货 | `warehouse.pick` 校验放行单有效性并沿父子链重新校验 |
| 出库单限数量、用途、收货人 | 梯次利用批准时自动生成 `outbound_orders`，超限量出库被拒绝 |
| 买家拒收只能退回隔离待复核 | `sign_off(rejected)`：包回到 `quarantined+frozen`，不自动回可用库存 |
| 装车货物不能删除，只能召回/退运 | `recall_or_return`，历史出库单与审计日志保留 |
| 数量核对：放行/拆出/称重/已发差额未解释阻止结清 | `compute_reconciliation` + `close_batch`（409 `reconciliation_blocked`） |
| 单包策略下每次拆分/换标/出库沿父子链重校验 | `revalidate_parent_chain` 在拣货、装车前强制执行 |
| 检验排程：结果绑定包/步骤/设备校准/资质/前置 | `services/scheduling.py`，见下方「周三检验班排程」 |
| 全链路留痕 | `audit_logs` 表 + `/api/audit/logs` |

### 周三检验班排程（P-8 / P-9 示例）

- **资源**：绝缘仪 `INS-1` 可用 09:00-10:20，热像舱 `THM-1` 可用 09:00-10:40；`T1` 具绝缘+热像资质（工作至 10:45），`T2` 仅热像（至 10:30）。设备与人员资质在 `inspection_stations` / `inspector_profiles` 表。
- **两种排法**（`POST /api/scheduling/compare`，前端「检验排程」页）：
  - 按包连续 `pack_continuous`：T1 连续做 P-8 绝缘 09:00-09:30、P-8 热像 09:30-09:50、P-9 绝缘 09:50-10:10、P-9 热像 10:10-10:35，**完成 10:35、交接 0 次**；
  - 按设备阶段交错 `equipment_stage`：T1 绝缘（P-8 09:00-09:30、P-9 09:30-09:50），T2 热像（P-8 09:30-09:50、P-9 09:50-10:15），**完成 10:15、交接 2 次**。
  - 两条均输出完成时刻、设备空等区间与交接记录。
- **结果绑定**：每条结果写入 `step_results`，冻结包序列号、工序、设备编号及其当时校准有效期、操作者资质、前置结果；提交方包号不符或读数夹带他包序列号一律 409（`cross_pack_reading`）。
- **失败只能复测**：绝缘/热像失败自动生成 `attempt+1` 的复测步骤，原失败结果保留不可覆盖（`result_immutable`），无「改为合格」接口；未达合格前不签发。
- **设备停机**：`POST /.../downtime` 只重排未开始的步骤，已完成/进行中保持实际时间（`kept=true`）。
- **校准提前失效**：`POST /.../calibration` 将超出新有效期的已完成结果标记 `valid=false` 并生成复做步骤，失败包转为 `retest_required`。
- **包件取消**：`POST /.../cancel` 取消未开始步骤、历史结果保留；已签发结论不能取消（`already_released`）。
- **迟到回执**：`POST /.../late-receipt` 在包已签发时只登记并置 `review_required`，绝不覆盖 `released` 结论；未签发时按空验结果处理（失败同样触发复测）。
- **一键回放**：`POST /api/scheduling/replay` 在同一资源上演示热像失败、校准提前失效、包件取消、迟到回执四类异常。

### 样例 L-7（种子数据自动播种）

- **P-1**：碰撞记录（COL-2026-001）且未复检；**P-2 / P-3 / P-4**：身份链完整、检测合格、无共同热事件。
- 前端「L-7 场景」页或 `POST /api/scenario/l7/compare` 可对比两种策略（仿真在独立事务中回滚，不污染真实数据）：
  - 整批冻结：P-1~P-4 全部不可放行，待拣货单 0、买家通知 0；
  - 单包隔离：P-2/P-3/P-4 先放行（3 张拣货单、3 条买家通知）；P-3 收到迟到浸水回执后仅暂停 P-3，旧放行单失效、拣货单更正、买家收更正通知，P-2/P-4 继续流转；P-1 拆解发现失配重贴标签 → 模组 `M-UNKNOWN-9` 隔离待核、工单挂起。

---

## 2. 测试账号（种子数据，无密码，登录后请求自动带 `X-Username` 头）

| 账号 | 姓名 | 角色 | 班组 |
| --- | --- | --- | --- |
| `u_receiver` | 王接收 | 接收员 receiver | 收货一班 |
| `u_dismantler` | 李拆解 | 拆解员 dismantler | 拆解一班 |
| `u_dismantler2` | 赵拆解 | 拆解员 dismantler（双见证/换标第二人） | 拆解二班 |
| `u_safety` | 陈安全 | 安全工程师 safety | 安全工程组 |
| `u_warehouse` | 孙仓储 | 仓库 warehouse | 仓储班 |
| `u_downstream` | 周买家 | 下游买家 downstream | 华东储能公司 |
| `u_admin` | 管理员 | 中心管理员 admin | 中心管理组 |
| `u_t1` | T1 检验员 | 检验员 inspector（绝缘+热像资质） | 周三检验班 |
| `u_t2` | T2 检验员 | 检验员 inspector（仅热像资质） | 周三检验班 |

种子检测设备：`DEV-OK`（校准有效 1 年）、`DEV-EXPIRED`（校准已过期 30 天，用于验证门禁）。
种子检验资源：`INS-1` 绝缘仪、`THM-1` 热像舱，检验员资质见 `inspector_profiles`。

---

## 3. 一键启动（Docker Compose）

```bash
cd /workspace/94
docker compose up --build
```

- 前端：http://localhost:8080
- 后端 API：http://localhost:8000
- 交互式 API 文档（Swagger UI）：http://localhost:8000/docs
- 首次启动自动建表并播种 L-7 场景数据。

---

## 4. 本地开发启动（容器内预装运行时方式）

### 4.1 后端

```bash
cd backend
/opt/python-3.12/bin/python3 -m venv .venv
.venv/bin/pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
# 数据库指向本机 PostgreSQL（预装库 ccdev，或自建 battery_demo）
export DATABASE_URL="postgresql+psycopg2://ccdev:ccdev@127.0.0.1:5432/battery_demo"
.venv/bin/uvicorn app.main:app --reload --port 8000
```

服务启动时自动 `create_all` 建表并播种；Swagger UI：http://127.0.0.1:8000/docs。

### 4.2 前端

```bash
cd frontend
/opt/node-22/bin/npm install --registry=https://registry.npmmirror.com --cache=./.npm-cache
/opt/node-22/bin/npm run dev      # http://127.0.0.1:5173 ，/api 已代理到 8000
# 生产构建/预览
/opt/node-22/bin/npm run build && /opt/node-22/bin/npm run preview   # http://127.0.0.1:8080
```

---

## 5. 验证方式

### 5.1 自动化测试（39 个用例）

```bash
cd backend
# 先建测试库：psql -h 127.0.0.1 -U ccdev -d ccdev -c "CREATE DATABASE battery_test;"
DATABASE_URL="postgresql+psycopg2://ccdev:ccdev@127.0.0.1:5432/battery_test" \
  .venv/bin/python -m pytest tests/ -v
```

覆盖：重复扫码、收货单前置项、整批/单包隔离、出库包只能召回、角色鉴权、双见证、同人见证拒绝、未知模组隔离与工单挂起、换标两人确认与留痕、碰撞/浸水 BMS 不可抵消、复检解除、校准过期、绝缘失败、热事件结案、退回补检失效与下游更正、拒收回隔离、召回退运、数量核对阻截结清、L-7 双策略对比与回滚无污染；以及检验排程：两种排法完成时刻/空等/交接、结果绑定（包/步骤/设备校准/资质/前置）、跨包与重复覆盖拒绝、热像失败生成复测、停机只重排未开始、校准提前失效作废复做、包件取消、迟到回执不覆盖已签发结论、无资质检验员拒绝。

### 5.2 命令行演示

```bash
DATABASE_URL="postgresql+psycopg2://ccdev:ccdev@127.0.0.1:5432/battery_demo" \
  .venv/bin/python demo_l7.py            # L-7 两种隔离策略
DATABASE_URL="postgresql+psycopg2://ccdev:ccdev@127.0.0.1:5432/battery_demo" \
  .venv/bin/python demo_scheduling.py    # 周三检验班两种排法 + 四类异常回放
```

### 5.3 界面手工验证路径

1. 用 `u_receiver` 登录，在「收货/隔离」页扫码收一包并再次扫同一序列号 → 提示返回原收货单；
2. 「L-7 场景」页点击运行仿真，对比两种策略的可放行包、待拣货单、买家通知；
3. 切 `u_safety`：查看 P-1 门禁（碰撞未复检）→ 对 P-2 批准梯次利用（自动生成出库单与买家通知）；
4. 切 `u_warehouse`：拣货 → 装车；切 `u_downstream`：拒收 → 回「收货/隔离」确认包回到隔离冻结；
5. 「仓库/核对」页对 L-7 尝试结清 → 存在差额时被阻止，填写解释后可结清；
6. 切 `u_t1`：「检验排程」页运行两种排法对比（完成 10:35 / 10:15、交接 0 / 2 次、设备空等）→ 对第二个计划「执行并让 P-9 热像失败」查看自动复测 → 签发结论（P-9 不签发）→ 提交 P-8 迟到回执查看「已签发不覆盖、仅标记复核」；或点「回放异常场景」一次看全四类异常。

---

## 6. 实际执行过的验证结论（本工作区）

- ✅ `pytest tests/`：**39 passed**（27 原有 + 12 排程；PostgreSQL `battery_test` 库，真实 PG 18 而非 SQLite）。
- ✅ `demo_scheduling.py`：按包连续完成 10:35、交接 0 次；按设备阶段交错完成 10:15、交接 2 次（T1→T2 各包换人）；绝缘仪空等 30 分、热像舱空等 55 分。回放：热像失败自动生成第 2 次复测且原失败保留（改合格被拒 `result_immutable`、跨包被拒 `cross_pack_reading`）；校准提前失效使 P-8/P-9 绝缘结果作废并复做；P-9 取消 2 个未开始步骤、结论 `cancelled`；签发后 P-8 迟到回执 `applied=False`、结论仍 `released`、`review_required=True`。
- ✅ `demo_l7.py`：整批冻结可放行 `[]`、拣货单 0、通知 0；单包隔离初始放行 `['P-2','P-3','P-4']`、拣货单 3、通知 3；P-3 迟到浸水后拣货 `blocked`、更正通知 1 条、P-2/P-4 继续流转；P-1 失配模组 `M-UNKNOWN-9` 为 `quarantine_review` 且 `pack_id=None`，工单关闭被阻止。
- ✅ HTTP 端到端抽验（uvicorn 实跑 `:8011`）：`/api/scheduling/setup` 返回 INS-1/THM-1 与 T1/T2 资质；`/compare` 两排法结果与题述一致；`/replay` 四类异常齐全；跨包提交与读数夹带他包序列号均 409；正确提交返回绑定的包/步骤/设备/资质/前置。
- ✅ 前端 `npm run build` 构建成功（含 `Scheduling` 分块）；`vite preview`（8080）HTTP 200 且排程页面分块可访问。
- ⚠️ 当前容器未安装 docker CLI，`docker compose up --build` 配置已交付但**未在本容器内实际执行**；在具备 Docker 的环境中可直接一键启动。

---

## 7. 目录结构

```
94/
├── docker-compose.yml          # 一键启动 db + backend + frontend
├── backend/
│   ├── Dockerfile / entrypoint.sh
│   ├── requirements.txt
│   ├── demo_l7.py              # L-7 命令行演示
│   ├── demo_scheduling.py      # 周三检验班排程 + 异常回放演示
│   ├── app/
│   │   ├── main.py             # FastAPI 入口（异常处理/CORS/建表播种）
│   │   ├── models.py           # SQLAlchemy 模型（含检验排程表）
│   │   ├── enums.py            # 8 种包状态等枚举
│   │   ├── seed.py             # 账号/设备/L-7/检验资源种子
│   │   ├── services/           # 业务规则（收货、拆解、安全放行、仓库、L-7 仿真、检验排程）
│   │   └── routers/            # API 路由（含 /api/scheduling）
│   └── tests/                  # pytest 39 例
└── frontend/
    ├── Dockerfile / nginx.conf
    └── src/views/              # 收货、拆解、检测台、检验排程、安全审批、仓库、下游签收、L-7、审计
```
