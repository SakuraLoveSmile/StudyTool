# 联网知识库实施与验收

状态：实现中。已授权本地开发、连接测试、APK + Windows 联网安装包和 Docker 部署镜像交付，以及推送当前分支、运行并修复 Actions；未授权正式 Release、镜像注册表发布或生产部署。用户暂无服务器/域名，客户端支持填写 HTTPS 服务地址。

## 用户效果与任务

- T1：导入教材后按正文顺序查看章节，原文可溯源；DOCX 使用段落，不伪造页码。目录和附录不得混入正文章节。
- T2：按章生成可保存的总结、概念、结构导图、背诵段落、管理学关联；真题已考必须由已确认且非模拟真题的实际关联支持。关联建议不能冒充管理学真题证据。
- T3：限定资料和章节提问，支持追问和服务端历史；未找到依据时明确资料不足。所有新增 API 继承现有账号、CSRF 和租户隔离。
- T4：沿用现有暖灰风格，新增响应式知识库页面；通过同一 HTTPS 站点在 Windows 和安卓联网使用，保留网页/PWA入口。缓存不得保存私有 API 或账号资料。
- T5：交付原生 Java/WebView 安卓 APK 和独立 Electron Windows 联网安装包；首次设置 HTTPS origin，登录/上传在服务站点内完成，支持重试、切换服务并清理旧会话；不影响旧 Windows 本地版。
- T6：导出不含凭据和用户资料的 Docker 前后端镜像包，含 Compose、开户/备份说明及校验和；Actions 构建 APK、Windows 包和 Linux x64 镜像并保存 artifact，不自动发布或部署。

## 唯一共享接口

前缀 `/api/library`。错误沿项目使用 HTTP `detail`，前端展示可读信息。真实生成使用现有服务端 LLM 设置；未配置应报错，演示不得标为真实生成。POST 复用 `Idempotency-Key`，防止重复点击或网络重试产生重复结果。

引用 Citation：`chunk_id, doc_id, doc_name, location, quote`，quote 必须为真实切片原文子串。内容生成携带引用；引用之外的关联建议须标明，不能捏造页码、年份或题目。生成中资料变动应报冲突，历史结果返回 `source_stale`，删除来源后不继续显示为有效依据。

- `GET /chapters?doc_id=...` → Chapter[]：`id, doc_id, title, chunk_count, character_count`。chapter id 稳定且只在所属文档解释。
- `GET /guides?doc_id=...&chapter_id=...` → Guide[]（可省略筛选）。
- `GET /guides/{guide_id}` → Guide；`GET /answers/{answer_id}` → Answer，均重新核验来源状态。
- `POST /guides` 请求：`doc_id, chapter_id, management_doc_ids: string[], exam_doc_ids: string[]` → Guide。
- Guide：`id, doc_id, chapter_id, chapter_title, summary, concepts: {name, explanation, recitation, keywords: string[], citations: Citation[]}[], mind_map: {title, children: {title, children: {title}[]}[]} , connections: {accounting_concept, management_concept, explanation, application, evidence_level: 'suggestion'|'textbook_supported', citations: Citation[]}[], exam_points: {concept, question_id, year: number|null, stem, doc_name, location}[], citations: Citation[], warnings: string[], created_at, source_stale: boolean`。
- 真题关联由服务端读取已确认题目及考点链接计算，不采信模型输出的真题年份/频次；无确认关联时显示“暂无已确认真题关联”。management_doc_ids 为空时所有管理学关联强制 suggestion 并明确提示教材未提供；指定资料必须存在且已解析。
- 真题类型接受 `exam`、`exam_and_answer`；确认资格沿现有 `is_confirmed` 正式池语义，不能另用旧 `needs_review` 标记排除已人工确认的真题。仍须非模拟且题干在当前来源切片中可核实。
- `POST /ask` 请求：`question, doc_ids: string[], chapter_id?: string|null, previous_answer_id?: string|null` → Answer。
- 指定 `chapter_id` 时 `doc_ids` 恰好一份；多资料问答不带章节。来源刷新时同步当前回答，刷新失败则停用引用和追问，不能只刷新列表继续展示旧有效状态。
- Answer：`id, question, answer, citations: Citation[], warnings: string[], doc_ids: string[], chapter_id: string|null, previous_answer_id: string|null, created_at, source_stale: boolean`。
- `GET /answers` → Answer[]，历史限定当前租户；支持 `doc_id` 可选筛选。

