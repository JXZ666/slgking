package com.jxz666.slgking.ui.community

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.jxz666.slgking.bridge.AccountChanges
import com.jxz666.slgking.bridge.SlgApi
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.launch
import org.json.JSONArray
import org.json.JSONObject

/** Opening the shell does no remote work; groups explicitly opt into requests. */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun MessageCenter(onClose: () -> Unit) {
    key(AccountChanges.revision) { MessageCenterContent(onClose) }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun MessageCenterContent(onClose: () -> Unit) {
    var group by remember { mutableStateOf<String?>(null) }
    var page by remember { mutableStateOf(1) }
    var refresh by remember { mutableStateOf(0) }
    var payload by remember { mutableStateOf<JSONObject?>(null) }
    var activitySession by remember { mutableStateOf(JSONObject()) }
    var error by remember { mutableStateOf<String?>(null) }
    var loading by remember { mutableStateOf(false) }
    var busy by remember { mutableStateOf(false) }
    var notice by remember { mutableStateOf<JSONObject?>(null) }
    val revision = AccountChanges.revision
    var generation by remember { mutableStateOf(0) }
    val scope = rememberCoroutineScope()
    fun current() = revision == AccountChanges.revision
    LaunchedEffect(group, page, refresh) {
        val request = ++generation
        payload = null; error = null
        val requested = group ?: return@LaunchedEffect
        loading = true
        if (requested == "活动") { ActivityBadgeState.clear(); activitySession = JSONObject() }
        try {
            val result = when (requested) {
                "公告" -> {
                    val response = SlgApi.raw("remote.config")
                    announcementConfig(response)
                }
                "活动" -> {
                    val session = SlgApi.raw("account.status")
                    ensureActive()
                    if (!current() || generation != request) return@LaunchedEffect
                    activitySession = session
                    require(canWrite(session) && opaqueId(session, "account_id") != null) {
                        if (session.optBoolean("logged_in")) "开发者身份不参与普通账号积分活动" else "登录普通账号后查看活动资格"
                    }
                    SlgApi.maintenanceRewardStatus()
                }
                else -> SlgApi.raw("notifications.list", JSONObject().put("page", page))
            }
            require(!result.optBoolean("offline") && !result.has("error")) { result.optString("error", "读取失败，请检查网络") }
            ensureActive(); if (current() && generation == request) {
                payload = result
                if (requested == "活动") ActivityBadgeState.confirm(revision, result, activitySession)
            }
        } catch (e: CancellationException) { throw e }
        catch (e: Exception) { if (current() && generation == request) error = e.message ?: "读取失败，请重试" }
        finally { if (current() && generation == request) loading = false }
    }
    BackHandler { if (notice != null) notice = null else if (group != null) group = null else onClose() }
    Scaffold(topBar = { TopAppBar(title = { Text(if (notice != null) "评论讨论" else group ?: "消息") }, navigationIcon = {
        TextButton(onClick = { if (notice != null) notice = null else if (group != null) group = null else onClose() }) { Text("返回") }
    }) }) { padding ->
        Column(Modifier.fillMaxSize().padding(padding).verticalScroll(rememberScrollState()).padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            val selected = notice
            if (selected != null) {
                NotificationDiscussion(selected) { refresh++ }
            } else if (group == null) {
                Text("按需查看公告、活动和回复；不在后台轮询。")
                listOf("公告", "活动", "回复").forEach { name -> OutlinedButton(onClick = { group = name; page = 1 }, modifier = Modifier.fillMaxWidth()) { Text(name) } }
            } else {
                if (loading) LinearProgressIndicator(Modifier.fillMaxWidth())
                error?.let { Text(it, color = MaterialTheme.colorScheme.error) }
                TextButton(enabled = !loading && !busy, onClick = { refresh++ }) { Text("刷新／重试") }
                val data = payload
                if (data != null) when (group) {
                    "公告" -> {
                        if (data.optBoolean("maintenance")) Text("服务器维护中：${data.optString("maintenance_msg")}")
                        val announcements = announcementItems(data)
                        if (announcements.isNotEmpty()) announcements.forEach { item ->
                            Card(Modifier.fillMaxWidth()) { Column(Modifier.padding(12.dp)) { Text(item.optString("title", "公告"), style = MaterialTheme.typography.titleMedium); Text(item.optString("content", item.optString("body", item.optString("text")))) } }
                        } else Text((data.opt("announcement") as? String)?.takeIf { it.isNotBlank() } ?: data.optString("notice").takeIf { it.isNotBlank() } ?: "暂无公告")
                    }
                    "活动" -> {
                        val campaign = data.optJSONObject("campaign")
                        if (campaign == null) Text("当前没有进行中的活动") else {
                            Text(campaign.optString("title", "维护补偿"), style = MaterialTheme.typography.titleMedium)
                            Text("积分：${campaign.optInt("amount")}")
                            val claimed = data.optBoolean("claimed")
                            val claimable = rewardClaimable(data, activitySession)
                            Text(if (claimed) "已领取" else if (claimable) "已确认当前账号可领取" else "当前账号或测试通道尚未确认可领取")
                            if (claimable) Button(enabled = !busy, onClick = {
                                busy = true
                                scope.launch {
                                    try { SlgApi.maintenanceRewardClaim(campaign.optString("id")); ensureActive(); if (current()) { ActivityBadgeState.clear(); refresh++ } }
                                    catch (e: CancellationException) { throw e }
                                    catch (e: Exception) { if (current()) { ActivityBadgeState.clear(); error = e.message ?: "领取失败" } }
                                    finally { if (current()) busy = false }
                                }
                            }) { Text("领取活动积分") }
                        }
                    }
                    else -> {
                        Text("未读 ${data.optInt("unread_count")} 条")
                        val notices = objects(data.optJSONArray("notifications"))
                        notices.forEach { item ->
                            if (opaqueId(item) != null && opaqueId(item, "reply_id") != null) OutlinedButton(onClick = { notice = item }, modifier = Modifier.fillMaxWidth()) {
                                Text((if (item.optBoolean("read")) "" else "未读 · ") + item.optString("nickname", "用户") + " 回复了你")
                            }
                        }
                        if (notices.isEmpty()) Text("暂无可查看的回复消息")
                        TextButton(enabled = !busy && notices.isNotEmpty(), onClick = {
                            busy = true
                            scope.launch {
                                try { SlgApi.raw("notifications.read", JSONObject().put("all", true)); ensureActive(); if (current()) refresh++ }
                                catch (e: CancellationException) { throw e }
                                catch (e: Exception) { if (current()) error = e.message ?: "标记失败" }
                                finally { if (current()) busy = false }
                            }
                        }) { Text("全部标为已读") }
                        val actual = data.optInt("page", page)
                        Row {
                            TextButton(enabled = !loading && actual > 1, onClick = { page = actual - 1 }) { Text("上一页") }
                            Text("第 $actual 页", Modifier.padding(top = 12.dp))
                            TextButton(enabled = !loading && actual < data.optInt("total_pages"), onClick = { page = actual + 1 }) { Text("下一页") }
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun NotificationDiscussion(notice: JSONObject, onRead: () -> Unit) {
    val id = opaqueId(notice, "reply_id") ?: return
    val revision = AccountChanges.revision
    var context by remember(id, revision) { mutableStateOf<JSONObject?>(null) }
    var error by remember(id, revision) { mutableStateOf<String?>(null) }
    var reload by remember(id, revision) { mutableStateOf(0) }
    LaunchedEffect(id, revision, reload) {
        context = null; error = null
        try {
            val value = SlgApi.raw("replies.context", JSONObject().put("id", id))
            ensureActive(); if (revision == AccountChanges.revision) {
                require(value.optJSONObject("root") != null) { "讨论不存在或已不可查看" }
                context = value
            }
        } catch (e: CancellationException) { throw e }
        catch (e: Exception) { if (revision == AccountChanges.revision) error = e.message ?: "讨论读取失败" }
    }
    when {
        error != null -> { Text("${error!!}；未显示旧正文，也未标记已读", color = MaterialTheme.colorScheme.error); TextButton(onClick = { reload++ }) { Text("重试") } }
        context == null -> CircularProgressIndicator()
        else -> {
            val root = context!!.getJSONObject("root")
            val rootId = opaqueId(root) ?: opaqueId(root, "cloud_id")
            Text(context!!.optString("game"), style = MaterialTheme.typography.titleMedium)
            PublicAuthor(root)
            Text(root.optString("content"))
            if (rootId != null) ReplyThread(rootId, id) {
                val noticeId = opaqueId(notice) ?: return@ReplyThread
                SlgApi.raw("notifications.read", JSONObject().put("ids", JSONArray().put(noticeId)))
                if (revision == AccountChanges.revision) onRead()
            } else Text("讨论缺少有效编号，未标记已读")
        }
    }
}
