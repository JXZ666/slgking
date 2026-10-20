package com.jxz666.slgking.ui.game

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Modifier
import androidx.compose.ui.layout.Layout
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.constrainHeight
import com.jxz666.slgking.bridge.GameDetail
import com.jxz666.slgking.bridge.TagRow
import com.jxz666.slgking.bridge.SlgApi
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.delay
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.launch
import org.json.JSONArray
import org.json.JSONObject
import kotlin.coroutines.coroutineContext

private fun missingTranslationFields(detail: GameDetail) = translationFields(detail.item.title,
    detail.item.titleZh, detail.titleTranslationState, detail.overview, detail.overviewZh,
    detail.overviewTranslationState)

@Composable
internal fun ExpandableDetailText(text: String, collapsedLines: Int, stateKey: String) {
    var expanded by rememberSaveable(stateKey) { mutableStateOf(false) }
    var overflowing by remember(stateKey, text) { mutableStateOf(false) }
    Text(text, style = MaterialTheme.typography.bodyMedium,
        maxLines = if (expanded) Int.MAX_VALUE else collapsedLines,
        overflow = TextOverflow.Ellipsis,
        onTextLayout = { if (!expanded) overflowing = it.hasVisualOverflow })
    if (overflowing || expanded) TextButton(onClick = { expanded = !expanded }) {
        Text(if (expanded) "收起" else "展开全文")
    }
}

@Composable
internal fun DetailTags(gameId: Int, tags: List<TagRow>, onTagPicked: (String) -> Unit) {
    var expanded by rememberSaveable(gameId) { mutableStateOf(false) }
    var overflowing by remember(gameId, tags) { mutableStateOf(false) }
    if (tags.isEmpty()) { Text("暂无标签", style = MaterialTheme.typography.bodySmall); return }
    // Measure each chip with the available screen width. Unlike horizontal scrolling, all text wraps.
    Layout(content = {
        tags.forEach { tag -> FilterChip(selected = false, onClick = { onTagPicked(tag.name) },
            label = { Text(tag.display.ifBlank { tag.name }, softWrap = true) }) }
    }, modifier = Modifier.fillMaxWidth()) { measurables, constraints ->
        val gap = 6.dp.roundToPx()
        val children = measurables.map { it.measure(constraints.copy(minWidth = 0, minHeight = 0)) }
        val rows = tagRows(children.map { it.width }, constraints.maxWidth, gap)
        val visible = if (expanded) rows else rows.take(3)
        val heights = visible.map { row -> row.maxOf { children[it].height } }
        overflowing = rows.size > 3
        val height = heights.sum() + gap * (visible.size - 1).coerceAtLeast(0)
        layout(constraints.maxWidth, constraints.constrainHeight(height)) {
            var y = 0
            visible.forEachIndexed { rowIndex, row ->
                var x = 0
                row.forEach { index -> children[index].placeRelative(x, y); x += children[index].width + gap }
                y += heights[rowIndex] + gap
            }
        }
    }
    if (overflowing) TextButton(onClick = { expanded = !expanded }) {
        Text(if (expanded) "收起标签" else "展开全部标签（${tags.size}）")
    }
}

@Composable
internal fun DetailOverview(detail: GameDetail, chinese: Boolean) {
    Text("简介 · ${if (chinese) "中文" else "原文"}", style = MaterialTheme.typography.titleSmall)
    Spacer(Modifier.height(4.dp))
    val original = detail.overview?.takeIf { it.isNotBlank() }
    if (original == null && detail.overviewZh.isNullOrBlank()) {
        Text(if (detail.item.catalogId == "dlsite") "此目录仅收录商品基本资料，暂无简介原文。可通过上方来源网页查看商品详情。" else "暂无简介原文。",
            style = MaterialTheme.typography.bodyMedium)
    } else {
        val displayed = if (chinese) detail.overviewZh?.takeIf { it.isNotBlank() } ?: original else original
        if (chinese && detail.overviewZh.isNullOrBlank()) Text("暂无中文译文，以下保留原文。", style = MaterialTheme.typography.bodySmall)
        ExpandableDetailText(displayed ?: "暂无原文", 6, "overview-${detail.item.id}-$chinese")
    }
}

