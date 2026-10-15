package com.studytool.connected;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.net.http.SslError;
import android.os.Build;
import android.os.Bundle;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.view.WindowInsets;
import android.webkit.CookieManager;
import android.webkit.SslErrorHandler;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebStorage;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.Set;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public final class MainActivity extends Activity {
    private static final int CHOOSE_FILE = 1201;
    private static final int SAVE_FILE = 1202;
    private static final int BACKGROUND = 0xfff5f4f0;
    private static final int INK = 0xff292524;
    private static final int ACCENT = 0xff6b705c;
    private SharedPreferences preferences;
    private volatile String origin;
    private String retryUrl;
    private WebView webView;
    private LinearLayout root;
    private FrameLayout browserFrame;
    private View errorView;
    private ProgressBar progress;
    private ValueCallback<Uri[]> fileCallback;
    private boolean clearingSession;
    private PendingDownload pendingDownload;
    private AuthenticatedDownload.Cancellation downloadCancellation;
    private AlertDialog downloadDialog;
    private final ExecutorService downloads = Executors.newSingleThreadExecutor();

    private static final class PendingDownload {
        final String origin;
        final String url;
        final String userAgent;
        PendingDownload(String origin, String url, String userAgent) {
            this.origin = origin;
            this.url = url;
            this.userAgent = userAgent;
        }
    }

    @Override public void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        preferences = getSharedPreferences("connected-service", MODE_PRIVATE);
        WebView.setWebContentsDebuggingEnabled(BuildConfig.DEBUG);
        if (Build.VERSION.SDK_INT >= 33) {
            getOnBackInvokedDispatcher().registerOnBackInvokedCallback(
                    android.window.OnBackInvokedDispatcher.PRIORITY_DEFAULT, this::handleBack);
        }
        if (preferences.getBoolean("clear_pending", false)) {
            clearSessionAndSettings();
            return;
        }
        String storedOrigin = preferences.getString("origin", null);
        if (storedOrigin != null) {
            try { origin = ServerOrigin.parse(storedOrigin); }
            catch (IllegalArgumentException ignored) { preferences.edit().remove("origin").apply(); }
        }
        if (origin == null) showSettings(); else showBrowser();
    }

    private int dp(int value) { return Math.round(value * getResources().getDisplayMetrics().density); }

    private void newRoot() {
        root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(BACKGROUND);
        root.setOnApplyWindowInsetsListener((view, insets) -> {
            if (Build.VERSION.SDK_INT >= 30) {
                android.graphics.Insets bars = insets.getInsets(WindowInsets.Type.systemBars());
                view.setPadding(bars.left, bars.top, bars.right, bars.bottom);
            } else {
                view.setPadding(insets.getSystemWindowInsetLeft(), insets.getSystemWindowInsetTop(),
                        insets.getSystemWindowInsetRight(), insets.getSystemWindowInsetBottom());
            }
            return insets;
        });
        setContentView(root);
        root.requestApplyInsets();
    }

    private TextView text(String value, int size) {
        TextView view = new TextView(this);
        view.setText(value);
        view.setTextSize(size);
        view.setTextColor(INK);
        view.setLineSpacing(dp(4), 1);
        return view;
    }

    private Button button(String label) {
        Button view = new Button(this);
        view.setText(label);
        view.setTextColor(Color.WHITE);
        view.setAllCaps(false);
        GradientDrawable background = new GradientDrawable();
        background.setColor(ACCENT);
        background.setCornerRadius(dp(10));
        view.setBackground(background);
        view.setPadding(dp(16), dp(10), dp(16), dp(10));
        view.setMinHeight(dp(48));
        return view;
    }

    private LinearLayout.LayoutParams spaced(int top) {
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(-1, -2);
        params.topMargin = dp(top);
        return params;
    }

    private void showSettings() {
        newRoot();
        ScrollView scroll = new ScrollView(this);
        scroll.setFillViewport(true);
        LinearLayout outer = new LinearLayout(this);
        outer.setOrientation(LinearLayout.VERTICAL);
        outer.setPadding(dp(24), dp(40), dp(24), dp(32));
        TextView brand = text("知学 · 联网版", 18);
        outer.addView(brand);

        LinearLayout card = new LinearLayout(this);
        card.setOrientation(LinearLayout.VERTICAL);
        card.setPadding(dp(24), dp(28), dp(24), dp(28));
        GradientDrawable background = new GradientDrawable();
        background.setColor(Color.WHITE);
        background.setCornerRadius(dp(16));
        card.setBackground(background);
        outer.addView(card, spaced(24));
        TextView title = text("连接学习服务", 26);
        title.setTypeface(null, Typeface.BOLD);
        card.addView(title);
        card.addView(text("安卓与 Windows 连接同一服务后，可共用知识库、学习资料与 AI 问答。", 15), spaced(16));
        card.addView(text("服务地址", 15), spaced(24));
        EditText address = new EditText(this);
        address.setSingleLine(true);
        address.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        address.setHint("https://learn.example.com");
        address.setTextColor(INK);
        address.setTextSize(16);
        address.setContentDescription("HTTPS 服务地址");
        card.addView(address, spaced(8));
        card.addView(text("目前尚未部署服务器。部署完成后，填写 HTTPS 域名即可连接；请勿填写账号、密钥或网页路径。", 14), spaced(16));
        Button connect = button("连接");
        card.addView(connect, spaced(24));
        connect.setOnClickListener(view -> {
            final String selected;
            try { selected = ServerOrigin.parse(address.getText().toString()); }
            catch (IllegalArgumentException e) { address.setError(e.getMessage()); return; }
            origin = selected;
            preferences.edit().putString("origin", origin).apply();
            showBrowser();
        });
        scroll.addView(outer);
        root.addView(scroll, new LinearLayout.LayoutParams(-1, -1));
    }

    // The service is a Vue application. JavaScript is required, while origin
    // restrictions and the absence of a native bridge retain the browser boundary.
    @android.annotation.SuppressLint("SetJavaScriptEnabled")
    private void showBrowser() {
        newRoot();
        LinearLayout toolbar = new LinearLayout(this);
        toolbar.setGravity(Gravity.CENTER_VERTICAL);
        toolbar.setPadding(dp(16), dp(6), dp(12), dp(6));
        TextView title = text("知学 · 联网版", 17);
        title.setTypeface(null, Typeface.BOLD);
        toolbar.addView(title, new LinearLayout.LayoutParams(0, -2, 1));
        Button settings = new Button(this);
        settings.setText("服务设置");
        settings.setTextColor(ACCENT);
        settings.setContentDescription("切换服务器并清除本机登录状态");
        toolbar.addView(settings);
        settings.setOnClickListener(view -> confirmSwitch());
        root.addView(toolbar, new LinearLayout.LayoutParams(-1, -2));
        progress = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        progress.setMax(100);
        root.addView(progress, new LinearLayout.LayoutParams(-1, dp(3)));
        browserFrame = new FrameLayout(this);
        root.addView(browserFrame, new LinearLayout.LayoutParams(-1, 0, 1));
        webView = new WebView(this);
        webView.setBackgroundColor(BACKGROUND);
        WebSettings settingsApi = webView.getSettings();
        settingsApi.setJavaScriptEnabled(true);
        settingsApi.setDomStorageEnabled(true);
        settingsApi.setAllowFileAccess(false);
        settingsApi.setAllowContentAccess(false);
        settingsApi.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        settingsApi.setGeolocationEnabled(false);
        settingsApi.setJavaScriptCanOpenWindowsAutomatically(false);
        settingsApi.setSupportMultipleWindows(false);
        settingsApi.setSafeBrowsingEnabled(true);
        CookieManager.getInstance().setAcceptCookie(true);
        CookieManager.getInstance().setAcceptThirdPartyCookies(webView, false);
        webView.setDownloadListener(this::chooseDownloadLocation);
        webView.setWebChromeClient(new WebChromeClient() {
            @Override public void onProgressChanged(WebView view, int value) {
                progress.setProgress(value);
                progress.setVisibility(value < 100 ? View.VISIBLE : View.INVISIBLE);
            }

            @Override public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback,
                                                       FileChooserParams params) {
                cancelFileChooser();
                fileCallback = callback;
                Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT);
                intent.addCategory(Intent.CATEGORY_OPENABLE);
                String[] mimeTypes = chooserMimeTypes(params.getAcceptTypes());
                intent.setType(mimeTypes.length == 1 ? mimeTypes[0] : "*/*");
                if (mimeTypes.length > 1) intent.putExtra(Intent.EXTRA_MIME_TYPES, mimeTypes);
                intent.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, params.getMode() == FileChooserParams.MODE_OPEN_MULTIPLE);
                intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
                try { startActivityForResult(Intent.createChooser(intent, "选择教材或资料"), CHOOSE_FILE); }
                catch (ActivityNotFoundException e) {
                    cancelFileChooser();
                    Toast.makeText(MainActivity.this, "未找到文件选择器，请安装系统文件管理器。", Toast.LENGTH_LONG).show();
                }
                return true;
            }
        });
        webView.setWebViewClient(new WebViewClient() {
            @Override public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                String target = request.getUrl().toString();
                if (ServerOrigin.contains(origin, target)) return false;
                if (request.isForMainFrame() && ServerOrigin.isExternalWebLink(target)) openExternal(target);
                return true;
            }

            @Override public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
                String scheme = request.getUrl().getScheme();
                if (!"https".equalsIgnoreCase(scheme)
                        || (request.isForMainFrame() && !ServerOrigin.contains(origin, request.getUrl().toString()))) {
                    return new WebResourceResponse("text/plain", "UTF-8", 403, "Blocked", null,
                            new ByteArrayInputStream(new byte[0]));
                }
                return null;
            }

            @Override public void onPageStarted(WebView view, String url, android.graphics.Bitmap icon) {
                if (!ServerOrigin.contains(origin, url)) { view.stopLoading(); return; }
                retryUrl = url;
                hideError();
            }

            @Override public void onPageFinished(WebView view, String url) {
                CookieManager.getInstance().flush();
            }

            @Override public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request.isForMainFrame()) showError("无法连接学习服务", "请检查网络、服务地址和服务器状态后重试。");
            }

            @Override public void onReceivedHttpError(WebView view, WebResourceRequest request, WebResourceResponse response) {
                if (request.isForMainFrame()) showError("服务暂时无法打开", "服务器返回 HTTP " + response.getStatusCode() + "，请稍后重试。");
            }

            @Override public void onReceivedSslError(WebView view, SslErrorHandler handler, SslError error) {
                handler.cancel();
                showError("无法验证服务证书", "请确认服务使用有效 HTTPS 证书，并检查设备时间后重试。");
            }
        });
        browserFrame.addView(webView, new FrameLayout.LayoutParams(-1, -1));
        retryUrl = origin + "/library";
        webView.loadUrl(retryUrl);
    }

    private void openExternal(String url) {
        try {
            Intent intent = new Intent(Intent.ACTION_VIEW, Uri.parse(url));
            intent.addCategory(Intent.CATEGORY_BROWSABLE);
            startActivity(intent);
        } catch (ActivityNotFoundException e) {
            Toast.makeText(this, "未找到可打开链接的浏览器。", Toast.LENGTH_SHORT).show();
        }
    }

    private void showError(String title, String detail) {
        if (browserFrame == null || isFinishing() || clearingSession) return;
        hideError();
        LinearLayout panel = new LinearLayout(this);
        panel.setOrientation(LinearLayout.VERTICAL);
        panel.setGravity(Gravity.CENTER);
        panel.setPadding(dp(28), dp(28), dp(28), dp(28));
        panel.setBackgroundColor(BACKGROUND);
        panel.addView(text(title, 22));
        panel.addView(text(detail, 15), spaced(16));
        Button retry = button("重试");
        panel.addView(retry, spaced(24));
        retry.setOnClickListener(view -> {
            hideError();
            if (webView != null) webView.loadUrl(ServerOrigin.contains(origin, retryUrl) ? retryUrl : origin + "/library");
        });
        errorView = panel;
        browserFrame.addView(panel, new FrameLayout.LayoutParams(-1, -1));
        progress.setVisibility(View.INVISIBLE);
    }

    private void hideError() {
        if (errorView != null && browserFrame != null) browserFrame.removeView(errorView);
        errorView = null;
    }

    private void confirmSwitch() {
        new AlertDialog.Builder(this)
                .setTitle("切换学习服务")
                .setMessage("当前地址：" + origin + "\n\n切换将清除本机登录状态和网页缓存。服务器上的教材和学习资料会保留，连接后需要重新登录。")
                .setNegativeButton("取消", null)
                .setPositiveButton("清除并切换", (dialog, which) -> clearSessionAndSettings())
                .show();
    }

    private void clearSessionAndSettings() {
        if (clearingSession) return;
        clearingSession = true;
        preferences.edit().putBoolean("clear_pending", true).remove("origin").apply();
        cancelDownload();
        cancelFileChooser();
        // Block and destroy the old page before asynchronous cookie removal, so it
        // cannot recreate a session during the server switch.
        destroyWebView(true);
        WebStorage.getInstance().deleteAllData();
        origin = null;
        newRoot();
        TextView clearing = text("正在清除本机登录状态…", 18);
        clearing.setPadding(dp(24), dp(40), dp(24), dp(24));
        root.addView(clearing);
        CookieManager.getInstance().removeAllCookies(removed -> {
            CookieManager.getInstance().flush();
            preferences.edit().remove("clear_pending").apply();
            clearingSession = false;
            if (!isFinishing() && !isDestroyed()) showSettings();
        });
    }

    static String[] chooserMimeTypes(String[] accepts) {
        Set<String> types = new LinkedHashSet<>();
        if (accepts != null) for (String accept : accepts) {
            if (accept == null) continue;
            for (String item : accept.split(",")) {
                switch (item.trim().toLowerCase(java.util.Locale.ROOT)) {
                    case ".pdf": types.add("application/pdf"); break;
                    case ".docx": types.add("application/vnd.openxmlformats-officedocument.wordprocessingml.document"); break;
                    case ".jpg": case ".jpeg": types.add("image/jpeg"); break;
                    case ".png": types.add("image/png"); break;
                    default:
                        if (item.trim().matches("[A-Za-z0-9!#$&^_.+*-]+/[A-Za-z0-9!#$&^_.+*-]+")) types.add(item.trim());
                }
            }
        }
        return types.isEmpty() ? new String[]{"*/*"} : types.toArray(new String[0]);
    }

    @Override protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == SAVE_FILE) {
            PendingDownload download = pendingDownload;
            pendingDownload = null;
            if (resultCode == RESULT_OK && data != null && data.getData() != null && download != null
                    && download.origin.equals(origin) && "content".equalsIgnoreCase(data.getData().getScheme())) {
                saveDownload(download, data.getData());
            }
            return;
        }
        if (requestCode != CHOOSE_FILE || fileCallback == null) return;
        Uri[] result = WebChromeClient.FileChooserParams.parseResult(resultCode, data);
        ArrayList<Uri> safeUris = new ArrayList<>();
        if (result != null) for (Uri uri : result) {
            if (uri != null && "content".equalsIgnoreCase(uri.getScheme())) safeUris.add(uri);
        }
        ValueCallback<Uri[]> callback = fileCallback;
        fileCallback = null;
        callback.onReceiveValue(safeUris.isEmpty() ? null : safeUris.toArray(new Uri[0]));
    }

    private void chooseDownloadLocation(String url, String userAgent, String disposition, String mime, long length) {
        if (!ServerOrigin.contains(origin, url)) {
            Toast.makeText(this, "仅支持当前学习服务的文件下载。", Toast.LENGTH_LONG).show();
            return;
        }
        if (pendingDownload != null || downloadCancellation != null) {
            Toast.makeText(this, "请先完成或取消当前下载。", Toast.LENGTH_SHORT).show();
            return;
        }
        String type = DownloadPolicy.mimeType(mime);
        Intent intent = new Intent(Intent.ACTION_CREATE_DOCUMENT);
        intent.addCategory(Intent.CATEGORY_OPENABLE);
        intent.setType(type);
        intent.putExtra(Intent.EXTRA_TITLE, DownloadPolicy.filename(disposition, type));
        intent.addFlags(Intent.FLAG_GRANT_WRITE_URI_PERMISSION);
        pendingDownload = new PendingDownload(origin, url, userAgent);
        try { startActivityForResult(intent, SAVE_FILE); }
        catch (ActivityNotFoundException e) {
            pendingDownload = null;
            Toast.makeText(this, "未找到系统文件保存器。", Toast.LENGTH_LONG).show();
        }
    }

    private void saveDownload(PendingDownload download, Uri destination) {
        AuthenticatedDownload.Cancellation cancellation = new AuthenticatedDownload.Cancellation();
        downloadCancellation = cancellation;
        downloadDialog = new AlertDialog.Builder(this)
                .setTitle("正在保存教材文件")
                .setMessage("下载完成前请保持此页面。取消或失败时，保存位置可能保留未完成的文件。")
                .setCancelable(false)
                .setNegativeButton("取消下载", (dialog, which) -> cancellation.cancel())
                .show();
        downloads.execute(() -> {
            String result = "教材文件已保存。";
            try (OutputStream output = getContentResolver().openOutputStream(destination, "w")) {
                if (output == null) throw new IOException("无法写入所选保存位置。");
                AuthenticatedDownload.save(download.origin, download.url, download.userAgent,
                        url -> CookieManager.getInstance().getCookie(url), output, cancellation,
                        () -> download.origin.equals(origin));
            } catch (IOException | SecurityException e) {
                if (cancellation.cancelled) result = "下载已取消，保存位置可能保留未完成的文件。";
                else if (e instanceof java.net.SocketTimeoutException) result = "下载超时，请检查网络后重试。保存位置可能保留未完成的文件。";
                else result = "下载失败，请检查网络、登录状态或保存位置后重试。保存位置可能保留未完成的文件。";
                if (e instanceof IOException && e.getMessage() != null
                        && (e.getMessage().startsWith("登录已") || e.getMessage().startsWith("已阻止")
                        || e.getMessage().startsWith("文件下载") || e.getMessage().startsWith("文件未完整")
                        || e.getMessage().startsWith("服务返回"))) result = e.getMessage() + " 保存位置可能保留未完成的文件。";
            }
            String message = result;
            runOnUiThread(() -> {
                if (downloadCancellation == cancellation) downloadCancellation = null;
                if (downloadDialog != null) { downloadDialog.dismiss(); downloadDialog = null; }
                if (!isFinishing() && !isDestroyed()) Toast.makeText(this, message, Toast.LENGTH_LONG).show();
            });
        });
    }

    private void cancelDownload() {
        pendingDownload = null;
        if (downloadCancellation != null) downloadCancellation.cancel();
        if (downloadDialog != null) { downloadDialog.dismiss(); downloadDialog = null; }
    }

    private void cancelFileChooser() {
        if (fileCallback != null) { fileCallback.onReceiveValue(null); fileCallback = null; }
    }

    private void destroyWebView(boolean clearData) {
        if (webView == null) return;
        webView.stopLoading();
        webView.setWebChromeClient(null);
        webView.setWebViewClient(new WebViewClient());
        if (clearData) { webView.clearCache(true); webView.clearHistory(); webView.clearFormData(); }
        if (browserFrame != null) browserFrame.removeView(webView);
        webView.destroy();
        webView = null;
        browserFrame = null;
        errorView = null;
    }

    @Override public void onBackPressed() {
        handleBack();
    }

    private void handleBack() {
        if (clearingSession) return;
        if (webView != null && webView.canGoBack()) { hideError(); webView.goBack(); }
        else finish();
    }

    @Override protected void onPause() {
        if (webView != null) webView.onPause();
        CookieManager.getInstance().flush();
        super.onPause();
    }

    @Override protected void onResume() {
        super.onResume();
        if (webView != null) webView.onResume();
    }

    @Override protected void onDestroy() {
        cancelFileChooser();
        cancelDownload();
        downloads.shutdownNow();
        destroyWebView(false);
        super.onDestroy();
    }
}
