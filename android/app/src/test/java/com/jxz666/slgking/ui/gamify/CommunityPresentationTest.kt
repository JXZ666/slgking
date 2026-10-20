package com.jxz666.slgking.ui.gamify

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class CommunityPresentationTest {
    @Test fun inviteCodeUsesRealServerContract() {
        val payload = JSONObject().put("invite_code", "0123456789abcdef")
            .put("eligible_to_redeem", true).put("redeemed", false)
        assertEquals("0123456789abcdef", referralCode(payload))
        assertTrue(canBindReferral(payload))
        assertEquals("", referralCode(JSONObject().put("code", "incorrect legacy field")))
    }
    @Test fun bindingRequiresExplicitEligibilityAndNotAlreadyBound() {
        assertFalse(canBindReferral(JSONObject()))
        assertFalse(canBindReferral(JSONObject().put("eligible_to_redeem", false)))
        assertFalse(canBindReferral(JSONObject().put("eligible_to_redeem", true).put("redeemed", true)))
    }
    @Test fun repeatSigninAlwaysUsesServerAlreadyState() {
        assertEquals("今天已签到过啦", signinMessage(JSONObject().put("already", true).put("gained", 10)))
        assertEquals("签到成功，+25 积分", signinMessage(JSONObject().put("gained", 10).put("bonus", 15)))
        assertEquals("签到完成", signinMessage(JSONObject()))
    }
    @Test fun boardSupportsEmptyAndThirtyRows() {
        assertTrue(leaderboardRows(JSONObject().put("board", "points").put("entries", JSONArray()), "points").isEmpty())
        val entries = JSONArray()
        repeat(30) { entries.put(JSONObject().put("rank", it + 1).put("nickname", "玩家").put("score", 3000000000L)) }
        val rows = leaderboardRows(JSONObject().put("board", "wardrobe").put("entries", entries), "wardrobe")
        assertEquals(30, rows.size)
        assertEquals(3000000000L, rows.first().score)
    }
    @Test fun boardPublicIdsAreOptionalAndDoNotExposeOtherAccountFields() {
        val rows = JSONArray()
            .put(JSONObject().put("rank", 1).put("score", 30).put("nickname", "First").put("author_public_id", "author-uuid").put("public_id", "fallback"))
            .put(JSONObject().put("rank", 2).put("score", 20).put("nickname", "Second").put("public_id", "legacy-uuid"))
            .put(JSONObject().put("rank", 3).put("score", 10).put("nickname", "Third").put("account_id", "private-internal-id"))
            .put(JSONObject().put("rank", 4).put("score", 0).put("nickname", "Fourth").put("author_public_id", JSONObject.NULL).put("public_id", "null"))
        val parsed = leaderboardRows(JSONObject().put("board", "points").put("entries", rows), "points")
        assertEquals(listOf("author-uuid", "legacy-uuid", null, null), parsed.map { it.publicId })
        assertEquals(listOf(30L, 20L, 10L, 0L), parsed.map { it.score })
        assertEquals("Third", parsed[2].nickname)
    }
    @Test(expected = IllegalArgumentException::class) fun boardRejectsWrongResponseType() {
        leaderboardRows(JSONObject().put("board", "wardrobe").put("entries", JSONArray()), "points")
    }
    @Test(expected = IllegalArgumentException::class) fun boardRejectsDuplicateRanks() {
        val row = JSONObject().put("rank", 1).put("score", 10)
        leaderboardRows(JSONObject().put("board", "points").put("entries", JSONArray().put(row).put(row)), "points")
    }
    @Test(expected = IllegalArgumentException::class) fun boardRejectsMoreThanThirtyRows() {
        val entries = JSONArray()
        repeat(31) { entries.put(JSONObject().put("rank", it + 1).put("score", 10)) }
        leaderboardRows(JSONObject().put("board", "points").put("entries", entries), "points")
    }
    @Test fun timestampsUseServerSecondsAndShanghaiTimezone() {
        assertEquals("时间未知", commentTimestamp(0))
        assertEquals("1970-01-01 08:00（北京时间）", commentTimestamp(1))
    }
    @Test fun timestampsAcceptActualIsoServerValueAndOffsets() {
        assertEquals("2026-10-07 08:00（北京时间）", commentTimestamp("2026-10-07T00:00:00Z"))
        assertEquals("2026-10-07 08:00（北京时间）", commentTimestamp("2026-10-07T08:00:00+08:00"))
        assertEquals("2026-10-07 08:00（北京时间）", commentTimestamp("2026-10-07T00:00:00.123456+0000"))
        assertEquals("1970-01-01 08:00（北京时间）", commentTimestamp("1"))
        assertEquals("时间未知", commentTimestamp("2026-02-30T00:00:00Z"))
        assertEquals("时间未知", commentTimestamp(JSONObject.NULL))
    }
    @Test fun missingNicknameAndIdNeverBecomeLiteralNullOrDeleteTarget() {
        assertEquals("未设置昵称", cloudCommentNickname(JSONObject().put("nickname", JSONObject.NULL)))
        assertEquals("未设置昵称", cloudCommentNickname(JSONObject().put("nickname", "   ")))
        assertEquals("玩家", cloudCommentNickname(JSONObject().put("nickname", "玩家")))
        assertEquals("", cloudCommentId(JSONObject()))
        assertEquals("", cloudCommentId(JSONObject().put("id", JSONObject.NULL)))
        assertEquals("uuid-id", cloudCommentId(JSONObject().put("id", "uuid-id")))
    }
}
