package com.jxz666.slgking.ui.library

import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.rememberScrollState
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.automirrored.filled.KeyboardArrowRight
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.Edit
import androidx.compose.material.icons.filled.VideoLibrary
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items as gridItems
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.ExtendedFloatingActionButton
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.jxz666.slgking.bridge.CollectionRow
import com.jxz666.slgking.bridge.GameItem
import com.jxz666.slgking.bridge.SlgApi
import com.jxz666.slgking.bridge.SlgApiException
import kotlinx.coroutines.ensureActive
import kotlin.coroutines.coroutineContext
import com.jxz666.slgking.bridge.CatalogChanges
import com.jxz666.slgking.ui.catalog.CatalogDatePolicy
import kotlinx.coroutines.launch
import kotlinx.coroutines.CancellationException
import androidx.lifecycle.viewmodel.compose.viewModel
import com.jxz666.slgking.ui.catalog.CatalogViewModel
import com.jxz666.slgking.ui.catalog.CategoryChips
import com.jxz666.slgking.ui.catalog.categorySummary

/** 手机追踪：版本更新、想玩与收藏；自建游戏保留在次级个人记录。 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun LibraryScreen(openDetail: (Int) -> Unit, scrollResetEpoch: Int = 0) {
    val tracking: TrackingViewModel = viewModel(key = "library-tracking")
    val userVm: CatalogViewModel = viewModel(key = "library-user-games")
    val userGames = userVm.items
    val wantVm: CatalogViewModel = viewModel(key = "library-want-games")
    var recordsOpen by androidx.compose.runtime.saveable.rememberSaveable { mutableStateOf(false) }
    var selectedTab by androidx.compose.runtime.saveable.rememberSaveable { mutableStateOf(0) }
    val updatesScroll = androidx.compose.foundation.lazy.rememberLazyListState()
    val wantsScroll = androidx.compose.foundation.lazy.rememberLazyListState()
    val collectionsScroll = androidx.compose.foundation.lazy.rememberLazyListState()
    val recordsScroll = androidx.compose.foundation.lazy.rememberLazyListState()
    com.jxz666.slgking.ui.common.ConsumeScrollReset(scrollResetEpoch) {
        updatesScroll.scrollToItem(0)
        wantsScroll.scrollToItem(0)
        collectionsScroll.scrollToItem(0)
        recordsScroll.scrollToItem(0)
    }
    var deleting by remember { mutableStateOf(setOf<Int>()) }
    var deleteTarget by remember { mutableStateOf<GameItem?>(null) }
    var recordActionError by remember { mutableStateOf<String?>(null) }
    var tagFilter by androidx.compose.runtime.saveable.rememberSaveable { mutableStateOf("") }
    var openCollectionId by androidx.compose.runtime.saveable.rememberSaveable { mutableStateOf<Int?>(null) }
    var openCollectionName by androidx.compose.runtime.saveable.rememberSaveable { mutableStateOf("") }
    var openCollectionCount by androidx.compose.runtime.saveable.rememberSaveable { mutableStateOf(0) }
    var addGameOpen by remember { mutableStateOf(false) }
    var editGame by remember { mutableStateOf<GameItem?>(null) }
    var manageOpen by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()

    fun reload() {
        recordActionError = null
        userVm.refresh()
        wantVm.refresh()
        tracking.refreshCollections()
        tracking.refreshUpdates()
    }

    LaunchedEffect(Unit) {
        // Do not reset a paginated list or record filters on detail return / rotation.
        if (userVm.query.view != "user") {
            userVm.setQueryQuiet { it.copy(view = "user", catalogId = "all") }
            userVm.refresh()
        } else userVm.invalidateChangedCatalogs()
        if (wantVm.query.view != "want") {
            wantVm.setQueryQuiet { it.copy(view = "want", catalogId = "all") }
            wantVm.refresh()
        } else wantVm.invalidateChangedCatalogs()
        tracking.ensureLoaded()
    }
    LaunchedEffect(CatalogChanges.revision) { tracking.ensureLoaded() }
    androidx.activity.compose.BackHandler(enabled = recordsOpen && openCollectionId == null) { recordsOpen = false }

    when {
        openCollectionId != null -> CollectionGamesScreen(
            collection = tracking.collections.firstOrNull { it.id == openCollectionId }
                ?: CollectionRow(openCollectionId!!, openCollectionName, openCollectionCount),
            onBack = { openCollectionId = null; tracking.refreshCollections() },
            openDetail = openDetail,
            onMembersChanged = { tracking.refreshCollections() },
            scrollResetEpoch = scrollResetEpoch,
        )
        else -> Scaffold(
            topBar = {
                Column(Modifier.fillMaxWidth().padding(horizontal = 16.dp)) {
                    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                        Text(if (recordsOpen) "我的记录" else "追踪", style = MaterialTheme.typography.titleLarge,
                            modifier = Modifier.weight(1f))
                        TextButton(modifier = Modifier.heightIn(min = 48.dp), onClick = { recordsOpen = !recordsOpen }) {
                            Text(if (recordsOpen) "返回追踪" else "我的记录")
                        }
                    }
                    Text("个人清单与版本记录，不代表本机已安装或可运行。", style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                    if (!recordsOpen) {
                        androidx.compose.material3.TabRow(selectedTabIndex = selectedTab) {
                            listOf("版本更新", "想玩", "收藏夹").forEachIndexed { index, label ->
                                val count = when (index) {
                                    0 -> trackingCountLabel(tracking.updatesLoaded, tracking.updatesLoading,
                                        tracking.updatesError, tracking.updates.size)
                                    1 -> trackingCountLabel(wantVm.endReached || wantVm.items.isNotEmpty() ||
                                        (!wantVm.loading && wantVm.error == null && wantVm.query.view == "want"),
                                        wantVm.loading, wantVm.error, wantVm.total)
                                    else -> trackingCountLabel(tracking.collectionsLoaded, tracking.collectionsLoading,
                                        tracking.collectionsError, tracking.collections.size)
                                }
                                androidx.compose.material3.Tab(selected = selectedTab == index,
                                    onClick = { selectedTab = index }, modifier = Modifier.heightIn(min = 64.dp),
                                    text = {
                                        Column(horizontalAlignment = Alignment.CenterHorizontally) {
                                            Text(label, style = MaterialTheme.typography.labelLarge,
                                                textAlign = androidx.compose.ui.text.style.TextAlign.Center)
                                            Text(count, style = MaterialTheme.typography.labelSmall)
                                        }
                                    })
                            }
                        }
                    }
                }
            },
            floatingActionButton = {
                if (recordsOpen) ExtendedFloatingActionButton(
                    onClick = { addGameOpen = true },
                    icon = { Icon(Icons.Filled.Add, null) }, text = { Text("添加游戏") },
                )
            },
        ) { padding ->
            val state = if (recordsOpen) recordsScroll else when (selectedTab) {
                0 -> updatesScroll
                1 -> wantsScroll
                else -> collectionsScroll
            }
            LazyColumn(Modifier.fillMaxSize().padding(padding), state = state,
                contentPadding = androidx.compose.foundation.layout.PaddingValues(
                    start = 16.dp, end = 16.dp, top = 8.dp, bottom = if (recordsOpen) 96.dp else 16.dp),
                verticalArrangement = Arrangement.spacedBy(8.dp)) {
                if (recordsOpen) {
                    recordActionError?.let { message -> item {
                        Text(message, color = MaterialTheme.colorScheme.error)
                        TextButton(onClick = { recordActionError = null }) { Text("关闭提示") }
                    } }
                        item {
                            Text("我添加的游戏（${userVm.total}）",
                                style = MaterialTheme.typography.titleMedium)
                        }
                        item {
                            Column {
                                Text("分类多选匹配任一分类，标签匹配全部标签", style = MaterialTheme.typography.bodySmall,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant)
                                CategoryChips(userVm.query.categoryIds) { selected ->
                                    userVm.setQuery { it.copy(categoryIds = selected, uncategorized = false) }
                                }
                                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                                    FilterChip(selected = userVm.query.uncategorized,
                                        onClick = { userVm.setQuery { it.copy(categoryIds = emptySet(), uncategorized = !it.uncategorized) } },
                                        label = { Text("未分类") })
                                    TextButton(onClick = {
                                        tagFilter = ""
                                        userVm.setQuery { it.copy(categoryIds = emptySet(), uncategorized = false, tags = emptySet()) }
                                    }) { Text("清空筛选") }
                                }
                                OutlinedTextField(value = tagFilter, onValueChange = { tagFilter = it },
                                    label = { Text("标签（用逗号分隔）") }, singleLine = true, modifier = Modifier.fillMaxWidth())
                                TextButton(onClick = {
                                    val tags = tagFilter.split(',', '，').map { it.trim() }.filter { it.isNotEmpty() }.toSet()
                                    userVm.setQuery { it.copy(tags = tags) }
                                }) { Text("应用标签") }
                            }
                        }
                        if (userVm.loading) {
                            item { CircularProgressIndicator() }
                        } else if (userVm.error != null) {
                            item {
                                Text(userVm.error ?: "加载失败", color = MaterialTheme.colorScheme.error)
                                TextButton(onClick = { userVm.refresh() }) { Text("重试") }
                            }
                        } else if (userGames.isEmpty()) {
                            item {
                                Text("没有匹配的自建游戏。可清空筛选，或点右下角「添加游戏」。",
                                    style = MaterialTheme.typography.bodySmall,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant)
                            }
                        } else {
                            items(userGames, key = { it.id }) { g ->
                                Card(Modifier.fillMaxWidth()) {
                                    Row(verticalAlignment = Alignment.CenterVertically,
                                        modifier = Modifier.padding(horizontal = 12.dp, vertical = 6.dp)) {
                                        Column(Modifier.weight(1f).clickable { openDetail(g.id) }) {
                                            Text(g.displayTitle, style = MaterialTheme.typography.bodyLarge,
                                                maxLines = 1, overflow = TextOverflow.Ellipsis)
                                            Text("分类：${categorySummary(g.categories)}",
                                                style = MaterialTheme.typography.labelMedium,
                                                color = MaterialTheme.colorScheme.onSurfaceVariant)
                                            g.developer?.let {
                                                Text(it, style = MaterialTheme.typography.labelSmall,
                                                    color = MaterialTheme.colorScheme.onSurfaceVariant)
                                            }
                                        }
                                        IconButton(onClick = { editGame = g }) {
                                            Icon(Icons.Filled.Edit, "编辑")
                                        }
                                        IconButton(enabled = g.id !in deleting, onClick = { deleteTarget = g }) {
                                            Icon(Icons.Filled.Delete, "删除")
                                        }
                                    }
                                }
                            }
                        }
                        if (!userVm.loading && userVm.error == null && !userVm.endReached) {
                            item {
                                TextButton(onClick = { userVm.loadMore() }, enabled = !userVm.loadingMore) {
                                    Text(if (userVm.loadingMore) "加载中…" else "加载更多")
                                }
                            }
                        }
                } else when (selectedTab) {
                    0 -> {
                        item {
                            TrackingSectionToolbar("版本更新", "刷新", !tracking.updatesLoading && tracking.marking.isEmpty()) {
                                tracking.refreshUpdates()
                            }
                        }
                        if (tracking.updatesLoading) item { TrackingLoading() }
                        (tracking.markError ?: tracking.updatesError)?.let { message -> item {
                            TrackingError(message, !tracking.updatesLoading && tracking.marking.isEmpty()) { tracking.refreshUpdates() }
                        } }
                        if (tracking.updatesLoaded && !tracking.updatesLoading && tracking.updatesError == null && tracking.updates.isEmpty()) item {
                            TrackingEmpty("暂无版本更新", "在目录详情加入想玩，目录更新后可在这里查看新版。")
                        }
                        items(tracking.updates, key = { "update-${it.id}" }) { change ->
                            Card(Modifier.fillMaxWidth()) {
                                Row(Modifier.fillMaxWidth().padding(12.dp), verticalAlignment = Alignment.CenterVertically,
                                    horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                                    Column(Modifier.weight(1f).heightIn(min = 48.dp).clickable { openDetail(change.id) },
                                        verticalArrangement = Arrangement.spacedBy(4.dp)) {
                                        Text(change.title, style = MaterialTheme.typography.titleSmall,
                                            maxLines = 2, overflow = TextOverflow.Ellipsis)
                                        Text(if (change.slug.startsWith("dlsite-")) "DLsite" else "SLG大类",
                                            style = MaterialTheme.typography.labelMedium,
                                            color = MaterialTheme.colorScheme.onSurfaceVariant)
                                        Text("${change.seenVersion ?: "未知版本"} → ${change.version ?: "未知版本"}",
                                            style = MaterialTheme.typography.bodySmall)
                                    }
                                    TextButton(modifier = Modifier.heightIn(min = 48.dp),
                                        enabled = change.id !in tracking.marking,
                                        onClick = { tracking.markSeen(change) }) {
                                        Text(if (change.id in tracking.marking) "处理中…" else "标为已读")
                                    }
                                }
                            }
                        }
                    }
                    1 -> {
                        item { TrackingSectionToolbar("想玩", "刷新", !wantVm.loading && !wantVm.loadingMore) { wantVm.refresh() } }
                        if (wantVm.loading) item { TrackingLoading() }
                        if (wantVm.error != null) item {
                            TrackingError(wantVm.error ?: "加载失败", !wantVm.loading && !wantVm.loadingMore) {
                                if (wantVm.items.isEmpty()) wantVm.refresh()
                                else { wantVm.clearError(); wantVm.loadMore() }
                            }
                        }
                        if (!wantVm.loading && wantVm.error == null && wantVm.items.isEmpty()) item {
                            TrackingEmpty("还没有想玩的游戏", "在目录详情点「想玩」，即可加入个人清单。")
                        }
                        items(wantVm.items, key = { "want-${it.id}" }) { game ->
                            Card(onClick = { openDetail(game.id) }, modifier = Modifier.fillMaxWidth()) {
                                Column(Modifier.fillMaxWidth().heightIn(min = 48.dp).padding(14.dp),
                                    verticalArrangement = Arrangement.spacedBy(4.dp)) {
                                    Text(game.displayTitle, style = MaterialTheme.typography.titleSmall,
                                        maxLines = 2, overflow = TextOverflow.Ellipsis)
                                    Text("${if (game.catalogId == "dlsite") "DLsite" else "SLG大类"} · ${game.version ?: "版本未知"}",
                                        style = MaterialTheme.typography.bodySmall,
                                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                                }
                            }
                        }
                        if (wantVm.loadingMore) item { TrackingLoading() }
                        if (!wantVm.loading && !wantVm.loadingMore && !wantVm.endReached && wantVm.error == null) item {
                            TextButton(modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp), onClick = { wantVm.loadMore() }) {
                                Text("加载更多想玩")
                            }
                        }
                    }
                    else -> {
                        item { TrackingSectionToolbar("收藏夹", "管理", tracking.collectionsLoaded && !tracking.collectionsLoading) { manageOpen = true } }
                        if (tracking.collectionsLoading) item { TrackingLoading() }
                        tracking.collectionsError?.let { message -> item {
                            TrackingError(message, !tracking.collectionsLoading) { tracking.refreshCollections() }
                        } }
                        if (tracking.collectionsLoaded && !tracking.collectionsLoading && tracking.collectionsError == null && tracking.collections.isEmpty()) item {
                            TrackingEmpty("还没有收藏夹", "在游戏详情页点「收藏」即可创建。")
                        }
                        items(tracking.collections, key = { "collection-${it.id}" }) { collection ->
                            Card(onClick = {
                                openCollectionId = collection.id
                                openCollectionName = collection.name
                                openCollectionCount = collection.count
                            }, modifier = Modifier.fillMaxWidth()) {
                                Row(Modifier.fillMaxWidth().heightIn(min = 72.dp).padding(14.dp),
                                    verticalAlignment = Alignment.CenterVertically,
                                    horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                                    Icon(Icons.Filled.VideoLibrary, null, tint = MaterialTheme.colorScheme.primary)
                                    Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                                        Text(collection.name, style = MaterialTheme.typography.titleSmall,
                                            maxLines = 2, overflow = TextOverflow.Ellipsis)
                                        Text("${collection.count} 款", style = MaterialTheme.typography.bodySmall,
                                            color = MaterialTheme.colorScheme.onSurfaceVariant)
                                    }
                                    Icon(Icons.AutoMirrored.Filled.KeyboardArrowRight, "进入收藏夹")
                                }
                            }
                        }
                    }
                }
            }
        }
    }


    deleteTarget?.let { target ->
        AlertDialog(onDismissRequest = { if (target.id !in deleting) deleteTarget = null },
            title = { Text("删除个人记录？") }, text = { Text("将删除「${target.displayTitle}」的自建记录。") },
            confirmButton = { TextButton(enabled = target.id !in deleting, onClick = {
                if (target.id in deleting) return@TextButton
                deleting = deleting + target.id
                scope.launch {
                    try { SlgApi.userGameDelete(target.id); deleteTarget = null; reload() }
                    catch (e: CancellationException) { throw e }
                    catch (e: Exception) { recordActionError = e.message ?: "删除失败，请重试"; deleteTarget = null }
                    finally { deleting = deleting - target.id }
                }
            }) { Text(if (target.id in deleting) "删除中…" else "删除") } },
            dismissButton = { TextButton(enabled = target.id !in deleting, onClick = { deleteTarget = null }) { Text("取消") } })
    }

    if (addGameOpen) {
        UserGameDialog(
            initial = null,
            onSaved = { addGameOpen = false; reload() },
            onDismiss = { addGameOpen = false },
        )
    }
    editGame?.let { g ->
        UserGameDialog(
            initial = g,
            onSaved = { editGame = null; reload() },
            onDismiss = { editGame = null },
        )
    }
    if (manageOpen) {
        ManageCollectionsDialog(
            collections = tracking.collections,
            onRenamed = { tracking.refreshCollections() },
            onDeleted = { tracking.refreshCollections() },
            onDismiss = { manageOpen = false },
        )
    }
}

/** Only the selected membership is removed; the game and every other collection survive. */
internal fun remainingCollectionIds(current: List<Int>, removedId: Int): List<Int> =
    current.filterNot { it == removedId }.distinct()

