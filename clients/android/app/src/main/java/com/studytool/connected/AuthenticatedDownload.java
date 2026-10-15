package com.studytool.connected;

import java.io.IOException;
import java.io.InputStream;
import java.io.InterruptedIOException;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.function.BooleanSupplier;

/** Downloads only to a caller-authorized stream, without automatic redirects. */
final class AuthenticatedDownload {
    interface ConnectionFactory { HttpURLConnection open(String url) throws IOException; }
    interface CookieSource { String get(String url); }

    static final class Cancellation {
        volatile boolean cancelled;
        volatile HttpURLConnection connection;
        void cancel() {
            cancelled = true;
            HttpURLConnection active = connection;
            if (active != null) active.disconnect();
        }
    }

    private AuthenticatedDownload() {}

    static void save(String origin, String url, String userAgent, CookieSource cookies,
                     OutputStream output, Cancellation cancellation, BooleanSupplier serviceActive) throws IOException {
        save(origin, url, userAgent, cookies, output, cancellation, serviceActive,
                target -> (HttpURLConnection) new URL(target).openConnection());
    }

    static void save(String origin, String url, String userAgent, CookieSource cookies,
                     OutputStream output, Cancellation cancellation, BooleanSupplier serviceActive,
                     ConnectionFactory factory) throws IOException {
        String target = url;
        for (int redirects = 0; redirects <= 5; redirects++) {
            checkActive(cancellation, serviceActive);
            if (!ServerOrigin.contains(origin, target)) throw new IOException("已阻止跨服务文件下载。");
            HttpURLConnection connection = factory.open(target);
            cancellation.connection = connection;
            try {
                connection.setInstanceFollowRedirects(false);
                connection.setConnectTimeout(15000);
                connection.setReadTimeout(30000);
                connection.setRequestMethod("GET");
                if (userAgent != null && !userAgent.isEmpty()) connection.setRequestProperty("User-Agent", userAgent);
                String cookie = cookies.get(target);
                if (cookie != null && !cookie.isEmpty()) connection.setRequestProperty("Cookie", cookie);
                checkActive(cancellation, serviceActive);
                int status = connection.getResponseCode();
                if (status == 301 || status == 302 || status == 303 || status == 307 || status == 308) {
                    try { target = DownloadPolicy.redirectTarget(origin, target, connection.getHeaderField("Location")); }
                    catch (IllegalArgumentException e) { throw new IOException(e.getMessage()); }
                    continue;
                }
                if (status == 401 || status == 403) throw new IOException("登录已失效或无权下载，请重新登录后重试。");
                if (status < 200 || status >= 300 || status == 204) throw new IOException("文件下载失败（HTTP " + status + "）。");
                // A login redirect can be same-origin, but is not the requested attachment.
                String type = DownloadPolicy.mimeType(connection.getContentType());
                if ("text/html".equalsIgnoreCase(type)) throw new IOException("服务返回了网页，请重新登录后重试下载。");
                long expected = connection.getContentLengthLong();
                long copied = 0;
                try (InputStream input = connection.getInputStream()) {
                    byte[] buffer = new byte[65536];
                    int read;
                    while ((read = input.read(buffer)) != -1) {
                        checkActive(cancellation, serviceActive);
                        output.write(buffer, 0, read);
                        copied += read;
                    }
                }
                checkActive(cancellation, serviceActive);
                if (expected >= 0 && copied != expected) throw new IOException("文件未完整下载，请重试。");
                output.flush();
                return;
            } finally {
                connection.disconnect();
                cancellation.connection = null;
            }
        }
        throw new IOException("文件下载跳转次数过多。");
    }

    private static void checkActive(Cancellation cancellation, BooleanSupplier serviceActive) throws IOException {
        if (cancellation.cancelled || !serviceActive.getAsBoolean() || Thread.currentThread().isInterrupted()) {
            throw new InterruptedIOException("下载已取消。");
        }
    }
}
