# 数据资产与生命周期治理平台

面向数据资产编目、冷热分层、生命周期管理、血缘追踪与合规治理的统一数据治理平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/prefetch_capacity.py  回迁容量明细、冷存读取与暂存占用
│   ├── app/scenario.py          回迁容量演示场景（80GB 冷存对象 + 唯一暂存位）
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 资产编目 | `asset_catalog` | 数据资产 | 资产编号、资产名称、所属域 |
| 分层策略 | `tier_policy` | 分层策略 | 策略编号、目标资产、当前层级 |
| 血缘图谱 | `lineage_graph` | 血缘记录 | 记录编号、上游资产、下游资产 |
| 保留规则 | `retention_rule` | 保留规则 | 规则编号、目标资产、保留天数 |
| 合规检查 | `compliance_check` | 合规规则 | 规则编号、检查维度、合规标准 |
| 冷存储管理 | `cold_storage` | 冷存任务 | 任务编号、源资产、目标存储 |
| 预热回迁 | `hot_prefetch` | 回迁任务 | 任务编号、冷存位置、目标分区 |
| 回迁容量明细 | `prefetch_capacity` | 回迁容量明细 | 明细编号、冷存对象、目标分区、计划数据量GB |
| 版本快照 | `version_snapshot` | 数据快照 | 快照编号、关联资产、快照时间 |
| 标签体系 | `label_tag` | 数据标签 | 标签编号、标签名称、标签类型 |
| 审计日志 | `audit_log` | 审计记录 | 记录编号、操作主体、操作类型 |
| 访问网关 | `access_gate` | 访问策略 | 策略编号、目标资产、授权主体 |
| 成本追踪 | `cost_tracker` | 成本记录 | 记录编号、关联资产、存储成本 |
| Schema注册 | `schema_registry` | Schema版本 | 版本编号、关联资产、字段清单 |
| 分区规划 | `partition_plan` | 分区方案 | 方案编号、关联资产、分区键 |
| 补数任务 | `backfill_job` | 补数作业 | 作业编号、目标资产、补数区间 |
| 质量报告 | `quality_report` | 质量报告 | 报告编号、关联资产、检核时间 |
| 回收清理 | `reclaim_job` | 回收任务 | 任务编号、目标资产、回收范围 |
| 变更通知 | `change_notice` | 变更通知 | 通知编号、关联资产、变更类型 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 回迁容量明细

`prefetch_capacity` 把待回迁分区的计划数据量按冷存对象聚合，派发前就能看出哪些目标分区
共用同一次冷存读取。派发由冷存储管理读取明细后发起，服务层记录每次物理读取：

- 读取编号、冷存对象、物理读取GB（按对象大小计一次，不随目标分区数翻倍）
- 暂存占用区间（唯一 80GB 暂存位）
- 收到数据的目标分区及其时限承诺、实际数据去向

接口：

| 接口 | 说明 |
| --- | --- |
| `GET /api/prefetch_capacity/summary` | 按冷存对象聚合的计划量与对象去重物理量 |
| `GET /api/prefetch_capacity/overview?as_of=` | 各时间段实际读取、暂存峰值占用与数据去向 |
| `GET /api/prefetch_capacity/snapshot?as_of=` | 按时间点核对每个目标取得的数据、暂存占用与是否按期 |
| `GET /api/prefetch_capacity/tradeoff` | 复用暂存省读量 vs 临时请求交付时限的冲突对照 |
| `POST /api/prefetch_capacity/dispatch` | `{capacity_id, as_of, hold_until}` 实际派发一次冷存读取 |
| `POST /api/prefetch_capacity/{id}/actions` | `派发读取` / `回收占用` / `重置演示场景` |

演示场景（日期 2026-11-22）：同一个 80GB 冷存对象 `S3://glacier/obj-80g` 由 A、B 两个
回迁目标共用，任务队列只有一个 80GB 暂存位；另有一笔 10:30 可开始、最晚 13:00 交付的
临时请求。按各目标窗口分别派发：A 08:00 读取、09:00 交付、10:00 保留到期；临时请求
10:30 读取、11:30 交付；B 12:00 读取、13:00 交付。源端实际读取共 240GB（共享对象被读
两次，各计 80GB），目标侧取得量按目标分区单独保留，不与源端读数相加。若把 A 的暂存
留到 B 的保留截止（18:00），唯一暂存位会让临时请求错过 13:00 承诺。

前端入口：`/prefetch_capacity`（左侧「回迁容量明细」）。

测试：

```bash
cd backend
.venv/bin/python test_prefetch_capacity.py   # 回迁容量场景
.venv/bin/python test_cross_module.py        # 原有跨模块链
```

领域：数据工程
子方向：数仓与数据湖
