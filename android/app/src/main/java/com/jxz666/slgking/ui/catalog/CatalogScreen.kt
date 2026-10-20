package com.jxz666.slgking.ui.catalog

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
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyGridState
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.foundation.lazy.grid.rememberLazyGridState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ArrowDownward
import androidx.compose.material.icons.filled.ArrowUpward
import androidx.compose.material.icons.filled.FilterList
import androidx.compose.material.icons.filled.Search
import androidx.compose.material.icons.filled.SearchOff
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.SegmentedButton
import androidx.compose.material3.SegmentedButtonDefaults
import androidx.compose.material3.SingleChoiceSegmentedButtonRow
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.derivedStateOf
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.unit.dp
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.Button
import androidx.lifecycle.viewmodel.compose.viewModel
import com.jxz666.slgking.bridge.GameItem
import com.jxz666.slgking.bridge.SlgApi
import com.jxz666.slgking.bridge.SlgApiException
import com.jxz666.slgking.bridge.TagRow
import com.jxz666.slgking.ui.common.GameCard
import com.jxz666.slgking.ui.common.BrandHeader
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.launch

private val SORTS = listOf(
    "score" to "xp推荐", "rating" to "评分", "heat" to "热度",
    "updated" to "更新", "title" to "名称",
)

private val VIEWS = listOf("all" to "全部", "want" to "想玩", "downloaded" to "已下载")

