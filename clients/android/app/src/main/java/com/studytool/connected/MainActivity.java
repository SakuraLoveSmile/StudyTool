package com.studytool.connected;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.res.Configuration;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.net.http.SslError;
import android.os.Build;
import android.os.Bundle;
import android.text.InputType;
import android.text.TextUtils;
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
    private static final int BACKGROUND = 0xfff6f3ea;
    private static final int PAPER = 0xfffffdf7;
    private static final int INK = 0xff25392f;
    private static final int ACCENT = 0xff285c4b;
    private static final int SUBTLE = 0xff647268;
    private static final int LINE = 0xffdce2d7;
    private SharedPreferences preferences;
    private volatile String origin;
    private String retryUrl;
    private WebView webView;
    private LinearLayout root;
    private FrameLayout browserFrame;
    private View errorView;
    private ProgressBar progress;
    private LinearLayout connectionPanel;
    private LinearLayout connectionOuter;
    private TextView tabletIntroduction;
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
                android.graphics.Insets bars = insets.getInsets(
                        WindowInsets.Type.systemBars() | WindowInsets.Type.ime());
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

    private GradientDrawable rounded(int color, int radius, boolean border) {
        GradientDrawable background = new GradientDrawable();
        background.setColor(color);
        background.setCornerRadius(dp(radius));
        if (border) background.setStroke(dp(1), LINE);
        return background;
    }

    private void showSettings() {
        newRoot();
        ScrollView scroll = new ScrollView(this);
        scroll.setFillViewport(true);
        connectionOuter = new LinearLayout(this);
        connectionOuter.setOrientation(LinearLayout.VERTICAL);
        connectionOuter.setGravity(Gravity.CENTER_HORIZONTAL);
        connectionPanel = new LinearLayout(this);
        connectionPanel.setOrientation(LinearLayout.VERTICAL);
        connectionOuter.addView(connectionPanel);
        connectionOuter.addOnLayoutChangeListener((view, left, top, right, bottom,
                                                  oldLeft, oldTop, oldRight, oldBottom) -> {
            int available = right - left - view.getPaddingLeft() - view.getPaddingRight();
            int contentWidth = Math.min(dp(620), Math.max(0, available));
            if (connectionPanel != null && connectionPanel.getLayoutParams().width != contentWidth) {
                connectionPanel.setLayoutParams(new LinearLayout.LayoutParams(contentWidth, -2));
            }
        });
        TextView brand = text("知学  /  学习手账", 15);
        brand.setTextColor(ACCENT);
        brand.setTypeface(null, Typeface.BOLD);
        connectionPanel.addView(brand);
        TextView heading = text("让每一次学习，\n都有迹可循。", 30);
        heading.setTypeface(null, Typeface.BOLD);
        connectionPanel.addView(heading, spaced(20));
        connectionPanel.addView(text("读懂教材 · 联系真题 · 主动回忆", 15), spaced(12));
        tabletIntroduction = text("在平板上，一边读教材，一边问本章；\n在手机上，随时复习，把知识记扎实。", 16);
        tabletIntroduction.setTextColor(SUBTLE);
        connectionPanel.addView(tabletIntroduction, spaced(18));
        LinearLayout card = new LinearLayout(this);
        card.setOrientation(LinearLayout.VERTICAL);
        card.setPadding(dp(24), dp(24), dp(24), dp(24));
        card.setBackground(rounded(PAPER, 20, true));
        connectionPanel.addView(card, spaced(28));
        TextView title = text("连接你的学习空间", 23);
        title.setTypeface(null, Typeface.BOLD);
        card.addView(title);
        TextView detail = text("使用与 Windows 相同的服务地址，登录后继续学习已有教材与回忆卡。", 15);
        detail.setTextColor(SUBTLE);
        card.addView(detail, spaced(12));
        TextView addressLabel = text("服务地址", 15);
        card.addView(addressLabel, spaced(24));
        EditText address = new EditText(this);
        address.setId(View.generateViewId());
        addressLabel.setLabelFor(address.getId());
        address.setSingleLine(true);
        address.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        address.setHint("https://learn.example.com");
        address.setTextColor(INK);
        address.setHintTextColor(SUBTLE);
        address.setTextSize(16);
        address.setMinHeight(dp(52));
        address.setPadding(dp(14), dp(10), dp(14), dp(10));
        address.setBackground(rounded(BACKGROUND, 12, true));
        address.setContentDescription("HTTPS 服务地址");
        card.addView(address, spaced(8));
        TextView hint = text("填写 HTTPS 域名即可，不含网页路径、账号或密钥。", 13);
        hint.setTextColor(SUBTLE);
        card.addView(hint, spaced(12));
        Button connect = button("进入学习空间");
        card.addView(connect, spaced(24));
        connect.setOnClickListener(view -> {
            final String selected;
            try { selected = ServerOrigin.parse(address.getText().toString()); }
            catch (IllegalArgumentException e) { address.setError(e.getMessage()); return; }
            origin = selected;
            preferences.edit().putString("origin", origin).apply();
            connectionPanel = null;
            connectionOuter = null;
            tabletIntroduction = null;
            showBrowser();
        });
        TextView footer = text("教材和学习记录保存在你的服务端，换设备也能接着学。", 13);
        footer.setTextColor(SUBTLE);
        connectionPanel.addView(footer, spaced(20));
        scroll.addView(connectionOuter);
        root.addView(scroll, new LinearLayout.LayoutParams(-1, -1));
        updateConnectionLayout();
    }

    private void updateConnectionLayout() {
        if (connectionPanel == null) return;
        int width = getResources().getConfiguration().screenWidthDp;
        boolean tablet = width >= 840;
        int padding = tablet ? 40 : 24;
        connectionOuter.setPadding(dp(padding), dp(tablet ? 56 : 28), dp(padding), dp(28));
        connectionPanel.setLayoutParams(new LinearLayout.LayoutParams(
                dp(Math.min(620, Math.max(0, width - padding * 2))), -2));
        tabletIntroduction.setVisibility(tablet ? View.VISIBLE : View.GONE);
    }

    @Override public void onConfigurationChanged(Configuration configuration) {
        super.onConfigurationChanged(configuration);
        // Preserve the live Vue state. Loading its URL on rotation could discard
        // an answer or replay a learning request.
        updateConnectionLayout();
        if (root != null) {
            root.requestApplyInsets();
            root.requestLayout();
        }
    }

    // The service is a Vue application. JavaScript is required, while origin
    // restrictions and the absence of a native bridge retain the browser boundary.
    @android.annotation.SuppressLint("SetJavaScriptEnabled")
    private void showBrowser() {
        newRoot();
        LinearLayout toolbar = new LinearLayout(this);
        toolbar.setGravity(Gravity.CENTER_VERTICAL);
        toolbar.setPadding(dp(12), 0, dp(8), 0);
        TextView title = text("知学 · 学习手账", 14);
        title.setSingleLine(true);
        title.setEllipsize(TextUtils.TruncateAt.END);
        title.setTypeface(null, Typeface.BOLD);
        toolbar.addView(title, new LinearLayout.LayoutParams(0, -2, 1));
        Button settings = new Button(this);
        settings.setText("服务");
        settings.setTextSize(13);
        settings.setAllCaps(false);
        settings.setMinHeight(dp(44));
        settings.setMinimumHeight(dp(44));
        settings.setMinWidth(dp(64));
        settings.setMinimumWidth(dp(64));
        settings.setPadding(dp(12), 0, dp(12), 0);
        settings.setBackground(rounded(PAPER, 12, true));
        settings.setTextColor(ACCENT);
        settings.setContentDescription("切换服务器并清除本机登录状态");
        toolbar.addView(settings, new LinearLayout.LayoutParams(-2, dp(44)));
        settings.setOnClickListener(view -> confirmSwitch());
        root.addView(toolbar, new LinearLayout.LayoutParams(-1, dp(44)));
        progress = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        progress.setMax(100);
        root.addView(progress, new LinearLayout.LayoutParams(-1, dp(3)));
        browserFrame = new FrameLayout(this);
        root.addView(browserFrame, new LinearLayout.LayoutParams(-1, 0, 1));
        webView = new WebView(this);
        webView.setBackgroundColor(BACKGROUND);
        WebSettings settingsApi = webView.getSettings();
        settingsApi.setUserAgentString(settingsApi.getUserAgentString() + " StudyToolAndroid/" + BuildConfig.VERSION_NAME);
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
        retryUrl = origin + "/today";
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
            if (webView != null) webView.loadUrl(ServerOrigin.contains(origin, retryUrl) ? retryUrl : origin + "/today");
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
