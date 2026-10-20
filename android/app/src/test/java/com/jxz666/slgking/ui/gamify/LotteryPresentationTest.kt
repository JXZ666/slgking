package com.jxz666.slgking.ui.gamify

import org.json.JSONArray
import org.json.JSONException
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Test

class LotteryPresentationTest {
    @Test fun unwrapsAuthoritativeCloudDrawAndBalance() {
        val raw = JSONObject("""{"balance":999,"draw":{"prize":{"kind":"points","value":20},"balance":115}}""")
        val draw = lotteryDraw(raw)
        assertEquals(115, draw.getInt("balance"))
        assertEquals("获得 20 积分", lotteryResultText(draw))
    }
    @Test(expected = JSONException::class) fun malformedDrawDoesNotInventThanksPrize() {
        lotteryDraw(JSONObject("""{"prize":{"kind":"points","value":10}}"""))
    }
    @Test fun usesKnownPrizeTextForTitleName() {
        assertEquals("获得传奇头衔", lotteryResultText(JSONObject(
            """{"prize":{"kind":"title","value":"legend","text":"获得传奇头衔"}}""")))
    }
    @Test fun duplicateTitleShowsCompensation() {
        assertEquals("重复头衔，折算 100 积分", lotteryResultText(JSONObject(
            """{"prize":{"kind":"title","value":"legend","duplicate":true,"points":100}}""")))
    }
    @Test fun flatHistoryAndZeroPointsRemainTruthful() {
        assertEquals("获得 0 积分", lotteryResultText(JSONObject("""{"kind":"points","value":"0"}""")))
        assertEquals("获得 30 积分", lotteryResultText(JSONObject("""{"kind":"points","value":"30"}""")))
    }
    @Test fun supportsLegacyPrizeLabels() {
        assertEquals("积分", flexPrizeText(JSONArray("""[["star"],"积分",0.5]""")))
        assertEquals("奖品", flexPrizeText("奖品"))
    }
    @Test fun doesNotMislabelUnknownResponseAsThanks() {
        assertEquals("抽奖已完成", lotteryResultText(JSONObject()))
    }
}
