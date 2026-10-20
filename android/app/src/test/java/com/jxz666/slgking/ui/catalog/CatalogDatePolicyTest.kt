package com.jxz666.slgking.ui.catalog

import org.junit.Assert.*
import org.junit.Test
class CatalogDatePolicyTest {
    @Test fun presetsDoNotRequireCustomDates() {
        listOf("all", "7", "30", "90").forEach { assertNull(CatalogDatePolicy.error(it, null, null)) }
    }
    @Test fun strictDatesAndRangeOrder() {
        assertNull(CatalogDatePolicy.error("custom", "2024-02-29", "2024-03-01"))
        assertNull(CatalogDatePolicy.error("custom", "2026-10-16", "2026-10-16"))
        listOf("2026-02-29", "2026-13-01", "2026-1-01", "", "2026-10-16garbage").forEach {
            assertNotNull(CatalogDatePolicy.error("custom", it, "2026-12-31"))
        }
        assertNotNull(CatalogDatePolicy.error("custom", "2026-11-01", "2026-10-01"))
        assertNotNull(CatalogDatePolicy.error("custom", null, null))
    }
    @Test fun dateCopyPreservesCombinedFilters() {
        val original = CatalogQuery(catalogId = "dlsite", q = "abc", tags = setOf("RPG"), view = "want", statuses = setOf("want"), languages = setOf("ja"))
        val dated = original.copy(datePreset = "custom", dateStart = "2026-10-01", dateEnd = "2026-10-16")
        assertEquals(original.q, dated.q)
        assertEquals(original.tags, dated.tags)
        assertEquals(original.view, dated.view)
        assertEquals(original.statuses, dated.statuses)
        assertEquals(original.languages, dated.languages)
        assertEquals("all", original.datePreset)
    }
    @Test fun catalogSnapshotsRetainIndependentDateSelections() {
        val slg = CatalogQuery(datePreset = "7")
        val dlsite = CatalogQuery(catalogId = "dlsite", datePreset = "30")
        val saved = mapOf(slg.catalogId to slg, dlsite.catalogId to dlsite)
        assertEquals("7", saved.getValue("western_slg").datePreset)
        assertEquals("30", saved.getValue("dlsite").datePreset)
    }
}
