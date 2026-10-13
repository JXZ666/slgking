package com.jxz666.slgking.ui.comments

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class CommentPresentationTest {
    private fun row(id: Any): JSONObject = JSONObject().put("id", id)

    @Test fun opaqueIdsRemainDistinctAndKeepExactValue() {
        val a = row("5abcde123456abcdef01")
        val b = row("6abcde123456abcdef02")
        assertEquals(listOf(a, b), mergeCloudComments(emptyList(), listOf(a, b)))
        assertEquals("5abcde123456abcdef01", cloudCommentId(a))
        assertNotEquals(cloudCommentKey(a), cloudCommentKey(b))
    }

    @Test fun dropsInvalidRowsAndDuplicatesOnFirstPage() {
        val a = row("abc")
        assertEquals(listOf(a), mergeCloudComments(emptyList(), listOf(
            JSONObject(), row(JSONObject.NULL), row(""), row("  "), a, row("abc"))))
        assertEquals(1, commentObjects(JSONArray().put(a).put(JSONObject.NULL).put("bad")).size)
    }

    @Test fun overlappingPagesKeepUnseenIdsIncludingNumericLegacyId() {
        val a = row("abc")
        val b = row("def")
        val legacy = row(7)
        assertEquals(listOf(a, b, legacy), mergeCloudComments(listOf(a), listOf(row("abc"), b, legacy)))
        assertEquals("cloud:7", cloudCommentKey(legacy))
        assertNotEquals("local-7", cloudCommentKey(legacy))
    }

    @Test fun rendersCurrentAndLegacyAuthorTimeFields() {
        val current = JSONObject().put("nickname", "测试用户").put("ts", "2026-10-07T04:05:00+00:00")
        assertEquals("测试用户", commentAuthor(current))
        assertEquals("2026-10-07 12:05（北京时间）", commentTime(current))
        assertEquals("旧用户", commentAuthor(JSONObject().put("author", "旧用户")))
        assertEquals("匿名", commentAuthor(JSONObject().put("nickname", JSONObject.NULL)))
        assertEquals("", commentTime(JSONObject()))
    }

    @Test fun isoZoneAndOffsetsUseBeijingTimeAcrossDayBoundary() {
        assertEquals("2026-10-08 04:00（北京时间）", commentTime(
            JSONObject().put("ts", "2026-10-07T20:00:00Z")))
        assertEquals("2026-10-08 04:00（北京时间）", commentTime(
            JSONObject().put("ts", "2026-10-08T04:00:00.123456+08:00")))
        assertEquals("2026-10-08 04:00（北京时间）", commentTime(
            JSONObject().put("ts", "2026-10-07T13:00:00-0700")))
        assertEquals("2026-10-07 04:05", commentTime(
            JSONObject().put("time", "2026-10-07 04:05:00")))
    }

    @Test fun replacementQueryRejectsLateOlderPageAndSortResponses() {
        val queries = CommentRequestGeneration()
        val latest = queries.next()
        assertTrue(queries.accepts(latest))
        val popular = queries.next()
        assertFalse(queries.accepts(latest))
        assertTrue(queries.accepts(popular))
    }
}
