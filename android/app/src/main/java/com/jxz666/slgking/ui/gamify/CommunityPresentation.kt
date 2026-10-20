package com.jxz666.slgking.ui.gamify

import org.json.JSONObject
import java.text.ParsePosition
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone

internal data class BoardRow(val rank: Int, val nickname: String, val score: Long, val publicId: String? = null)
internal data class BoardState(
    val rows: List<BoardRow> = emptyList(),
    val loading: Boolean = false,
    val loaded: Boolean = false,
    val error: String? = null,
)

internal fun leaderboardRows(payload: JSONObject, expected: String): List<BoardRow> {
    require(expected in listOf("points", "wardrobe") && payload.getString("board") == expected) {
        "服务器返回的排行榜类型不正确"
    }
    val entries = payload.getJSONArray("entries")
    require(entries.length() <= 30) { "服务器返回的排行榜超过前 30 名" }
    val rows = (0 until entries.length()).map { index ->
        val entry = entries.getJSONObject(index)
        val rank = entry.getInt("rank")
        val score = entry.getLong("score")
        require(rank in 1..30 && score >= 0) { "排行榜数值格式不正确" }
        val publicId = (entry.opt("author_public_id") as? String)?.trim()?.takeIf { it.isNotEmpty() && it != "null" }
            ?: (entry.opt("public_id") as? String)?.trim()?.takeIf { it.isNotEmpty() && it != "null" }
        BoardRow(rank, entry.optString("nickname").ifBlank { "未设置昵称" }.take(32), score, publicId)
    }
    require(rows.map { it.rank }.distinct().size == rows.size) { "排行榜排名重复" }
    return rows
}

internal fun referralCode(payload: JSONObject): String = payload.optString("invite_code").trim()
internal fun canBindReferral(payload: JSONObject): Boolean =
    payload.optBoolean("eligible_to_redeem", false) && !payload.optBoolean("redeemed", false)

internal fun signinMessage(payload: JSONObject): String {
    if (payload.optBoolean("already", false)) return "今天已签到过啦"
    val gained = payload.optInt("gained") + payload.optInt("bonus")
    return if (gained > 0) "签到成功，+$gained 积分" else "签到完成"
}

internal fun cloudCommentNickname(payload: JSONObject): String =
    (payload.opt("nickname") as? String)?.trim()?.takeIf { it.isNotEmpty() } ?: "未设置昵称"

internal fun cloudCommentId(payload: JSONObject): String = when (val id = payload.opt("id")) {
    is String -> id.trim()
    is Number -> id.toString()
    else -> ""
}

internal fun commentTimestamp(raw: Any?): String {
    val value = raw?.toString()?.trim().orEmpty()
    val seconds = value.toLongOrNull()
    val instant = if (seconds != null) {
        if (seconds <= 0 || seconds > Long.MAX_VALUE / 1000) return "时间未知"
        Date(seconds * 1000)
    } else {
        val iso = Regex("""^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:?\d{2})$""")
            .matchEntire(value) ?: return "时间未知"
        val fraction = iso.groupValues[2].take(3).padEnd(3, '0')
        val zone = iso.groupValues[3].let {
            if (it != "Z" && it.length == 5) it.substring(0, 3) + ":" + it.substring(3) else it
        }
        val normalized = iso.groupValues[1] + "." + fraction + zone
        val parser = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss.SSSXXX", Locale.US)
        parser.isLenient = false
        val position = ParsePosition(0)
        val parsed = parser.parse(normalized, position)
        if (parsed == null || position.index != normalized.length) return "时间未知"
        parsed
    }
    val formatter = SimpleDateFormat("yyyy-MM-dd HH:mm", Locale.CHINA)
    formatter.timeZone = TimeZone.getTimeZone("Asia/Shanghai")
    return formatter.format(instant) + "（北京时间）"
}