@Composable
internal fun DetailTranslationHeader(detail: GameDetail, chinese: Boolean, setChinese: (Boolean) -> Unit,
    onMutation: () -> Unit, openSettings: () -> Unit) {
    val scope = rememberCoroutineScope()
    var running by remember(detail.item.id) { mutableStateOf(false) }
    var activeFields by remember(detail.item.id) { mutableStateOf(emptyList<String>()) }
    var fieldErrors by remember(detail.item.id) { mutableStateOf(emptyMap<String, String>()) }
    var editor by remember(detail.item.id) { mutableStateOf<String?>(null) }
    var requested by rememberSaveable(detail.item.id) { mutableStateOf(false) }
    fun translate() {
        if (running) return
        val fields = missingTranslationFields(detail)
        if (fields.isEmpty()) return
        running = true; activeFields = fields; fieldErrors = emptyMap()
        scope.launch {
            try {
                val start = SlgApi.raw("game.translate.start", JSONObject().put("id", detail.item.id).put("fields", JSONArray(fields)))
                val job = start.optString("job")
                check(job.isNotBlank()) { "翻译任务未启动，请重试" }
                val observed = mutableSetOf<String>()
                fun consume(snapshot: JSONObject) {
                    val completed = snapshot.optJSONObject("summary")?.optJSONObject("fields")
                    val newFields = fields.filter { field ->
                        field !in observed && completed?.optJSONObject(field)?.optString("status") in
                            setOf("succeeded", "cached", "skipped", "error", "failed", "untranslatable")
                    }
                    if (newFields.isEmpty()) return
                    val failures = fieldErrors.toMutableMap()
                    newFields.forEach { field ->
                        val value = completed!!.getJSONObject(field)
                        val error = value.optString("error").takeIf { it.isNotBlank() && it != "null" }
                        if (error != null) failures[field] = error
                        else if (value.optString("status") in setOf("error", "failed"))
                            failures[field] = "翻译失败，请检查翻译设置后重试"
                        observed.add(field)
                    }
                    activeFields = fields.filterNot { it in observed }
                    fieldErrors = failures
                    // A completed overview must be visible while a slower title is still running (and vice versa).
                    // The body is keyed by gameId; disposal cancels this observer before it can refresh another game.
                    onMutation()
                }
                var result = start
                while (true) {
                    coroutineContext.ensureActive()
                    consume(result)
                    if (!result.optBoolean("running", true)) break
                    delay(400)
                    result = SlgApi.raw("job.poll", JSONObject().put("job", job))
                }
                result.optString("error").takeIf { it.isNotBlank() && it != "null" }?.let { error ->
                    fieldErrors = fieldErrors + fields.filterNot { it in observed }.associateWith { error }
                }
            } catch (e: CancellationException) { throw e }
            catch (e: Exception) {
                fieldErrors = fieldErrors + activeFields.associateWith { e.message ?: "翻译失败，请检查设置" }
            }
            finally { running = false; activeFields = emptyList() }
        }
    }
    LaunchedEffect(chinese) { if (chinese && !requested) { requested = true; translate() } }
    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        FilterChip(selected = !chinese, onClick = { setChinese(false) }, label = { Text("原文") })
        FilterChip(selected = chinese, onClick = { setChinese(true) }, label = { Text("中文") })
    }
    Text(if (chinese) detail.item.titleZh?.takeIf { it.isNotBlank() } ?: detail.item.title else detail.item.title,
        style = MaterialTheme.typography.titleLarge)
    if (chinese && !detail.item.titleZh.isNullOrBlank()) Text("原名：${detail.item.title}", style = MaterialTheme.typography.bodySmall)
    if (chinese) {
        listOf("title" to "标题", "overview" to "简介").forEach { (field, label) ->
            if (running && field in activeFields) Text("$label：翻译中…", style = MaterialTheme.typography.bodySmall)
            fieldErrors[field]?.let { Text("$label：$it", color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall) }
        }
        if (detail.titleTranslationState == "untranslatable") Text("标题无需翻译或暂无可用译文。", style = MaterialTheme.typography.bodySmall)
        if (detail.overviewTranslationState == "untranslatable") Text("简介暂无可用译文，保留原文。", style = MaterialTheme.typography.bodySmall)
        if (missingTranslationFields(detail).isNotEmpty() && !running) {
            TextButton(onClick = { requested = true; translate() }) { Text(if (fieldErrors.isEmpty()) "翻译缺失内容" else "重试翻译") }
        }
        TextButton(onClick = openSettings) { Text("翻译设置（免费／API）") }
    }
    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        TextButton(modifier = Modifier.weight(1f), onClick = { editor = "title" }, enabled = !running) { Text("编辑中文标题") }
        TextButton(modifier = Modifier.weight(1f), onClick = { editor = "overview" }, enabled = !running) { Text("编辑中文简介") }
    }
    editor?.let { kind -> TranslationEditor(detail, kind, onDismiss = { editor = null },
        onSaved = { editor = null; onMutation() }) }
}

