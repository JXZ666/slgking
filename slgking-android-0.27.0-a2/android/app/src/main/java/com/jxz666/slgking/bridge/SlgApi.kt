package com.jxz666.slgking.bridge

import org.json.JSONObject

/**
 * 桥的 typed 门面。全部方法挂起、在 IO 执行；失败抛 SlgApiException(error 文案)。
 * UI 层捕获 SlgApiException 呈现中文错误。
 */
class SlgApiException(message: String) : Exception(message)

open class SlgApiResult protected constructor(val json: JSONObject) {
    val ok: Boolean get() = json.optBoolean("ok", false)
    val error: String get() = json.optString("error", "未知错误")
}

object SlgApi {
    private val completedSyncs = mutableSetOf<String>()

    private suspend fun call(method: String, params: JSONObject = JSONObject()): JSONObject {
        val r = PythonBridge.call(method, params)
        if (!r.optBoolean("ok", false)) throw SlgApiException(r.optString("error", "未知错误"))
        if (method in setOf("game.rate", "game.state", "game.note", "game.collections.set", "usergames.add", "usergames.update", "usergames.delete", "backup.import")) CatalogChanges.changed()
        if (method == "sync.poll" && !r.optBoolean("running") && r.optBoolean("catalog_updated") && completedSyncs.add(r.optString("job"))) CatalogChanges.changed()
        return r
    }

    /** 通用透传：云端结构变化频繁的端点直接拿原始 JSON。 */
    suspend fun raw(method: String, params: JSONObject = JSONObject()): JSONObject =
        call(method, params)

    data class AccountStatus(val loggedIn: Boolean, val accountId: String?)

    suspend fun accountStatus(): AccountStatus {
        val r = call("account.status")
        return AccountStatus(
            r.optBoolean("logged_in"),
            r.optString("account_id").takeIf { it.isNotBlank() && it != "null" })
    }

    suspend fun accountLogin(loginKey: String, recoveryCode: String?): JSONObject =
        call("account.login", JSONObject()
            .put("login_key", loginKey)
            .put("recovery_code", recoveryCode ?: ""))

    suspend fun accountCreate(): JSONObject = call("account.create")

    suspend fun accountLogout() {
        call("account.logout")
    }

    suspend fun accountMe(): JSONObject = call("account.me").getJSONObject("me")

    suspend fun commentsPage(slug: String, page: Int, sort: String = "latest"): JSONObject =
        call("comments.page", JSONObject()
            .put("slug", slug).put("page", page).put("sort", sort))

    suspend fun commentPost(slug: String, content: String, public: Boolean): JSONObject =
        call("comments.post", JSONObject()
            .put("slug", slug).put("content", content).put("public", public))

    suspend fun commentVote(id: String, value: Int): JSONObject =
        call("comments.vote", JSONObject().put("id", id).put("value", value)).getJSONObject("vote")

    suspend fun commentReport(id: String) {
        call("comments.report", JSONObject().put("id", id))
    }

    suspend fun commentDeleteCloud(id: String) {
        call("comments.cloud_delete", JSONObject().put("id", id))
    }

    suspend fun commentDeleteLocal(id: Long) {
        call("comments.local_delete", JSONObject().put("id", id))
    }

    suspend fun commentsMine(): org.json.JSONArray =
        call("comments.mine").optJSONArray("comments") ?: org.json.JSONArray()

    suspend fun backupExport(): JSONObject = call("backup.export")

    suspend fun backupImport(path: String): JSONObject =
        call("backup.import", JSONObject().put("path", path))

    suspend fun backupPreview(path: String): JSONObject =
        call("backup.preview", JSONObject().put("path", path))

    suspend fun updateCheck(localVersion: String, force: Boolean = false): JSONObject? =
        call("update.check", JSONObject()
            .put("local_version", localVersion).put("force", force))
            .optJSONObject("release")

    suspend fun translateSettings(): JSONObject = call("translate.settings")

    suspend fun translateSave(fields: JSONObject): JSONObject =
        call("translate.save", fields)

    suspend fun diagnose(): org.json.JSONArray =
        call("diagnose.run").optJSONArray("diagnostics") ?: org.json.JSONArray()

    suspend fun remoteConfig(): JSONObject =
        call("remote.config").optJSONObject("config") ?: JSONObject()

    suspend fun jobPoll(jobId: String): JSONObject =
        call("job.poll", JSONObject().put("job", jobId))

    // --- 想玩版本提醒 / 反馈 / 邀请 / 维护补偿 ---

    data class WishlistChange(
        val id: Int, val slug: String, val title: String,
        val seenVersion: String?, val version: String?, val lastUpdated: String?,
    )