章节生成读取完整章节，在上下文超过边界时显式报错，不能静默只读前几段而声称全章完成。检索按选定文档/章节先限制再排序，不得检索到范围外资料；追问仅引用所选范围，历史只作对话语境，不能作为新的教材依据。

## 分工与文件所有权

主 agent：共享契约、路由与导航接入、DOCX 分章修正、真实资料隔离导入、最终浏览器联合核验和文档。共享文件 frontend/src/api/client.ts、router/index.ts、components/Navbar.vue、backend/app/main.py 只由主 agent 写。

后端 worker（Sol high，自行完成）：独占 backend/app/models/library.py、services/library.py、routers/library.py、database.py 及 tests/test_library.py。参考现有 db_session、LLMClient、request_operations、access/tenancy；实现 T1 查询及 T2/T3 校验持久化。验收资料范围、错误、引用校验、真实真题关联与旧库增量兼容，不调用付费服务。

前端 worker（Sol high，自行完成）：独占 frontend/src/views/KnowledgeLibraryView.vue、components/KnowledgeMindMap.vue、types/library.ts、api/library.ts。api/library.ts 以主 agent 导出的 axios `api` 实例调用，保留账号/session/CSRF 行为。继承 AnalysisView 的 page-stack/surface 与 MaterialsView 的表单；顶部选教材、章节，下方总结/导图/背诵/关联四标签，问答独立区域，历史可重开，引用可查看原文。手机单列，桌面双列；不得改全局 token 或添加组件库。验收真实 API 状态、空/加载/错误/过期状态和窄屏溢出。

联网入口 worker（Sol high，自行完成）：独占 frontend/index.html、public/manifest.webmanifest、public/app-icon.svg、public/sw.js、src/composables/useInstallApp.ts、src/components/InstallAppButton.vue、src/main.ts；T4 提供在线安装入口，不缓存用户数据/API，不改变部署或桌面后端。主 agent 在导航接入安装按钮。说明 HTTPS 及实际安卓/Windows 安装待真机验证。

只读资料 analyst（Sol high）：真实 DOCX 章节结构和少量 PDF 内页科目核查，/tmp 可写，不改业务源码，不付费 OCR。将分章证据交主 agent。前后端依冻结接口并行；主 agent 完成接入后才开展联合验证。

## T5/T6 本轮并行边界

共享约定：服务地址只接受 HTTPS origin，可带端口；客户端只嵌入同源页面，外链交系统浏览器，拒绝危险 scheme，不绕过证书，不向远程页面注入文件/进程权限或 desktopAPI。切换地址前清理旧服务会话和缓存；服务端数据仍留原站点。新界面为暖灰背景 #f5f4f0、白卡片、正文 #292524、强调 #6b705c，单列“连接学习服务”表单，不换网站组件风格。APK 候选 debug 签名、Windows 未签名状态必须明确，不冒充正式签名发布。

