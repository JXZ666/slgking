package com.jxz666.slgking.ui.tools

import android.app.DownloadManager
import android.content.Intent
import android.net.Uri
import android.text.format.Formatter
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.FilterChip
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.unit.dp
import com.jxz666.slgking.BuildConfig
import com.jxz666.slgking.bridge.SlgApi
import com.jxz666.slgking.bridge.SlgApiException
import com.jxz666.slgking.bridge.Stats
import com.jxz666.slgking.tools.UpdateInstaller
import com.jxz666.slgking.ui.ThemePrefs
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import org.json.JSONObject
import java.io.File

/** 工具 Tab：目录同步 + 翻译 + 诊断 + 公告 + 帮助/关于 + 主题 + 备份导入 + 应用内更新。 */
@Composable
fun ToolsScreen(openTranslationSettings: Boolean = false, onTranslationSettingsConsumed: () -> Unit = {}) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()

    var stats by remember { mutableStateOf<Stats?>(null) }
    var syncing by remember { mutableStateOf(false) }
    var syncJobId by remember { mutableStateOf<String?>(null) }
    var syncGeneration by remember { mutableStateOf(0) }
    var syncCancelRequested by remember { mutableStateOf(false) }
    var stopRequestBusy by remember { mutableStateOf(false) }
    var syncText by remember { mutableStateOf<String?>(null) }
    var syncProgress by remember { mutableStateOf<Pair<Long, Long>?>(null) }
    var syncError by remember { mutableStateOf<String?>(null) }
    var syncSummary by remember { mutableStateOf<String?>(null) }
    var release by remember { mutableStateOf<JSONObject?>(null) }
    var sheet by remember { mutableStateOf<String?>(null) }
    LaunchedEffect(openTranslationSettings) {
        if (openTranslationSettings) { sheet = "translate"; onTranslationSettingsConsumed() }
    }
    var importResult by remember { mutableStateOf<String?>(null) }
    var backupBusy by remember { mutableStateOf(false) }
    var pendingBackup by remember { mutableStateOf<File?>(null) }
    var backupPreview by remember { mutableStateOf<JSONObject?>(null) }
    var download by remember { mutableStateOf<UpdateInstaller.Download?>(null) }
    var updateError by remember { mutableStateOf<String?>(null) }
    var installing by remember { mutableStateOf(false) }

    DisposableEffect(Unit) {
        onDispose { pendingBackup?.delete() }
    }

    LaunchedEffect(Unit) {
        while (isActive) {
            try {
                download = withContext(Dispatchers.IO) { UpdateInstaller.current(context) }
            } catch (e: CancellationException) { throw e } catch (e: Exception) { updateError = e.message }
            delay(1000)
        }
    }

    fun refreshStats() {
        scope.launch {
            try { stats = SlgApi.stats() } catch (e: CancellationException) { throw e } catch (_: Exception) { }
        }
    }

    fun checkUpdate(force: Boolean = false) {
        scope.launch {
            try {
                release = SlgApi.updateCheck(com.jxz666.slgking.BuildConfig.VERSION_NAME, force)
            } catch (e: CancellationException) { throw e } catch (_: Exception) { }
        }
    }

    fun startSync(resume: Boolean = false) {
        if (syncing) return
        val generation = ++syncGeneration
        syncing = true
        syncJobId = null
        syncCancelRequested = false
        stopRequestBusy = false
        syncText = null
        syncProgress = null
        syncError = null
        syncSummary = null
        scope.launch {
            try {
                // Resuming only attaches a listener; it never starts another Python worker.
                val config = if (resume) JSONObject() else SlgApi.remoteConfig()
                if (!resume && (config.optBoolean("maintenance") ||
                    config.optString("maintenance").equals("true", true))) {
                    syncError = config.optString("maintenance_msg")
                        .takeIf { it.isNotBlank() } ?: "服务器维护中，同步功能暂时不可用"
                    return@launch
                }
                val selectedCatalog = context.getSharedPreferences("slgking_shell", 0)
                    .getString("catalog_id", "western_slg") ?: "western_slg"
                var job = if (resume) SlgApi.syncPoll("") else SlgApi.syncStart(selectedCatalog)
                val jobId = job.id
                if (jobId.isBlank()) throw SlgApiException("同步任务编号为空")
                while (true) {
                    if (generation != syncGeneration) return@launch
                    if (job.id != jobId) throw SlgApiException("同步任务已变更，请重新进入工具页")
                    syncJobId = jobId
                    val stage = job.stage
                    if (stage != null) {
                        val total = stage.optLong("total", -1).takeIf { it > 0 }
                        val done = stage.optLong("done", 0)
                        syncText = SyncStagePolicy.label(stage.optString("catalog_id"), stage.optString("phase"), done, total, stage.optString("unit"))
                        syncProgress = SyncStagePolicy.progress(done, total)
                    } else {
                        syncText = job.text
                        syncProgress = null
                    }
                    syncCancelRequested = syncCancelRequested || job.cancelRequested
                    if (!job.running) break
                    delay(500)
                    job = SlgApi.syncPoll(jobId)
                }
                syncError = job.error
                val summary = job.summary
                val counts = summary?.let { s ->
                    "新增 ${s.optInt("new")} · 封面 ${s.optInt("covers")}" +
                        (if (s.optBoolean("unchanged")) " · 目录已是最新" else "") +
                        " · 目录 ${s.optInt("catalogue")} 款"
                }
                val result = when {
                    job.cancelled && job.catalogUpdated -> "目录已更新，封面下载已停止" +
                        (counts?.let { "\n$it" } ?: "")
                    job.cancelled -> "已停止，目录未变更"
                    else -> counts
                }
                val sourceNote = sourceStatusNote(summary?.optJSONObject("source_status"))
                syncSummary = listOfNotNull(result, sourceNote.takeIf { it.isNotEmpty() })
                    .joinToString("\n").takeIf { it.isNotEmpty() }
                summary?.optJSONArray("catalogues")?.let { results ->
                    syncSummary = (0 until results.length()).joinToString("\n") { i ->
                        val r = results.getJSONObject(i)
                        val name = if (r.optString("catalog_id") == "dlsite") "DLsite" else "SLG大类"
                        when {
                            r.has("error") -> "$name：失败，${r.optString("error") }"
                            r.optBoolean("cancelled") -> "$name：已停止" + if (r.optBoolean("catalog_updated")) "，目录已更新" else "，目录未变更"
                            else -> "$name：目录 ${r.optInt("catalogue")} 款，新增 ${r.optInt("new")}"
                        }
                    } + if (job.cancelled) "\n已停止，剩余目录未继续更新" else ""
                }
            } catch (e: CancellationException) {
                // Leaving this tab cancels the listener, not the underlying sync task.
                throw e
            } catch (e: SlgApiException) {
                if (!resume || e.message != "没有该同步任务") syncError = e.message
            } catch (e: Exception) {
                syncError = "${e.javaClass.simpleName}: ${e.message}"
            } finally {
                if (generation == syncGeneration) {
                    syncing = false
                    syncJobId = null
                    syncCancelRequested = false
                    stopRequestBusy = false
                    refreshStats()
                }
            }
        }
    }

    fun stopSync() {
        val jobId = syncJobId ?: return
        if (!syncing || syncCancelRequested || stopRequestBusy) return
        val generation = syncGeneration
        stopRequestBusy = true
        syncError = null
        scope.launch {
            try {
                SlgApi.syncCancel(jobId)
                if (generation == syncGeneration && syncing && syncJobId == jobId) {
                    syncCancelRequested = true
                }
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                if (generation == syncGeneration && syncing && syncJobId == jobId) {
                    syncError = "停止请求失败：${e.message ?: e.javaClass.simpleName}，可重试"
                }
            } finally {
                if (generation == syncGeneration && syncJobId == jobId) stopRequestBusy = false
            }
        }
    }

    LaunchedEffect(Unit) {
        refreshStats()
        checkUpdate()
        startSync(resume = true)
    }

    val backupLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.OpenDocument()) { uri ->
        if (uri != null && !backupBusy && pendingBackup == null) {
            backupBusy = true
            scope.launch {
                var tmp: File? = null
                try {
                    // Allocate before suspending so cancellation can always clean up the file.
                    tmp = File(context.cacheDir, "import-${java.util.UUID.randomUUID()}.json")
                    val file = tmp
                    withContext(Dispatchers.IO) {
                        val input = context.contentResolver.openInputStream(uri)
                            ?: error("无法读取备份文件")
                        input.use { stream -> file.outputStream().use { stream.copyTo(it) } }
                        require(file.length() > 0) { "备份文件为空" }
                    }
                    backupPreview = SlgApi.backupPreview(file.absolutePath).getJSONObject("preview")
                    pendingBackup = file
                    tmp = null
                } catch (e: CancellationException) { throw e } catch (e: Exception) { importResult = "备份预检失败：${e.message}"
                } finally {
                    tmp?.delete()
                    backupBusy = false
                }
            }
        }
    }

    val exportLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.CreateDocument("application/json")) { uri ->
        if (uri != null && !backupBusy) {
            backupBusy = true
            scope.launch {
                try {
                    val result = SlgApi.backupExport()
                    val payload = result.getJSONObject("data")
                    val data = JSONObject().put("app", "slgking")
                        .put("version", BuildConfig.VERSION_NAME).put("data", payload)
                    withContext(Dispatchers.IO) {
                        val out = context.contentResolver.openOutputStream(uri, "wt")
                            ?: error("无法写入备份文件")
                        out.use { it.write(data.toString().toByteArray(Charsets.UTF_8)); it.flush() }
                    }
                    importResult = "备份已导出（评分/状态/收藏夹/自建游戏/分类/私人评论/偏好）"
                } catch (e: CancellationException) { throw e } catch (e: Exception) { importResult = "导出失败：${e.message}"
                } finally { backupBusy = false }
            }
        }
    }

    pendingBackup?.let { file ->
        fun cancelImport() {
            file.delete()
            pendingBackup = null
            backupPreview = null
        }
        AlertDialog(
            onDismissRequest = { if (!backupBusy) cancelImport() },
            title = { Text("确认导入备份") },
            text = {
                val preview = backupPreview
                Text("已识别 ${preview?.optInt("total_records") ?: 0} 条记录。\n" +
                    "将覆盖本机的评分、状态、备注、收藏夹、标签翻译、本机积分与签到记录、头衔及排除项；备份未包含的记录也可能清除。\n" +
                    "自建游戏会合并。建议先导出当前资料再导入。\n" +
                    "PC 本地路径 ${preview?.optInt("local_path_count") ?: 0} 条，仅保留信息，手机不能启动 PC 游戏。\n" +
                    "取消不会修改任何资料。")
            },
            confirmButton = {
                TextButton(enabled = !backupBusy, onClick = {
                    backupBusy = true
                    pendingBackup = null
                    backupPreview = null
                    scope.launch {
                        try {
                            val imported = SlgApi.backupImport(file.absolutePath).optJSONObject("imported")
                            val parts = mutableListOf<String>()
                            imported?.keys()?.let { keys ->
                                while (keys.hasNext()) {
                                    val key = keys.next()
                                    parts.add("$key ${imported.optInt(key)}")
                                }
                            }
                            importResult = if (parts.isEmpty()) "导入完成：无变化"
                                else "导入完成：" + parts.joinToString(" · ")
                            refreshStats()
                        } catch (e: CancellationException) { throw e } catch (e: Exception) { importResult = "导入失败：${e.message}"
                        } finally { file.delete(); backupBusy = false }
                    }
                }) { Text("确认覆盖本机资料") }
            },
            dismissButton = { TextButton(enabled = !backupBusy, onClick = { cancelImport() }) { Text("取消") } },
        )
    }

    LazyColumn(
        Modifier.fillMaxSize(),
        contentPadding = androidx.compose.foundation.layout.PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        // 应用内更新
        release?.let { rel ->
            item {
                Card {
                    Column(Modifier.padding(16.dp)) {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Text("发现新版本 ${rel.optString("version")}",
                                style = MaterialTheme.typography.titleSmall,
                                color = MaterialTheme.colorScheme.primary,
                                modifier = Modifier.weight(1f))
                        }
                        Spacer(Modifier.height(4.dp))
                        Text(rel.optString("name"), style = MaterialTheme.typography.labelMedium)
                        Text(rel.optString("body").take(300),
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant, maxLines = 6)
                        Spacer(Modifier.height(8.dp))
                        val apkUrl = rel.optString("asset_url").takeIf {
                            !rel.isNull("asset_url") && it.isNotBlank() && it != "null"
                        }
                        val assetError = rel.optString("asset_error").takeIf { it.isNotBlank() && it != "null" }
                        assetError?.let { Text(it, color = MaterialTheme.colorScheme.error) }
                        if (apkUrl != null && assetError == null) {
                            val active = download?.status in listOf(DownloadManager.STATUS_PENDING,
                                DownloadManager.STATUS_RUNNING, DownloadManager.STATUS_PAUSED)
                            Button(enabled = !active, onClick = {
                                scope.launch {
                                    try {
                                        updateError = null
                                        download = withContext(Dispatchers.IO) { UpdateInstaller.download(context, apkUrl) }
                                    } catch (e: CancellationException) { throw e } catch (e: Exception) { updateError = e.message }
                                }
                            }) { Text(if (active) "正在下载" else "下载更新包") }
                        } else {
                            OutlinedButton(onClick = { UpdateInstaller.openReleasesPage(context) }) {
                                Text("到发布页查看")
                            }
                        }
                        Spacer(Modifier.height(4.dp))
                        TextButton(onClick = { release = null }) { Text("本次忽略") }
                    }
                }
            }
        }
        download?.let { state ->
            item {
                Card {
                    Column(Modifier.padding(16.dp)) {
                        Text("更新包", style = MaterialTheme.typography.titleSmall)
                        when (state.status) {
                            DownloadManager.STATUS_PENDING -> Text("等待下载…")
                            DownloadManager.STATUS_RUNNING, DownloadManager.STATUS_PAUSED -> {
                                Text(if (state.status == DownloadManager.STATUS_PAUSED) "下载已暂停，等待网络" else "正在下载…")
                                if (state.total > 0) LinearProgressIndicator(
                                    progress = { (state.received.toFloat() / state.total).coerceIn(0f, 1f) },
                                    modifier = Modifier.fillMaxWidth())
                                else LinearProgressIndicator(Modifier.fillMaxWidth())
                            }
                            DownloadManager.STATUS_SUCCESSFUL -> {
                                Text("下载完成。安装前将检查应用、版本与签名。")
                                Button(enabled = !installing, onClick = {
                                    installing = true
                                    scope.launch {
                                        try {
                                            updateError = null
                                            withContext(Dispatchers.IO) { UpdateInstaller.validate(context, state.fileName) }
                                            if (UpdateInstaller.canInstall(context)) UpdateInstaller.install(context, state.fileName)
                                            else {
                                                updateError = "请允许安装应用后返回，再点击安装更新包"
                                                UpdateInstaller.requestInstallPermission(context)
                                            }
                                        } catch (e: CancellationException) { throw e } catch (e: Exception) { updateError = e.message
                                        } finally { installing = false }
                                    }
                                }) { Text("安装更新包") }
                                OutlinedButton(enabled = !installing, onClick = {
                                    scope.launch {
                                        try {
                                            updateError = null
                                            download = withContext(Dispatchers.IO) {
                                                UpdateInstaller.discard(context)
                                                UpdateInstaller.download(context, state.url)
                                            }
                                        } catch (e: CancellationException) { throw e
                                        } catch (e: Exception) { updateError = e.message }
                                    }
                                }) { Text("重新下载") }
                            }
                            else -> {
                                Text(state.error ?: "下载失败，请重试", color = MaterialTheme.colorScheme.error)
                                OutlinedButton(onClick = {
                                    scope.launch {
                                        try {
                                            updateError = null
                                            download = withContext(Dispatchers.IO) { UpdateInstaller.download(context, state.url) }
                                        } catch (e: CancellationException) { throw e } catch (e: Exception) { updateError = e.message }
                                    }
                                }) { Text("重试下载") }
                            }
                        }
                    }
                }
            }
        }
        updateError?.let { message -> item { Text(message, color = MaterialTheme.colorScheme.error) } }
        if (backupBusy) item { LinearProgressIndicator(Modifier.fillMaxWidth()) }
        importResult?.let { msg ->
            item {
                Card {
                    Column(Modifier.padding(14.dp)) {
                        Text(msg, style = MaterialTheme.typography.bodySmall)
                        TextButton(onClick = { importResult = null }) { Text("关闭") }
                    }
                }
            }
        }
        // 目录同步
        item {
            val prefs = context.getSharedPreferences("slgking_shell", 0)
            var selected by remember { mutableStateOf(prefs.getString("catalog_id", "western_slg") ?: "western_slg") }
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                listOf("western_slg" to "SLG大类", "dlsite" to "DLsite", "all" to "全部").forEach { (id, label) ->
                    FilterChip(selected = selected == id, enabled = !syncing,
                        onClick = { selected = id; prefs.edit().putString("catalog_id", id).apply() },
                        label = { Text(label) })
                }
            }
            SyncCard(
                stats = stats, syncing = syncing, syncText = syncText,
                syncProgress = syncProgress, syncError = syncError, syncSummary = syncSummary,
                stopping = stopRequestBusy || syncCancelRequested,
                canStop = syncJobId != null,
                onStart = { startSync() }, onStop = { stopSync() },
            )
        }
        // 主题
        item {
            Card {
                Column(Modifier.padding(16.dp)) {
                    Text("主题", style = MaterialTheme.typography.titleMedium)
                    Spacer(Modifier.height(8.dp))
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        listOf("system" to "跟随系统", "light" to "浅色", "dark" to "深色").forEach { (k, label) ->
                            FilterChip(selected = ThemePrefs.mode == k,
                                onClick = { ThemePrefs.set(context, k) },
                                label = { Text(label) })
                        }
                    }
                }
            }
        }
        // 工具入口
        item {
            Card {
                Column(Modifier.padding(vertical = 6.dp)) {
                    ToolEntry("AI 翻译设置") { sheet = "translate" }
                    ToolEntry("隐藏条目管理") { sheet = "hidden" }
                    ToolEntry("连接诊断") { sheet = "diagnose" }
                    ToolEntry("公告") { sheet = "announce" }
                    ToolEntry("导入 PC 备份（评分/收藏夹/私人评论）", enabled = !backupBusy && pendingBackup == null) {
                        backupLauncher.launch(arrayOf("application/json", "*/*"))
                    }
                    ToolEntry("导出备份（手机 → PC / 换机迁移）", enabled = !backupBusy && pendingBackup == null) {
                        exportLauncher.launch("slgking-backup.json")
                    }
                    ToolEntry("意见反馈") { sheet = "feedback" }
                    ToolEntry("帮助文档") { sheet = "help" }
                    ToolEntry("关于 / 免费声明 / GPL-3.0") { sheet = "about" }
                    ToolEntry("网页版（手机浏览器）") {
                        try {
                            context.startActivity(
                                Intent(Intent.ACTION_VIEW, Uri.parse("https://slg-king.com/m/#/"))
                                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
                        } catch (e: CancellationException) { throw e } catch (_: Exception) { }
                    }
                }
            }
        }
    }

    when (sheet) {
        "translate" -> TranslateSheet(onDismiss = { sheet = null })
        "diagnose" -> DiagnoseSheet(onDismiss = { sheet = null })
        "announce" -> AnnounceSheet(onDismiss = { sheet = null })
        "hidden" -> HiddenGamesSheet(onDismiss = { sheet = null })
        "help" -> HelpSheet(onDismiss = { sheet = null })
        "about" -> AboutSheet(onDismiss = { sheet = null })
        "feedback" -> FeedbackSheet(onDismiss = { sheet = null })
    }
}

