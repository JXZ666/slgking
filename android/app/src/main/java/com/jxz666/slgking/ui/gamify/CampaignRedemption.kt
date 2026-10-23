package com.jxz666.slgking.ui.gamify

import org.json.JSONObject

internal fun campaignRedemptionMessage(result: JSONObject): String = when {
    result.optString("campaign_id") == "echoes-launch" && result.optBoolean("already_claimed") ->
        "此账号已领取过「深渊回响」，本次不重复增加积分。"
    result.optString("campaign_id") == "echoes-launch" ->
        "已解锁史诗头衔「深渊回响」，云积分 +${result.optInt("gained")}。"
    else -> "兑换成功"
}
