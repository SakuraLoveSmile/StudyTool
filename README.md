# 学习助手 (StudyTool)

> 公网多人部署：见 [部署与备份说明](deploy/README.md) 和 [本次上线执行记录](RELEASE.md)。公网模式使用独立账号、每用户数据库及上传目录；下方快速启动仅用于本机开发，不可直接暴露公网。

联网版客户端：安卓 APK 源码见 [Android 客户端](clients/android/README.md)，Windows 安装包源码见 [Windows 联网客户端](clients/windows/README.md)。两端填写同一 HTTPS 服务地址；镜像离线部署包和 Actions 构建说明见 [服务端交付](deploy/README.md)。此版本与原 Windows 本地版独立安装、独立保存客户端配置。

个人在本机使用的专业课备考网页工具，将专业课教材、历年真题与答案整理成可溯源的学习资料，实现：
**导入资料 → 分析考点 → 压缩背诵内容 → 查看答题模板 → 练习新题 → 获得批改**。

---

## 一、 系统特性与设计准则

1. **单机架构与本地隐私**：
   - 服务默认仅监听 `127.0.0.1`。
   - 原文件在本地保存；OCR 与生成仅发送必要页面与文本到配置的模型接口，API Key 仅存于本地环境，不泄露给前端或日志。
2. **数据严肃性与输入可信**：
   - **双保险入库**：AI 识别的题目与答案必须经人工在“资料库/真题确认台”确认后，才进入统计池。
   - **真实年份分母**：近五年统计以库中已收录的最新真题年份为终点，分母只计入实际录入的年份，并在看板明确展示收录与缺失年份。
   - **案例杜绝虚构**：学习卡与解答严格仅使用上传资料；资料未收录企业案例时明确标注缺失，严禁模型自行编造。
3. **单机混合检索**：
   - **SQLite FTS5**（BM25 全文分词检索）+ **本地 Embedding 向量**（余弦相似度计算），通过 RRF (Reciprocal Rank Fusion) 算法合并重排，无需额外配置独立的外部向量数据库。
4. **练习与逐项语义批改**：
   - AI 模拟题明确标记为模拟试题，绝不混淆计入真题统计。
   - 出题时后台保存标准答案与评分要点细则（作答前严格对前端隐藏）。
   - 提交作答后逐项输出“已覆盖 / 部分覆盖 / 缺失”，指出概念错误与无依据臆造，给出高分规范改写建议。

---

## 二、 快速启动指南

### 1. 后端启动 (FastAPI)

```bash
cd backend

# 安装依赖 (Python 3.10+)
pip install -r requirements.txt

# 配置环境变量（支持 OpenAI / DeepSeek / 通义千问 / Ollama 等兼容接口）
cp .env.example .env
# 编辑 .env 填入 LLM_API_KEY、OCR 配置和 DEMO_MODE=false。
# 只有测试数据才可以显式使用 DEMO_MODE=true；正式导入不会把模拟文本当成资料。

# 如需演示数据，请先明确知道该命令会重置当前 DATA_DIR 中的演示表。
# python3 scripts/seed_demo_data.py --reset

# 启动后端服务 (监听 127.0.0.1:8000)
python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

后端 API 文档（Swagger UI）：http://127.0.0.1:8000/docs

### 2. 前端启动 (Vue 3 + Vite)

```bash
cd frontend

# 安装依赖
npm install