    suspend fun wishlistUpdates(): List<WishlistChange> {
        val arr = call("wishlist.updates").optJSONArray("changes") ?: org.json.JSONArray()
        return (0 until arr.length()).map {
            val o = arr.getJSONObject(it)
            WishlistChange(
                id = o.getInt("id"), slug = o.getString("slug"),
                title = o.getString("title"),
                seenVersion = o.optString("seen_version").takeIf { it.isNotBlank() && it != "null" },
                version = o.optString("version").takeIf { it.isNotBlank() && it != "null" },
                lastUpdated = o.optString("last_updated").takeIf { it.isNotBlank() && it != "null" },
            )
        }
    }

    suspend fun wishlistMarkSeen(id: Int, version: String? = null) {
        val p = JSONObject().put("id", id)
        version?.let { p.put("version", it) }
        call("wishlist.mark.seen", p)
    }

    suspend fun feedbackSubmit(category: String, content: String) {
        call("feedback.submit", JSONObject()
            .put("category", category).put("content", content))
    }

    suspend fun referralMe(): JSONObject =
        call("referral.me").getJSONObject("referral")

    suspend fun referralRedeem(code: String) {
        call("referral.redeem", JSONObject().put("code", code))
    }

    suspend fun maintenanceRewardStatus(): JSONObject =
        call("maintenance_reward.status").getJSONObject("status")

    suspend fun maintenanceRewardClaim(campaignId: String) {
        call("maintenance_reward.claim", JSONObject().put("campaign_id", campaignId))
    }

    suspend fun stats(): Stats =
        Stats.parse(call("stats")) ?: throw SlgApiException("stats 解析失败")

    data class CatalogPage(val items: List<GameItem>, val total: Int, val page: Int)

    suspend fun catalogList(
        view: String = "all",
        q: String? = null,
        tags: List<String> = emptyList(),
        exclude: List<String> = emptyList(),
        sort: String = "score",
        desc: Boolean = true,
        collectionId: Int? = null,
        page: Int = 1,
        pageSize: Int = 30,
        categoryIds: List<String> = emptyList(),
        uncategorized: Boolean = false,
        catalogId: String = "western_slg",
        languages: List<String> = emptyList(),
        workTypes: List<String> = emptyList(),
    ): CatalogPage {
        val p = JSONObject()
            .put("view", view)
            .put("sort", sort)
            .put("desc", desc)
            .put("page", page)
            .put("page_size", pageSize)
            .put("catalog_id", catalogId)
            .put("languages", org.json.JSONArray(languages))
            .put("work_types", org.json.JSONArray(workTypes))
        q?.let { p.put("q", it) }
        val r = call("catalog.list", p.apply {
            if (tags.isNotEmpty()) put("tags", org.json.JSONArray(tags))
            if (exclude.isNotEmpty()) put("exclude", org.json.JSONArray(exclude))
            collectionId?.let { put("collection_id", it) }
            if (categoryIds.isNotEmpty()) put("category_ids", org.json.JSONArray(categoryIds))
            put("uncategorized", uncategorized)
        })
        val arr = r.optJSONArray("items") ?: org.json.JSONArray()
        return CatalogPage(
            items = (0 until arr.length()).map { arr.getJSONObject(it).optGameItem() },
            total = r.optInt("total"),
            page = r.optInt("page", page),
        )
    }

    suspend fun gameGet(id: Int): GameDetail {
        val r = call("game.get", JSONObject().put("id", id))
        val g = r.getJSONObject("game")
        val item = g.optGameItem()
        val tagsArr = g.optJSONArray("tags") ?: org.json.JSONArray()
        val collections = g.optJSONArray("collections") ?: org.json.JSONArray()
        return GameDetail(
            item = item,
            overview = g.optString("overview").takeIf { it.isNotBlank() && it != "null" },
            url = g.optString("url").takeIf { it.isNotBlank() && it != "null" },
            origin = g.optString("origin", "site"),
            promoted = g.optBoolean("promoted"),
            siteViews = if (g.isNull("site_views")) null else g.optInt("site_views"),
            siteLikes = if (g.isNull("site_likes")) null else g.optInt("site_likes"),
            siteComments = if (g.isNull("site_comments")) null else g.optInt("site_comments"),
            note = g.optString("note").takeIf { it.isNotBlank() && it != "null" },
            tags = (0 until tagsArr.length()).map {
                val t = tagsArr.getJSONObject(it)
                TagRow(t.optString("name"), t.optString("zh", t.optString("name")))
            },
            collectionIds = (0 until collections.length()).map { collections.getInt(it) },
            sources = g.optJSONArray("sources")?.let { a -> (0 until a.length()).map {
                val s = a.getJSONObject(it)
                GameSource(s.getString("source_id"), s.getString("label"), s.getString("url"))
            } } ?: emptyList(),
            dlsiteFacts = g.optJSONObject("dlsite_facts"),
        )
    }