/** 目录 Tab：搜索 + 排序 chips + 视图分段 + 筛选抽屉 + 封面网格（无限滚动）。 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun CatalogScreen(
    openDetail: (Int) -> Unit,
    pendingTag: String? = null,
    onConsumePendingTag: () -> Unit = {},
    vm: CatalogViewModel = viewModel(),
) {
    val context = LocalContext.current
    val shellPreferences = remember(context) { context.getSharedPreferences("slgking_shell", 0) }
    val catalogId = vm.query.catalogId
    val gridState = remember(catalogId) {
        val saved = vm.scrollPositions[catalogId] ?: (0 to 0)
        LazyGridState(saved.first, saved.second)
    }
    DisposableEffect(catalogId) {
        onDispose { vm.scrollPositions[catalogId] = gridState.firstVisibleItemIndex to gridState.firstVisibleItemScrollOffset }
    }
    var previousQuery by remember { mutableStateOf(vm.query) }
    var searchOpen by remember { mutableStateOf(false) }
    var filterOpen by remember { mutableStateOf(false) }
    var quickItem by remember { mutableStateOf<GameItem?>(null) }

    // 详情页标签点选跳筛
    LaunchedEffect(pendingTag) {
        pendingTag?.let { tag ->
            vm.setQuery { it.copy(tags = (it.tags + tag).toSet()) }
            onConsumePendingTag()
        }
    }

    // 预加载：滚到倒数第 6 个时取下一页
    val shouldLoadMore by remember(gridState) {
        derivedStateOf {
            val info = gridState.layoutInfo
            val last = info.visibleItemsInfo.lastOrNull()?.index ?: 0
            info.totalItemsCount > 0 && last >= info.totalItemsCount - 6
        }
    }
    LaunchedEffect(vm.query) {
        if (previousQuery.catalogId == vm.query.catalogId && previousQuery != vm.query) gridState.scrollToItem(0)
        previousQuery = vm.query
    }
    LaunchedEffect(shouldLoadMore, vm.items.size, vm.loading) {
        if (shouldLoadMore) vm.loadMore()
    }
    LaunchedEffect(Unit) {
        vm.invalidateChangedCatalogs()
        val savedId = shellPreferences.getString("catalog_id", "western_slg") ?: "western_slg"
        if (savedId in setOf("western_slg", "dlsite", "all") && savedId != vm.query.catalogId) vm.selectCatalog(savedId)
        else if (vm.items.isEmpty()) vm.loadFirst()
    }

    LaunchedEffect(com.jxz666.slgking.bridge.CatalogChanges.revision) { vm.invalidateChangedCatalogs() }

    Column(Modifier.fillMaxSize()) {
        BrandHeader()
        SingleChoiceSegmentedButtonRow(Modifier.fillMaxWidth().padding(horizontal = 12.dp)) {
            listOf("western_slg" to "SLG大类", "dlsite" to "DLsite", "all" to "全部").forEachIndexed { index, (id, label) ->
                SegmentedButton(selected = catalogId == id, onClick = { shellPreferences.edit().putString("catalog_id", id).apply(); vm.selectCatalog(id) },
                    shape = SegmentedButtonDefaults.itemShape(index, 3)) { Text(label) }
            }
        }
        // 顶栏：搜索 + 筛选
        Row(
            Modifier
                .fillMaxWidth()
                .padding(horizontal = 12.dp, vertical = 6.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            OutlinedTextField(
                value = vm.query.q,
                onValueChange = { text -> vm.setQuery { q -> q.copy(q = text) } },
                placeholder = { Text("搜索游戏名 / 译名") },
                leadingIcon = { Icon(Icons.Filled.Search, null) },
                singleLine = true,
                shape = MaterialTheme.shapes.large,
                keyboardOptions = KeyboardOptions(imeAction = ImeAction.Search),
                modifier = Modifier.weight(1f),
            )
            Spacer(Modifier.width(8.dp))
            IconButton(onClick = { filterOpen = true }) {
                Icon(Icons.Filled.FilterList, "筛选")
            }
        }

        // 视图分段
        SingleChoiceSegmentedButtonRow(
            Modifier
                .fillMaxWidth()
                .padding(horizontal = 12.dp)
        ) {
            VIEWS.forEachIndexed { index, (key, label) ->
                SegmentedButton(
                    selected = vm.query.view == key,
                    onClick = { vm.setQuery { it.copy(view = key) } },
                    shape = SegmentedButtonDefaults.itemShape(index = index, count = VIEWS.size),
                ) { Text(label) }
            }
        }

        // 排序 chips + 方向（短标签保证 5 个在一屏内，小屏仍可横滑）
        Row(
            Modifier
                .fillMaxWidth()
                .horizontalScroll(rememberScrollState())
                .padding(horizontal = 12.dp, vertical = 6.dp),
            horizontalArrangement = Arrangement.spacedBy(6.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            (when (catalogId) { "western_slg" -> SORTS; "dlsite" -> listOf("updated" to "更新", "title" to "名称", "source_rating" to "原生评分"); else -> listOf("title" to "名称", "updated" to "更新") }).forEach { (key, label) ->
                FilterChip(
                    selected = vm.query.sort == key,
                    onClick = {
                        vm.setQuery {
                            if (it.sort == key) it.copy(desc = !it.desc) else it.copy(sort = key, desc = true)
                        }
                    },
                    label = { Text(label) },
                    leadingIcon = if (vm.query.sort == key) {
                        {
                            Icon(
                                if (vm.query.desc) Icons.Filled.ArrowDownward
                                else Icons.Filled.ArrowUpward,
                                null, Modifier.height(16.dp),
                            )
                        }
                    } else null,
                )
            }
        }

        // 计数行
        Row(
            Modifier
                .fillMaxWidth()
                .padding(horizontal = 16.dp, vertical = 2.dp),
        ) {
            Text(
                "共 ${vm.total} 款" +
                    if (vm.query.tags.isNotEmpty() || vm.query.exclude.isNotEmpty() || vm.query.q.isNotBlank() || vm.query.languages.isNotEmpty() || vm.query.workTypes.isNotEmpty() || vm.query.datePreset != "all")
                        " · 已筛选" else "",
                style = MaterialTheme.typography.labelMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }

        Box(Modifier.fillMaxSize().padding(horizontal = 12.dp)) {
            when {
                vm.loading -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                    CircularProgressIndicator()
                }
                vm.error != null -> ErrorRetry(vm.error!!) { vm.refresh() }
                vm.items.isEmpty() -> EmptyCatalog()
                else -> androidx.compose.material3.pulltorefresh.PullToRefreshBox(
                    isRefreshing = false,
                    onRefresh = { vm.refresh() },
                    modifier = Modifier.fillMaxSize(),
                ) {
                    CatalogGrid(
                        gridState = gridState, vm = vm, openDetail = openDetail,
                        onLongPress = { quickItem = it },
                    )
                }
            }
        }
    }

    if (filterOpen) {
        FilterSheet(
            vm = vm,
            onDismiss = { filterOpen = false },
        )
    }
    quickItem?.let { item ->
        QuickActionsSheet(
            item = item,
            onDismiss = { quickItem = null },
            onChanged = { updated -> vm.updateItem(updated) },
        )
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun CatalogGrid(
    gridState: LazyGridState,
    vm: CatalogViewModel,
    openDetail: (Int) -> Unit,
    onLongPress: (GameItem) -> Unit,
) {
    LazyVerticalGrid(
        state = gridState,
        columns = GridCells.Adaptive(minSize = 160.dp),
        horizontalArrangement = Arrangement.spacedBy(10.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
        contentPadding = androidx.compose.foundation.layout.PaddingValues(bottom = 24.dp),
        modifier = Modifier.fillMaxSize(),
    ) {
        items(vm.items, key = { it.id }) { item ->
            GameCard(
                item = item,
                onClick = { openDetail(item.id) },
                onLongClick = { onLongPress(item) },
            )
        }
        if (vm.loadingMore) {
            item(span = { androidx.compose.foundation.lazy.grid.GridItemSpan(maxLineSpan) }) {
                Box(Modifier.fillMaxWidth().padding(12.dp), contentAlignment = Alignment.Center) {
                    CircularProgressIndicator(Modifier.height(24.dp))
                }
            }
        } else if (vm.endReached && vm.items.size > 8) {
            item(span = { androidx.compose.foundation.lazy.grid.GridItemSpan(maxLineSpan) }) {
                Box(Modifier.fillMaxWidth().padding(12.dp), contentAlignment = Alignment.Center) {
                    Text("没有更多了", style = MaterialTheme.typography.labelMedium,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            }
        }
    }
}

@Composable
private fun EmptyCatalog() {
    Column(
        Modifier.fillMaxSize().padding(24.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center,
    ) {
        Icon(Icons.Filled.SearchOff, null, Modifier.height(48.dp))
        Spacer(Modifier.height(12.dp))
        Text("没有匹配的游戏", style = MaterialTheme.typography.titleSmall)
        Text("试试放宽筛选或换个关键词", style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}

@Composable
fun ErrorRetry(message: String, onRetry: () -> Unit) {
    Column(
        Modifier.fillMaxSize().padding(24.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center,
    ) {
        Text("出错了", style = MaterialTheme.typography.titleSmall)
        Spacer(Modifier.height(6.dp))
        Text(message, style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.error)
        Spacer(Modifier.height(12.dp))
        Button(onClick = onRetry) { Text("重试") }
    }
}

/** 筛选抽屉：标签多选 + 排除区 + 实时命中数。 */
@OptIn(ExperimentalMaterial3Api::class, androidx.compose.foundation.layout.ExperimentalLayoutApi::class)
@Composable
private fun FilterSheet(vm: CatalogViewModel, onDismiss: () -> Unit) {
    var tags by remember { mutableStateOf(listOf<TagRow>()) }
    var tagSearch by remember { mutableStateOf("") }
    var excludeMode by remember { mutableStateOf(false) }
    var languages by remember { mutableStateOf(emptyList<String>()) }
    var workTypes by remember { mutableStateOf(emptyList<String>()) }
    var filterError by remember { mutableStateOf<String?>(null) }
    var filterAttempt by remember { mutableStateOf(0) }

    LaunchedEffect(vm.query.catalogId, filterAttempt) {
        filterError = null
        try {
            tags = SlgApi.tags(catalogId = vm.query.catalogId)
            if (vm.query.catalogId == "dlsite") {
                val filters = SlgApi.raw("dlsite.filters")
                fun values(key: String): List<String> {
                    val a = filters.optJSONArray(key) ?: return emptyList()
                    return (0 until a.length()).map { a.optString(it) }.filter { it.isNotBlank() }
                }
                languages = values("languages")
                workTypes = values("work_types")
            }
        } catch (e: CancellationException) { throw e }
        catch (e: Exception) { filterError = e.message ?: "筛选选项加载失败" }
    }

    var working by remember { mutableStateOf(vm.query) }
    DisposableEffect(Unit) { onDispose { vm.cancelPreview() } }
    LaunchedEffect(Unit) { vm.previewHits(working) }

    ModalBottomSheet(
        onDismissRequest = onDismiss,
        sheetState = androidx.compose.material3.rememberModalBottomSheetState(
            skipPartiallyExpanded = true),
    ) {
        Column(Modifier.heightIn(max = (androidx.compose.ui.platform.LocalConfiguration.current.screenHeightDp * 0.85f).dp).verticalScroll(rememberScrollState()).padding(horizontal = 16.dp).padding(bottom = 20.dp)) {
            // 头部固定：标题 / 清空 / 排除模式
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text("筛选", style = MaterialTheme.typography.titleMedium, modifier = Modifier.weight(1f))
                if (working.tags.isNotEmpty() || working.exclude.isNotEmpty() || working.languages.isNotEmpty() || working.workTypes.isNotEmpty() || working.datePreset != "all") {
                    TextButton(onClick = {
                        working = working.copy(tags = emptySet(), exclude = emptySet(), languages = emptySet(), workTypes = emptySet(), datePreset = "all", dateStart = null, dateEnd = null)
                        vm.previewHits(working)
                    }) { Text("清空") }
                }
                FilterChip(
                    selected = excludeMode,
                    onClick = { excludeMode = !excludeMode },
                    label = { Text("排除模式") },
                )
            }
            filterError?.let { message ->
                Text(message, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall)
                TextButton(onClick = { filterAttempt++ }) { Text("重新加载筛选") }
            }
            Text("更新日期（北京时间自然日）", style = MaterialTheme.typography.labelMedium)
            Row(Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                CatalogDatePolicy.presets.forEach { (id, label) ->
                    FilterChip(selected = working.datePreset == id, label = { Text(label) }, onClick = {
                        working = working.copy(datePreset = id)
                        if (CatalogDatePolicy.error(id, working.dateStart, working.dateEnd) == null) vm.previewHits(working) else vm.cancelPreview()
                    })
                }
            }
            if (working.datePreset == "custom") {
                Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                    OutlinedTextField(value = working.dateStart ?: "", onValueChange = {
                        working = working.copy(dateStart = it.take(10))
                        if (CatalogDatePolicy.error(working.datePreset, working.dateStart, working.dateEnd) == null) vm.previewHits(working) else vm.cancelPreview()
                    }, label = { Text("开始 YYYY-MM-DD") }, singleLine = true, modifier = Modifier.weight(1f))
                    OutlinedTextField(value = working.dateEnd ?: "", onValueChange = {
                        working = working.copy(dateEnd = it.take(10))
                        if (CatalogDatePolicy.error(working.datePreset, working.dateStart, working.dateEnd) == null) vm.previewHits(working) else vm.cancelPreview()
                    }, label = { Text("结束 YYYY-MM-DD") }, singleLine = true, modifier = Modifier.weight(1f))
                }
                CatalogDatePolicy.error(working.datePreset, working.dateStart, working.dateEnd)?.let {
                    Text(it, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall)
                }
            }
            Text("按作品更新日期筛选，日期未知的条目不计入指定范围。", style = MaterialTheme.typography.labelSmall)
            if (working.catalogId == "dlsite") {
                Text("DLsite 语言与作品形式", style = MaterialTheme.typography.labelMedium)
                FilterValues("语言", languages, working.languages) { value ->
                    working = working.copy(languages = if (value in working.languages) working.languages - value else working.languages + value)
                    vm.previewHits(working)
                }
                FilterValues("作品形式", workTypes, working.workTypes) { value ->
                    working = working.copy(workTypes = if (value in working.workTypes) working.workTypes - value else working.workTypes + value)
                    vm.previewHits(working)
                }
            }
            OutlinedTextField(
                value = tagSearch,
                onValueChange = { tagSearch = it },
                placeholder = { Text("搜索标签") },
                singleLine = true,
                modifier = Modifier.fillMaxWidth(),
            )
            Spacer(Modifier.height(6.dp))
            if (working.tags.isNotEmpty() || working.exclude.isNotEmpty()) {
                Text(
                    buildString {
                        if (working.tags.isNotEmpty()) append("已选：${working.tags.joinToString("、")}")
                        if (working.exclude.isNotEmpty()) {
                            if (isNotEmpty()) append("\n")
                            append("已排除：${working.exclude.joinToString("、")}")
                        }
                    },
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.primary,
                )
                Spacer(Modifier.height(4.dp))
            }
            // 中部可滚动：固定高度 + 懒网格标签流（命中数与按钮固定可见）
            val filtered = tags.filter {
                tagSearch.isBlank() || it.display.contains(tagSearch, true) || it.name.contains(tagSearch, true)
            }
            androidx.compose.foundation.layout.Box(
                Modifier
                    .fillMaxWidth()
                    .heightIn(min = 100.dp, max = if (working.catalogId == "western_slg") 350.dp else 180.dp)
            ) {
                androidx.compose.foundation.lazy.grid.LazyVerticalGrid(
                    columns = androidx.compose.foundation.lazy.grid.GridCells.Adaptive(96.dp),
                    modifier = Modifier.fillMaxSize(),
                    horizontalArrangement = Arrangement.spacedBy(6.dp),
                    verticalArrangement = Arrangement.spacedBy(6.dp),
                ) {
                    items(filtered.take(300)) { tag ->
                        val included = tag.name in working.tags
                        val excluded = tag.name in working.exclude
                        FilterChip(
                            selected = if (excludeMode) excluded else included,
                            onClick = {
                                working = if (excludeMode) {
                                    working.copy(
                                        exclude = if (excluded) working.exclude - tag.name else working.exclude + tag.name,
                                        tags = working.tags - tag.name,
                                    )
                                } else {
                                    working.copy(
                                        tags = if (included) working.tags - tag.name else working.tags + tag.name,
                                        exclude = working.exclude - tag.name,
                                    )
                                }
                                vm.previewHits(working)
                            },
                            label = { Text("${tag.display} ${tag.count}", maxLines = 1) },
                        )
                    }
                }
            }
            Spacer(Modifier.height(8.dp))
            Text(
                vm.filterHitCount?.let { "命中 $it 款" } ?: "",
                style = MaterialTheme.typography.labelMedium,
            )
            Spacer(Modifier.height(8.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedButtonCompat(onClick = onDismiss, label = "取消", modifier = Modifier.weight(1f))
                Button(
                    onClick = {
                        vm.setQuery { working }
                        onDismiss()
                    },
                    modifier = Modifier.weight(1f),
                    enabled = CatalogDatePolicy.error(working.datePreset, working.dateStart, working.dateEnd) == null,
                ) { Text("应用筛选") }
            }
        }
    }
}

