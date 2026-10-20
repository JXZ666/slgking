package com.jxz666.slgking.ui.comments

import org.json.JSONArray
import org.json.JSONObject
import java.text.ParsePosition
import java.text.SimpleDateFormat
import java.util.Locale
import java.util.TimeZone

/** Cloud IDs are opaque strings, including legacy numeric IDs; local IDs stay numeric. */
internal fun cloudCommentId(row: JSONObject): String? =
    if (row.isNull("id")) null else row.optString("id").takeIf { it.isNotBlank() }

internal fun cloudCommentKey(row: JSONObject): String = "cloud:${requireNotNull(cloudCommentId(row))}"

internal fun commentObjects(array: JSONArray?): List<JSONObject> =
    if (array == null) emptyList() else (0 until array.length()).mapNotNull { array.optJSONObject(it) }

/** Deduplicate both the first page and overlapping later pages without converting IDs. */
internal fun mergeCloudComments(previous: List<JSONObject>, next: List<JSONObject>): List<JSONObject> {
    val seen = mutableSetOf<String>()
    return (previous + next).filter { row -> cloudCommentId(row)?.let { seen.add(it) } == true }
}

internal fun commentAuthor(row: JSONObject): String =
    row.optString("nickname").takeIf { it.isNotBlank() && it != "null" }
        ?: row.optString("author").takeIf { it.isNotBlank() && it != "null" } ?: "匿名"

internal fun commentTime(row: JSONObject): String {
    val raw = row.optString("ts").takeIf { it.isNotBlank() && it != "null" }
        ?: row.optString("time").takeIf { it != "null" }.orEmpty()
    if (raw.isEmpty()) return ""
    // Python emits UTC ISO strings, optionally with microseconds. Normalize the
    // fraction to milliseconds and offset to RFC 822 for Android API 24 parsing.
    val iso = Regex("^(\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2})(?:\\.(\\d+))?(Z|[+-]\\d{2}:?\\d{2})$")
        .matchEntire(raw) ?: return raw.replace("T", " ").take(16)
    val fraction = iso.groupValues[2].padEnd(3, '0').take(3)
    val offset = if (iso.groupValues[3] == "Z") "+0000" else iso.groupValues[3].replace(":", "")
    val normalized = "${iso.groupValues[1]}.$fraction$offset"
    val parser = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss.SSSZ", Locale.US).apply {
        isLenient = false
        timeZone = TimeZone.getTimeZone("UTC")
    }
    val position = ParsePosition(0)
    val parsed = parser.parse(normalized, position)
        ?.takeIf { position.index == normalized.length }
        ?: return raw.replace("T", " ").take(16)
    return SimpleDateFormat("yyyy-MM-dd HH:mm", Locale.US).apply {
        timeZone = TimeZone.getTimeZone("Asia/Shanghai")
    }.format(parsed) + "（北京时间）"
}

/** Each replacement query invalidates every earlier response, even if cancellation arrives late. */
internal class CommentRequestGeneration {
    var current: Int = 0
        private set
    fun next(): Int = ++current
    fun accepts(generation: Int): Boolean = generation == current
}
