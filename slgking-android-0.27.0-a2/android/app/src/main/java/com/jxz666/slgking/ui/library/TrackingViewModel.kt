package com.jxz666.slgking.ui.library

import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.jxz666.slgking.bridge.CollectionRow
import com.jxz666.slgking.bridge.SlgApi
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Job
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.launch
import kotlin.coroutines.coroutineContext

/** Section requests survive rotation and never block another section. */
internal class TrackingViewModel : ViewModel() {
    var updates by mutableStateOf(emptyList<SlgApi.WishlistChange>())
        private set
    var updatesLoading by mutableStateOf(false)
        private set
    var updatesLoaded by mutableStateOf(false)
        private set
    var updatesError by mutableStateOf<String?>(null)
        private set
    var markError by mutableStateOf<String?>(null)
        private set
    var collections by mutableStateOf(emptyList<CollectionRow>())
        private set
    var collectionsLoading by mutableStateOf(false)
        private set
    var collectionsLoaded by mutableStateOf(false)
        private set
    var collectionsError by mutableStateOf<String?>(null)
        private set
    var marking by mutableStateOf(emptySet<Int>())
        private set
    private val updatesGate = TrackingRequestGate()
    private val collectionsGate = TrackingRequestGate()
    private val markGate = TrackingMarkGate()
    private var updatesJob: Job? = null
    private var collectionsJob: Job? = null
    private var catalogRevision = com.jxz666.slgking.bridge.CatalogChanges.revision

    fun ensureLoaded() {
        val nextRevision = com.jxz666.slgking.bridge.CatalogChanges.revision
        if (nextRevision != catalogRevision) {
            catalogRevision = nextRevision
            refreshUpdates()
            refreshCollections()
            return
        }
        if (!updatesLoaded && !updatesLoading && updatesError == null) refreshUpdates()
        if (!collectionsLoaded && !collectionsLoading && collectionsError == null) refreshCollections()
    }

    fun refreshUpdates() {
        markError = null
        loadUpdates()
    }

    private fun loadUpdates() {
        // A fetch begun before markSeen completed could resurrect an already-read row.
        if (marking.isNotEmpty()) return
        updatesJob?.cancel()
        val request = updatesGate.begin()
        updatesLoading = true
        updatesError = null
        updatesJob = viewModelScope.launch {
            try {
                val result = SlgApi.wishlistUpdates()
                coroutineContext.ensureActive()
                if (updatesGate.owns(request)) {
                    updates = result
                    updatesLoaded = true
                }
            } catch (e: CancellationException) { throw e
            } catch (e: Exception) {
                if (updatesGate.owns(request)) updatesError = e.message ?: "加载版本更新失败"
            } finally {
                if (updatesGate.owns(request)) updatesLoading = false
            }
        }
    }

    fun refreshCollections() {
        collectionsJob?.cancel()
        val request = collectionsGate.begin()
        collectionsLoading = true
        collectionsError = null
        collectionsJob = viewModelScope.launch {
            try {
                val result = SlgApi.collections()
                coroutineContext.ensureActive()
                if (collectionsGate.owns(request)) {
                    collections = result
                    collectionsLoaded = true
                }
            } catch (e: CancellationException) { throw e
            } catch (e: Exception) {
                if (collectionsGate.owns(request)) collectionsError = e.message ?: "加载收藏夹失败"
            } finally {
                if (collectionsGate.owns(request)) collectionsLoading = false
            }
        }
    }

    fun markSeen(change: SlgApi.WishlistChange) {
        if (!markGate.begin(change.id)) return
        marking = markGate.snapshot()
        markError = null
        updatesGate.invalidate()
        updatesJob?.cancel()
        updatesLoading = false
        viewModelScope.launch {
            try {
                SlgApi.wishlistMarkSeen(change.id, change.version)
                coroutineContext.ensureActive()
                updates = updates.filterNot { it.id == change.id }
                updatesError = null
            } catch (e: CancellationException) { throw e
            } catch (e: Exception) {
                markError = e.message ?: "标为已读失败，请重试"
            } finally {
                markGate.finish(change.id)
                marking = markGate.snapshot()
                if (marking.isEmpty()) loadUpdates()
            }
        }
    }
}
