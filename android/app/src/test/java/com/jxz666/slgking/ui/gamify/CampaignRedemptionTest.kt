package com.jxz666.slgking.ui.gamify

import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class CampaignRedemptionTest {
    @Test fun firstClaimUsesActualAwardAndNamesTheTitle() {
        val result = JSONObject().put("campaign_id", "echoes-launch").put("gained", 100)
        val message = campaignRedemptionMessage(result)
        assertTrue(message.contains("深渊回响"))
        assertTrue(message.contains("+100"))
    }
    @Test fun duplicateDoesNotClaimAnotherHundred() {
        val result = JSONObject().put("campaign_id", "echoes-launch").put("already_claimed", true).put("gained", 0)
        val message = campaignRedemptionMessage(result)
        assertTrue(message.contains("已领取过"))
        assertFalse(message.contains("+100"))
    }
    @Test fun ExistingGroupCodeRetainsGenericMessage() {
        assertEquals("兑换成功", campaignRedemptionMessage(JSONObject().put("group", true)))
    }
}
