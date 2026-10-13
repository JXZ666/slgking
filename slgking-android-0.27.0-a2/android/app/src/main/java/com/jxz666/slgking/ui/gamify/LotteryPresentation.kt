package com.jxz666.slgking.ui.gamify

import org.json.JSONObject

/** The bridge wraps the authoritative cloud response under draw. */
internal fun lotteryDraw(response: JSONObject): JSONObject = response.getJSONObject("draw")

/** 奖项条目自适应取文案：对象取 text/name，数组取第 2 列。 */
internal fun flexPrizeText(value: Any?): String = when (value) {
    is org.json.JSONObject -> value.optString("text").ifBlank { value.optString("name") }.ifBlank { value.optString("label") }
    is org.json.JSONArray -> value.optString(1, value.optString(0, ""))
    is String -> value
    else -> ""
}

internal fun lotteryResultText(draw: JSONObject): String {
    val prize = draw.optJSONObject("prize") ?: draw
    val text = draw.optString("message").ifBlank { draw.optString("text") }
        .ifBlank { flexPrizeText(prize) }
    if (text.isNotBlank()) return text
    return when (prize.optString("kind")) {
        "points" -> "获得 ${prize.optInt("value")} 积分"
        "title" -> if (prize.optBoolean("duplicate"))
            "重复头衔，折算 ${prize.optInt("points")} 积分"
        else "获得头衔「${prize.optString("title_name", prize.optString("title_id", prize.optString("value")))}」"
        "none", "thanks" -> "谢谢参与"
        else -> flexPrizeText(draw.opt("prize")).ifBlank { "抽奖已完成" }
    }
}

