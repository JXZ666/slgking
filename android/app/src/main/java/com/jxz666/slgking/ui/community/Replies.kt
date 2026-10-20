package com.jxz666.slgking.ui.community

import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.jxz666.slgking.bridge.AccountChanges
import com.jxz666.slgking.bridge.SlgApi
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.launch
import org.json.JSONObject

/** Composed only after explicit expansion. No background prefetch. */
@Composable
fun ReplySection(rootId: String, count: Int = 0) {
    var expanded by remember(rootId, AccountChanges.revision) { mutableStateOf(false) }
    TextButton(onClick = { expanded = !expanded }) { Text(if (expanded) "收起追评" else "追评（$count）") }
    if (expanded) ReplyThread(rootId)
}

@Composable
internal fun ReplyThread(rootId: String, focusId: String? = null, onLoaded: suspend () -> Unit = {}) {
    val revision = AccountChanges.revision
    key(rootId, revision) { ReplyThreadContent(rootId, focusId, onLoaded) }
}

@Composable
private fun ReplyThreadContent(rootId: String, focusId: String?, onLoaded: suspend () -> Unit) {
    val identityRevision = AccountChanges.revision
    var page by remember { mutableStateOf(1) }
    var ownOnly by remember { mutableStateOf(false) }
    var refresh by remember { mutableStateOf(0) }
    var result by remember { mutableStateOf<JSONObject?>(null) }
    var rows by remember { mutableStateOf(emptyList<JSONObject>()) }
    var status by remember { mutableStateOf(JSONObject()) }
    var error by remember { mutableStateOf<String?>(null) }
    var busy by remember { mutableStateOf(false) }
    var loading by remember { mutableStateOf(false) }
    var content by remember { mutableStateOf("") }
    var target by remember { mutableStateOf<String?>(null) }
    var targetName by remember { mutableStateOf("") }
    var focusedLoaded by remember { mutableStateOf(false) }
    var generation by remember { mutableStateOf(0) }
    val operation = remember { ReplyOperation() }
    val scope = rememberCoroutineScope()
    fun current() = identityRevision == AccountChanges.revision
    LaunchedEffect(page, ownOnly, refresh) {
        val request = ++generation
        loading = true; rows = emptyList(); error = null; result = null
        try {
            status = SlgApi.raw("account.status")
            ensureActive(); if (!current() || generation != request) return@LaunchedEffect
            val p = JSONObject().put("root_id", rootId).put("page", page)
            if (!focusedLoaded && !ownOnly) focusId?.let { p.put("focus_id", it) }
            val public = SlgApi.raw(if (ownOnly) "replies.mine" else "replies.page", p)
            ensureActive(); if (!current() || generation != request) return@LaunchedEffect
            val publicRows = objects(public.optJSONArray("replies"))
            var mine = emptyList<JSONObject>()
            if (canWrite(status) && !ownOnly) {
                try { mine = objects(SlgApi.raw("replies.mine", JSONObject().put("root_id", rootId).put("page", 1)).optJSONArray("replies")) }
                catch (e: CancellationException) { throw e }
                catch (_: Exception) { error = "本人审核状态读取失败，可重试" }
            }
            ensureActive(); if (!current() || generation != request) return@LaunchedEffect
            result = public
            rows = if (ownOnly) publicRows.map { JSONObject(it.toString()).put("own", true) }
                else mergeReplies(publicRows, mine, public.optInt("page", page))
            if (!ownOnly && focusId != null && !focusedLoaded) {
                if (shouldMarkRead(focusId, publicRows)) { onLoaded(); ensureActive(); focusedLoaded = true }
                else error = "目标回复已不可查看，没有标记消息已读"
            }
        } catch (e: CancellationException) { throw e }
        catch (e: Exception) { if (current() && generation == request) error = e.message ?: "追评加载失败" }
        finally { if (current() && generation == request) loading = false }
    }
    Column(Modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(6.dp)) {
        if (canWrite(status)) FilterChip(selected = ownOnly, onClick = { if (!busy) { ownOnly = !ownOnly; page = 1 } }, label = { Text("仅看我的审核记录") })
        if (loading) LinearProgressIndicator(Modifier.fillMaxWidth())
        error?.let { Text(it, color = MaterialTheme.colorScheme.error); TextButton(onClick = { refresh++ }, enabled = !busy && !loading) { Text("重试") } }
        rows.forEach { row ->
            val id = opaqueId(row) ?: return@forEach
            Card(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(10.dp)) {
                    PublicAuthor(row)
                    row.optString("reply_to_nickname").takeIf { it.isNotBlank() && it != "null" }?.let { Text("回复 $it", style = MaterialTheme.typography.labelSmall) }
                    Text(row.optString("content"))
                    Text(row.optString("created"), style = MaterialTheme.typography.labelSmall)
                    val own = row.optBoolean("own")
                    if (own) Text(when (row.optString("status")) { "pending" -> "待审核"; "rejected" -> "未通过审核"; "approved", "public" -> "已公开"; else -> "状态待确认" }, style = MaterialTheme.typography.labelSmall)
                    if (canWrite(status)) Row {
                        if (row.optString("status", "approved") in setOf("approved", "public")) TextButton(enabled = !busy, onClick = { target = id; targetName = row.optString("nickname", "用户") }) { Text("回复") }
                        TextButton(enabled = !busy, onClick = {
                            if (busy) return@TextButton
                            busy = true
                            scope.launch {
                                try {
                                    SlgApi.raw(if (own) "replies.delete" else "replies.report", JSONObject().put("id", id))
                                    ensureActive(); if (current()) { error = if (own) null else "举报已提交"; if (own) refresh++ }
                                } catch (e: CancellationException) { throw e }
                                catch (e: Exception) { if (current()) error = e.message ?: "操作失败" }
                                finally { if (current()) busy = false }
                            }
                        }) { Text(if (own) "删除" else "举报") }
                    }
                }
            }
        }
        if (!loading && result != null && rows.isEmpty()) Text("暂无可显示的追评")
        val data = result
        if (data != null) Row {
            val actualPage = data.optInt("page", page)
            TextButton(enabled = !loading && !busy && actualPage > 1, onClick = { page = actualPage - 1 }) { Text("上一页") }
            Text("第 $actualPage 页", Modifier.padding(top = 12.dp))
            TextButton(enabled = !loading && !busy && (data.optBoolean("has_next") || actualPage < data.optInt("total_pages")), onClick = { page = actualPage + 1 }) { Text("下一页") }
        }
        if (canWrite(status)) {
            Text(if (target == null) "回复原评论" else "回复 $targetName")
            if (target != null) TextButton(enabled = !busy, onClick = { target = null }) { Text("改为回复原评论") }
            OutlinedTextField(content, { content = it }, enabled = !busy, label = { Text("追评（2～500字）") }, modifier = Modifier.fillMaxWidth(), minLines = 2, maxLines = 5)
            Button(enabled = !busy && validReply(content), onClick = {
                if (busy) return@Button
                busy = true
                val text = content.trim(); val selectedTarget = target
                val op = operation.id(text, selectedTarget)
                scope.launch {
                    try {
                        val answer = SlgApi.raw("replies.post", JSONObject().put("root_id", rootId).put("content", text).put("reply_to_id", selectedTarget ?: JSONObject.NULL).put("operation_id", op))
                        ensureActive(); if (!current()) return@launch
                        if (answer.optJSONObject("reply")?.optString("status") == "rejected") error = "未通过审核，正文已保留，请修改后重试"
                        else { content = ""; target = null; operation.clear(); page = 1; refresh++ }
                    } catch (e: CancellationException) { throw e }
                    catch (e: Exception) { if (current()) error = "提交失败，正文已保留：${e.message ?: "请重试"}" }
                    finally { if (current()) busy = false }
                }
            }) { Text(if (busy) "正在提交…" else "发布追评") }
        } else Text(if (status.optBoolean("logged_in")) "开发者模式仅支持阅读讨论" else "登录普通账号后可发表和举报追评", style = MaterialTheme.typography.labelSmall)
    }
}

