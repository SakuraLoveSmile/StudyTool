# RAG 反馈接入 e2e（Playwright）

独立依赖树。**本目录的任何内容都不会进入 RAG 前端依赖**：不写
`frontend/package.json`、`frontend/package-lock.json`、`frontend/vendor/`。

## 运行

```bash
cd e2e
npm ci
npx playwright test --reporter=list
```

## 模式与选择理由

两种页面形态 = **同一份 RAG 前端源码**、两个 Vite dev server，用 `webServer[].env` 注入
不同的构建期变量（Vite 会把进程环境里 `VITE_` 前缀的变量暴露为 `import.meta.env`）：

| project | 端口 | 构建期变量 | 用途 |
|---|---|---|---|
| `unconfigured` | 5274 | 都不设置 | 验证未配置时无反馈入口、组件包不被加载（F1） |
| `configured` | 5275 | `VITE_FEEDBACK_API_BASE=http://127.0.0.1:5199`、`VITE_FEEDBACK_APP_ID=rag` | 宿主集成级用例 |
| `component` | 5276（fixture 静态服务器） | 直接加载 UMD 产物 | 组件级用例（运行期切换 api-base） |

选这个方案而不是「各自构建 dist」，是为了验证**真实构建期变量路径**本身，
同时**无需改动 `frontend/vite.config.ts` 或任何产品文件**。

## 版本固定（防静默退化）

`@feedback/web` 通过**只读引用**已核验产物 tarball 锁定：

```
"@feedback/web": "file:../frontend/vendor/feedback-web-0.2.0.tgz"
```

该 tarball 的 SHA-256 必须等于 `.feedback-work/vendor-verified/SHA256SUMS` 中登记的
`578eb1db7fb7ef9814f3852cf603acca6718a107f5b192a90a5bdbbe3a216c0d`；
`specs/component-level.spec.ts` 会在运行时断言实装版本为 `0.2.0` 且 sha256 一致，
不一致即 fail。

**不要**把 fixture 指向 `frontend/node_modules/@feedback/web`：该路径随前端安装状态漂移，
可能静默测到 0.1.0。

`@playwright/test` 固定 `1.63.0`：其 `playwright-core/browsers.json` 的 chromium
revision 正是 **1243**，与全局缓存 `~/Library/Caches/ms-playwright/chromium-1243` 匹配，
因此不触发浏览器下载。

## 覆盖矩阵

| 用例 | 文件 | 验的是什么 |
|---|---|---|
| 未配置无入口 + 组件包未被请求 | `specs/unconfigured.spec.ts` | F1：两个变量都空 → 不挂载、不动态导入 |
| 未配置下学习页面行为不变 | `specs/unconfigured.spec.ts` | 接入前行为一致的回归 |
| 挂载门禁与公开属性 | `specs/host-integration.spec.ts` | 会话就绪才挂载；`apiBase`/`appId`/`pageLabel` 取构建期配置 |
| 登录页不挂载 | 同上 | 不加载组件界面、无入口 |
| 路由保留草稿 | 同上 | 路由切换后草稿文字与截图字节仍在、实例不重建、`page-label` 更新 |
| 组件内独立登录 | 同上 | 只打反馈服务；`clientLabel=web:rag`；宿主 `/api/auth/login` 零调用 |
| 退出清理 | 同上 | **移除前** 已置空 `api-base`、清空令牌与草稿（顺序 + 后果双证据） |
| 迟到响应不进入新会话 | 同上 | 世代切换后放行旧响应：新实例零继承、无事件、无重放 |
| 未保存保留草稿 | 同上 | 5xx 后草稿保留；重试复用**同一幂等键与同一截图字节** |
| 归档失败不重复提交 | 同上 | `failed` 只走只读 `GET`，绝不再次 `POST`；刷新后可达 `archived` |
| 组件加载失败 | `specs/component-level.spec.ts` | 学习页面不受影响、无反馈入口、诊断脱敏 |
| 同一实例上的身份世代 | 同上 | 运行期换 `api-base`：旧令牌/草稿清空、迟到响应被丢弃 |
| 清洗身份的归一化陷阱 | 同上 | `api-base` 属性必须真的被移除（不是哨兵值残留） |
| 依赖来源固定 | 同上 | 实装 0.2.0 + vendor tarball sha256 与核验清单一致 |

## 关键实现说明

- **全部网络用 `page.route` 拦截**，不依赖真实 Feedback 服务与真实 backend；
  宿主 `/api/*` 注入固定用户与 `csrf_token`；反馈服务跨 origin，响应自带 CORS 头。
- **迟到响应**用「先拿住请求 → 切换身份 → 再放行响应」实现，断言的是旧结果被丢弃，
  不是「恰好没返回」。组件级用例走同一实例运行期换 `api-base`；
  宿主级用例走换用户/会话世代重建实例。
- **退出顺序**用 `Element.prototype.remove` 探针在「真正移除之前」同步读取实例状态：
  内置 custom element reaction 不经过原型上的 `attributeChangedCallback` 属性查找，
  覆盖原型方法观察不到属性回调，所以改用移除点探针。
- **组件实例身份状态**（`accessToken` / `draft` / `identityEpoch` …）是 TypeScript `private`，
  但运行时就是实例自有属性（`Object.getOwnPropertyNames` 实测可见），据此断言比只查 DOM 更直接。

## 不在本次范围

- 三视口（390/768/1440）人工浏览器巡检与合成敏感标记的像素级遮挡验证，
  由队长现场验收阶段进行。
- `frontend/src/components/Navbar.vue` 的 `data-feedback-capture-mask` 像素级效果
  （实际截图产物中敏感区被覆盖）不在本套件断言范围内。