/** 收藏夹成员：所有目录、组合筛选与明确移出动作。 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun CollectionGamesScreen(
    collection: CollectionRow,
    onBack: () -> Unit,
    openDetail: (Int) -> Unit,
    onMembersChanged: () -> Unit,
    scrollResetEpoch: Int = 0,
) {
    val vm: CatalogViewModel = viewModel(key = "collection-games-${collection.id}")
    val gridState = androidx.compose.foundation.lazy.grid.rememberLazyGridState()
    com.jxz666.slgking.ui.common.ConsumeScrollReset(scrollResetEpoch) { gridState.scrollToItem(0) }
    val scope = rememberCoroutineScope()
    var name by androidx.compose.runtime.saveable.rememberSaveable(collection.id) { mutableStateOf(vm.query.q) }
    var tagsText by androidx.compose.runtime.saveable.rememberSaveable(collection.id) { mutableStateOf(vm.query.tags.joinToString("，")) }
    var status by androidx.compose.runtime.saveable.rememberSaveable(collection.id) { mutableStateOf(vm.query.statuses.firstOrNull() ?: "") }
    var datePreset by androidx.compose.runtime.saveable.rememberSaveable(collection.id) { mutableStateOf(vm.query.datePreset) }
    var dateStart by androidx.compose.runtime.saveable.rememberSaveable(collection.id) { mutableStateOf(vm.query.dateStart ?: "") }
    var dateEnd by androidx.compose.runtime.saveable.rememberSaveable(collection.id) { mutableStateOf(vm.query.dateEnd ?: "") }
    var filtersOpen by androidx.compose.runtime.saveable.rememberSaveable(collection.id) { mutableStateOf(false) }
    var removeTarget by remember(collection.id) { mutableStateOf<GameItem?>(null) }
    var removing by remember(collection.id) { mutableStateOf<Int?>(null) }
    var removeError by remember(collection.id) { mutableStateOf<String?>(null) }
    val nearEnd by remember {
        androidx.compose.runtime.derivedStateOf {
            val info = gridState.layoutInfo
            val last = info.visibleItemsInfo.lastOrNull()?.index ?: 0
            info.totalItemsCount > 0 && last >= info.totalItemsCount - 6
        }
    }
    LaunchedEffect(collection.id) {
        if (vm.query.view != "collection" || vm.query.collectionId != collection.id || vm.query.catalogId != "all") {
            vm.setQueryQuiet { it.copy(view = "collection", collectionId = collection.id, catalogId = "all", sort = "title", hiddenMode = "exclude") }
            vm.refresh()
        } else vm.invalidateChangedCatalogs()
    }
    LaunchedEffect(CatalogChanges.revision, collection.id) { vm.invalidateChangedCatalogs() }
    LaunchedEffect(nearEnd, vm.items.size, vm.loading) { if (nearEnd) vm.loadMore() }
    androidx.activity.compose.BackHandler(onBack = onBack)
    val dateError = CatalogDatePolicy.error(datePreset, dateStart, dateEnd)

    Scaffold(topBar = {
        androidx.compose.material3.TopAppBar(title = { Text("${collection.name}（${vm.total}）") },
            navigationIcon = { IconButton(onClick = onBack) { Icon(Icons.AutoMirrored.Filled.ArrowBack, "返回") } })
    }) { padding ->
        Column(Modifier.fillMaxSize().padding(padding)) {
            Column(Modifier.fillMaxWidth().padding(horizontal = 12.dp)) {
                OutlinedTextField(value = name, onValueChange = { name = it }, label = { Text("收藏夹内搜索名称") },
                    singleLine = true, modifier = Modifier.fillMaxWidth())
                TextButton(onClick = { filtersOpen = !filtersOpen }, modifier = Modifier.fillMaxWidth()) {
                    Text(if (filtersOpen) "收起筛选" else "状态、标签与更新日期")
                }
                Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    TextButton(modifier = Modifier.weight(1f), enabled = dateError == null, onClick = {
                        vm.setQuery { it.copy(q = name.trim(), tags = tagsText.split(',', '，').map { t -> t.trim() }.filter { t -> t.isNotEmpty() }.toSet(),
                            statuses = if (status.isBlank()) emptySet() else setOf(status), datePreset = datePreset,
                            dateStart = dateStart.takeIf { value -> datePreset == "custom" && value.isNotBlank() },
                            dateEnd = dateEnd.takeIf { value -> datePreset == "custom" && value.isNotBlank() }) }
                    }) { Text("应用筛选") }
                    TextButton(modifier = Modifier.weight(1f), onClick = {
                        name = ""; tagsText = ""; status = ""; datePreset = "all"; dateStart = ""; dateEnd = ""
                        vm.setQuery { it.copy(q = "", tags = emptySet(), statuses = emptySet(), datePreset = "all", dateStart = null, dateEnd = null) }
                    }) { Text("清空") }
                }
                if (filtersOpen) Column(Modifier.heightIn(max = 260.dp).verticalScroll(rememberScrollState())) {
                    Row(Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                        listOf("" to "全部状态", "want" to "想玩", "downloaded" to "已下载").forEach { (id, label) ->
                            FilterChip(selected = status == id, onClick = { status = id }, label = { Text(label) })
                        }
                    }
                    OutlinedTextField(value = tagsText, onValueChange = { tagsText = it }, label = { Text("标签（逗号分隔，匹配全部）") }, modifier = Modifier.fillMaxWidth())
                    Text("更新日期（北京时间自然日）", style = MaterialTheme.typography.labelMedium)
                    Row(Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                        CatalogDatePolicy.presets.forEach { (id, label) -> FilterChip(selected = datePreset == id,
                            onClick = { datePreset = id }, label = { Text(label) }) }
                    }
                    if (datePreset == "custom") {
                        Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                            OutlinedTextField(value = dateStart, onValueChange = { dateStart = it.take(10) }, label = { Text("开始 YYYY-MM-DD") },
                                singleLine = true, modifier = Modifier.weight(1f))
                            OutlinedTextField(value = dateEnd, onValueChange = { dateEnd = it.take(10) }, label = { Text("结束 YYYY-MM-DD") },
                                singleLine = true, modifier = Modifier.weight(1f))
                        }
                        dateError?.let { Text(it, color = MaterialTheme.colorScheme.error) }
                    }
                    Text("包含各目录和自建记录；指定日期时不包含更新日期未知的条目。", style = MaterialTheme.typography.bodySmall)
                }
                removeError?.let { Text(it, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall) }
            }
            Box(Modifier.fillMaxWidth().weight(1f)) {
                when {
                    vm.loading -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) { CircularProgressIndicator() }
                    vm.error != null && vm.items.isEmpty() -> com.jxz666.slgking.ui.catalog.ErrorRetry(vm.error!!) { vm.refresh() }
                    vm.items.isEmpty() -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                        Text("此收藏夹暂无匹配成员，可清空筛选。", color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                    else -> LazyVerticalGrid(state = gridState, columns = GridCells.Adaptive(160.dp),
                        horizontalArrangement = Arrangement.spacedBy(10.dp), verticalArrangement = Arrangement.spacedBy(12.dp),
                        contentPadding = androidx.compose.foundation.layout.PaddingValues(12.dp), modifier = Modifier.fillMaxSize()) {
                        gridItems(vm.items, key = { it.id }) { game -> Column {
                            com.jxz666.slgking.ui.common.GameCard(item = game, onClick = { openDetail(game.id) }, onLongClick = {})
                            TextButton(modifier = Modifier.fillMaxWidth(), enabled = removing == null,
                                onClick = { removeError = null; removeTarget = game }) {
                                Text(if (removing == game.id) "移出中…" else "移出收藏夹")
                            }
                        } }
                        item(span = { androidx.compose.foundation.lazy.grid.GridItemSpan(maxLineSpan) }) {
                            Column(Modifier.fillMaxWidth().padding(12.dp), horizontalAlignment = Alignment.CenterHorizontally) {
                                when {
                                    vm.loadingMore -> CircularProgressIndicator()
                                    vm.error != null -> {
                                        Text("加载失败：${vm.error}", color = MaterialTheme.colorScheme.error)
                                        TextButton(onClick = { vm.clearError(); vm.loadMore() }) { Text("重试加载") }
                                    }
                                    !vm.endReached -> TextButton(onClick = { vm.loadMore() }) { Text("加载更多") }
                                    else -> Text("已加载全部 ${vm.total} 款", style = MaterialTheme.typography.labelMedium)
                                }
                            }
                        }
                    }
                }
            }
        }
    }
    removeTarget?.let { target ->
        AlertDialog(onDismissRequest = { if (removing == null) removeTarget = null }, title = { Text("移出收藏夹？") },
            text = { Column {
                Text("仅将「${target.displayTitle}」移出「${collection.name}」，保留游戏、个人记录及其他收藏夹成员关系。")
                removeError?.let { Text(it, color = MaterialTheme.colorScheme.error) }
            } }, confirmButton = { TextButton(enabled = removing == null, onClick = {
                if (removing != null) return@TextButton
                removing = target.id; removeError = null
                scope.launch {
                    try {
                        // Never replace memberships from a stale list-card snapshot.
                        val memberships = SlgApi.gameGet(target.id).collectionIds
                        coroutineContext.ensureActive()
                        SlgApi.setGameCollections(target.id, remainingCollectionIds(memberships, collection.id))
                        coroutineContext.ensureActive()
                        removeTarget = null
                        vm.invalidateChangedCatalogs()
                        onMembersChanged()
                    } catch (e: CancellationException) { throw e }
                    catch (e: Exception) { removeError = e.message ?: "移出失败，已保留成员；请重试" }
                    finally { removing = null }
                }
            }) { Text(if (removing == target.id) "移出中…" else "移出") } },
            dismissButton = { TextButton(enabled = removing == null, onClick = { removeTarget = null }) { Text("取消") } })
    }
}

@Composable
fun UserGameDialog(
    initial: GameItem?,
    onSaved: () -> Unit,
    onDismiss: () -> Unit,
) {
    var title by remember { mutableStateOf(initial?.title ?: "") }
    var developer by remember { mutableStateOf(initial?.developer ?: "") }
    var engine by remember { mutableStateOf(initial?.engine ?: "") }
    var version by remember { mutableStateOf(initial?.version ?: "") }
    var categories by remember { mutableStateOf(initial?.categories?.toSet() ?: emptySet()) }
    var sourceUrl by remember { mutableStateOf("") }
    var sourceReady by remember { mutableStateOf(initial == null) }
    var sourceError by remember { mutableStateOf<String?>(null) }
    LaunchedEffect(initial?.id) {
        if (initial != null) try {
            sourceUrl = SlgApi.gameGet(initial.id).url?.takeUnless { it.startsWith("user://") } ?: ""
            sourceReady = true
        } catch (e: CancellationException) { throw e } catch (e: Exception) {
            sourceError = "来源读取失败：${e.message}。请关闭后重试。"
        }
    }
    var saving by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    val scope = rememberCoroutineScope()

    AlertDialog(
        onDismissRequest = { if (!saving) onDismiss() },
        title = { Text(if (initial == null) "添加我的游戏" else "编辑我的游戏") },
        text = {
            Column(Modifier.verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedTextField(value = title, onValueChange = { title = it },
                    label = { Text("标题 *") }, singleLine = true, enabled = !saving)
                OutlinedTextField(value = developer, onValueChange = { developer = it },
                    label = { Text("开发者") }, singleLine = true, enabled = !saving)
                OutlinedTextField(value = engine, onValueChange = { engine = it },
                    label = { Text("引擎") }, singleLine = true, enabled = !saving)
                OutlinedTextField(value = version, onValueChange = { version = it },
                    label = { Text("版本") }, singleLine = true, enabled = !saving)
                OutlinedTextField(value = sourceUrl, onValueChange = { sourceUrl = it; sourceError = null },
                    label = { Text("来源网址（可选）") }, singleLine = true, enabled = !saving && sourceReady,
                    keyboardOptions = androidx.compose.foundation.text.KeyboardOptions(keyboardType = androidx.compose.ui.text.input.KeyboardType.Uri),
                    isError = sourceError != null, supportingText = {
                        Text(sourceError ?: "可记录手机移植版来源；仅支持 HTTP / HTTPS 网页。")
                    })
                Text("游戏分类（可多选）", style = MaterialTheme.typography.titleSmall)
                CategoryChips(categories, enabled = !saving) { categories = it }
                Text(if (categories.isEmpty()) "未分类" else categorySummary(categories.toList()),
                    style = MaterialTheme.typography.bodySmall)
                error?.let { Text(it, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.labelSmall) }
            }
        },
        confirmButton = {
            Button(
                onClick = {
                    if (saving) return@Button
                    saving = true
                    scope.launch {
                        try {
                            val url = sourceUrl.trim()
                            if (url.isNotEmpty() && com.jxz666.slgking.ui.game.validWebUrl(url) == null) {
                                sourceError = "请输入有效的 HTTP / HTTPS 网页地址"
                                return@launch
                            }
                            val params = org.json.JSONObject()
                                .put("title", title.trim()).put("developer", developer.trim().takeIf { it.isNotEmpty() } ?: org.json.JSONObject.NULL)
                                .put("engine", engine.trim().takeIf { it.isNotEmpty() } ?: org.json.JSONObject.NULL)
                                .put("version", version.trim().takeIf { it.isNotEmpty() } ?: org.json.JSONObject.NULL)
                                .put("categories", org.json.JSONArray(categories.toList())).put("source_url", url)
                            if (initial != null) params.put("id", initial.id)
                            SlgApi.raw(if (initial == null) "usergames.add" else "usergames.update", params)
                            onSaved()
                        } catch (e: CancellationException) { throw e } catch (e: Exception) {
                            error = e.message
                        } finally {
                            saving = false
                        }
                    }
                },
                enabled = title.isNotBlank() && !saving && sourceReady,
            ) { Text(if (saving) "保存中…" else "保存") }
        },
        dismissButton = { TextButton(onClick = onDismiss, enabled = !saving) { Text("取消") } },
    )
}

@Composable
private fun ManageCollectionsDialog(
    collections: List<CollectionRow>,
    onRenamed: () -> Unit,
    onDeleted: () -> Unit,
    onDismiss: () -> Unit,
) {
    var renaming by remember { mutableStateOf<CollectionRow?>(null) }
    var newName by remember { mutableStateOf("") }
    var error by remember { mutableStateOf<String?>(null) }
    val scope = rememberCoroutineScope()
    var busy by remember { mutableStateOf(false) }
    fun mutate(block: suspend () -> Unit) {
        if (busy) return
        busy = true
        scope.launch {
            try { block() }
            catch (e: CancellationException) { throw e }
            catch (e: Exception) { error = e.message ?: "操作失败，请重试" }
            finally { busy = false }
        }
    }

    AlertDialog(
        onDismissRequest = { if (!busy) onDismiss() },
        title = { Text("管理收藏夹") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                collections.forEach { c ->
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Text("${c.name}（${c.count}）", modifier = Modifier.weight(1f))
                        IconButton(enabled = !busy, onClick = { renaming = c; newName = c.name }) {
                            Icon(Icons.Filled.Edit, "重命名")
                        }
                        IconButton(enabled = !busy, onClick = {
                            mutate { SlgApi.collectionDelete(c.id); onDeleted() }
                        }) { Icon(Icons.Filled.Delete, "删除") }
                    }
                }
                renaming?.let { target ->
                    OutlinedTextField(value = newName, onValueChange = { newName = it },
                        label = { Text("重命名「${target.name}」") }, singleLine = true)
                    Button(onClick = {
                        mutate { SlgApi.collectionSave(newName.trim(), target.id); renaming = null; onRenamed() }
                    }, enabled = newName.isNotBlank() && !busy) { Text("保存名称") }
                }
                error?.let { Text(it, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.labelSmall) }
            }
        },
        confirmButton = { TextButton(enabled = !busy, onClick = onDismiss) { Text("关闭") } },
    )
}
