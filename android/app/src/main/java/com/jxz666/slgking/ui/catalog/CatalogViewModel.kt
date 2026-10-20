package com.jxz666.slgking.ui.catalog

import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.jxz666.slgking.bridge.SlgApi
import com.jxz666.slgking.bridge.SlgApiException
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Job
import kotlinx.coroutines.ensureActive
import kotlin.coroutines.coroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

class CatalogViewModel : ViewModel() {

    var query by androidx.compose.runtime.mutableStateOf(CatalogQuery())
        private set
    var items by androidx.compose.runtime.mutableStateOf(listOf<com.jxz666.slgking.bridge.GameItem>())
        private set
    var total by androidx.compose.runtime.mutableStateOf(0)
        private set
    var loading by androidx.compose.runtime.mutableStateOf(false)
        private set
    var loadingMore by androidx.compose.runtime.mutableStateOf(false)
        private set
    var error by androidx.compose.runtime.mutableStateOf<String?>(null)
        private set
    var endReached by androidx.compose.runtime.mutableStateOf(false)
        private set
    var filterHitCount by androidx.compose.runtime.mutableStateOf<Int?>(null)
        private set

    private data class SavedCatalog(val query: CatalogQuery, val items: List<com.jxz666.slgking.bridge.GameItem>, val total: Int, val page: Int, val endReached: Boolean, val error: String?)
    private val savedCatalogs = mutableMapOf<String, SavedCatalog>()
    val scrollPositions = mutableMapOf<String, Pair<Int, Int>>()
    private var cacheRevision = com.jxz666.slgking.bridge.CatalogChanges.revision

    fun invalidateChangedCatalogs() {
        val next = com.jxz666.slgking.bridge.CatalogChanges.revision
        if (next == cacheRevision) return
        savedCatalogs.replaceAll { _, saved -> saved.copy(items = emptyList()) }
        cacheRevision = next
        refresh()
    }

    fun selectCatalog(id: String) {
        invalidateChangedCatalogs()
        if (id == query.catalogId) return
        savedCatalogs[query.catalogId] = SavedCatalog(query, items, total, page, endReached, error)
        generation++
        loadJob?.cancel()
        moreJob?.cancel()
        cancelPreview()
        loading = false
        loadingMore = false
        val saved = savedCatalogs[id]
        query = saved?.query ?: CatalogQuery(catalogId = id, sort = if (id == "western_slg") "score" else if (id == "dlsite") "updated" else "title")
        items = saved?.items ?: emptyList()
        total = saved?.total ?: 0
        page = saved?.page ?: 1
        endReached = saved?.endReached ?: false
        error = saved?.error
        if (saved == null || items.isEmpty()) loadFirst()
    }

    private var page = 1
    private val pageSize = 30
    private var loadJob: Job? = null
    private var searchJob: Job? = null
    private var moreJob: Job? = null
    private var generation = 0
    private var previewGeneration = 0

    fun setQuery(transform: (CatalogQuery) -> CatalogQuery) {
        val next = transform(query)
        if (next == query) return
        query = next
        loadFirst()
    }

    fun setQueryQuiet(transform: (CatalogQuery) -> CatalogQuery) {
        // 筛选抽屉内编辑中：只更新状态不拉首页
        query = transform(query)
    }

    fun loadFirst() {
        val request = ++generation
        val snapshot = query
        loadJob?.cancel()
        moreJob?.cancel()
        loadingMore = false
        page = 1
        endReached = false
        loading = true
        error = null
        loadJob = viewModelScope.launch {
            try {
                val result = fetch(1, snapshot)
                coroutineContext.ensureActive()
                if (request != generation || snapshot != query) return@launch
                items = result.items
                total = result.total
                page = result.page
                endReached = items.size >= total || result.items.isEmpty()
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                if (request == generation) error = e.message ?: e.javaClass.simpleName
            } finally {
                if (request == generation) loading = false
            }
        }
    }

    fun loadMore() {
        if (loading || loadingMore || endReached || error != null) return
        val request = generation
        val snapshot = query
        val nextPage = page + 1
        loadingMore = true
        moreJob = viewModelScope.launch {
            try {
                val result = fetch(nextPage, snapshot)
                coroutineContext.ensureActive()
                if (request != generation || snapshot != query) return@launch
                page = result.page
                total = result.total
                val existing = items.map { it.id }.toSet()
                items = items + result.items.filter { it.id !in existing }
                endReached = items.size >= total || result.items.isEmpty()
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                if (request == generation) error = e.message ?: e.javaClass.simpleName
            } finally {
                if (request == generation) loadingMore = false
            }
        }
    }

    fun refresh() = loadFirst()

    fun clearError() {
        error = null
    }

    fun updateItem(item: com.jxz666.slgking.bridge.GameItem) {
        items = items.map { if (it.id == item.id) item else it }
        savedCatalogs.keys.toList().forEach { id ->
            val saved = savedCatalogs.getValue(id)
            savedCatalogs[id] = saved.copy(items = saved.items.map { if (it.id == item.id) item else it })
        }
    }

    fun removeItem(gameId: Int) {
        items = items.filter { it.id != gameId }
        total = (total - 1).coerceAtLeast(0)
        savedCatalogs.keys.toList().forEach { id ->
            val saved = savedCatalogs.getValue(id)
            val remaining = saved.items.filter { it.id != gameId }
            savedCatalogs[id] = saved.copy(items = remaining, total = (saved.total - (saved.items.size - remaining.size)).coerceAtLeast(0))
        }
    }

    /** 筛选抽屉的实时命中数（防抖）。 */
    fun previewHits(next: CatalogQuery) {
        if (CatalogDatePolicy.error(next.datePreset, next.dateStart, next.dateEnd) != null) { cancelPreview(); return }
        val request = ++previewGeneration
        searchJob?.cancel()
        filterHitCount = null
        searchJob = viewModelScope.launch {
            delay(250)
            try {
                val result = fetch(1, next, 1)
                coroutineContext.ensureActive()
                if (request == previewGeneration) filterHitCount = result.total
            } catch (e: CancellationException) {
                throw e
            } catch (_: Exception) {
                if (request == previewGeneration) filterHitCount = null
            }
        }
    }

    fun cancelPreview() {
        previewGeneration++
        searchJob?.cancel()
        filterHitCount = null
    }

    private suspend fun fetch(pageToLoad: Int, snapshot: CatalogQuery, size: Int = pageSize) = SlgApi.catalogList(
        catalogId = snapshot.catalogId,
        datePreset = snapshot.datePreset,
        dateStart = snapshot.dateStart.takeIf { snapshot.datePreset == "custom" },
        dateEnd = snapshot.dateEnd.takeIf { snapshot.datePreset == "custom" },
        hiddenMode = snapshot.hiddenMode,
        languages = snapshot.languages.toList(),
        workTypes = snapshot.workTypes.toList(),
        view = snapshot.view,
        statuses = snapshot.statuses.toList(),
        q = snapshot.q.ifBlank { null },
        tags = snapshot.tags.toList(),
        exclude = snapshot.exclude.toList(),
        sort = snapshot.sort,
        desc = snapshot.desc,
        collectionId = snapshot.collectionId,
        page = pageToLoad,
        pageSize = size,
        categoryIds = if (snapshot.view == "user") snapshot.categoryIds.toList() else emptyList(),
        uncategorized = snapshot.view == "user" && snapshot.uncategorized,
    )
}
