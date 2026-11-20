# 数据分析实验与数据应用服务平台

面向指标口径管理、分析实验设计、OLAP查询、数据看板构建与数据API发布的数据应用平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
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
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 指标定义 | `metric_def` | 指标 | 指标编号、指标名称、计算公式 |
| 维度管理 | `dimension` | 分析维度 | 维度编号、维度名称、维度类型 |
| 数据集管理 | `dataset` | 分析数据集 | 数据集编号、数据集名称、来源类型 |
| 分析实验 | `experiment` | 实验方案 | 方案编号、实验名称、分析数据集 |
| OLAP查询 | `query_builder` | 查询模板 | 模板编号、模板名称、查询语句 |
| 数据看板 | `dashboard` | 看板配置 | 看板编号、看板名称、包含图表 |
| 图表配置 | `chart` | 图表 | 图表编号、图表名称、图表类型 |
| 数据API发布 | `data_service` | 数据服务 | 服务编号、服务名称、输出数据集 |
| 智能告警 | `alert` | 告警规则 | 规则编号、告警名称、监控指标 |
| 分析用户组 | `user_group` | 用户组 | 组编号、组名称、组描述 |
| 数据快照 | `snapshot` | 快照记录 | 快照编号、数据集名称、快照时间 |
| 任务调度 | `scheduler` | 调度任务 | 任务编号、任务名称、调度类型 |
| 缓存管理 | `cache` | 缓存配置 | 缓存编号、缓存目标、缓存策略 |
| 分析管道 | `pipeline` | 分析节点 | 节点编号、输入数据集、分析算子 |
| 分析模型 | `model` | 模型配置 | 模型编号、模型名称、模型类型 |
| 数据集市 | `data_mart` | 集市表 | 表编号、表名称、主题域 |
| 数据导出 | `export` | 导出任务 | 任务编号、导出对象、导出格式 |
| 数据审计 | `audit` | 审计记录 | 记录编号、操作类型、操作人 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 审计时间迁移

审计记录的 `操作时间` 历史上是文本。`backend/app/services/audit_time_migration.py`
提供迁移流水线：在迁移环境构建 SQLite 兼容数据库（默认 `backend/data/audit_time_compat.db`，
可用 `AUDIT_TIME_COMPAT_DB` 覆盖），把旧文本换算成可按时区排序的新版记录，
同时保留原始文本与旧读取方式（`/api/audit` 及其导出不受影响）。

- 精度分级：带偏移量 → `absolute`（可换算为绝对时间）；纯日期 → `day`
  （可确定到日）；本地时间无时区 / 缺失 → 进迁移未决清单，绝不混入已确认的
  绝对时间序列。同一操作地迟到的午夜记录对采用**先切换可解析记录**策略，
  未决记录不阻塞同组已确认记录。
- 同一操作编号重复迁移幂等；已确认的换算证据不会被回填失败后的重试改写；
  换算前后的来源可通过 `source_hash` / `evidence_hash` 核实。
- 迁移按批次发布，某批解析失效可整批回滚；旧清单操作顺序（`source_seq`）
  始终保留，回滚后仍可核对。
- 接口：`POST /api/audit/time-migration/run`（整体迁移）、`POST .../entries`
  （单条录入）、`POST .../rollback`（回滚）、`GET .../index`（兼容索引）、
  `GET .../snapshot-view`（数据快照消费：日序列 + 迁移未决清单 + 合计）、
  `GET .../legacy-order`（旧清单顺序与旧材料日计数）、`GET .../batches`。

端到端验证（换算、重跑幂等、回滚前后的快照合计，纯标准库即可运行）：

```bash
python3 backend/scripts/verify_audit_time_migration.py
```