# 启动开发服务器 (监听 127.0.0.1:5174，自动反向代理 /api 到 8000)
npm run dev
```

在浏览器打开：http://127.0.0.1:5174

首次升级已有 `DATA_DIR` 时，服务启动会执行 schema v6 的增量学习表迁移。迁移前请先停止服务并备份整个 `DATA_DIR`（至少包括 `app.db` 及 SQLite 的 WAL/SHM 文件）；正式资料导入建议先用一章教材和少量真题确认 OCR/LLM 配置有效，再批量处理。迁移、备份与回滚注意事项见下文。

---

## 三、核心模块功能说明

0. **联网知识库 (`/library`)**：按章生成教材总结、思维导图、背诵短段与管理学关联，显示可追溯的已确认真题重点；支持限定资料问答、追问及服务端历史。Windows 与安卓可通过同一在线站点使用，并添加到桌面。配置、资料边界与验收说明见 [联网知识库使用说明](docs/knowledge-library.md)。

1. **资料库与真题确认台 (`/materials`)**：
   - 支持 PDF（文字版直接切分，扫描页按需 OCR 保留真实物理页码）、DOCX（保留章节及段落索引，不伪造页码）、图片（JPG/PNG）。
   - 题目与答案分离或同一文件均可解析，解析后提供人工修改、关联考点与一键确认。
2. **考点分析 (`/analysis`)**：
   - 全局考情看板：展示已收录年份、缺失年份、题目总数。
   - 频次排序：按考查年份数、近五年出现数、最近考查年份严格排序。
   - 考点优先级标记：“重点背 ★ / 普通 / 暂缓”。
   - 支持同义知识点一键合并（自动合并真题关联并重新计算统计）。
3. **背诵与答题 (`/cards`)**：
   - 四维学习卡：
     - **精简背诵版**：定义、口诀要点、关键词。
     - **答题结构**：概念 → 特征 → 适用条件 → 案例的考场规范骨架。
     - **展开答题版**：按教材规范组织的完整大题论述。
     - **易混点与真实案例**：边界辨析，无案例明确标记缺失。
   - 原文溯源：点击引用跳转查看原文切片，支持即时校对修改。
   - 问答辅助助手：针对历年真题或自拟题目生成 AI 规范作答、答题思路与依据。
4. **练习与批改 (`/practice`)**：
   - 选择考点、题量（1~5 题）、难度模式（基础/综合），生成模拟题。
   - 提交作答后，阅卷引擎基于预设要点清单进行逐项语义核对，给出错误警示与改写建议。
   - 保存历史练习记录，支持针对薄弱考点再次强化练习。
5. **学习与回忆 V1 (`/api/learning`)**：
   - 按章节管理回忆卡，支持卡片草稿、修订版本、人工审核和历史记录；卡片列表可按章节、状态和关键词筛选。卡片引用会校验来源切片、文档归属、内容摘录与内容哈希，避免无法核对的内容进入正常学习列表。
   - 支持按章节/知识点创建带不可变资料快照的 AI 生成批次。生成结果先保存为候选草稿；必须由人工审核（批准或拒绝）后，才会进入正常卡片列表。AI 输出不能替代人工校对。
   - 提供今日学习日流程：打开/读取队列、获取下一项、揭示答案、按 `forgotten` / `fuzzy` / `mastered` 评分、推进新卡轮次、暂停学习，以及显式 override 查看被暂停内容。请求带有学习日/内容版本校验，过期状态需要刷新。
   - 支持学习设置与学习范围（每日新卡/复习上限、章节和知识点筛选等）。学习服务以本地 SQLite 持久化记录学习日、尝试和进度。

### 学习与回忆 V1 API 概览

以下路径均以 `/api/learning` 为前缀。具体请求字段和响应以 Swagger/OpenAPI（`http://127.0.0.1:8000/docs`）为准：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET / PUT | `/settings` | 读取/更新学习设置 |
| PUT | `/scope` | 更新学习范围（章节/知识点） |
| GET / POST | `/chapters` | 列出/创建章节 |
| PATCH | `/chapters/{chapter_id}` | 更新章节 |
| POST / GET | `/generations`, `/generations/{generation_id}` | 创建/查询 AI 生成批次（集合 GET 返回批次列表） |
| GET / POST | `/cards` | 列出/创建回忆卡（`include_candidates=true` 可列出草稿与 AI 候选） |
| PATCH | `/cards/{card_id}` | 更新回忆卡 |
| POST | `/cards/{card_id}/publish` | 发布已审核修订（`reset_progress=true` 会完整重置该卡进度） |
| POST | `/cards/{card_id}/lifecycle` | 设置生命周期（`draft`/`published`/`paused`/`archived`/`rejected`） |
| POST | `/cards/{card_id}/revisions` | 创建卡片修订版本 |
| POST | `/revisions/review` | 批量审核修订版本（批准/拒绝） |
| GET | `/cards/{card_id}/history` | 查询卡片历史 |
| GET | `/today` | 读取指定或当前学习日摘要 |
| POST | `/today/open` | 打开/恢复学习日并构建队列 |
| POST | `/today/next` | 获取下一项学习内容（返回完整学习日摘要 + `next_item`） |
| POST | `/today/override` | 开关今日 override |
| POST | `/attempts/{attempt_id}/reveal` | 揭示当前尝试答案与依据 |
| POST | `/attempts/{attempt_id}/rate` | 提交回忆评分（`forgotten`/`fuzzy`/`mastered`） |
| POST | `/today/advance` | 完成当前轮次并推进或结束学习日 |
| POST | `/today/pause` | 暂停当前学习日 |

> 生成和回忆卡内容服务在安装不完整时会返回明确的 `501`；这不表示 AI 已成功生成内容。生成接口本身只创建批次/任务，模型调用、来源校验和人工审核仍须分别完成。

#### V1 固定行为（已由测试固定）

