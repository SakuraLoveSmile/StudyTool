package com.studytool.connected;

import org.junit.Test;
import static org.junit.Assert.*;

public class ServerOriginTest {
    @Test public void canonicalizesHttpsOrigins() {
        assertEquals("https://learn.example.com", ServerOrigin.parse("HTTPS://LEARN.EXAMPLE.COM:443/"));
        assertEquals("https://learn.example.com:8443", ServerOrigin.parse("https://learn.example.com:8443"));
        assertEquals("https://[::1]", ServerOrigin.parse("https://[::1]/"));
    }

    @Test public void rejectsUnsafeOrAmbiguousSetupAddresses() {
        String[] rejected = {null, "", " https://learn.example.com", "https://learn.example.com ",
                "http://learn.example.com", "file:///etc/passwd", "javascript:alert(1)",
                "https://user:pass@learn.example.com", "https://learn.example.com/library",
                "https://learn.example.com?token=x", "https://learn.example.com#x",
                "https://learn.example.com:0", "https://learn.example.com:65536",
                "https://learn.example.com:", "https://learn.example.com\\@evil.example",
                "https://learn.example.com/%2e%2e", "https://learn.example.com//"};
        for (String address : rejected) {
            assertThrows(String.valueOf(address), IllegalArgumentException.class, () -> ServerOrigin.parse(address));
        }
    }

    @Test public void navigationMatchesSchemeHostAndEffectivePort() {
        String origin = "https://learn.example.com";
        assertTrue(ServerOrigin.contains(origin, "https://learn.example.com/library?chapter=1#concept"));
        assertTrue(ServerOrigin.contains(origin, "HTTPS://LEARN.EXAMPLE.COM:443/login"));
        assertFalse(ServerOrigin.contains(origin, "http://learn.example.com/library"));
        assertFalse(ServerOrigin.contains(origin, "https://learn.example.com:8443/library"));
        assertFalse(ServerOrigin.contains(origin, "https://learn.example.com.evil.example/library"));
        assertFalse(ServerOrigin.contains(origin, "https://learn.example.com@evil.example/library"));
        assertFalse(ServerOrigin.contains(origin, "https://user@learn.example.com/library"));
        assertFalse(ServerOrigin.contains(origin, "javascript:alert(1)"));
        assertFalse(ServerOrigin.contains(origin, "content://learn.example.com/document"));
        assertFalse(ServerOrigin.contains(origin, null));
    }

    @Test public void onlyWebLinksMayOpenInSystemBrowser() {
        assertTrue(ServerOrigin.isExternalWebLink("https://example.org/page"));
        assertTrue(ServerOrigin.isExternalWebLink("http://example.org/page"));
        for (String link : new String[]{"intent://example.org", "javascript:alert(1)",
                "content://files/item", "file:///sdcard/test", "data:text/html,test",
                "https://user:secret@example.org", "//example.org", null}) {
            assertFalse(String.valueOf(link), ServerOrigin.isExternalWebLink(link));
        }
    }
}
