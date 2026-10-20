package com.jxz666.slgking.ui.game

/** Pure packing rule shared with tests; rows use actual measured chip width, including long labels. */
internal fun tagRows(widths: List<Int>, available: Int, gap: Int): List<List<Int>> {
    val rows = mutableListOf<MutableList<Int>>()
    var used = 0
    widths.forEachIndexed { index, raw ->
        val width = raw.coerceAtMost(available).coerceAtLeast(0)
        if (rows.isEmpty() || (rows.last().isNotEmpty() && used + gap + width > available)) {
            rows.add(mutableListOf()); used = 0
        }
        if (rows.last().isNotEmpty()) used += gap
        rows.last().add(index); used += width
    }
    return rows
}

internal fun translationFields(title: String, titleZh: String?, titleState: String?,
    overview: String?, overviewZh: String?, overviewState: String?): List<String> = buildList {
    if (title.isNotBlank() && titleZh.isNullOrBlank() &&
        titleState != "untranslatable") add("title")
    if (!overview.isNullOrBlank() && overviewZh.isNullOrBlank() &&
        overviewState != "untranslatable") add("overview")
}
