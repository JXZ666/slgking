package com.jxz666.slgking.ui.comments

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Edit
import androidx.compose.material.icons.filled.ThumbDown
import androidx.compose.material.icons.filled.ThumbUp
import androidx.compose.material.icons.filled.Warning
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.ExtendedFloatingActionButton
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SegmentedButton
import androidx.compose.material3.SegmentedButtonDefaults
import androidx.compose.material3.SingleChoiceSegmentedButtonRow
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
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
import com.jxz666.slgking.bridge.SlgApi
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Job
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.launch
import org.json.JSONObject

/** 评论区：公开评论分页 + 私人评论 + 发表/点赞/举报。 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun CommentsScreen(slug: String, title: String, onBack: () -> Unit) {
    var sort by remember { mutableStateOf("latest") }
    var page by remember { mutableStateOf(1) }
    var rows by remember { mutableStateOf(listOf<JSONObject>()) }
    var local by remember { mutableStateOf(listOf<JSONObject>()) }
    var offline by remember { mutableStateOf(false) }
    var loading by remember { mutableStateOf(true) }
    var loadingMore by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    var composerOpen by remember { mutableStateOf(false) }
    var loggedIn by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()

    var totalPages by remember { mutableStateOf(0) }
    var loadJob by remember { mutableStateOf<Job?>(null) }
    val requests = remember(slug) { CommentRequestGeneration() }

    fun loadPage(pageToLoad: Int, replace: Boolean) {
        if (!replace && (loading || loadingMore)) return
        val generation = if (replace) requests.next() else requests.current
        val requestedSort = sort
        if (replace) {
            loadJob?.cancel()
            loading = true
            loadingMore = false
        } else loadingMore = true
        loadJob = scope.launch {
            try {
                val r = SlgApi.commentsPage(slug, pageToLoad, requestedSort)
                ensureActive()
                if (!requests.accepts(generation) || sort != requestedSort) return@launch
                offline = r.optBoolean("offline")
                local = commentObjects(r.optJSONArray("local_private"))
                    .filter { it.optLong("id") > 0 }.distinctBy { it.optLong("id") }
                rows = mergeCloudComments(if (replace) emptyList() else rows,
                    commentObjects(r.optJSONArray("comments")))
                totalPages = r.optInt("total_pages")
                page = r.optInt("page", pageToLoad)
                error = null
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                if (requests.accepts(generation) && sort == requestedSort) error = e.message ?: "评论加载失败"
            } finally {
                if (requests.accepts(generation) && sort == requestedSort) {
                    loading = false
                    loadingMore = false
                }
            }
        }
    }

    LaunchedEffect(slug) {
        loggedIn = try { SlgApi.accountStatus().loggedIn }
        catch (e: CancellationException) { throw e }
        catch (_: Exception) { false }
    }
    LaunchedEffect(slug, sort) { loadPage(1, replace = true) }
    BackHandler(onBack = onBack)

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text(title.ifBlank { "评论区" }, maxLines = 1, overflow = TextOverflow.Ellipsis) },
                navigationIcon = {
                    IconButton(onClick = onBack) { Icon(Icons.AutoMirrored.Filled.ArrowBack, "返回") }
                },
            )
        },
        floatingActionButton = {
            ExtendedFloatingActionButton(
                onClick = { composerOpen = true },
                icon = { Icon(Icons.Filled.Edit, null) },
                text = { Text("写评论") },
            )
        },
    ) { padding ->
        Column(Modifier.fillMaxSize().padding(padding)) {
            Row(
                Modifier.padding(horizontal = 12.dp, vertical = 4.dp),
                horizontalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                FilterChip(selected = sort == "latest",
                    onClick = { sort = "latest" }, label = { Text("最新") })
                FilterChip(selected = sort == "popular",
                    onClick = { sort = "popular" }, label = { Text("热门") })
            }
            if (offline) {
                Card(Modifier.padding(horizontal = 12.dp)) {
                    Text(
                        "云端评论加载失败；以下为本地私人评论。",
                        Modifier.padding(10.dp),
                        style = MaterialTheme.typography.labelMedium,
                        color = MaterialTheme.colorScheme.error,
                    )
                }
            }
            if (offline) TextButton(onClick = { loadPage(1, replace = true) }) { Text("重试云端评论") }
            when {
                loading -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                    CircularProgressIndicator()
                }
                error != null -> com.jxz666.slgking.ui.catalog.ErrorRetry(error!!) {
                    loadPage(1, replace = true)
                }
                else -> LazyColumn(
                    Modifier.fillMaxSize(),
                    contentPadding = androidx.compose.foundation.layout.PaddingValues(
                        start = 12.dp, end = 12.dp, top = 6.dp, bottom = 88.dp),
                    verticalArrangement = Arrangement.spacedBy(8.dp),
                ) {
                    if (local.isNotEmpty()) {
                        item {
                            Text("私人评论（仅本机）", style = MaterialTheme.typography.titleSmall)
                        }
                        items(local, key = { "local-${it.optLong("id")}" }) { c ->
                            LocalCommentRow(c, onDelete = {
                                SlgApi.commentDeleteLocal(c.optLong("id"))
                                loadPage(1, replace = true)
                            })
                        }
                        item {
                            Text("公开评论", style = MaterialTheme.typography.titleSmall)
                        }
                    }
                    if (rows.isEmpty() && !offline) {
                        item {
                            Column(Modifier.padding(vertical = 20.dp)) {
                                Text("评论区暂无公开评论", style = MaterialTheme.typography.titleSmall)
                                Spacer(Modifier.height(6.dp))
                                Text(
                                    "目录里的「评论数」来自目录站统计，不是本站评论区数量。\n" +
                                        "本站公开评论需登录发表、审核通过后才会显示；\n" +
                                        "也可以在发表时选「私人」，仅保存在本机。",
                                    style = MaterialTheme.typography.bodySmall,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                                )
                            }
                        }
                    }
                    items(rows, key = { cloudCommentKey(it) }) { c ->
                        CloudCommentRow(c, scope, loggedIn) { loadPage(1, replace = true) }
                    }
                    if (page < totalPages) {
                        item {
                            Box(Modifier.fillMaxWidth().padding(8.dp),
                                contentAlignment = Alignment.Center) {
                                if (loadingMore) CircularProgressIndicator(
                                    Modifier.height(22.dp))
                                else TextButton(onClick = { loadPage(page + 1, false) }) {
                                    Text("加载更多")
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    if (composerOpen) {
        ComposerSheet(
            slug = slug,
            loggedIn = loggedIn,
            onDone = { composerOpen = false; loadPage(1, replace = true) },
            onDismiss = { composerOpen = false },
        )
    }
}

@Composable
private fun LocalCommentRow(c: JSONObject, onDelete: suspend () -> Unit) {
    var busy by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    val scope = rememberCoroutineScope()
    Card {
        Column(Modifier.padding(12.dp)) {
            Row {
                Text("私人 · 仅自己可见",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.weight(1f))
                TextButton(enabled = !busy, onClick = {
                    if (busy) return@TextButton
                    busy = true
                    scope.launch {
                        try { onDelete(); error = null }
                        catch (e: CancellationException) { throw e }
                        catch (e: Exception) { error = e.message ?: "删除失败" }
                        finally { busy = false }
                    }
                }) { Text("删除") }
            }
            Text(c.optString("content"), style = MaterialTheme.typography.bodyMedium)
            error?.let { Text(it, color = MaterialTheme.colorScheme.error) }
        }
    }
}

@Composable
private fun CloudCommentRow(
    c: JSONObject,
    scope: kotlinx.coroutines.CoroutineScope,
    loggedIn: Boolean,
    onDeleted: () -> Unit,
) {
    val id = cloudCommentId(c) ?: return
    var voted by remember(id) { mutableStateOf(0) }
    var up by remember(id, c) { mutableStateOf(c.optInt("votes_up", c.optInt("likes"))) }
    var down by remember(id, c) { mutableStateOf(c.optInt("votes_down", c.optInt("dislikes"))) }
    var busy by remember(id) { mutableStateOf(false) }
    var message by remember(id) { mutableStateOf<String?>(null) }
    val own = c.optBoolean("own")
    fun vote(value: Int) {
        if (busy || !loggedIn || own) return
        busy = true
        scope.launch {
            try {
                val result = SlgApi.commentVote(id, value)
                ensureActive()
                up = result.getInt("votes_up")
                down = result.getInt("votes_down")
                voted = value
                message = if (result.optBoolean("hidden")) "评论已被隐藏，刷新后查看" else null
            } catch (e: CancellationException) { throw e }
            catch (e: Exception) { message = e.message ?: "投票失败" }
            finally { busy = false }
        }
    }
    Card {
        Column(Modifier.padding(12.dp)) {
            Text(commentAuthor(c), style = MaterialTheme.typography.titleSmall)
            c.optString("author_title_name").takeIf { it.isNotBlank() }?.let {
                Text(it, style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.primary)
            }
            Text(commentTime(c), style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
            Text(c.optString("content"), style = MaterialTheme.typography.bodyMedium)
            Row(verticalAlignment = Alignment.CenterVertically) {
                IconButton(onClick = { vote(if (voted == 1) 0 else 1) }, enabled = loggedIn && !own && !busy) {
                    Icon(Icons.Filled.ThumbUp, "赞", tint = if (voted == 1) MaterialTheme.colorScheme.primary
                        else MaterialTheme.colorScheme.onSurfaceVariant)
                }
                Text("$up", style = MaterialTheme.typography.labelSmall)
                IconButton(onClick = { vote(if (voted == -1) 0 else -1) }, enabled = loggedIn && !own && !busy) {
                    Icon(Icons.Filled.ThumbDown, "踩", tint = if (voted == -1) MaterialTheme.colorScheme.error
                        else MaterialTheme.colorScheme.onSurfaceVariant)
                }
                Text("$down", style = MaterialTheme.typography.labelSmall)
            }
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(if (own) "我的评论 · ${c.optString("status")}" else "",
                    style = MaterialTheme.typography.labelSmall, modifier = Modifier.weight(1f))
                TextButton(enabled = loggedIn && !busy, onClick = {
                    if (busy) return@TextButton
                    busy = true
                    scope.launch {
                        try {
                            if (own) { SlgApi.commentDeleteCloud(id); onDeleted() }
                            else { SlgApi.commentReport(id); message = "举报已提交" }
                        } catch (e: CancellationException) { throw e }
                        catch (e: Exception) { message = e.message ?: "操作失败" }
                        finally { busy = false }
                    }
                }) { Text(if (own) "删除" else "举报") }
            }
            if (!loggedIn) Text("登录后可点赞或举报", style = MaterialTheme.typography.labelSmall)
            message?.let { Text(it, style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.primary) }
        }
    }
}

/** 发表弹层：公开（20-500，审核制）/ 私人（≤500，本机）。 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun ComposerSheet(slug: String, loggedIn: Boolean, onDone: () -> Unit, onDismiss: () -> Unit) {
    var publicMode by remember { mutableStateOf(false) }
    var content by remember { mutableStateOf("") }
    var message by remember { mutableStateOf<String?>(null) }
    var busy by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()

    ModalBottomSheet(
        onDismissRequest = { if (!busy) onDismiss() },
        sheetState = androidx.compose.material3.rememberModalBottomSheetState(
            skipPartiallyExpanded = true,
            confirmValueChange = { !busy },
        ),
    ) {
        Column(Modifier.padding(horizontal = 16.dp).padding(bottom = 24.dp).imePadding()) {
            Text("发表评论", style = MaterialTheme.typography.titleMedium)
            Spacer(Modifier.height(8.dp))
            SingleChoiceSegmentedButtonRow(Modifier.fillMaxWidth()) {
                SegmentedButton(selected = publicMode,
                    onClick = { publicMode = true },
                    enabled = loggedIn && !busy,
                    shape = SegmentedButtonDefaults.itemShape(0, 2)) { Text("公开") }
                SegmentedButton(selected = !publicMode,
                    onClick = { publicMode = false },
                    enabled = !busy,
                    shape = SegmentedButtonDefaults.itemShape(1, 2)) { Text("私人") }
            }
            Text(
                if (publicMode) "公开评论将进入审核，通过后他人可见（20-500 字）"
                else "私人评论仅保存在本机（≤500 字）",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            if (!loggedIn) Text("未登录也可保存本机私人评论；公开发表需要登录。", style = MaterialTheme.typography.labelSmall)
            Spacer(Modifier.height(8.dp))
            OutlinedTextField(
                value = content,
                onValueChange = { if (it.length <= 500) content = it },
                enabled = !busy,
                minLines = 4, maxLines = 8,
                modifier = Modifier.fillMaxWidth(),
                placeholder = { Text("写下你的体验…") },
            )
            Text("${content.length}/500", style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
            message?.let {
                Text(it, style = MaterialTheme.typography.labelMedium,
                    color = if (it.startsWith("✓")) MaterialTheme.colorScheme.primary
                    else MaterialTheme.colorScheme.error)
            }
            Spacer(Modifier.height(10.dp))
            Button(
                onClick = {
                    if (busy) return@Button
                    busy = true
                    scope.launch {
                        try {
                            if (publicMode && !loggedIn) {
                                message = "请先登录后发表公开评论。"
                            } else if (publicMode && content.trim().length < 20) {
                                message = "公开评论至少需要 20 个字符，请写些具体体验。"
                            } else {
                                SlgApi.commentPost(slug, content.trim(), publicMode)
                                message = if (publicMode) "✓ 已提交，待审核" else "✓ 已保存"
                                kotlinx.coroutines.delay(600)
                                onDone()
                            }
                        } catch (e: CancellationException) { throw e }
                        catch (e: Exception) {
                            message = e.message ?: "保存失败，请重试"
                        } finally {
                            busy = false
                        }
                    }
                },
                enabled = content.isNotBlank() && !busy && (!publicMode || loggedIn),
                modifier = Modifier.fillMaxWidth(),
            ) { Text(if (publicMode) "公开发表" else "保存私人评论") }
        }
    }
}
