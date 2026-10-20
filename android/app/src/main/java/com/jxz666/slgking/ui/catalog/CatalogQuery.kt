package com.jxz666.slgking.ui.catalog

/** 目录查询（对齐桥 catalog.list）。 */
data class CatalogQuery(
    val datePreset: String = "all",
    val dateStart: String? = null,
    val dateEnd: String? = null,
    val hiddenMode: String = "exclude",
    val catalogId: String = "western_slg",
    val languages: Set<String> = emptySet(),
    val workTypes: Set<String> = emptySet(),
    val view: String = "all",
    val statuses: Set<String> = emptySet(),
    val q: String = "",
    val tags: Set<String> = emptySet(),
    val exclude: Set<String> = emptySet(),
    val sort: String = "score",
    val desc: Boolean = true,
    val collectionId: Int? = null,
    val categoryIds: Set<String> = emptySet(),
    val uncategorized: Boolean = false,
)

internal object CatalogDatePolicy {
    val presets = listOf("all" to "不限", "7" to "近7天", "30" to "近30天", "90" to "近90天", "custom" to "自定义")
    fun error(preset: String, start: String?, end: String?): String? {
        if (preset !in presets.map { it.first }) return "未知日期范围"
        if (preset != "custom") return null
        val format = java.text.SimpleDateFormat("yyyy-MM-dd", java.util.Locale.ROOT).apply { isLenient = false }
        fun valid(value: String?): Boolean {
            if (value == null || !value.matches(Regex("[0-9]{4}-[0-9]{2}-[0-9]{2}"))) return false
            return try { format.parse(value) != null } catch (_: Exception) { false }
        }
        if (!valid(start) || !valid(end)) return "请输入有效开始与结束日期（YYYY-MM-DD）"
        if (start!! > end!!) return "开始日期不能晚于结束日期"
        return null
    }
}