/** 意见反馈：bug/建议/数据纠错/其他，20-500 字，需登录。 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun FeedbackSheet(onDismiss: () -> Unit) {
    var category by remember { mutableStateOf("bug") }
    var content by remember { mutableStateOf("") }
    var message by remember { mutableStateOf<String?>(null) }
    var busy by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()

    ModalBottomSheet(onDismissRequest = onDismiss) {
        Column(Modifier.padding(horizontal = 16.dp).padding(bottom = 28.dp).imePadding()) {
            Text("意见反馈", style = MaterialTheme.typography.titleMedium)
            Text("反馈仅开发者可见，需登录账号（内容 20–500 字）",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
            Spacer(Modifier.height(8.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                listOf("bug" to "问题", "suggestion" to "建议",
                    "data_correction" to "数据纠错", "other" to "其他").forEach { (k, label) ->
                    FilterChip(selected = category == k, onClick = { category = k },
                        label = { Text(label) })
                }
            }
            Spacer(Modifier.height(8.dp))
            OutlinedTextField(
                value = content,
                onValueChange = { if (it.length <= 500) content = it },
                minLines = 4, maxLines = 8,
                modifier = Modifier.fillMaxWidth(),
                placeholder = { Text("请具体描述（20 字起）…") },
            )
            Text("${content.length}/500", style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
            message?.let {
                Text(it, style = MaterialTheme.typography.labelMedium,
                    color = if (it.startsWith("✓")) MaterialTheme.colorScheme.primary
                    else MaterialTheme.colorScheme.error)
            }
            Spacer(Modifier.height(8.dp))
            Button(
                onClick = {
                    scope.launch {
                        busy = true
                        try {
                            SlgApi.feedbackSubmit(category, content.trim())
                            message = "✓ 反馈已提交，感谢！"
                            kotlinx.coroutines.delay(600)
                            onDismiss()
                        } catch (e: CancellationException) { throw e } catch (e: Exception) {
                            message = e.message
                        } finally { busy = false
                        }
                    }
                },
                enabled = content.trim().length >= 20 && !busy,
                modifier = Modifier.fillMaxWidth(),
            ) { Text("提交反馈") }
        }
    }
}

@Composable
private fun ToolEntry(label: String, enabled: Boolean = true, onClick: () -> Unit) {
    androidx.compose.material3.Surface(onClick = onClick, enabled = enabled,
        color = androidx.compose.ui.graphics.Color.Transparent) {
        Row(Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 13.dp),
            verticalAlignment = Alignment.CenterVertically) {
            Text(label, modifier = Modifier.weight(1f))
            Text("›", color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}

@Composable
private fun SyncCard(
    stats: Stats?,
    syncing: Boolean,
    syncText: String?,
    syncProgress: Pair<Long, Long>?,
    syncError: String?,
    syncSummary: String?,
    stopping: Boolean,
    canStop: Boolean,
    onStart: () -> Unit,
    onStop: () -> Unit,
) {
    Card {
        Column(Modifier.padding(16.dp)) {
            Text("游戏目录数据", style = MaterialTheme.typography.titleMedium)
            Spacer(Modifier.height(4.dp))
            Text(
                stats?.let {
                    "目录 ${it.games} 款 · 已下载 ${it.downloaded} · 已评分 ${it.rated}" +
                        (it.lastSync?.let { ls -> "\n上次同步 $ls" } ?: "")
                } ?: "读取中…",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Spacer(Modifier.height(10.dp))
            if (syncing) {
                Text(
                    if (stopping) "正在停止，等待当前操作结束" else syncText ?: "正在准备同步…",
                    style = MaterialTheme.typography.labelMedium,
                )
                syncProgress?.let { (d, t) ->
                    Spacer(Modifier.height(6.dp))
                    LinearProgressIndicator(
                        progress = { (d.toFloat() / t).coerceIn(0f, 1f) },
                        modifier = Modifier.fillMaxWidth(),
                    )
                }
                if (syncProgress == null) {
                    Spacer(Modifier.height(6.dp))
                    LinearProgressIndicator(Modifier.fillMaxWidth())
                }
                Spacer(Modifier.height(8.dp))
                OutlinedButton(
                    onClick = onStop, enabled = canStop && !stopping,
                    modifier = Modifier.fillMaxWidth(),
                ) { Text(if (stopping) "正在停止…" else "停止") }
            } else {
                Button(onClick = onStart, modifier = Modifier.fillMaxWidth()) {
                    Text("更新游戏数据")
                }
            }
            syncError?.let {
                Spacer(Modifier.height(6.dp))
                Text("同步失败：$it", color = MaterialTheme.colorScheme.error,
                    style = MaterialTheme.typography.bodySmall)
            }
            syncSummary?.let {
                Spacer(Modifier.height(6.dp))
                Text(it, style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.primary)
            }
        }
    }
}

/** AI 翻译设置（OpenAI 兼容）+ 标签翻译执行。 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun TranslateSheet(onDismiss: () -> Unit) {
    var settings by remember { mutableStateOf<JSONObject?>(null) }
    var provider by remember { mutableStateOf("deepseek") }
    var engine by remember { mutableStateOf("openai") }
    var baseUrl by remember { mutableStateOf("") }
    var model by remember { mutableStateOf("") }
    var apiKey by remember { mutableStateOf("") }
    var showKey by remember { mutableStateOf(false) }
    var advanced by remember { mutableStateOf(false) }
    var companyMenu by remember { mutableStateOf(false) }
    var modelMenu by remember { mutableStateOf(false) }
    var message by remember { mutableStateOf<String?>(null) }
    var loading by remember { mutableStateOf(true) }
    var saving by remember { mutableStateOf(false) }
    var running by remember { mutableStateOf(false) }
    var runText by remember { mutableStateOf<String?>(null) }
    var reload by remember { mutableStateOf(0) }
    val scope = rememberCoroutineScope()
    LaunchedEffect(reload) {
        loading = true
        try {
            val value = SlgApi.translateSettings()
            settings = value
            provider = value.optString("provider", "deepseek")
            engine = value.optString("engine", "google_free")
            baseUrl = value.optString("base_url")
            model = value.optString("model")
            running = value.optBoolean("running")
            message = null
        } catch (e: CancellationException) { throw e }
        catch (_: Exception) { message = "加载设置失败，请重试；原设置未修改" }
        finally { loading = false }
    }
    // A task may continue when the sheet is dismissed; don't allow concurrent saves.
    LaunchedEffect(running) {
        while (running) {
            delay(700)
            try { running = SlgApi.translateSettings().optBoolean("running") }
            catch (e: CancellationException) { throw e }
            catch (_: Exception) { /* Keep the lock until confirmed idle. */ }
        }
    }
    val presets = settings?.optJSONArray("presets")
    val companies = (0 until (presets?.length() ?: 0)).map { presets!!.getJSONObject(it) }
    val selected = companies.firstOrNull { it.optString("id") == provider }
    val models = selected?.optJSONArray("models")
    val modelNames = (0 until (models?.length() ?: 0)).map { models!!.getString(it) }
    val ready = settings != null && !loading && !saving && !running
    val sameTarget = provider == settings?.optString("provider") &&
        baseUrl.trim().trimEnd('/') == settings?.optString("base_url")?.trimEnd('/')
    val hasSavedKey = sameTarget && settings?.optBoolean("has_key") == true
    val dirty = settings != null && (provider != settings?.optString("provider") ||
        engine != settings?.optString("engine") || baseUrl.trim() != settings?.optString("base_url") ||
        model.trim() != settings?.optString("model") || apiKey.isNotBlank())

    ModalBottomSheet(onDismissRequest = { if (!saving) onDismiss() },
        sheetState = androidx.compose.material3.rememberModalBottomSheetState(skipPartiallyExpanded = true)) {
        LazyColumn(Modifier.fillMaxWidth().fillMaxHeight(0.85f).imePadding()
            .padding(horizontal = 16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            item {
                Text("翻译设置", style = MaterialTheme.typography.titleLarge)
                Text("API Key 保存在应用私有数据库；不会上传到项目服务器。",
                    style = MaterialTheme.typography.bodySmall)
            }
            if (loading) item { CircularProgressIndicator() }
            if (settings == null && !loading) item {
                Text(message ?: "加载失败", color = MaterialTheme.colorScheme.error)
                OutlinedButton(onClick = { reload++ }) { Text("重新加载") }
            }
            if (settings != null) {
                item {
                    Column(Modifier.fillMaxWidth()) {
                        OutlinedButton(onClick = { engine = "openai" }, enabled = ready,
                            modifier = Modifier.fillMaxWidth()) {
                            Text(if (engine == "openai") "已选：AI 服务商翻译" else "使用 AI 服务商翻译")
                        }
                        OutlinedButton(onClick = { engine = "google_free" }, enabled = ready,
                            modifier = Modifier.fillMaxWidth()) {
                            Text(if (engine == "google_free") "已选：免费机翻" else "使用免费机翻")
                        }
                    }
                }
                if (engine == "openai") {
                    item {
                        Box {
                            OutlinedButton(onClick = { companyMenu = true }, enabled = ready,
                                modifier = Modifier.fillMaxWidth()) {
                                Text("公司：${selected?.optString("label") ?: provider} ▾")
                            }
                            DropdownMenu(expanded = companyMenu, onDismissRequest = { companyMenu = false }) {
                                companies.forEach { company ->
                                    DropdownMenuItem(text = { Text(company.optString("label")) }, onClick = {
                                        val next = company.optString("id")
                                        if (next != provider) {
                                            provider = next
                                            baseUrl = company.optString("base_url")
                                            model = company.optString("default_model")
                                            apiKey = ""
                                        }
                                        companyMenu = false
                                    })
                                }
                            }
                        }
                    }
                    item {
                        Box {
                            OutlinedButton(onClick = { modelMenu = true }, enabled = ready && modelNames.isNotEmpty(),
                                modifier = Modifier.fillMaxWidth()) { Text("选择预置模型 ▾") }
                            DropdownMenu(expanded = modelMenu, onDismissRequest = { modelMenu = false }) {
                                (modelNames + listOf(model).filter { it.isNotBlank() && it !in modelNames }).forEach { name ->
                                    DropdownMenuItem(text = { Text(name) }, onClick = { model = name; modelMenu = false })
                                }
                            }
                        }
                        OutlinedTextField(value = model, onValueChange = { model = it }, enabled = ready,
                            label = { Text("模型名称（也可手动输入）") }, singleLine = true,
                            modifier = Modifier.fillMaxWidth())
                        if (selected?.optBoolean("compatibility") == true)
                            Text("兼容预置：请确认账号仍可使用该模型，或手动填写。", style = MaterialTheme.typography.bodySmall)
                    }
                    item {
                        OutlinedTextField(value = apiKey, onValueChange = { apiKey = it }, enabled = ready,
                            label = { Text(if (hasSavedKey) "API Key（已保存，留空保持）" else "API Key（当前接口必填）") },
                            visualTransformation = if (showKey) VisualTransformation.None else PasswordVisualTransformation(),
                            trailingIcon = { TextButton(onClick = { showKey = !showKey }) { Text(if (showKey) "隐藏" else "显示") } },
                            singleLine = true, modifier = Modifier.fillMaxWidth())
                    }
                    item {
                        TextButton(onClick = { advanced = !advanced }, enabled = ready) {
                            Text(if (advanced) "收起高级设置" else "高级设置：接口地址")
                        }
                        if (advanced || provider == "custom") {
                            OutlinedTextField(value = baseUrl, onValueChange = { baseUrl = it }, enabled = ready,
                                label = { Text("OpenAI 兼容接口地址") }, singleLine = true,
                                modifier = Modifier.fillMaxWidth())
                        }
                    }
                }
                message?.let { text -> item { Text(text, style = MaterialTheme.typography.bodyMedium) } }
                item {
                    Button(onClick = {
                        saving = true
                        message = null
                        scope.launch {
                            try {
                                val result = SlgApi.translateSave(JSONObject().put("provider", provider)
                                    .put("engine", engine).put("base_url", baseUrl.trim()).put("model", model.trim())
                                    .put("api_key", apiKey.trim()))
                                settings = result
                                apiKey = ""
                                message = "已保存"
                            } catch (e: CancellationException) { throw e }
                            catch (e: Exception) { message = e.message ?: "保存失败，原设置未修改" }
                            finally { saving = false }
                        }
                    }, enabled = ready, modifier = Modifier.fillMaxWidth()) {
                        Text(if (saving) "保存中…" else "保存设置")
                    }
                }
                item {
                    OutlinedButton(onClick = {
                        running = true
                        runText = "启动标签翻译…"
                        scope.launch {
                            try {
                                var job = SlgApi.raw("translate.tags.run")
                                while (job.optBoolean("running")) {
                                    runText = job.optString("text")
                                    delay(700)
                                    job = SlgApi.jobPoll(job.optString("job"))
                                }
                                runText = job.optJSONObject("summary")?.let { "完成：翻译 ${it.optInt("translated")} 个标签" }
                                    ?: job.optString("error", "任务结束")
                            } catch (e: CancellationException) { throw e }
                            catch (e: Exception) { runText = "失败：${e.message}" }
                            finally { running = false }
                        }
                    }, enabled = ready && !dirty && engine == "openai", modifier = Modifier.fillMaxWidth()) {
                        Text(if (running) "翻译中…" else "翻译标签")
                    }
                    Text(if (engine == "google_free") "免费机翻可翻译简介；固定标签需 AI 服务商。"
                        else "翻译标签将使用已保存配置并产生服务商费用。修改后请先保存。",
                        style = MaterialTheme.typography.bodySmall)
                }
                runText?.let { text -> item { Text(text, color = MaterialTheme.colorScheme.primary) } }
            }
            item { Spacer(Modifier.height(28.dp)) }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun DiagnoseSheet(onDismiss: () -> Unit) {
    var lines by remember { mutableStateOf<String?>(null) }
    val clipboard = LocalClipboardManager.current
    LaunchedEffect(Unit) {
        try {
            val arr = SlgApi.diagnose()
            lines = (0 until arr.length()).joinToString("\n") { i ->
                val row = arr.getJSONObject(i)
                "${row.optString("name", "路由")}：${row.optString("result", row.toString())}"
            }
        } catch (e: CancellationException) { throw e } catch (e: Exception) {
            lines = "诊断失败：${e.message}"
        }
    }
    ModalBottomSheet(onDismissRequest = onDismiss) {
        Column(Modifier.padding(horizontal = 16.dp).padding(bottom = 28.dp)) {
            Text("连接诊断", style = MaterialTheme.typography.titleMedium)
            Spacer(Modifier.height(8.dp))
            when {
                lines == null -> CircularProgressIndicator()
                else -> {
                    Text(lines!!, style = MaterialTheme.typography.bodySmall)
                    Spacer(Modifier.height(8.dp))
                    OutlinedButton(onClick = {
                        clipboard.setText(AnnotatedString(lines ?: ""))
                    }) { Text("复制结果") }
                }
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun AnnounceSheet(onDismiss: () -> Unit) {
    var config by remember { mutableStateOf<JSONObject?>(null) }
    LaunchedEffect(Unit) {
        try { config = SlgApi.remoteConfig() } catch (e: CancellationException) { throw e } catch (_: Exception) { }
    }
    ModalBottomSheet(onDismissRequest = onDismiss) {
        Column(Modifier.padding(horizontal = 16.dp).padding(bottom = 28.dp)) {
            Text("公告", style = MaterialTheme.typography.titleMedium)
            Spacer(Modifier.height(8.dp))
            val c = config
            when {
                c == null -> CircularProgressIndicator()
                c.optString("maintenance").toBoolean() || c.optBoolean("maintenance") ->
                    Text("服务器维护中：${c.optString("maintenance_msg", "请稍后再试")}",
                        color = MaterialTheme.colorScheme.error)
                c.optString("announcement").isNotBlank() ->
                    Text(c.optString("announcement"), style = MaterialTheme.typography.bodyMedium)
                c.optString("notice").isNotBlank() ->
                    Text(c.optString("notice"), style = MaterialTheme.typography.bodyMedium)
                else -> Text("暂无公告", color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun HelpSheet(onDismiss: () -> Unit) {
    var query by remember { mutableStateOf("") }
    ModalBottomSheet(onDismissRequest = onDismiss) {
        LazyColumn(Modifier.fillMaxWidth().fillMaxHeight(0.9f).padding(horizontal = 16.dp),
            contentPadding = androidx.compose.foundation.layout.PaddingValues(bottom = 28.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp)) {
            item { Text("帮助", style = MaterialTheme.typography.titleMedium) }
            item { OutlinedTextField(query, { query = it }, label = { Text("搜索标题和正文") }, modifier = Modifier.fillMaxWidth(), singleLine = true) }
            val topics = searchHelp(query)
            if (topics.isEmpty()) item { Text("没有匹配的帮助内容") }
            topics.forEach { topic -> item(key = topic.title) {
                Column { Text(topic.title, style = MaterialTheme.typography.titleSmall)
                    Text(topic.body, style = MaterialTheme.typography.bodyMedium) }
            } }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun AboutSheet(onDismiss: () -> Unit) {
    val clipboard = LocalClipboardManager.current
    val context = LocalContext.current
    ModalBottomSheet(onDismissRequest = onDismiss) {
        Column(Modifier.padding(horizontal = 16.dp).padding(bottom = 28.dp)) {
            Text("关于", style = MaterialTheme.typography.titleMedium)
            com.jxz666.slgking.ui.common.BrandAbout()
            Spacer(Modifier.height(8.dp))
            Text(
                "黄油之王 · 安卓版 v${BuildConfig.VERSION_NAME}\n\n" +
                    "本软件完全免费，如遇收费请立即举报。\n\n" +
                    "游戏目录数据来自项目服务器；本应用不托管游戏文件。\n\n" +
                    "许可证：GNU GPL v3.0（源码随项目发布）。\n\n" +
                    "安卓客户端由 zeratulzhou 移植（MIT 许可），谨此致谢。\n\n" +
                    "交流反馈：QQ 群 1124074040 · GitHub JXZ666/slgking\n\n" +
                    "网页版（手机浏览器浏览）：https://slg-king.com/m/#/",
                style = MaterialTheme.typography.bodyMedium,
            )
            Spacer(Modifier.height(8.dp))
            OutlinedButton(onClick = {
                clipboard.setText(AnnotatedString("1124074040"))
            }) { Text("复制 QQ 群号") }
            Spacer(Modifier.height(8.dp))
            OutlinedButton(onClick = {
                try {
                    context.startActivity(
                        Intent(Intent.ACTION_VIEW, Uri.parse("https://slg-king.com/m/#/"))
                            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
                } catch (e: CancellationException) { throw e } catch (_: Exception) { }
            }) { Text("打开网页版") }
        }
    }
}

/** Only display allowlisted collector states, never raw URLs or server error details. */
private fun sourceStatusNote(status: JSONObject?): String {
    status ?: return ""
    val label = when (status.optString("source_id")) {
        "f95zone" -> "F95镜像"
        "dlsite" -> "DLsite"
        else -> return ""
    }
    val state = when (status.optString("state")) {
        "complete" -> "采集完成"
        "partial" -> "预算内分批推进"
        "paused" -> "采集暂停"
        "error" -> "采集失败"
        "running" -> "采集中"
        else -> return ""
    }
    return "$label：" + if (status.optString("reason_code") == "awaiting_first_run") "等待首次采集" else state
}
