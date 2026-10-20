package com.jxz666.slgking.ui.game

import org.junit.Assert.*
import org.junit.Test

class DetailRulesTest {
    @Test fun narrowScreenPreservesAllTwentyFiveTags() {
        val rows = tagRows(List(25) { 90 }, 180, 6)
        assertEquals((0 until 25).toList(), rows.flatten())
        assertEquals(25, rows.size)
        assertEquals(3, rows.take(3).size)
    }
    @Test fun longTagOccupiesItsOwnWrappedRow() {
        assertEquals(listOf(listOf(0), listOf(1), listOf(2)), tagRows(listOf(700, 45, 190), 200, 8))
    }
    @Test fun exactBoundaryDoesNotInventOverflow() {
        assertEquals(listOf(listOf(0, 1)), tagRows(listOf(96, 96), 200, 8))
        assertEquals(listOf(listOf(0), listOf(1)), tagRows(listOf(97, 96), 200, 8))
        assertTrue(tagRows(emptyList(), 320, 8).isEmpty())
    }
    @Test fun largerFontWrapsRatherThanLosingTags() {
        val small = tagRows(List(22) { 75 }, 320, 6)
        val large = tagRows(List(22) { 150 }, 320, 6)
        assertTrue(large.size > small.size)
        assertEquals(small.flatten(), large.flatten())
    }
    @Test fun cachedBothFieldsNeverStartsOfflineRequest() {
        assertTrue(translationFields("Title", "标题", "manual", "Overview", "简介", "auto").isEmpty())
    }
    @Test fun missingOverviewNeverRequestsEmptyDlsiteText() {
        assertEquals(listOf("title"), translationFields("RJ Title", null, "missing", "  ", null, "missing"))
    }
    @Test fun partialCachedOrManualFieldOnlyTranslatesMissingOne() {
        assertEquals(listOf("overview"), translationFields("Title", "人工标题", "manual", "a".repeat(5000), null, "missing"))
        assertEquals(listOf("title"), translationFields("Title", null, "missing", "Description", "缓存简介", "auto"))
    }
    @Test fun terminalUntranslatableDoesNotLoop() {
        assertTrue(translationFields("123", null, "untranslatable", "456", null, "untranslatable").isEmpty())
    }
    @Test fun entirelyMissingTranslationRequestsBothFields() {
        assertEquals(listOf("title", "overview"), translationFields("Title", null, null, "Description", null, null))
    }
}