- Android worker（Sol high）：独占 clients/android/**，实现 Java WebView、URL 校验、原生文件选择、错误/重试/切换和 Gradle wrapper；交付可安装 APK，验证构建/URL测试。无服务或设备证据时不能称双端端到端通过。
- Windows worker（Sol high）：独占 clients/windows/**，实现独立配置和 session、受限本地设置 IPC、远程无 preload、NSIS 哈希补丁及安装包脚本；不修改 desktop/**。交付安装包或具体平台构建阻塞。
- Knowledge worker（Sol high）：独占 services/library.py 与 test_library.py；真题证据进入生成输入，实际采用的历史逐项验证快照，过期409且无模型调用。保持 API 形状兼容，独立于客户端工作。
- 主 agent：独占 deploy/build_bundle.py、smoke_images.py、connected-builds.yml、.dockerignore 与共享文档；镜像实际构建、临时隔离网络登录/CSRF/上传/租户检查，最终原生或浏览器界面核验和证据集成。
- Reviewer（Sol high）：只读独立审查实际客户端、安全和构建契约；无文件写入权，不扩大实现。

## 验证与边界

在独立临时 DATA_DIR、DEMO_MODE=false、禁用真实远程凭据的进程中导入用户 DOCX；PDF 仅保留真实文件，未完成 OCR 不伪装已解析。隔离 API 测试用明确替身覆盖生成、坏引用、越界资料、真题关联、并发重复和租户边界；替身结果只用于测试。主 agent 用真实浏览器检查桌面和 390px 手机布局、真实教材章节/来源、生成失败提示与问答空态；真实模型、全部 PDF OCR、安卓/Windows 真机安装及公网部署分别报告，未执行不写通过。

## 本次执行记录

- T1 待体验：真实教材导入隔离目录后为 21 章，首章 65 个切片、32368 字符；原文件下载 SHA-256 与输入一致。保留表格、内容控件、文本框，去重兼容分支；目录、重复页眉和附录不进入正文章节。图片/公式内容仍需原文件核对。
- T2 实现中：生成、导图、背诵、管理学关联、服务端历史和真实真题重点已写代码；本地模型协议替身完成页面操作验证，尚未调用真实模型验证内容质量。没有生成用户教材的全书总结。
- T3 实现中：限定范围问答、连续追问、持久化与引用验证已写代码；真实浏览器实测本地协议替身的问答/追问/原文弹窗，以及来源校对后刷新当前回答与章节失效。未调用真实问答模型。
- T4 实现中：manifest、192/512 PNG 图标、安装入口及 network-only service worker 已实现；浏览器在 390、1280、1536 宽度核验布局并修复导航挤压，静态资源 HTTP 200。尚未公网部署或进行安卓/Windows 真机安装。

最终受影响自动检查：`tests/test_library.py`、`test_docx_chapters.py`、`test_parser.py`、`test_schema_migration_drill.py`、`test_request_operations.py`、`test_public_isolation.py` 合计 64 passed；前端 `npm run build` 与 `git diff --check` 通过。自动检查不代表真实模型或设备验收。

独立审查发现并修复：带页码目录吞掉正文、Word 兼容分支重复读取、真题确认状态与旧 needs_review 标记不一致、刷新后当前回答引用状态未同步。后两项分别通过实际创建/校对/确认 API 回归及真实浏览器操作验证。

本地预览为 `http://127.0.0.1:18760/library`，仅本机访问，使用 artifacts 中的真实教材副本。网页设置采用服务端统一配置；真实 LLM/Embedding 连接测试曾成功，硅基流动 PaddleOCR-VL-1.5 的 OCR 连接仍失败，用户已暂停网络排查。不得输出凭据或把当前配置打入镜像。资料、验收产物和截图不提交。

下一依赖：尚无服务器和 HTTPS 域名，不能完成公网登录验收；没有全书真实生成、全卷 OCR。已提供 PDF 实为南京大学 920 会计学真题，扫描状态仍 pending；完整管理学教材与独立管理学真题未收到，相关内容只能标学习建议。

本轮新增检查与交付结果在完成后追加，不能用历史检查替代。

## 2026-10-15 双端交付补齐

- T2/T3：修复确认真题仅事后展示、未参与生成的问题；确认题干/考点现在进入模型输入，未经确认或失效题目仍排除。正文与真题证据共用80000字符上限，超限413。追问采用的每条历史都核验来源快照，过期409且不调用模型。新增缺口先以19项失败复现，修复后知识库与DOCX相关测试62 passed，使用临时数据库/模型替身。未生成全书真实内容。
- T5 实现中：Java/WebView APK实际构建成功，debug和release各11项测试通过，两种lint均无问题；只有debug APK可直接安装，release未配置正式签名。Windows独立联网壳使用Electron44.7.2，已实现配置IPC隔离、无远程preload、单实例、下载/返回入口和NSIS脚本；打包及设备状态见后续记录。主agent在macOS实际打开新Electron运行时，验证首次设置、HTTP地址拒绝及重复启动退出并聚焦；不代表Windows安装验收。安卓设备未操作。
- T6 实现中：Linux amd64前后端镜像已实际构建并导出（压缩镜像175752517字节），均非root。隔离Docker网络使用仅探针信任的临时CA完成HTTPS登录、改密、CSRF拒绝、DOCX上传解析、知识库章节及双账号隔离；后台重启后账号/教材/章节仍保留。向量调用使用本地协议替身，网络禁止外访，无付费模型调用；没有公网证书或真实AI质量结论。镜像未嵌入凭据/数据库，交付文件SHA256逐项校验通过。
- Actions源码已加入Android、Windows、server三项构建，Windows附安装/启动/退出/卸载冒烟，server附容器检查；只上传artifact，不发布Release/GHCR。尚未推送、尚未远程运行，不能把本地结果记为远程CI通过。
- 审查发现的Windows多进程配置竞争、安卓附件下载缺失均已修复并复核；上传仍保留content资源限制，文件选择器授权路径经官方Chromium源码核对，不为未经证实的问题放宽权限。

阻塞只影响相应范围：暂无HTTPS服务器/域名；无安卓或Windows设备流程验收；OCR故障按用户要求暂停；管理学教材与独立管理学试卷缺失。真实模型内容质量、全书生成及全卷OCR仍未验证，不标已验收。

最终本地交付：`artifacts/connected-delivery/` 中提供调试APK、Windows x64 NSIS安装器（114198654字节，未签名）和Linux amd64服务端tar包，附SHA256、说明、Docker检查及Windows打包记录。Windows实际pack退出0，ASAR内9个源码文件与当前src逐字节一致；本地18项测试通过。Windows安装/启动PowerShell冒烟已接Actions，但当前macOS没有执行条件，仍记未运行。新安装器不包含本机后端、教材或密钥。

## 本次 Actions 执行记录

用户允许推送和运行 Actions 后，提交 `dad5632` 已推送到 `codex/connected-knowledge-library`。首轮 `38758843874`：server 通过，Android 在 SDK 安装阶段因 setup-android 默认请求已下架的 `tools` 包失败；Windows 测试和安装包构建通过，但冒烟脚本的清理异常遮住原始错误。

修正 SDK 包清单并补齐 Windows 阶段日志后，提交 `df857bb` 的第二轮 `38760798954`：Android 和 server 均通过。下载 APK 的 SHA-256 与 CI 一致且 APK v2 签名验证成功；服务端包全部校验和通过，BUILD.json 记录该提交、干净工作区与 linux/amd64。SMOKE.json 确认隔离 HTTPS 账号/CSRF/上传/章节/租户及重启持久化检查通过；模型仍使用明确的本地协议替身。

Windows 第二轮安装退出 0、文件和卸载登记存在，但首窗检查 120 秒超时；进程仍存在且普通启动日志为空。当前没有 Windows 首窗通过证据，不能称双端验收完成。后续仅针对该新证据补充诊断和修复，保留安装/正常退出/卸载门禁。

第三轮 `38762562817` 补充原生窗口和启动证据后，stderr 明确显示 smoke profile 父目录校验拒绝；原生错误框存在，首次设置尚未显示。安装与卸载均成功，清理误报已解决。提交 `9e2cfd1` 改为规范实际父目录后比较，保留全新子目录、名称和边界限制；真实目录别名、已有目录、穿越及链接回归通过，本地 Windows 客户端 21 项测试与源码检查通过。具体 Windows 临时路径差异由下一轮 CI 的 `smoke-paths` 记录核实。

最终运行：[38764036705](https://github.com/SakuraLoveSmile/StudyTool/actions/runs/38764036705)，构建提交 `9e2cfd1cd7dee9b2a8df168b5f6ab037673cbe16`，Android、Windows、server 全部成功。

- T5 待体验：Actions 已交付调试签名 APK、未签名 Windows x64 安装包。Windows 2022 runner 实际安装成功，3.343 秒内检测到“连接学习服务 · StudyTool Connected”窗口，正常退出码 0、无残留进程，卸载码 0、程序和卸载登记均移除。此检查未连接实际服务，也不代表用户设备全流程验收。安卓 APK 下载后 SHA-256 与 CI 一致，APK v2 签名验证通过，安卓设备流程仍未执行。
- Windows 根因已由最终 `smoke-paths` 实录确认：.NET 返回 `C:\Users\runneradmin\AppData\Local\Temp`，Node 返回 `C:\Users\RUNNER~1\AppData\Local\Temp`；两者规范化后相同。原字符串比较误拒绝，修复后通过。
- T6 待体验：最终 Linux amd64 镜像包已下载，所有文件校验和通过；BUILD.json 指向上述提交且工作区干净。真实隔离容器检查和重启持久化均通过，远程模型未调用。三个产品产物和 Windows 冒烟证据保存在该次 Actions artifacts，未发布正式 Release、推送注册表或部署。

本轮构建与交付已完成；整体仍为实现中：生产服务器/HTTPS 未提供，用户设备流程与真实 AI 内容质量未验证，OCR 排查暂停、扫描卷未确认、管理学资料未齐，全书真实内容尚未生成。用户尚未确认体验结果，不标已验收。

## 2026-10-16 预览版发布

用户明确授权“构建完成了就发 release”后，已发布 [联网版 0.1.0 预览版](https://github.com/SakuraLoveSmile/StudyTool-Releases/releases/tag/connected-v0.1.0)。发布仓库沿用公开下载仓库 `SakuraLoveSmile/StudyTool-Releases`，源码仓库的 `connected-v0.1.0` 标签指向实际构建提交 `9e2cfd1cd7dee9b2a8df168b5f6ab037673cbe16`；下载仓库自身标签指向其 main，源码依据以 manifest 为准。

发布包含原 CI 的调试签名 APK、未签名 Windows x64 安装包、封装同次 CI 文件的 Linux amd64 Docker 包，以及 README、RELEASE_MANIFEST.json、SHA256SUMS.txt，共六个附件。服务端封装前后逐项核对 13 个 CI 原文件；六个远端附件的大小与 GitHub SHA-256 均匹配本地，公开 README 匿名下载后哈希一致。该 Release 为已公开的 prerelease，既有 latest 仍为 `desktop-v1.0.8-beta.1`。

T5/T6 维持待体验；发布不代表生产部署或用户设备验收，真实模型、资料和 OCR 等剩余范围沿上节记录。

## 2026-10-16 GHCR 与直接拉取 Compose

T6 待体验：用户授权补齐 GHCR 发布和自动拉取部署文件后，已新增发布 workflow、带 OCI 来源标签的构建、纯 image Compose、配置模板及中文部署说明。生产数据继续使用原专用 bind 目录和 UID 10001，证书卷与账号契约保留。镜像平台为 linux/amd64；Release Compose 与配置模板固定镜像摘要。

- 镜像发布源：`7d63e24897335d68508ad28091379a07e519b384`，源码标签 `connected-server-v0.1.0`。原 `connected-v0.1.0` 标签和 APK/EXE/离线包未修改。[发布运行 38771412994](https://github.com/SakuraLoveSmile/StudyTool/actions/runs/38771412994) 成功，先隔离检查后推送 GHCR，两个包均已公开。
- Backend：`ghcr.io/sakuralovesmile/studytool-connected-backend:0.1.0`，digest `sha256:d5cba37d256f81b3306c98d05800c09de4e2d4e9567e0fbad2f1d8495b9b7bb5`。
- Frontend：`ghcr.io/sakuralovesmile/studytool-connected-frontend:0.1.0`，digest `sha256:a814d3213c57b8dd7a9c1cde8b88d60895b0990efdf5475141a1ea37e9883965`。
- [验证运行 38772738407](https://github.com/SakuraLoveSmile/StudyTool/actions/runs/38772738407) 在 Ubuntu 上成功退出；验证脚本源为 `6e7b5dc`。无认证 Docker config 对生产 Compose 的两镜像执行真实匿名 pull，再使用独立 Compose project、内部网络、临时命名数据卷和探针专用 CA，完成 HTTPS/账号/改密/CSRF/合成 DOCX 上传解析/章节/双账号隔离及重启后持久化检查。模型是本地协议替身，没有真实模型调用。测试用命名卷替换生产 bind，仅用于隔离数据与 Linux 清理，不代表生产挂载权限已现场验收。
- 首轮 Ubuntu `38772516441` 的匿名拉取及功能探针通过，但临时 bind 中 UID 10001 文件导致宿主清理失败；改用专属临时命名卷后最终整项成功。本机 Docker 的匿名拉取返回 denied，而宿主匿名注册表读取与 Ubuntu Docker 均通过；本机 Docker 网络原因未进一步归因，不能把本机 pull 写为通过。
- 现有公开预览 Release 新增 `compose.yaml`、`ghcr.env.example`、`GHCR.md`、`GHCR_MANIFEST.json`、`GHCR_SMOKE.json`、`GHCR_SHA256SUMS.txt`；部署不再依赖离线导入或源码构建。填写服务器域名与模型参数，准备数据目录，执行 compose pull/up，再创建账号。

本轮仅完成镜像、部署入口及隔离验证；尚未部署用户生产服务器，真实 AI/OCR、全书内容、资料缺口与用户设备验收维持前述状态，不标已验收。

## 2026-10-16 单应用与按版本发布修订

T6 实现中：按用户现有 Nginx 和版本更新要求，将推荐部署改为一个无 Caddy 的应用容器，网页与 API 同端口；镜像改为 `ghcr.io/sakuralovesmile/studytool:<版本>`，默认 Compose 与环境模板使用 `:latest`，`APP_IMAGE` 可固定版本。摘要仅保留在构建证据与精确回滚中。新增公开 Release 发布步骤，复用 `RELEASE_REPO_TOKEN` 向 `StudyTool-Releases` 的 `connected-v<版本>` 上传部署附件；先隔离验证版本与匿名拉取，再推广 latest，避免未验证构建覆盖默认入口。

无 Caddy 应用的既有本地验证：51 项静态授权/账号/隔离/桌面 OCR 回归通过；本地镜像在临时 Nginx HTTPS 后完成账号、CSRF、合成 DOCX、章节、隔离与重启持久化检查，真实浏览器可显示登录页面。此处未重新运行后端检查，业务实现未变化。本轮新增的默认/latest 和固定版本 Compose 渲染、Python 语法、workflow job 依赖检查通过；发布编排以隔离替身核对旧客户端正文保留、旧部署改名和校验修正、失败验证阻止发布。远端新标准镜像与自动 Release 流程尚待 Actions 实际完成，之后补记结果。

旧版双镜像的 Compose 将保留为 `compose.caddy.yaml`，其部署说明与校验同步改名，避免新单应用 `compose.yaml` 覆盖后破坏旧证据；原 APK、Windows、离线包与其构建来源保持。未部署用户服务器，真实模型与设备验收状态不变。
