package com.studytool.connected;

import org.junit.Test;
import static org.junit.Assert.*;

public class FileChooserTest {
    @Test public void mapsWebsiteExtensionsToAndroidMimeFilters() {
        assertArrayEquals(new String[]{"application/pdf",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "image/jpeg", "image/png"},
                MainActivity.chooserMimeTypes(new String[]{".pdf,.docx,.jpg,.jpeg,.png"}));
    }

    @Test public void acceptsMimeTypesAndIgnoresMalformedFilters() {
        assertArrayEquals(new String[]{"application/pdf", "image/*"},
                MainActivity.chooserMimeTypes(new String[]{"application/pdf", "image/*", "not a type"}));
        assertArrayEquals(new String[]{"*/*"}, MainActivity.chooserMimeTypes(null));
        assertArrayEquals(new String[]{"*/*"}, MainActivity.chooserMimeTypes(new String[]{"", ".unknown"}));
    }
}
