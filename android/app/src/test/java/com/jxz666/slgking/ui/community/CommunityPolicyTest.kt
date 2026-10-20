package com.jxz666.slgking.ui.community

import org.json.JSONObject
import org.json.JSONArray
import org.junit.Assert.*
import org.junit.Test
import java.util.UUID

class CommunityPolicyTest {
    private fun ordinary() = JSONObject().put("logged_in", true).put("account_id", "normal")
    private fun validReward() = JSONObject().put("claimed", false).put("claimable", true).put("campaign", JSONObject().put("id", "activity").put("amount", 100).put("active", true))
    private fun row(id: Any, status: String = "approved") = JSONObject().put("id", id).put("status", status)
    @Test fun opaqueIdsDoNotLosePrecision() {
        assertEquals("90071992547409931234", opaqueId(row("90071992547409931234")))
        assertEquals("7", opaqueId(row(7)))
        assertNull(opaqueId(row(JSONObject.NULL)))
    }
    @Test fun publicAndOwnRowsMergeWithoutDuplicates() {
        val result = mergeReplies(listOf(row("a"), row("b")), listOf(row("a"), row("p", "pending"), row("r", "rejected")), 1)
        assertEquals(listOf("a", "b", "p", "r"), result.map { opaqueId(it) })
        assertTrue(result.first().optBoolean("own"))
        assertFalse(result[1].optBoolean("own"))
        assertTrue(result.last().optBoolean("own"))
    }
    @Test fun laterPagesDoNotRepeatPendingOrOtherApprovedRows() {
        val result = mergeReplies(listOf(row("b")), listOf(row("a"), row("p", "pending")), 2)
        assertEquals(listOf("b"), result.map { opaqueId(it) })
    }
    @Test fun uncertainRetryReusesUuidButChangesCreateNewOperation() {
        val op = ReplyOperation()
        val first = op.id(" hello ", null)
        UUID.fromString(first)
        assertEquals(first, op.id("hello", null))
        val target = op.id("hello", "target")
        assertNotEquals(first, target)
        assertNotEquals(target, op.id("modified", "target"))
        op.clear()
        assertNotEquals(first, op.id("hello", null))
    }
    @Test fun contentUsesUnicodeCharactersRatherThanUtf16Units() {
        assertFalse(validReply(" a "))
        assertTrue(validReply("ab"))
        assertTrue(validReply("😀".repeat(500)))
        assertFalse(validReply("a".repeat(501)))
    }
    @Test fun onlyOrdinaryAuthenticatedAccountsCanWrite() {
        assertFalse(canWrite(JSONObject()))
        assertFalse(canWrite(JSONObject().put("logged_in", true).put("developer_mode", true)))
        assertFalse(canWrite(JSONObject().put("logged_in", true).put("account_id", "developer")))
        assertTrue(canWrite(JSONObject().put("logged_in", true).put("account_id", "normal")))
    }
    @Test fun readRequiresFocusedPublicReplyPresent() {
        assertFalse(shouldMarkRead("focus", emptyList()))
        assertFalse(shouldMarkRead("focus", listOf(row("other"))))
        assertTrue(shouldMarkRead("focus", listOf(row("focus"))))
    }
    @Test fun privacyRequiresExplicitBooleanTrue() {
        assertNull(safePublicProfile(JSONObject()))
        assertNull(safePublicProfile(JSONObject().put("profile", JSONObject().put("profile_visible", false))))
        assertNull(safePublicProfile(JSONObject().put("profile", JSONObject().put("profile_visible", "true"))))
        val profile = JSONObject().put("profile_visible", true).put("profile_message", "message")
        assertSame(profile, safePublicProfile(JSONObject().put("profile", profile)))
    }
    @Test fun publicCardMustMatchRequestedIdentityBeforeDisplayingAnyProjection() {
        val profile = JSONObject().put("public_id", "expected").put("profile_visible", true).put("profile_message", "safe")
        val result = JSONObject().put("profile", profile)
        assertSame(profile, validatedPublicProfile(result, "expected"))
        try { validatedPublicProfile(result, "another"); fail("wrong author must fail") }
        catch (e: IllegalArgumentException) { assertTrue(e.message!!.contains("身份不匹配")) }
        profile.remove("public_id")
        try { validatedPublicProfile(result, "expected"); fail("unknown author must fail") }
        catch (_: IllegalArgumentException) { }
        profile.put("public_id", "expected").put("profile_visible", false)
        assertNull(validatedPublicProfile(result, "expected"))
    }
    @Test fun publicDecorationsPreferResolvedNamesForActualStringIds() {
        val profile = JSONObject().put("profile_visible", true)
            .put("equipped_title", "title_internal_id")
            .put("equipped_appearances", JSONObject().put("frame", "frame_internal_id"))
            .put("title_name", "目录旅人")
            .put("appearance_names", JSONObject().put("frame", "金色边框"))
        assertEquals("目录旅人", publicDecorations(profile).title)
        assertEquals(listOf("金色边框"), publicDecorations(profile).appearances)
        profile.remove("title_name"); profile.remove("appearance_names")
        assertNull(publicDecorations(profile).title)
        assertTrue(publicDecorations(profile).appearances.isEmpty())
    }
    @Test fun publicDecorationsSupportFutureNamedObjectsButNeverHiddenProjection() {
        val profile = JSONObject().put("profile_visible", true)
            .put("equipped_title", JSONObject().put("name", "收藏家"))
            .put("equipped_appearances", JSONArray().put(JSONObject().put("name", "紫色主题")).put("raw_id"))
        assertEquals("收藏家", publicDecorations(profile).title)
        assertEquals(listOf("紫色主题"), publicDecorations(profile).appearances)
        profile.put("profile_visible", false)
        assertNull(publicDecorations(profile).title)
        assertTrue(publicDecorations(profile).appearances.isEmpty())
    }
    @Test fun announcementObjectsKeepHistoryNewestFirstWithoutDuplicatingCurrent() {
        val current = JSONObject().put("id", "current").put("title", "Current").put("body", "full body")
        val old = JSONObject().put("id", "old").put("title", "Old")
        val recent = JSONObject().put("id", "recent").put("title", "Recent")
        val config = JSONObject().put("announcement", current).put("announcement_history", JSONArray().put(old).put(recent).put(current))
        assertEquals(listOf("current", "recent", "old"), announcementItems(config).map { opaqueId(it) })
        assertEquals("full body", announcementItems(config).first().optString("body"))
    }
    @Test fun offlineAnnouncementNeverBecomesAnEmptyNotice() {
        try {
            announcementConfig(JSONObject().put("offline", true).put("config", JSONObject()))
            fail("offline must fail")
        } catch (e: IllegalArgumentException) { assertTrue(e.message!!.contains("读取失败")) }
        val config = JSONObject().put("notice", "notice")
        assertSame(config, announcementConfig(JSONObject().put("offline", false).put("config", config)))
    }
    @Test fun rewardRequiresExplicitClaimableRatherThanEligibleOrActive() {
        val status = validReward().put("eligible", true).apply { remove("claimable") }
        assertFalse(rewardClaimable(status, ordinary()))
        status.put("claimable", "true")
        assertFalse(rewardClaimable(status, ordinary()))
        status.put("claimable", true)
        assertTrue(rewardClaimable(status, ordinary()))
        status.put("claimed", true)
        assertFalse(rewardClaimable(status, ordinary()))
        status.put("claimed", false).remove("claimable")
        status.getJSONObject("campaign").put("claimable", true)
        assertTrue(rewardClaimable(status, ordinary()))
    }
    @Test fun unknownClaimedAndInvalidAmountsOrIdsNeverGrantEligibility() {
        assertFalse(rewardClaimable(validReward().apply { remove("claimed") }, ordinary()))
        assertFalse(rewardClaimable(validReward().put("claimed", JSONObject.NULL), ordinary()))
        assertFalse(rewardClaimable(validReward().put("claimed", "false"), ordinary()))
        listOf(0, -1, true, "100", 1.0, JSONObject.NULL).forEach { bad ->
            val reward = validReward()
            reward.getJSONObject("campaign").put("amount", bad)
            assertFalse("invalid amount: $bad", rewardClaimable(reward, ordinary()))
        }
        listOf("", "  ", 3, JSONObject.NULL).forEach { bad ->
            val reward = validReward()
            reward.getJSONObject("campaign").put("id", bad)
            assertFalse("invalid id: $bad", rewardClaimable(reward, ordinary()))
        }
    }
    @Test fun guestDeveloperAndMissingAccountCannotReceiveRewardBadge() {
        listOf(JSONObject(), ordinary().put("logged_in", false), ordinary().put("developer_mode", true),
            ordinary().put("account_id", "developer"), ordinary().apply { remove("account_id") }).forEach { session ->
            assertFalse(rewardClaimable(validReward(), session))
        }
        assertTrue(rewardClaimable(validReward(), ordinary()))
    }
    @Test fun badgeOnlyRetainsConfirmedEligibilityForSameIdentityWithinFiveMinutes() {
        val memory = ActivityBadgeMemory()
        assertFalse(memory.visible(1, 0))
        memory.confirm(1, true, 10)
        assertTrue(memory.visible(1, 10 + 299999))
        assertFalse(memory.visible(1, 10 + 300000))
        memory.confirm(1, true, 20)
        assertFalse(memory.visible(2, 21))
        assertFalse(memory.visible(1, 22))
        memory.confirm(1, true, 30)
        memory.confirm(1, false, 31)
        assertFalse(memory.visible(1, 31))
        memory.confirm(1, true, 40)
        memory.clear()
        assertFalse(memory.visible(1, 41))
    }
}