@Composable
fun PublicAuthor(row: JSONObject, modifier: Modifier = Modifier) {
    var open by remember(row) { mutableStateOf(false) }
    val id = opaqueId(row, "author_public_id") ?: opaqueId(row, "public_id")
    TextButton(onClick = { open = true }, enabled = id != null, modifier = modifier) { Text(row.optString("nickname", "匿名用户")) }
    if (open && id != null) PublicAuthorDialog(id) { open = false }
}

@Composable
private fun PublicAuthorDialog(id: String, onClose: () -> Unit) {
    val revision = AccountChanges.revision
    var loading by remember(id, revision) { mutableStateOf(true) }
    var profile by remember(id, revision) { mutableStateOf<JSONObject?>(null) }
    var error by remember(id, revision) { mutableStateOf<String?>(null) }
    LaunchedEffect(id, revision) {
        try {
            val result = SlgApi.raw("account.public_profile", JSONObject().put("public_id", id))
            ensureActive(); if (revision == AccountChanges.revision) profile = validatedPublicProfile(result, id)
        } catch (e: CancellationException) { throw e }
        catch (e: Exception) { if (revision == AccountChanges.revision) error = e.message ?: "读取失败" }
        finally { if (revision == AccountChanges.revision) loading = false }
    }
    AlertDialog(onDismissRequest = onClose, title = { Text("公开名片") }, confirmButton = { TextButton(onClick = onClose) { Text("关闭") } }, text = {
        Column {
            when { loading -> CircularProgressIndicator(); error != null -> Text(error!!); profile == null -> Text("对方未公开资料"); else -> {
                Text(profile!!.optString("nickname", "用户"))
                profile!!.optString("profile_message").takeIf { it.isNotBlank() && it != "null" }?.let { Text(it) }
                val decorations = publicDecorations(profile!!)
                Text(decorations.title?.let { "头衔：$it" } ?: "未装备头衔或名称暂不可用")
                if (decorations.appearances.isEmpty()) Text("未装备装扮或名称暂不可用")
                else decorations.appearances.forEach { Text(it) }
            } }
        }
    })
}