@Composable
private fun OutlinedButtonCompat(onClick: () -> Unit, label: String, modifier: Modifier = Modifier) {
    androidx.compose.material3.OutlinedButton(onClick = onClick, modifier = modifier) { Text(label) }
}

/** 长按快速操作。 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun QuickActionsSheet(
    item: GameItem,
    onDismiss: () -> Unit,
    onChanged: (GameItem) -> Unit,
) {
    val clipboard = LocalClipboardManager.current
    val scope = androidx.compose.runtime.rememberCoroutineScope()
    var busy by remember { mutableStateOf(false) }

    ModalBottomSheet(onDismissRequest = { if (!busy) onDismiss() }) {
        Column(Modifier.padding(horizontal = 16.dp).padding(bottom = 28.dp)) {
            Text(item.displayTitle, style = MaterialTheme.typography.titleSmall,
                maxLines = 2)
            Spacer(Modifier.height(12.dp))
            QuickAction("想玩 / 取消想玩", current = item.status == "want", enabled = !busy) {
                if (busy) return@QuickAction
                busy = true
                scope.launch {
                    val next = if (item.status == "want") null else "want"
                    try {
                        SlgApi.setState(item.id, next)
                        onChanged(item.copy(status = next))
                    } catch (_: SlgApiException) { }
                    busy = false
                    onDismiss()
                }
            }
            QuickAction("已下载 / 取消", current = item.status == "downloaded", enabled = !busy) {
                if (busy) return@QuickAction
                busy = true
                scope.launch {
                    val next = if (item.status == "downloaded") null else "downloaded"
                    try {
                        SlgApi.setState(item.id, next)
                        onChanged(item.copy(status = next))
                    } catch (_: SlgApiException) { }
                    busy = false
                    onDismiss()
                }
            }
            QuickAction("复制标题", enabled = !busy) {
                clipboard.setText(AnnotatedString(item.displayTitle))
                onDismiss()
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun QuickAction(label: String, current: Boolean = false, enabled: Boolean = true, onClick: () -> Unit) {
    Surface(onClick = onClick, enabled = enabled, shape = MaterialTheme.shapes.medium, modifier = Modifier.fillMaxWidth()) {
        Row(Modifier.padding(14.dp), verticalAlignment = Alignment.CenterVertically) {
            Text(if (current) "$label ✓" else label, modifier = Modifier.weight(1f))
        }
    }
}

@Composable
private fun FilterValues(label: String, values: List<String>, selected: Set<String>, toggle: (String) -> Unit) {
    Row(Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()),
        verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        Text(label, style = MaterialTheme.typography.labelMedium)
        values.forEach { value -> FilterChip(selected = value in selected,
            onClick = { toggle(value) }, label = { Text(if (value == "__unknown__") "未知" else value) }) }
    }
}