- **人工审核闸门**：无论手工创建还是 AI 生成，卡片都以 `draft`/未确认状态落库；只有经过 `/revisions/review` 批准并 `/cards/{card_id}/publish` 发布后才会进入今日队列。默认 `/cards` 列表不返回候选，管理页需显式开启「含待审核候选」。
- **活体学习日**：所有每日路由（`/today`、`/today/open|next|override|advance|pause`、`/attempts/{id}/reveal|rate`）默认以 `Asia/Shanghai` 当天为准；传入过期 `study_date` 会返回 `409 DAY_CHANGED`，不会改写历史学习日。
- **复习优先**：存在到期复习或未完成续接时，新学内容默认不进队列；只有显式打开今日 override 才允许加入。`daily_review_limit` 只限制普通到期复习，从不隐藏续接内容。
- **当天额外回忆不改计划**：`forgotten` 触发的当天追加尝试只记录事件，不推进间隔阶梯、不改动到期日。
- **暂停/归档/来源失效是资格状态**：卡片一旦暂停、归档或来源 `invalid`，即使学习单元此前已经生成，`reveal`/`rate` 也会返回 `CARD_PAUSED`/`CARD_ARCHIVED`/`SOURCE_INVALID`。
- **时间戳**：服务写库使用 UTC；所有 `*_at` 字段对外返回带 `+00:00` 的 ISO-8601 时间戳。
- **进度重置**：发布新修订且 `reset_progress=true` 时会清空间隔、次数、到期日与首次学习时间并写入 `reset_at`，历史事件与修订记录保留。

---

## 四、Schema v6、备份与回滚注意事项

- 当前数据库版本为 **schema v6**。学习/回忆表以增量方式创建，旧资料表和既有内容不会被自动改写成新的学习卡。
- 已有数据库从旧版本升级时，启动会在任何 schema 变更前使用 SQLite Online Backup API 创建快照；备份失败会阻止升级继续。新数据库没有旧文件可备份。
- 升级前仍应主动停止后端，并复制整个 `DATA_DIR` 到独立位置；若直接复制 SQLite 文件，需一并保留存在的 `app.db-wal` 和 `app.db-shm`。不要把运行中的单个 `app.db` 当作唯一备份。
- 回滚不是自动降级功能：停止服务，确认目标快照完整并保留当前目录副本后，再恢复快照并重启；启动后至少检查 `/health`、`/docs` 和关键数据。当前代码发现数据库版本高于其支持版本时会拒绝降级，不要在未验证备份的情况下覆盖唯一数据副本。

---

## 五、后端诊断日志与任务排查

系统内置了结构化诊断日志落盘与任务调用链路追踪基础设施：

- **日志存储路径**：默认落盘在 `DATA_DIR/logs/backend.jsonl`（可通过 `LOG_DIR` 自定义），每行一条标准 JSON。
- **轮转与空间上限**：单文件默认 10 MiB（`LOG_MAX_BYTES=10485760`），保留 5 个历史切片（`LOG_BACKUP_COUNT=5`），总磁盘空间占用约 60 MiB，防止无限膨胀。
- **脱敏安全保证**：自动对 `sk-...` 密钥、Bearer Token、Cookies、URL 查询参数、提示词正文、切片原文及用户文件名进行严格白名单过滤与脱敏。
- **进程模式说明**：Python 标准库 `RotatingFileHandler` 专为单进程设计。本项目为个人单机应用，默认运行单 Worker（`uvicorn ...`）。若未来扩展至多 Worker 并发，多进程并发轮转同一文件可能发生滚动竞争，需配置集中日志收集器。

### 常用排查命令 (基于 `grep` 与 `jq`)

当任务遇到异常或重试时，只需复制任务 ID（如 `task-12345678`），即可通过以下命令排查完整调用链路：

1. **查看指定任务的完整执行日志**：
   ```bash
   grep '"task_id":"task-12345678"' data/logs/backend.jsonl | jq .
   ```

2. **仅筛选该任务的异常与失败阶段**：
   ```bash
   grep '"task_id":"task-12345678"' data/logs/backend.jsonl | jq 'select(.level=="ERROR" or .event=="task_failed" or .event=="unit_failed")'
   ```

3. **追踪网络请求尝试、超时阶段与耗时**：
   ```bash
   grep '"task_id":"task-12345678"' data/logs/backend.jsonl | jq 'select(.event=="request_dispatched" or .event=="response_headers" or .event=="response_body" or .event=="retry_wait" or .event=="request_failed") | {event, attempt, phase, duration_ms, status_code, retry_delay, error}'
   ```

4. **实时追踪后端诊断流**：
   ```bash
   tail -f data/logs/backend.jsonl | jq -c '{time: .timestamp, event: .event, task: .task_id, unit: .unit_id, msg: .message}'
   ```

---

## 六、自动化测试与验收边界

项目包含后端单元/集成测试及前端构建检查：

```bash
# 后端测试（从仓库根目录执行）
cd backend
pytest -v

# 仅运行学习/回忆调度测试
pytest -v tests/test_learning_scheduler.py

# 前端类型检查与构建测试
cd ../frontend
npm run build
```

AI 生成的回忆卡、答案辅助和模拟题都必须经过人工审阅；来源引用校验只能降低风险，不能替代事实核对。上述命令是自动化检查，不代表已经完成浏览器验收。浏览器验收是独立步骤：启动后端和前端后，在 `http://127.0.0.1:5174` 手动检查导航、学习队列、揭示/评分、错误提示以及人工审核后的内容可见性。
