# 安卓联网客户端

独立 Android WebView 客户端，连接已部署的学习服务。首次打开填写 HTTPS origin，例如 `https://learn.example.com`；连接后打开“今天”学习页，可使用服务端的登录、教材上传和问答。原生连接页采用暖纸与墨绿的学习手账风格；平板居中阅读，手机单列操作。

## 构建

需要 JDK 17、Android SDK Platform 35 / Build Tools 35.0.0、网络（首次下载 Gradle 和构建依赖）。项目不依赖第三方 Android UI 或网络库。设置 `ANDROID_HOME` 指向 SDK；只在当前命令设置 `JAVA_HOME`，不需要修改系统 Java 配置。

```sh
cd clients/android
./gradlew --no-daemon testDebugUnitTest lintDebug assembleDebug
```

调试 APK：`app/build/outputs/apk/debug/app-debug.apk`，应用 ID 为 `com.studytool.connected.debug`。只用于体验；调试证书不是正式发布证书，开启 WebView 调试。

Gradle 8.14 分发包 SHA256 已固定在 wrapper 配置中。已核对官方 wrapper JAR SHA256：`7d3a4ac4de1c32b59bc6a4eb8ecb8e612ccd0cf1ae1e99f66902da64df296172`。CI 可使用 Gradle 官方 wrapper validation。

## 正式签名

正式版应用 ID 为 `com.studytool.connected`。保管同一签名证书才能持续覆盖升级；调试版和正式版可并存。通过环境变量提供签名，仓库不保存证书或密码：

- `ANDROID_KEYSTORE_PATH`：密钥库绝对路径
- `ANDROID_KEYSTORE_PASSWORD`
- `ANDROID_KEY_ALIAS`
- `ANDROID_KEY_PASSWORD`

```sh
./gradlew --no-daemon testReleaseUnitTest lintRelease assembleRelease
```

四项全部提供时输出签名版 `app/build/outputs/apk/release/app-release.apk`；都未提供时仅输出 `app-release-unsigned.apk`，不能直接安装。部分提供会明确失败。调试 APK、未签名 release 构建和正式签名发布应分别记录。

## 学习界面与旋转

- 客户端 UA 追加 `StudyToolAndroid/0.1.14`（调试版含 `-debug`），用于启用安卓学习界面，不改变认证或接口权限。
- 连接后的原生服务栏高44dp，保留“服务”按钮以切换地址；系统栏和键盘区域保留安全间距。
- 横竖屏及窗口尺寸变化保留同一个 WebView，不重载当前页面、不重放学习请求；网页会按可用宽度重新布局。应用被系统终止后，重新打开仍从“今天”进入。

## 权限与边界

- 最低 Android 8.0（API 26），目标 API 35；仅请求 INTERNET，不申请存储、相机或麦克风权限。
- 文件上传通过系统文档选择器授权单次读取，支持网页声明的 PDF、DOCX、JPG、PNG 类型及多选。取消选择返回网页，不保留文件权限。
- 「查看教材原文件」遇到附件下载时，调用系统保存位置选择器；后台使用当前登录 Cookie 下载同源文件，每次跳转都校验 HTTPS origin，拒绝跨服务跳转。下载可取消，失败时给出提示；取消或失败可能在选定位置留下未完成文件。不会通过系统浏览器下载私有教材，不申请额外存储权限。
- 仅允许 HTTPS origin（可含端口），不能含账号、路径、查询参数或片段。主框架保持同源，其他 HTTP(S) 链接交给系统浏览器；拒绝危险 scheme。网页没有原生 JavaScript bridge、本地文件访问或证书绕过。
- 「服务设置」确认切换后销毁旧网页，清除 Cookies、WebStorage、表单与网页缓存，再输入新地址。服务器文件不受影响；切换后重新登录。系统浏览器中的会话不由本应用清除。
- 无自动更新与离线服务端。服务器需要有效 HTTPS 证书；Android 系统 WebView 建议保持更新。正式发布关闭 WebView 调试。
- 不与原 Windows 本地版共享应用 ID、数据目录或密钥。

## 需要设备验收的流程

构建和单元测试不能代替设备验证。部署 HTTPS 服务后，在设备检查：首次地址校验、登录/重启会话、PDF/DOCX 选择上传、取消/再次选择、多选、教材原文件保存与取消/失败、章节问答、返回、网络失败重试、外链系统打开、证书失败阻断、切换服务器后旧会话清除。当前源代码不预置真实服务地址或凭据。
