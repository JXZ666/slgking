package com.jxz666.slgking.ui.community

import org.json.JSONArray
import org.json.JSONObject
import java.util.UUID

internal fun objects(a: JSONArray?): List<JSONObject> = if (a == null) emptyList() else
    (0 until a.length()).mapNotNull { a.optJSONObject(it) }
internal fun opaqueId(o: JSONObject, name: String = "id"): String? =
    if (o.isNull(name)) null else o.optString(name).takeIf { it.isNotBlank() && it != "null" }
internal fun canWrite(status: JSONObject): Boolean = status.optBoolean("logged_in") &&
    !status.optBoolean("developer_mode") && status.optString("account_id") != "developer"

/** Own pending rows are separate from the public chronological page. Never coerce IDs to numbers. */
internal fun mergeReplies(public: List<JSONObject>, mine: List<JSONObject>, page: Int): List<JSONObject> {
    val ownIds = mine.mapNotNull { opaqueId(it) }.toSet()
    val seen = mutableSetOf<String>()
    val visible = public.map { JSONObject(it.toString()).apply {
        if (opaqueId(it) in ownIds) put("own", true)
    } }
    val pending = if (page == 1) mine.filter { it.optString("status") !in setOf("approved", "public") }
        .map { JSONObject(it.toString()).put("own", true) } else emptyList()
    return (visible + pending).filter { opaqueId(it)?.let(seen::add) == true }
}

internal class ReplyOperation {
    private var signature: Pair<String, String?>? = null
    private var operation: String? = null
    fun id(content: String, target: String?): String {
        val next = content.trim() to target
        if (signature != next) { signature = next; operation = UUID.randomUUID().toString() }
        return requireNotNull(operation)
    }
    fun clear() { signature = null; operation = null }
}

internal fun validReply(content: String): Boolean = content.trim().codePointCount(0, content.trim().length) in 2..500
internal fun shouldMarkRead(focusId: String, public: List<JSONObject>): Boolean = public.any { opaqueId(it) == focusId }
internal fun safePublicProfile(result: JSONObject): JSONObject? = result.optJSONObject("profile")
    ?.takeIf { it.opt("profile_visible") == true }

internal fun validatedPublicProfile(result: JSONObject, requestedId: String): JSONObject? {
    val profile = result.optJSONObject("profile") ?: throw IllegalArgumentException("公开名片暂不可用，请稍后重试")
    require((profile.opt("public_id") as? String) == requestedId) { "公开名片身份不匹配，请重新打开后重试" }
    return safePublicProfile(result)
}

internal data class PublicDecorations(val title: String?, val appearances: List<String>)
internal fun publicDecorations(profile: JSONObject): PublicDecorations {
    if (profile.opt("profile_visible") != true) return PublicDecorations(null, emptyList())
    fun name(value: Any?): String? = (value as? String)?.trim()?.takeIf { it.isNotEmpty() && it != "null" }
    val title = name(profile.opt("title_name"))
        ?: profile.optJSONObject("equipped_title")?.let { name(it.opt("name")) }
    val names = profile.optJSONObject("appearance_names")
    val resolved = names?.keys()?.asSequence()?.mapNotNull { name(names.opt(it)) }?.toList().orEmpty()
    val future = objects(profile.optJSONArray("equipped_appearances")).mapNotNull { name(it.opt("name")) }
    val objectNames = profile.optJSONObject("equipped_appearances")?.let { equipped ->
        equipped.keys().asSequence().mapNotNull { slot -> equipped.optJSONObject(slot)?.let { name(it.opt("name")) } }.toList()
    }.orEmpty()
    // Raw equipped string IDs are deliberately never shown as human-readable decoration names.
    return PublicDecorations(title, (if (resolved.isNotEmpty()) resolved else future + objectNames).distinct())
}

internal fun announcementConfig(response: JSONObject): JSONObject {
    require(!response.optBoolean("offline")) { "公告网络或配置读取失败，请检查网络后重试" }
    return response.optJSONObject("config") ?: throw IllegalStateException("公告配置读取失败，请重试")
}

internal fun rewardClaimable(status: JSONObject, session: JSONObject): Boolean {
    if (session.opt("logged_in") != true || !canWrite(session) || opaqueId(session, "account_id") == null) return false
    val campaign = status.optJSONObject("campaign") ?: return false
    val amount = campaign.opt("amount")
    return campaign.opt("active") == true && status.opt("claimed") == false &&
        (status.opt("claimable") == true || campaign.opt("claimable") == true) &&
        amount is Int && amount > 0 && (campaign.opt("id") as? String)?.isNotBlank() == true
}

internal fun announcementItems(config: JSONObject): List<JSONObject> {
    val current = config.optJSONObject("announcement")
    val history = config.optJSONArray("announcement_history") ?: current?.optJSONArray("announcement_history")
    val seen = mutableSetOf<String>()
    return (listOfNotNull(current) + objects(history).reversed() + objects(config.optJSONArray("announcements")))
        .filter { row ->
            val title = row.optString("title").trim(); val body = row.optString("body", row.optString("content")).trim()
            (title.isNotEmpty() || body.isNotEmpty()) && seen.add(opaqueId(row) ?: "$title\n$body")
        }
}
