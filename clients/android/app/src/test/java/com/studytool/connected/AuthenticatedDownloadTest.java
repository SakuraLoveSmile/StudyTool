package com.studytool.connected;

import org.junit.Test;
import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import static org.junit.Assert.*;

public class AuthenticatedDownloadTest {
    private static final String ORIGIN = "https://learn.example.com";

    private static final class Connection extends HttpURLConnection {
        final int status;
        final String location;
        final byte[] body;
        boolean disconnected;
        long declaredLength;
        Connection(int status, String location, String body) throws Exception {
            super(new URL(ORIGIN + "/file"));
            this.status = status;
            this.location = location;
            this.body = body.getBytes(StandardCharsets.UTF_8);
            declaredLength = this.body.length;
        }
        @Override public int getResponseCode() { return status; }
        @Override public String getHeaderField(String name) { return "Location".equalsIgnoreCase(name) ? location : null; }
        @Override public String getContentType() { return "application/pdf"; }
        @Override public long getContentLengthLong() { return declaredLength; }
        @Override public InputStream getInputStream() { return new ByteArrayInputStream(body); }
        @Override public void disconnect() { disconnected = true; }
        @Override public boolean usingProxy() { return false; }
        @Override public void connect() {}
    }

    @Test public void downloadsAuthenticatedAttachmentAcrossSameOriginRedirect() throws Exception {
        Connection redirect = new Connection(302, "/original.pdf", "");
        Connection file = new Connection(200, null, "pdf contents");
        List<String> targets = new ArrayList<>();
        List<String> cookieTargets = new ArrayList<>();
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        AuthenticatedDownload.save(ORIGIN, ORIGIN + "/file", "webview", target -> {
            cookieTargets.add(target); return "session=private";
        }, output, new AuthenticatedDownload.Cancellation(), () -> true, target -> {
            targets.add(target); return targets.size() == 1 ? redirect : file;
        });
        assertEquals(List.of(ORIGIN + "/file", ORIGIN + "/original.pdf"), targets);
        assertEquals(targets, cookieTargets);
        assertEquals("session=private", file.getRequestProperty("Cookie"));
        assertFalse(redirect.getInstanceFollowRedirects());
        assertFalse(file.getInstanceFollowRedirects());
        assertEquals("pdf contents", output.toString("UTF-8"));
        assertTrue(redirect.disconnected && file.disconnected);
    }

    @Test public void crossOriginRedirectNeverCreatesSecondConnectionOrSendsCookiesThere() throws Exception {
        for (String location : new String[]{"https://evil.example/file", "http://learn.example.com/file",
                "https://learn.example.com:8443/file", "https://user@learn.example.com/file", "file:///etc/passwd"}) {
            Connection redirect = new Connection(302, location, "");
            List<String> connections = new ArrayList<>();
            List<String> cookies = new ArrayList<>();
            IOException error = assertThrows(IOException.class, () -> AuthenticatedDownload.save(ORIGIN,
                    ORIGIN + "/file", "webview", target -> { cookies.add(target); return "session=private"; },
                    new ByteArrayOutputStream(), new AuthenticatedDownload.Cancellation(), () -> true,
                    target -> { connections.add(target); return redirect; }));
            assertTrue(error.getMessage().contains("跨服务"));
            assertEquals(List.of(ORIGIN + "/file"), connections);
            assertEquals(connections, cookies);
            assertTrue(redirect.disconnected);
        }
    }

    @Test public void cancellationOrServiceSwitchPreventsNetworkRequest() {
        AuthenticatedDownload.Cancellation cancellation = new AuthenticatedDownload.Cancellation();
        cancellation.cancel();
        for (boolean active : new boolean[]{true, false}) {
            assertThrows(java.io.InterruptedIOException.class, () -> AuthenticatedDownload.save(ORIGIN,
                    ORIGIN + "/file", null, target -> { fail("No cookie read"); return null; },
                    new ByteArrayOutputStream(), cancellation, () -> active,
                    target -> { fail("No network request"); return null; }));
        }
        assertThrows(java.io.InterruptedIOException.class, () -> AuthenticatedDownload.save(ORIGIN,
                ORIGIN + "/file", null, target -> null, new ByteArrayOutputStream(),
                new AuthenticatedDownload.Cancellation(), () -> false, target -> { fail("Service changed"); return null; }));
    }

    @Test public void reportsExpiredLoginAndTruncatedFiles() throws Exception {
        Connection unauthorized = new Connection(401, null, "");
        IOException login = assertThrows(IOException.class, () -> AuthenticatedDownload.save(ORIGIN,
                ORIGIN + "/file", null, target -> null, new ByteArrayOutputStream(),
                new AuthenticatedDownload.Cancellation(), () -> true, target -> unauthorized));
        assertTrue(login.getMessage().contains("登录已失效"));
        Connection incomplete = new Connection(200, null, "partial");
        incomplete.declaredLength = 100;
        IOException truncated = assertThrows(IOException.class, () -> AuthenticatedDownload.save(ORIGIN,
                ORIGIN + "/file", null, target -> null, new ByteArrayOutputStream(),
                new AuthenticatedDownload.Cancellation(), () -> true, target -> incomplete));
        assertTrue(truncated.getMessage().contains("未完整"));
    }

    @Test public void preservesChineseAttachmentNamesAndSanitizesSuggestedPaths() {
        assertEquals("会计学概论.pdf", DownloadPolicy.filename("attachment; filename*=utf-8''%E4%BC%9A%E8%AE%A1%E5%AD%A6%E6%A6%82%E8%AE%BA.pdf", "application/pdf"));
        assertEquals("a+b.pdf", DownloadPolicy.filename("attachment; filename*=UTF-8''a+b.pdf", "application/pdf"));
        assertEquals("_.._file.pdf", DownloadPolicy.filename("attachment; filename=\"../../file.pdf\"", "application/pdf"));
        assertEquals("教材资料.docx", DownloadPolicy.filename(null, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"));
        assertEquals("application/octet-stream", DownloadPolicy.mimeType("invalid\r\nheader"));
    }
}
