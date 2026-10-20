package com.jxz666.slgking.bridge

import org.json.JSONObject
import androidx.compose.runtime.getValue
import androidx.compose.runtime.setValue

object AccountChanges {
    var revision by androidx.compose.runtime.mutableIntStateOf(0)
        private set
    fun changed() { revision++ }
}

object CatalogChanges {
    var revision by androidx.compose.runtime.mutableIntStateOf(0)
        private set
    fun changed() { revision++ }
}

/** stats 端点结果。 */
data class Stats(
    val games: Int,
    val tags: Int,
    val downloaded: Int,
    val rated: Int,
    val lastSync: String?,
    val collections: Int,
    val seedInstalled: Boolean,
) {
    companion object {
        fun parse(o: JSONObject): Stats? = if (o.optBoolean("ok")) Stats(
            games = o.optInt("games"),
            tags = o.optInt("tags"),
            downloaded = o.optInt("downloaded"),
            rated = o.optInt("rated"),
            lastSync = o.optString("last_sync").takeIf { it.isNotBlank() && it != "null" },
            collections = o.optInt("collections"),
            seedInstalled = o.optBoolean("seed_installed"),
        ) else null
    }
}

/** 目录卡片条目。 */
data class GameItem(
    val id: Int,
    val slug: String,
    val title: String,
    val titleZh: String?,
    val rating: Double?,
    val status: String?,
    val myRating: Int?,
    val version: String?,
    val developer: String?,
    val engine: String?,
    val cover: String?,
    val lastUpdated: String?,
    val categories: List<String> = emptyList(),
    val catalogId: String = "western_slg",
    val ratingCount: Int? = null,
    val ratingScaleMax: Double? = null,
    val coverFile: String? = null,
    /** 仅当存在个人封面 override 时，`cover` 才是个人封面；否则它是旧服务器缓存。 */
    val hasCoverOverride: Boolean = false,
    /** 官方封面直连 URL（目录 API 的 cover_url）；为空时仍走旧服务器 cover。 */
    val coverUrl: String? = null,
) {
    val displayTitle: String get() = titleZh ?: title
}

/** 详情页数据。 */
data class GameDetail(
    val item: GameItem,
    val overview: String?,
    val url: String?,
    val origin: String,
    val promoted: Boolean,
    val siteViews: Int?,
    val siteLikes: Int?,
    val siteComments: Int?,
    val note: String?,
    val tags: List<TagRow>,
    val collectionIds: List<Int>,
    val sources: List<GameSource> = emptyList(),
    val dlsiteFacts: JSONObject? = null,
    val overviewZh: String? = null,
    val titleTranslationState: String? = null,
    val overviewTranslationState: String? = null,
    val hidden: Boolean = false,
)

data class GameSource(val sourceId: String, val label: String, val url: String)

data class TagRow(val name: String, val zh: String, val count: Int = 0) {
    val display: String get() = zh
}

data class CollectionRow(val id: Int, val name: String, val count: Int)

data class SyncJob(
    val id: String,
    val running: Boolean,
    val text: String?,
    val progress: IntArray?,
    val summary: JSONObject?,
    val error: String?,
    val cancelRequested: Boolean = false,
    val cancelled: Boolean = false,
    val catalogUpdated: Boolean = false,
    val stage: JSONObject? = null,
)

internal fun JSONObject.optGameItem(): GameItem {
    val titleZh = optString("title_zh")
    return GameItem(
        id = optInt("id"),
        slug = optString("slug"),
        title = optString("title"),
        titleZh = titleZh.takeIf { it.isNotBlank() && it != "null" },
        rating = if (isNull("rating")) null else optDouble("rating"),
        status = optString("status").takeIf { it.isNotBlank() && it != "null" },
        myRating = if (isNull("my_rating")) null else optInt("my_rating"),
        version = optString("version").takeIf { it.isNotBlank() && it != "null" },
        developer = optString("developer").takeIf { it.isNotBlank() && it != "null" },
        engine = optString("engine").takeIf { it.isNotBlank() && it != "null" },
        cover = optString("cover").takeIf { it.isNotBlank() && it != "null" },
        lastUpdated = optString("last_updated").takeIf { it.isNotBlank() && it != "null" },
        categories = optJSONArray("categories")?.let { arr ->
            (0 until arr.length()).map { arr.getString(it) }
        } ?: emptyList(),
        catalogId = optString("catalog_id", "western_slg"),
        ratingCount = if (isNull("rating_count")) null else optInt("rating_count"),
        ratingScaleMax = if (isNull("rating_scale_max")) null else optDouble("rating_scale_max"),
        coverFile = optString("cover_file").takeIf { it.isNotBlank() && it != "null" },
        hasCoverOverride = optBoolean("has_cover_override"),
        coverUrl = optString("cover_url").takeIf { it.isNotBlank() && it != "null" },
    )
}
