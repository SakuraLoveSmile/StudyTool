package com.studytool.connected;

import java.net.URI;
import java.net.URLDecoder;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

final class DownloadPolicy {
    private DownloadPolicy() {}

    static String redirectTarget(String origin, String current, String location) {
        if (location == null || location.isEmpty()) throw new IllegalArgumentException("下载跳转地址无效。");
        String next = URI.create(current).resolve(location).toString();
        if (!ServerOrigin.contains(origin, next)) throw new IllegalArgumentException("已阻止跨服务下载跳转，请联系服务管理员。");
        return next;
    }

    static String mimeType(String value) {
        if (value == null) return "application/octet-stream";
        String type = value.split(";", 2)[0].trim();
        return type.matches("[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+") ? type : "application/octet-stream";
    }

    static String filename(String disposition, String mimeType) {
        String name = null;
        if (disposition != null) {
            Matcher encoded = Pattern.compile("(?:^|;)\\s*filename\\*\\s*=\\s*([^;]+)", Pattern.CASE_INSENSITIVE).matcher(disposition);
            if (encoded.find()) {
                String value = encoded.group(1).trim().replaceAll("^\"|\"$", "");
                String[] parts = value.split("'", 3);
                if (parts.length == 3 && "utf-8".equalsIgnoreCase(parts[0])) {
                    try { name = URLDecoder.decode(parts[2].replace("+", "%2B"), "UTF-8"); }
                    catch (IllegalArgumentException | java.io.UnsupportedEncodingException ignored) { /* Fall back to plain filename. */ }
                }
            }
            if (name == null) {
                Matcher plain = Pattern.compile("(?:^|;)\\s*filename\\s*=\\s*(?:\"([^\"]*)\"|([^;]*))", Pattern.CASE_INSENSITIVE).matcher(disposition);
                if (plain.find()) name = plain.group(1) != null ? plain.group(1) : plain.group(2).trim();
            }
        }
        if (name != null) name = name.replaceAll("[\\p{Cntrl}\\\\/:*?\"<>|]", "_").replaceAll("^[. ]+|[. ]+$", "");
        if (name == null || name.isEmpty()) {
            String extension = "application/pdf".equalsIgnoreCase(mimeType) ? ".pdf"
                    : "application/vnd.openxmlformats-officedocument.wordprocessingml.document".equalsIgnoreCase(mimeType) ? ".docx" : ".bin";
            name = "教材资料" + extension;
        }
        if (name.length() > 180) {
            int dot = name.lastIndexOf('.');
            String extension = dot > 0 && name.length() - dot <= 12 ? name.substring(dot) : "";
            name = name.substring(0, 180 - extension.length()) + extension;
        }
        return name;
    }
}
