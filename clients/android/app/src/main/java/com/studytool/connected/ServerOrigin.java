package com.studytool.connected;

import java.net.URI;
import java.net.URISyntaxException;
import java.util.Locale;

/** Exact HTTPS origin boundary shared by setup and navigation. */
final class ServerOrigin {
    private ServerOrigin() {}

    static String parse(String value) {
        if (value == null || !value.equals(value.trim()) || value.isEmpty()) {
            throw new IllegalArgumentException("请输入完整 HTTPS 服务地址，不含空格。");
        }
        try {
            URI uri = new URI(value);
            String host = uri.getHost();
            String path = uri.getRawPath();
            int port = uri.getPort();
            if (!"https".equalsIgnoreCase(uri.getScheme()) || host == null || host.isEmpty()
                    || uri.getRawUserInfo() != null || uri.getRawQuery() != null
                    || uri.getRawFragment() != null || (path != null && !path.isEmpty() && !"/".equals(path))
                    || port == 0 || port > 65535 || uri.getRawAuthority().endsWith(":")) {
                throw new IllegalArgumentException("请仅填写 HTTPS 域名和可选端口，不含账号、路径、参数或片段。");
            }
            return new URI("https", null, host.toLowerCase(Locale.ROOT), port == 443 ? -1 : port,
                    null, null, null).toASCIIString();
        } catch (URISyntaxException e) {
            throw new IllegalArgumentException("服务地址格式不正确，请填写例如 https://learn.example.com。");
        }
    }

    static boolean contains(String origin, String target) {
        if (origin == null || target == null) return false;
        try {
            URI uri = new URI(target);
            if (uri.getRawUserInfo() != null || uri.getHost() == null) return false;
            String candidate = new URI(uri.getScheme(), null, uri.getHost(), uri.getPort(),
                    null, null, null).toString();
            return origin.equals(parse(candidate));
        } catch (IllegalArgumentException | URISyntaxException e) {
            return false;
        }
    }

    static boolean isExternalWebLink(String target) {
        try {
            URI uri = new URI(target);
            return ("https".equalsIgnoreCase(uri.getScheme()) || "http".equalsIgnoreCase(uri.getScheme()))
                    && uri.getHost() != null && uri.getRawUserInfo() == null;
        } catch (URISyntaxException | NullPointerException e) {
            return false;
        }
    }
}