    suspend fun rate(id: Int, stars: Int) {
        call("game.rate", JSONObject().put("id", id).put("stars", stars))
    }

    suspend fun coverGet(id: Int, retry: Boolean = false): String? = call("cover.get", JSONObject().put("id", id).put("retry", retry))
        .optString("path").takeIf { it.isNotBlank() && it != "null" }

    suspend fun setState(id: Int, status: String?) {
        call("game.state", JSONObject().put("id", id).put("status", status ?: ""))
    }

    suspend fun setNote(id: Int, note: String) {
        call("game.note", JSONObject().put("id", id).put("note", note))
    }

    suspend fun collections(): List<CollectionRow> {
        val r = call("collections.list")
        val arr = r.optJSONArray("collections") ?: org.json.JSONArray()
        return (0 until arr.length()).map {
            val c = arr.getJSONObject(it)
            CollectionRow(c.getInt("id"), c.getString("name"), c.getInt("count"))
        }
    }

    suspend fun collectionSave(name: String, id: Int? = null): Int {
        val p = JSONObject().put("name", name)
        id?.let { p.put("id", it) }
        return call("collection.save", p).getInt("id")
    }

    suspend fun collectionDelete(id: Int) {
        call("collection.delete", JSONObject().put("id", id))
    }

    suspend fun setGameCollections(id: Int, collectionIds: List<Int>) {
        call("game.collections.set", JSONObject()
            .put("id", id)
            .put("collection_ids", org.json.JSONArray(collectionIds)))
    }

    suspend fun tags(limit: Int = 500, catalogId: String = "western_slg"): List<TagRow> {
        val r = call("tags.list", JSONObject().put("limit", limit).put("catalog_id", catalogId))
        val arr = r.optJSONArray("tags") ?: org.json.JSONArray()
        return (0 until arr.length()).map {
            val t = arr.getJSONObject(it)
            TagRow(t.getString("name"), t.getString("zh"), t.getInt("count"))
        }
    }

    suspend fun userGameAdd(title: String, developer: String? = null, engine: String? = null,
                            version: String? = null, overview: String? = null,
                            categories: List<String> = emptyList()): Int =
        call("usergames.add", JSONObject()
            .put("title", title)
            .put("developer", developer ?: JSONObject.NULL)
            .put("engine", engine ?: JSONObject.NULL)
            .put("version", version ?: JSONObject.NULL)
            .put("overview", overview ?: JSONObject.NULL)
            .put("categories", org.json.JSONArray(categories))).getInt("id")

    suspend fun userGameUpdate(id: Int, title: String, developer: String? = null,
                               engine: String? = null, version: String? = null,
                               overview: String? = null, categories: List<String>? = null) {
        call("usergames.update", JSONObject()
            .put("id", id)
            .put("title", title)
            .put("developer", developer ?: JSONObject.NULL)
            .put("engine", engine ?: JSONObject.NULL)
            .put("version", version ?: JSONObject.NULL)
            .apply {
                overview?.let { put("overview", it) }
                categories?.let { put("categories", org.json.JSONArray(it)) }
            })
    }

    suspend fun userGameDelete(id: Int) {
        call("usergames.delete", JSONObject().put("id", id))
    }

    // --- 同步 ---

    private fun parseJob(r: JSONObject) = SyncJob(
        id = r.optString("job"),
        running = r.optBoolean("running"),
        text = r.optString("text").takeIf { it.isNotBlank() },
        progress = r.optJSONArray("progress")?.let { a ->
            if (a.length() >= 2) intArrayOf(a.getInt(0), a.getInt(1)) else null
        },
        summary = r.optJSONObject("summary"),
        error = r.optString("error").takeIf { it.isNotBlank() && it != "null" },
        cancelRequested = r.optBoolean("cancel_requested"),
        cancelled = r.optBoolean("cancelled"),
        catalogUpdated = r.optBoolean("catalog_updated"),
    )

    suspend fun syncStart(catalogId: String = "western_slg"): SyncJob =
        parseJob(call("sync.start", JSONObject().put("catalog_id", catalogId)))
    suspend fun syncPoll(job: String): SyncJob = parseJob(call("sync.poll", JSONObject().put("job", job)))
    suspend fun syncCancel(job: String) {
        call("sync.cancel", JSONObject().put("job", job))
    }
}