@Composable
private fun TranslationEditor(detail: GameDetail, kind: String, onDismiss: () -> Unit, onSaved: () -> Unit) {
    var text by remember(detail.item.id, kind) { mutableStateOf(if (kind == "title") detail.item.titleZh ?: "" else detail.overviewZh ?: "") }
    var saving by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    val scope = rememberCoroutineScope()
    fun save(value: String) {
        if (saving) return
        saving = true
        scope.launch {
            try {
                SlgApi.raw("game.translation.save", JSONObject().put("id", detail.item.id).put("kind", kind).put("text", value))
                coroutineContext.ensureActive(); onSaved()
            } catch (e: CancellationException) { throw e }
            catch (e: Exception) { error = e.message ?: "保存失败" }
            finally { saving = false }
        }
    }
    AlertDialog(onDismissRequest = { if (!saving) onDismiss() }, title = { Text(if (kind == "title") "编辑中文标题" else "编辑中文简介") },
        text = { Column {
            Text("只保存在本机。清空可移除人工覆盖，恢复已有自动译文或原文。", style = MaterialTheme.typography.bodySmall)
            OutlinedTextField(value = text, onValueChange = { text = it }, enabled = !saving,
                modifier = Modifier.fillMaxWidth(), minLines = if (kind == "title") 1 else 4, maxLines = 8)
            error?.let { Text(it, color = MaterialTheme.colorScheme.error) }
            TextButton(onClick = { save("") }, enabled = !saving) { Text("清除人工译文，恢复自动") }
        } },
        confirmButton = { TextButton(onClick = { save(text) }, enabled = !saving) { Text(if (saving) "保存中…" else "保存") } },
        dismissButton = { TextButton(onClick = onDismiss, enabled = !saving) { Text("取消") } })
}

@Composable
internal fun DetailTagsEditor(detail: GameDetail, onSaved: () -> Unit, onDismiss: () -> Unit) {
    var available by remember { mutableStateOf(emptyList<TagRow>()) }
    var selected by remember { mutableStateOf(detail.tags.associate { it.name to it.display }) }
    var query by remember { mutableStateOf("") }
    var saving by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    val scope = rememberCoroutineScope()
    LaunchedEffect(detail.item.id) {
        try { available = SlgApi.tags(catalogId = detail.item.catalogId) }
        catch (e: CancellationException) { throw e }
        catch (_: Exception) { error = "标签列表未加载，可重试打开或手动填写标签。" }
    }
    AlertDialog(onDismissRequest = { if (!saving) onDismiss() }, title = { Text("编辑本机标签") },
        text = { Column(Modifier.heightIn(max = 440.dp).verticalScroll(rememberScrollState())) {
            OutlinedTextField(value = query, onValueChange = { query = it }, label = { Text("搜索已有标签或输入新标签") }, enabled = !saving)
            Text("已选（点击移除）", style = MaterialTheme.typography.titleSmall)
            selected.forEach { (name, display) -> TextButton(onClick = { selected = selected - name }, enabled = !saving) { Text("$display ×") } }
            val term = query.trim()
            if (term.isNotEmpty() && term !in selected) TextButton(onClick = { selected = selected + (term to term); query = "" }, enabled = !saving) { Text("添加：$term") }
            available.filter { it.name !in selected && (term.isBlank() || it.name.contains(term, true) || it.display.contains(term, true)) }.take(50).forEach { tag ->
                TextButton(onClick = { selected = selected + (tag.name to tag.display) }, enabled = !saving) { Text(tag.display.ifBlank { tag.name }) }
            }
            Text("可继续搜索其他标签。标签仅改变本机记录。", style = MaterialTheme.typography.bodySmall)
            error?.let { Text(it, color = MaterialTheme.colorScheme.error) }
        } },
        confirmButton = { TextButton(enabled = !saving, onClick = {
            if (!saving) { saving = true; scope.launch {
                try {
                    SlgApi.raw("game.tags.set", JSONObject().put("id", detail.item.id).put("tags", JSONArray(selected.keys.toList())))
                    coroutineContext.ensureActive(); onSaved()
                } catch (e: CancellationException) { throw e }
                catch (e: Exception) { error = e.message ?: "保存失败" }
                finally { saving = false }
            } }
        }) { Text(if (saving) "保存中…" else "保存") } },
        dismissButton = { TextButton(onClick = onDismiss, enabled = !saving) { Text("取消") } })
}
