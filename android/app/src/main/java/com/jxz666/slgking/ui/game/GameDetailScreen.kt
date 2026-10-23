package com.jxz666.slgking.ui.game

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Bookmark
import androidx.compose.material.icons.filled.CollectionsBookmark
import androidx.compose.material.icons.filled.DownloadDone
import androidx.compose.material.icons.filled.Star
import androidx.compose.material.icons.filled.StarBorder
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Checkbox
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.key
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import org.json.JSONObject
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.jxz666.slgking.bridge.CollectionRow
import com.jxz666.slgking.bridge.GameDetail
import com.jxz666.slgking.bridge.SlgApi
import com.jxz666.slgking.bridge.SlgApiException
import com.jxz666.slgking.ui.catalog.ErrorRetry
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Job
import kotlinx.coroutines.ensureActive
import kotlin.coroutines.coroutineContext
import kotlinx.coroutines.launch
import com.jxz666.slgking.ui.catalog.categorySummary
import com.jxz666.slgking.ui.library.UserGameDialog

/** 游戏详情页：全屏覆盖，系统返回出栈。 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun GameDetailScreen(
    gameId: Int,
    onBack: () -> Unit,
    openComments: (slug: String, title: String) -> Unit = { _, _ -> },
    onTagPicked: (tag: String) -> Unit = { },
    onOpenTranslationSettings: () -> Unit = {},
) {
    val detailScroll = rememberScrollState()
    var detail by remember(gameId) { mutableStateOf<GameDetail?>(null) }
    var error by remember(gameId) { mutableStateOf<String?>(null) }
    var reloading by remember(gameId) { mutableStateOf(false) }
    val scope = rememberCoroutineScope()

    var reloadJob by remember(gameId) { mutableStateOf<Job?>(null) }
    DisposableEffect(gameId) { onDispose { reloadJob?.cancel() } }
    fun reload() {
        reloadJob?.cancel()
        reloadJob = scope.launch {
            reloading = true
            try {
                val loaded = SlgApi.gameGet(gameId)
                coroutineContext.ensureActive()
                detail = loaded
                error = null
            } catch (e: CancellationException) { throw e } catch (e: SlgApiException) {
                error = e.message
            } catch (e: Exception) {
                error = "${e.javaClass.simpleName}: ${e.message}"
            } finally {
                reloading = false
            }
        }
    }

    LaunchedEffect(gameId) { reload() }
    BackHandler(onBack = onBack)

    Scaffold(
        topBar = {
            TopAppBar(
                title = {
                    Text(detail?.item?.displayTitle ?: "",
                        maxLines = 1, overflow = TextOverflow.Ellipsis)
                },
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(Icons.AutoMirrored.Filled.ArrowBack, "返回")
                    }
                },
            )
        },
    ) { padding ->
        Box(Modifier.fillMaxSize().padding(padding)) {
            when {
                detail != null -> key(gameId) {
                    Column {
                      error?.let { message ->
                        Text("刷新失败：$message", color = MaterialTheme.colorScheme.error, modifier = Modifier.padding(12.dp))
                        TextButton(onClick = { reload() }) { Text("重试刷新") }
                      }
                      Box(Modifier.weight(1f)) { DetailBody(detail = detail!!, detailScroll = detailScroll, onMutation = { reload() },
                        openComments = openComments, onTagPicked = onTagPicked,
                        onBack = onBack, onOpenTranslationSettings = onOpenTranslationSettings) }
                    }
                }
                error != null -> ErrorRetry(error!!) { reload() }
                else -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                    CircularProgressIndicator()
                }
            }
        }
    }
}

@Composable
private fun DetailBody(
    detail: GameDetail,
    detailScroll: androidx.compose.foundation.ScrollState,
    onMutation: () -> Unit,
    openComments: (slug: String, title: String) -> Unit,
    onTagPicked: (tag: String) -> Unit,
    onBack: () -> Unit,
    onOpenTranslationSettings: () -> Unit,
) {
    val scope = rememberCoroutineScope()
    var chinese by rememberSaveable(detail.item.id) { mutableStateOf(false) }
    var showTagsEditor by remember { mutableStateOf(false) }
    var confirmHide by remember { mutableStateOf(false) }
    var showNoteEditor by remember { mutableStateOf(false) }
    var showUserEditor by remember { mutableStateOf(false) }
    var showCollections by remember { mutableStateOf(false) }
    var busy by remember { mutableStateOf(false) }
    var opError by remember { mutableStateOf<String?>(null) }

    fun run(block: suspend () -> Unit) {
        if (busy) return
        busy = true
        scope.launch {
            try {
                block()
                onMutation()
            } catch (e: CancellationException) { throw e } catch (e: SlgApiException) {
                opError = e.message
            } catch (e: Exception) {
                opError = "${e.javaClass.simpleName}: ${e.message}"
            } finally {
                busy = false
            }
        }
    }

    Column(
        Modifier
            .fillMaxSize()
            .verticalScroll(detailScroll)
            .padding(horizontal = 16.dp),
    ) {
        // 大封面
        Box(
            Modifier
                .fillMaxWidth()
                .aspectRatio(1.6f)
                .clip(MaterialTheme.shapes.large)
                .background(MaterialTheme.colorScheme.surfaceVariant),
        ) {
            com.jxz666.slgking.ui.common.GameCover(
                item = detail.item, modifier = Modifier.fillMaxSize(), contentScale = ContentScale.Fit,
            )
        }
        Spacer(Modifier.height(12.dp))

        DetailTranslationHeader(detail, chinese, { chinese = it }, onMutation, onOpenTranslationSettings)
        Spacer(Modifier.height(6.dp))
        listOfNotNull(
            detail.item.developer?.let { "开发者 $it" },
            detail.item.engine?.let { "引擎 $it" },
            detail.item.version?.let { "版本 $it" },
            detail.item.lastUpdated?.let { "更新 $it" },
            detail.item.rating?.takeIf { detail.item.catalogId != "dlsite" }?.let { "站内评分 %.1f".format(it) },
        ).joinToString(" · ").let {
            Text(it, style = MaterialTheme.typography.labelMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        Spacer(Modifier.height(12.dp))

        if (detail.item.catalogId == "dlsite") {
            DlsiteFacts(detail)
            Spacer(Modifier.height(12.dp))
        }
        SourceLinks(detail)
        Spacer(Modifier.height(12.dp))

        // 操作行
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.fillMaxWidth()) {
            ActionToggle(
                label = "想玩", icon = Icons.Filled.Bookmark,
                active = detail.item.status == "want", enabled = !busy,
                modifier = Modifier.weight(1f),
            ) {
                run {
                    SlgApi.setState(detail.item.id,
                        if (detail.item.status == "want") null else "want")
                }
            }
            ActionToggle(
                label = "已下载", icon = Icons.Filled.DownloadDone,
                active = detail.item.status == "downloaded", enabled = !busy,
                modifier = Modifier.weight(1f),
            ) {
                run {
                    SlgApi.setState(detail.item.id,
                        if (detail.item.status == "downloaded") null else "downloaded")
                }
            }
            ActionToggle(
                label = "收藏", icon = Icons.Filled.CollectionsBookmark,
                active = detail.collectionIds.isNotEmpty(), enabled = !busy,
                modifier = Modifier.weight(1f),
            ) { showCollections = true }
        }
        Text("想玩、下载状态和收藏是个人记录，不代表本机已安装或能运行。",
            style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.height(16.dp))

        // Keep the label above five touch targets so narrow screens and larger fonts do not clip stars.
        Text("我的评分", style = MaterialTheme.typography.titleSmall)
        Row(
            modifier = Modifier.fillMaxWidth(),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.SpaceBetween,
        ) {
            repeat(5) { index ->
                val star = index + 1
                IconButton(
                    onClick = {
                        if (busy) return@IconButton
                        run {
                            // 点当前同一星级 = 清除
                            if (detail.item.myRating == star) SlgApi.rate(detail.item.id, 0)
                            else SlgApi.rate(detail.item.id, star)
                        }
                    },
                    enabled = !busy,
                ) {
                    Icon(
                        if ((detail.item.myRating ?: 0) > index) Icons.Filled.Star
                        else Icons.Filled.StarBorder,
                        contentDescription = "$star 星",
                        tint = Color(0xFFF2C94C),
                        modifier = Modifier.size(34.dp),
                    )
                }
            }
        }
        Spacer(Modifier.height(8.dp))

        Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth()) {
            Text("备注", style = MaterialTheme.typography.titleSmall, modifier = Modifier.weight(1f))
            TextButton(onClick = { showNoteEditor = true }, enabled = !busy) { Text("编辑") }
        }
        ExpandableDetailText(detail.note?.takeIf { it.isNotBlank() } ?: "未填写", 2,
            "note-${detail.item.id}")
        Spacer(Modifier.height(8.dp))

        // 评论区入口
        if (detail.origin != "user") {
            OutlinedButton(
                onClick = { openComments(detail.item.slug, detail.item.displayTitle) },
                modifier = Modifier.fillMaxWidth(),
            ) { Text("写评论 & 查看评论区") }
            Spacer(Modifier.height(8.dp))
        }

        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Text("标签", style = MaterialTheme.typography.titleSmall, modifier = Modifier.weight(1f))
            TextButton(onClick = { showTagsEditor = true }, enabled = !busy) { Text("编辑标签") }
        }
        DetailTags(detail.item.id, detail.tags, onTagPicked)
        Spacer(Modifier.height(12.dp))
        DetailOverview(detail, chinese)
        Spacer(Modifier.height(8.dp))
        TextButton(onClick = { confirmHide = true }, enabled = !busy) {
            Text(if (detail.hidden) "恢复显示此游戏" else "隐藏此游戏")
        }

        detail.siteComments?.let {
            Text("目录站评论数 $it", style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        if (detail.origin == "user") {
            Text("分类：${categorySummary(detail.item.categories)}", style = MaterialTheme.typography.bodyMedium)
            TextButton(onClick = { showUserEditor = true }, enabled = !busy) { Text("编辑我的游戏") }
            Text("我添加的游戏", style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.primary)
        }
        Spacer(Modifier.height(28.dp))
    }

    if (showTagsEditor) {
        DetailTagsEditor(detail, onSaved = { showTagsEditor = false; onMutation() },
            onDismiss = { showTagsEditor = false })
    }
    if (confirmHide) {
        AlertDialog(onDismissRequest = { confirmHide = false },
            title = { Text(if (detail.hidden) "恢复显示" else "隐藏游戏") },
            text = { Text(if (detail.hidden) "让此游戏重新出现在目录中？" else "隐藏后不出现在常规目录；个人记录会保留，可在目录的隐藏游戏中恢复。") },
            confirmButton = { TextButton(enabled = !busy, onClick = {
                run {
                    SlgApi.raw("game.hidden.set", JSONObject().put("id", detail.item.id).put("hidden", !detail.hidden))
                    confirmHide = false
                    onBack()
                }
            }) { Text("确定") } },
            dismissButton = { TextButton(onClick = { confirmHide = false }) { Text("取消") } })
    }
    if (showUserEditor) {
        UserGameDialog(initial = detail.item, onSaved = { showUserEditor = false; onMutation() },
            onDismiss = { showUserEditor = false })
    }
    if (showNoteEditor) {
        NoteEditorDialog(
            initial = detail.note ?: "",
            onSave = { text -> run { SlgApi.setNote(detail.item.id, text) }; showNoteEditor = false },
            onDismiss = { showNoteEditor = false },
        )
    }
    if (showCollections) {
        CollectionPickerSheet(
            gameId = detail.item.id,
            current = detail.collectionIds,
            onDone = { showCollections = false; onMutation() },
        )
    }
    opError?.let { err ->
        AlertDialog(onDismissRequest = { opError = null },
            title = { Text("操作失败") },
            text = { Text(err) },
            confirmButton = { TextButton(onClick = { opError = null }) { Text("知道了") } })
    }
}

@Composable
private fun ActionToggle(
    label: String,
    icon: androidx.compose.ui.graphics.vector.ImageVector,
    active: Boolean,
    enabled: Boolean,
    modifier: Modifier = Modifier,
    onClick: () -> Unit,
) {
    val container = if (active) MaterialTheme.colorScheme.primaryContainer
    else MaterialTheme.colorScheme.surfaceVariant
    val content = if (active) MaterialTheme.colorScheme.onPrimaryContainer
    else MaterialTheme.colorScheme.onSurfaceVariant
    Surface(onClick = onClick, enabled = enabled, shape = MaterialTheme.shapes.large,
        color = container, contentColor = content, modifier = modifier) {
        Row(
            Modifier.padding(vertical = 12.dp),
            horizontalArrangement = Arrangement.Center,
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Icon(icon, null, Modifier.size(18.dp))
            Spacer(Modifier.width(6.dp))
            Text(label, style = MaterialTheme.typography.labelLarge)
        }
    }
}

@Composable
private fun NoteEditorDialog(initial: String, onSave: (String) -> Unit, onDismiss: () -> Unit) {
    var text by remember { mutableStateOf(initial) }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("编辑备注") },
        text = {
            OutlinedTextField(
                value = text,
                onValueChange = { text = it },
                minLines = 3,
                maxLines = 8,
                modifier = Modifier.fillMaxWidth(),
            )
        },
        confirmButton = { Button(onClick = { onSave(text) }) { Text("保存") } },
        dismissButton = { TextButton(onClick = onDismiss) { Text("取消") } },
    )
}

/** 收藏夹选择（多选 + 新建）。 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun CollectionPickerSheet(gameId: Int, current: List<Int>, onDone: () -> Unit) {
    var collections by remember { mutableStateOf(listOf<CollectionRow>()) }
    var selected by remember { mutableStateOf(current.toSet()) }
    var newName by remember { mutableStateOf("") }
    val scope = rememberCoroutineScope()
    var saving by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }

    LaunchedEffect(Unit) {
        try {
            collections = SlgApi.collections()
        } catch (e: CancellationException) { throw e } catch (e: Exception) {
            error = e.message
        }
    }

    ModalBottomSheet(onDismissRequest = { if (!saving) onDone() }) {
        Column(Modifier.padding(horizontal = 16.dp).padding(bottom = 28.dp)) {
            Text("收藏到收藏夹", style = MaterialTheme.typography.titleMedium)
            Spacer(Modifier.height(8.dp))
            collections.forEach { c ->
                Row(
                    verticalAlignment = Alignment.CenterVertically,
                    modifier = Modifier.fillMaxWidth(),
                ) {
                    Checkbox(
                        checked = c.id in selected, enabled = !saving,
                        onCheckedChange = { checked ->
                            selected = if (checked) selected + c.id else selected - c.id
                        },
                    )
                    Text("${c.name}（${c.count}）")
                }
            }
            Spacer(Modifier.height(8.dp))
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedTextField(
                    value = newName,
                    onValueChange = { newName = it },
                    placeholder = { Text("新收藏夹名") },
                    singleLine = true, enabled = !saving,
                    modifier = Modifier.weight(1f),
                )
                OutlinedButton(onClick = {
                    if (saving) return@OutlinedButton
                    saving = true
                    scope.launch {
                        try {
                            val id = SlgApi.collectionSave(newName)
                            collections = SlgApi.collections()
                            selected = selected + id
                            newName = ""
                        } catch (e: CancellationException) { throw e } catch (e: Exception) {
                            error = e.message
                        } finally { saving = false }
                    }
                }, enabled = newName.isNotBlank() && !saving) { Text("新建") }
            }
            error?.let {
                Spacer(Modifier.height(6.dp))
                Text(it, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.labelSmall)
            }
            Spacer(Modifier.height(12.dp))
            Button(
                onClick = {
                    if (saving) return@Button
                    saving = true
                    scope.launch {
                        try {
                            SlgApi.setGameCollections(gameId, selected.toList())
                            onDone()
                        } catch (e: CancellationException) { throw e } catch (e: Exception) {
                            error = e.message
                        } finally { saving = false }
                    }
                },
                enabled = !saving,
                modifier = Modifier.fillMaxWidth(),
            ) { Text(if (saving) "保存中…" else "保存") }
        }
    }
}
