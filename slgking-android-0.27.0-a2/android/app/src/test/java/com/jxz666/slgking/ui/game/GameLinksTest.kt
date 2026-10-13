package com.jxz666.slgking.ui.game

import org.junit.Assert.*
import org.junit.Test

class GameLinksTest {
    @Test fun acceptsWebOnly() {
        assertEquals("https://www.dlsite.com/maniax/work/=/product_id/RJ123456.html", validWebUrl("https://www.dlsite.com/maniax/work/=/product_id/RJ123456.html"))
        assertEquals("http://example.org/path?x=1#part", validWebUrl(" http://example.org/path?x=1#part "))
    }
    @Test fun rejectsOtherSchemesAndMalformedAddresses() {
        listOf("user://record", "file:///sdcard/a.apk", "javascript:alert(1)", "intent://x", "https:///missing", "https://a b.com", "https://name:pass@example.com", "https://example.com:99999", "https://example.com/\npath").forEach { assertNull(it, validWebUrl(it)) }
    }
    @Test fun showsHostWithoutCredentialsOrPaths() { assertEquals("example.com", sourceDomain("https://example.com/a?secret=1")) }
}
