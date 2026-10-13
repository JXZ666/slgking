package com.jxz666.slgking.ui.gamify

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.Checkbox
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp
import com.jxz666.slgking.bridge.SlgApi
import com.jxz666.slgking.bridge.SlgApiException
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import org.json.JSONObject
import java.util.UUID

private val RARITY_COLORS = mapOf(
    "普通" to Color(0xFF9A9AA2), "稀有" to Color(0xFF2F6FD0),
    "史诗" to Color(0xFF8B5CF6), "传说" to Color(0xFFE0A800),
    "至臻" to Color(0xFFE84393),
)

/** 积分流水。 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun LedgerSheet(me: JSONObject?, onDismiss: () -> Unit) {
    var rows by remember { mutableStateOf<List<JSONObject>?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    LaunchedEffect(Unit) {
        try {
            val r = SlgApi.raw("points.ledger")
            rows = (0 until (r.optJSONArray("ledger")?.length() ?: 0)).map {
                r.getJSONArray("ledger").getJSONObject(it)
            }
        } catch (e: CancellationException) { throw e }
        catch (e: Exception) { error = e.message }
    }
    ModalBottomSheet(onDismissRequest = onDismiss,
        sheetState = androidx.compose.material3.rememberModalBottomSheetState(skipPartiallyExpanded = true),
    ) {
        Column(Modifier.padding(horizontal = 16.dp).padding(bottom = 24.dp).fillMaxHeight(0.8f)) {
            Text("本机积分流水", style = MaterialTheme.typography.titleMedium)
            Text("仅记录本机积分变动；云端余额 ${me?.optInt("balance") ?: 0}", style = MaterialTheme.typography.labelMedium)
            Spacer(Modifier.height(8.dp))
            when {
                error != null -> Text("加载失败：$error", color = MaterialTheme.colorScheme.error)
                rows == null -> CircularProgressIndicator()
                rows!!.isEmpty() -> Text("暂无记录", color = MaterialTheme.colorScheme.onSurfaceVariant)
                else -> LazyColumn(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    items(rows!!) { r ->
                        Row {
                            Column(Modifier.weight(1f)) {
                                Text(r.optString("reason"), style = MaterialTheme.typography.bodyMedium)
                                Text(r.optString("at"), style = MaterialTheme.typography.labelSmall,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant)
                            }
                            Text(
                                (if (r.optInt("delta") >= 0) "+" else "") + r.optInt("delta"),
                                color = if (r.optInt("delta") >= 0) Color(0xFF3DD68C)
                                else MaterialTheme.colorScheme.error,
                            )
                        }
                    }
                }
            }
        }
    }
}

/** 积分商城（商品 + 每日/每周任务）。 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ShopSheet(me: JSONObject?, onChanged: () -> Unit, onDismiss: () -> Unit) {
    var items by remember { mutableStateOf<List<JSONObject>?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    var message by remember { mutableStateOf<String?>(null) }
    var busyId by remember { mutableStateOf<String?>(null) }
    var tab by remember { mutableStateOf("shop") }
    val scope = rememberCoroutineScope()
    val points = me?.optInt("balance") ?: 0

    LaunchedEffect(Unit) {
        try {
            val r = SlgApi.raw("shop.list")
            items = (0 until (r.optJSONArray("items")?.length() ?: 0)).map {
                r.getJSONArray("items").getJSONObject(it)
            }
        } catch (e: CancellationException) { throw e }
        catch (e: Exception) { error = e.message }
    }

    ModalBottomSheet(onDismissRequest = onDismiss,
        sheetState = androidx.compose.material3.rememberModalBottomSheetState(skipPartiallyExpanded = true),
    ) {
        Column(Modifier.padding(horizontal = 16.dp).padding(bottom = 24.dp).fillMaxHeight(0.85f)) {
            Text("积分商城", style = MaterialTheme.typography.titleMedium)
            Text("我的积分 $points", style = MaterialTheme.typography.labelMedium,
                color = MaterialTheme.colorScheme.primary)
            message?.let {
                Spacer(Modifier.height(4.dp))
                Text(it, style = MaterialTheme.typography.labelMedium)
            }
            Spacer(Modifier.height(8.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                FilterChip(selected = tab == "shop", onClick = { tab = "shop" },
                    label = { Text("商品") })
                FilterChip(selected = tab == "tasks", onClick = { tab = "tasks" },
                    label = { Text("每日 / 每周任务") })
            }
            Spacer(Modifier.height(8.dp))
            if (tab == "tasks") {
                QuestsTab(onClaimed = { onChanged() })
            } else when {
                error != null -> Text("加载失败：$error", color = MaterialTheme.colorScheme.error)
                items == null -> CircularProgressIndicator()
                else -> LazyColumn(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    items(items!!) { item ->
                        val id = item.optString("id")
                        val cost = item.optInt("cost")
                        val owned = me?.optJSONArray("titles")?.let { arr ->
                            (0 until arr.length()).any { arr.getString(it) == id }
                        } ?: false
                        Card {
                            Row(Modifier.padding(12.dp), verticalAlignment = Alignment.CenterVertically) {
                                Column(Modifier.weight(1f)) {
                                    Text(item.optString("name"), style = MaterialTheme.typography.bodyLarge)
                                    Text(
                                        item.optString("detail", "").ifBlank { item.optString("kind", "") },
                                        style = MaterialTheme.typography.labelSmall,
                                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                                        maxLines = 2,
                                    )
                                    item.optString("rarity").takeIf { it.isNotBlank() }?.let { rarity ->
                                        Text(rarity, color = RARITY_COLORS[rarity]
                                            ?: MaterialTheme.colorScheme.onSurfaceVariant,
                                            style = MaterialTheme.typography.labelSmall)
                                    }
                                }
                                if (owned) {
                                    Text("已拥有", color = MaterialTheme.colorScheme.primary,
                                        style = MaterialTheme.typography.labelMedium)
                                } else {
                                    Button(
                                        onClick = {
                                            if (busyId != null) return@Button
                                            busyId = id
                                            scope.launch {
                                                try {
                                                    SlgApi.raw("shop.buy", JSONObject().put("item_id", id))
                                                    currentCoroutineContext().ensureActive()
                                                    message = "购买成功：${item.optString("name")}"
                                                    onChanged()
                                                } catch (e: CancellationException) { throw e }
                                                catch (e: Exception) {
                                                    message = e.message
                                                } finally { busyId = null }
                                            }
                                        },
                                        enabled = points >= cost && busyId == null,
                                    ) { Text("$cost 积分") }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}

/** 云端已拥有头衔与装扮，装备后同时刷新衣橱和名片。 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun WardrobeSheet(me: JSONObject?, onChanged: () -> Unit, onDismiss: () -> Unit) {
    var wardrobe by remember { mutableStateOf<JSONObject?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    var message by remember { mutableStateOf<String?>(null) }
    var busy by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()

    suspend fun load() {
        val fresh = SlgApi.raw("wardrobe.list")
        currentCoroutineContext().ensureActive()
        wardrobe = fresh
        error = null
    }
    LaunchedEffect(Unit) {
        try { load() }
        catch (e: CancellationException) { throw e }
        catch (e: Exception) { error = e.message }
    }
    fun equip(params: JSONObject, name: String) {
        if (busy) return
        busy = true
        scope.launch {
            try {
                SlgApi.raw("wardrobe.equip", params)
                currentCoroutineContext().ensureActive()
                message = name
                onChanged()
                // 写入成功后刷新失败不应误报装备失败。
                try { load() } catch (e: CancellationException) { throw e }
                catch (e: Exception) { error = "已保存，衣橱刷新失败：${e.message}" }
            } catch (e: CancellationException) { throw e }
            catch (e: Exception) { message = e.message }
            finally { busy = false }
        }
    }
    ModalBottomSheet(onDismissRequest = { if (!busy) onDismiss() },
        sheetState = androidx.compose.material3.rememberModalBottomSheetState(skipPartiallyExpanded = true)) {
        Column(Modifier.padding(horizontal = 16.dp).padding(bottom = 24.dp).fillMaxHeight(0.8f)) {
            Text("头衔 / 衣橱", style = MaterialTheme.typography.titleMedium)
            Text("云端已拥有的装扮", style = MaterialTheme.typography.labelSmall)
            if (busy) LinearProgressIndicator(Modifier.fillMaxWidth())
            message?.let { Text(it, style = MaterialTheme.typography.labelMedium) }
            error?.let {
                Text(it, color = MaterialTheme.colorScheme.error)
                TextButton(enabled = !busy, onClick = { scope.launch {
                    try { load() } catch (e: CancellationException) { throw e }
                    catch (e: Exception) { error = e.message }
                } }) { Text("重试刷新") }
            }
            val data = wardrobe
            if (data == null && error == null) CircularProgressIndicator()
            else if (data != null) LazyColumn(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                listOf("titles" to "头衔", "appearances" to "装扮", "cosmetics" to "饰品").forEach { (kind, label) ->
                    val rows = jsonRows(data, kind)
                    if (rows.isNotEmpty()) item { Text(label, style = MaterialTheme.typography.titleSmall) }
                    items(rows, key = { "$kind:${it.optString("id")}" }) { entry ->
                        val active = entry.optBoolean("equipped")
                        val id = entry.optString("id")
                        val name = entry.optString("name", id)
                        Card {
                            Row(Modifier.padding(12.dp), verticalAlignment = Alignment.CenterVertically) {
                                Column(Modifier.weight(1f)) {
                                    Text(name, color = RARITY_COLORS[entry.optString("rarity")]
                                        ?: MaterialTheme.colorScheme.onSurface)
                                    if (kind == "appearances") Text(
                                        if (entry.optString("slot") == "avatar_frame") "头像框" else "评论框",
                                        style = MaterialTheme.typography.labelSmall)
                                    if (active) Text("使用中", color = MaterialTheme.colorScheme.primary)
                                }
                                if (!(kind == "titles" && active && id == "normal_user")) {
                                    OutlinedButton(enabled = !busy, onClick = {
                                        val params = when (kind) {
                                            "titles" -> JSONObject().put("title_id", if (active) "normal_user" else id)
                                            "appearances" -> JSONObject().put("appearance_slot", entry.optString("slot"))
                                                .put("item_id", if (active) "" else id)
                                            else -> JSONObject().put("cosmetic_id", if (active) "" else id)
                                        }
                                        equip(params, if (active) "已卸下 $name" else "已装备 $name")
                                    }) { Text(if (active) "卸下" else "装备") }
                                }
                            }
                        }
                    }
                }
                if (jsonRows(data, "appearances").isEmpty() && jsonRows(data, "cosmetics").isEmpty()) {
                    item { Text("暂无已拥有装扮，可在积分商城购买", style = MaterialTheme.typography.bodySmall) }
                }
            }
        }
    }
}

private fun jsonRows(data: JSONObject, field: String): List<JSONObject> {
    val rows = data.optJSONArray(field) ?: return emptyList()
    return (0 until rows.length()).mapNotNull { rows.optJSONObject(it) }
}

/** 排行榜。 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun LeaderboardSheet(onDismiss: () -> Unit) {
    var board by remember { mutableStateOf("points") }
    var states by remember { mutableStateOf(mapOf("points" to BoardState(), "wardrobe" to BoardState())) }
    val generations = remember { mutableMapOf("points" to 0, "wardrobe" to 0) }
    val scope = rememberCoroutineScope()
    fun load(target: String) {
        if (states.getValue(target).loading) return
        val generation = generations.getValue(target) + 1
        generations[target] = generation
        states = states + (target to states.getValue(target).copy(loading = true, error = null))
        scope.launch {
            try {
                val r = SlgApi.raw("leaderboard.list", JSONObject().put("board", target).put("limit", 30))
                currentCoroutineContext().ensureActive()
                val fresh = leaderboardRows(r.getJSONObject("board"), target)
                if (generations[target] == generation) states = states + (target to BoardState(fresh, loaded = true))
            } catch (e: CancellationException) { throw e }
            catch (e: Exception) {
                if (generations[target] == generation) states = states +
                    (target to states.getValue(target).copy(loading = false, error = e.message ?: "读取失败"))
            }
        }
    }
    LaunchedEffect(board) { if (!states.getValue(board).loaded) load(board) }
    val state = states.getValue(board)
    ModalBottomSheet(onDismissRequest = onDismiss,
        sheetState = androidx.compose.material3.rememberModalBottomSheetState(skipPartiallyExpanded = true),
    ) {
        Column(Modifier.padding(horizontal = 16.dp).padding(bottom = 24.dp).fillMaxHeight(0.8f)) {
            Text("社区排行榜", style = MaterialTheme.typography.titleMedium)
            Text("每榜仅前 30 名，不含管理员；只展示昵称、排名和数值。",
                style = MaterialTheme.typography.labelSmall)
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
                listOf("points" to "云端积分", "wardrobe" to "装扮").forEach { (k, label) ->
                    FilterChip(selected = board == k, onClick = { board = k }, label = { Text(label) })
                }
                TextButton(onClick = { load(board) }, enabled = !state.loading) { Text("刷新") }
            }
            Text(if (board == "points") "按当前云端积分排序" else "按已拥有头衔、头像框、名片框总数排序",
                style = MaterialTheme.typography.labelSmall)
            if (state.loading) LinearProgressIndicator(Modifier.fillMaxWidth())
            state.error?.let {
                Text("加载失败：$it", color = MaterialTheme.colorScheme.error)
                TextButton(onClick = { load(board) }, enabled = !state.loading) { Text("重试") }
                if (state.rows.isNotEmpty()) Text("显示上次已加载的数据", style = MaterialTheme.typography.labelSmall)
            }
            if (state.loaded && state.rows.isEmpty()) Text("暂时还没有上榜数据。")
            LazyColumn(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                items(state.rows, key = { it.rank }) { r ->
                    Row(Modifier.fillMaxWidth()) {
                        Text("#${r.rank}", Modifier.width(44.dp), color = MaterialTheme.colorScheme.onSurfaceVariant)
                        Text(r.nickname, Modifier.weight(1f), style = MaterialTheme.typography.bodyMedium)
                        Text("${r.score} ${if (board == "points") "积分" else "件装扮"}",
                            style = MaterialTheme.typography.bodyMedium)
                    }
                }
            }
        }
    }
}

/** 每日抽奖（老虎机结果呈现 + 简化滚轴动画）。 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun LotterySheet(onDismiss: () -> Unit, onChanged: () -> Unit = {}) {
    var status by remember { mutableStateOf<JSONObject?>(null) }
    var spinning by remember { mutableStateOf(false) }
    var display by remember { mutableStateOf("🎰 🎰 🎰") }
    var result by remember { mutableStateOf<String?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    var balanceAfter by remember { mutableStateOf(-1) }
    var pendingRequest by rememberSaveable { mutableStateOf<String?>(null) }
    val scope = rememberCoroutineScope()

    fun loadStatus() {
        scope.launch {
            try {
                val fresh = SlgApi.raw("lottery.status")
                currentCoroutineContext().ensureActive()
                status = fresh
            } catch (e: CancellationException) { throw e }
            catch (e: Exception) { error = e.message }
        }
    }

    LaunchedEffect(Unit) { loadStatus() }

    ModalBottomSheet(onDismissRequest = { if (!spinning) onDismiss() }) {
        Column(
            Modifier.padding(horizontal = 16.dp).padding(bottom = 32.dp).fillMaxWidth(),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Text("每日抽奖", style = MaterialTheme.typography.titleMedium)
            Spacer(Modifier.height(4.dp))
            status?.optInt("draws_today")?.let {
                Text("今日已抽 $it 次", style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            Spacer(Modifier.height(16.dp))
            Text(display, style = MaterialTheme.typography.displayMedium)
            Spacer(Modifier.height(8.dp))
            result?.let {
                Text(it, color = MaterialTheme.colorScheme.primary,
                    style = MaterialTheme.typography.titleMedium)
                Spacer(Modifier.height(8.dp))
            }
            error?.let {
                Text(it, color = MaterialTheme.colorScheme.error)
                if (status == null) TextButton(onClick = { error = null; loadStatus() }) { Text("重试加载") }
                Spacer(Modifier.height(8.dp))
            }
            Button(
                onClick = {
                    if (spinning) return@Button
                    spinning = true
                    if (pendingRequest == null) pendingRequest = UUID.randomUUID().hex32()
                    scope.launch {
                        result = null
                        error = null
                        try {
                            // 滚轴动画期间发起抽奖（服务端幂等 request_id）
                            val spinJob = launch {
                                val faces = listOf("🍒", "🍋", "🔔", "⭐", "💎", "7️⃣", "🎁")
                                repeat(24) {
                                    display = List(3) { faces.random() }.joinToString(" ")
                                    delay(70)
                                }
                            }
                            val r = SlgApi.raw("lottery.spin",
                                JSONObject().put("request_id", pendingRequest))
                            spinJob.join()
                            currentCoroutineContext().ensureActive()
                            val draw = lotteryDraw(r)
                            result = lotteryResultText(draw)
                            draw.optInt("balance", -1).let { if (it >= 0) balanceAfter = it }
                            pendingRequest = null
                            onChanged()
                            display = List(3) { "⭐" }.joinToString(" ")
                        } catch (e: CancellationException) { throw e }
                        catch (e: Exception) {
                            error = e.message
                        } finally {
                            spinning = false
                            if (currentCoroutineContext().isActive) loadStatus()
                        }
                    }
                },
                enabled = !spinning && status != null && status?.optString("source") == "cloud",
            ) { Text(if (spinning) "抽奖中…" else if (pendingRequest != null) "重试查询抽奖结果" else "开始抽奖") }
            Spacer(Modifier.height(8.dp))
            val source = if (status?.optString("source") == "cloud") "云端" else "本机"
            Text("${source}抽奖记录", style = MaterialTheme.typography.labelSmall)
            if (balanceAfter >= 0 || status?.has("balance") == true) {
                Text("当前积分 ${if (balanceAfter >= 0) balanceAfter else status!!.optInt("balance")}",
                    style = MaterialTheme.typography.labelMedium)
            }
            jsonRows(status ?: JSONObject(), "history").take(5).forEach { history ->
                Text("${history.optString("day", history.optString("at"))} · ${lotteryResultText(history)}",
                    style = MaterialTheme.typography.labelSmall)
            }
            status?.optJSONArray("paytable")?.let { pt ->
                // 每项结构为 [符号数组, 奖名, 概率]（数组而非对象），结构自适应解析
                val names = (0 until pt.length()).mapNotNull { i ->
                    when (val row = pt.opt(i)) {
                        is org.json.JSONObject ->
                            row.optString("text", row.optString("name", "")).ifBlank { null }
                        is org.json.JSONArray -> row.optString(1, "").ifBlank { null }
                        else -> null
                    }
                }
                if (names.isNotEmpty()) {
                    Text("奖池：" + names.joinToString("、"), style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            }
        }
    }
}

private fun UUID.hex32(): String = toString().replace("-", "")

/** 兑换码。 */
@Composable
fun RedeemDialog(onDone: () -> Unit, onDismiss: () -> Unit) {
    var code by remember { mutableStateOf("") }
    var message by remember { mutableStateOf<String?>(null) }
    var busy by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()

    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("兑换码") },
        text = {
            Column {
                OutlinedTextField(value = code, onValueChange = { code = it },
                    label = { Text("群兑换码") }, singleLine = true)
                message?.let {
                    Spacer(Modifier.height(6.dp))
                    Text(it, style = MaterialTheme.typography.labelMedium)
                }
            }
        },
        confirmButton = {
            Button(onClick = {
                if (busy) return@Button
                busy = true
                scope.launch {
                    try {
                        SlgApi.raw("redeem.code", JSONObject().put("code", code.trim()))
                        message = "兑换成功"
                        onDone()
                    } catch (e: CancellationException) { throw e }
                    catch (e: Exception) { message = e.message }
                    busy = false
                }
            }, enabled = code.isNotBlank() && !busy) { Text("兑换") }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("关闭") } },
    )
}

/** 设备管理。 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun DevicesSheet(onDismiss: () -> Unit) {
    var devices by remember { mutableStateOf<List<JSONObject>?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    val scope = rememberCoroutineScope()

    fun load() {
        scope.launch {
            try {
                val r = SlgApi.raw("account.devices")
                val arr = r.getJSONArray("devices")
                devices = (0 until arr.length()).map { arr.getJSONObject(it) }
            } catch (e: CancellationException) { throw e }
            catch (e: Exception) { error = e.message }
        }
    }
    LaunchedEffect(Unit) { load() }

    ModalBottomSheet(onDismissRequest = onDismiss) {
        Column(Modifier.padding(horizontal = 16.dp).padding(bottom = 24.dp)) {
            Text("受信设备", style = MaterialTheme.typography.titleMedium)
            Spacer(Modifier.height(8.dp))
            when {
                error != null -> Text(error!!, color = MaterialTheme.colorScheme.error)
                devices == null -> CircularProgressIndicator()
                else -> devices!!.forEach { d ->
                    Row(Modifier.fillMaxWidth().padding(vertical = 6.dp),
                        verticalAlignment = Alignment.CenterVertically) {
                        Column(Modifier.weight(1f)) {
                            Text(d.optString("label", d.optString("device", "设备")))
                            d.optString("last_used").takeIf { it.isNotBlank() }?.let {
                                Text(it, style = MaterialTheme.typography.labelSmall,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant)
                            }
                        }
                        TextButton(onClick = {
                            scope.launch {
                                try {
                                    SlgApi.raw("account.revoke_device",
                                        JSONObject().put("id", d.opt("id")))
                                    load()
                                } catch (e: CancellationException) { throw e }
                                catch (e: Exception) { error = e.message }
                            }
                        }) { Text("移除") }
                    }
                }
            }
        }
    }
}

/** 密钥换绑。 */
@Composable
fun RotateDialog(onDone: () -> Unit, onReveal: (JSONObject) -> Unit) {
    var key by remember { mutableStateOf("") }
    var error by remember { mutableStateOf<String?>(null) }
    var busy by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()

    AlertDialog(
        onDismissRequest = { if (!busy) onDone() },
        title = { Text("密钥换绑") },
        text = {
            Column {
                Text("生成新的登录密钥与恢复码，旧密钥立即作废。请先确认已准备好保存新密钥。",
                    style = MaterialTheme.typography.bodySmall)
                Spacer(Modifier.height(8.dp))
                OutlinedTextField(value = key, onValueChange = { key = it },
                    label = { Text("当前登录密钥") }, singleLine = true, enabled = !busy)
                error?.let {
                    Spacer(Modifier.height(6.dp))
                    Text(it, color = MaterialTheme.colorScheme.error,
                        style = MaterialTheme.typography.labelSmall)
                }
            }
        },
        confirmButton = {
            Button(onClick = {
                if (busy) return@Button
                busy = true
                scope.launch {
                    try {
                        val r = SlgApi.raw("account.rotate", JSONObject().put("login_key", key.trim()))
                        onDone()
                        r.optJSONObject("account")?.let(onReveal)
                    } catch (e: CancellationException) { throw e }
                    catch (e: Exception) { error = e.message }
                    busy = false
                }
            }, enabled = key.isNotBlank() && !busy) { Text("确认换绑") }
        },
        dismissButton = { TextButton(onClick = onDone, enabled = !busy) { Text("取消") } },
    )
}

/** 我的评论（云端）。 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun MyCommentsSheet(onDismiss: () -> Unit) {
    var rows by remember { mutableStateOf<List<JSONObject>?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    val scope = rememberCoroutineScope()
    var deletingId by remember { mutableStateOf<String?>(null) }
    var generation by remember { mutableStateOf(0) }

    fun load() {
        val request = ++generation
        scope.launch {
            try { val fresh = SlgApi.commentsMine().let { arr ->
                (0 until arr.length()).map { arr.getJSONObject(it) } }
                currentCoroutineContext().ensureActive()
                if (request == generation) { rows = fresh; error = null }
            }
            catch (e: CancellationException) { throw e }
            catch (e: Exception) { if (request == generation) error = e.message }
        }
    }
    LaunchedEffect(Unit) { load() }

    ModalBottomSheet(onDismissRequest = { if (deletingId == null) onDismiss() },
        sheetState = androidx.compose.material3.rememberModalBottomSheetState(skipPartiallyExpanded = true),
    ) {
        Column(Modifier.padding(horizontal = 16.dp).padding(bottom = 24.dp).fillMaxHeight(0.8f)) {
            Text("我的评论", style = MaterialTheme.typography.titleMedium)
            Spacer(Modifier.height(8.dp))
            when {
                error != null -> Column {
                    Text(error!!, color = MaterialTheme.colorScheme.error)
                    TextButton(onClick = { load() }, enabled = deletingId == null) { Text("重试 / 核对评论状态") }
                }
                rows == null -> CircularProgressIndicator()
                rows!!.isEmpty() -> Text("还没有发表过公开评论",
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
                else -> LazyColumn(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    items(rows!!) { c ->
                        Card {
                            Column(Modifier.padding(12.dp)) {
                                Text(c.optString("game"), style = MaterialTheme.typography.labelMedium,
                                    color = MaterialTheme.colorScheme.primary)
                                Spacer(Modifier.height(4.dp))
                                Text(c.optString("content"), style = MaterialTheme.typography.bodyMedium)
                                Text("${cloudCommentNickname(c)} · ${commentTimestamp(c.opt("ts"))}",
                                    style = MaterialTheme.typography.labelSmall)
                                Row(verticalAlignment = Alignment.CenterVertically) {
                                    Text("状态：${c.optString("status")}",
                                        style = MaterialTheme.typography.labelSmall,
                                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                                        modifier = Modifier.weight(1f))
                                    TextButton(onClick = {
                                        val id = cloudCommentId(c)
                                        if (deletingId != null || id.isBlank()) return@TextButton
                                        deletingId = id
                                        scope.launch {
                                            try {
                                                SlgApi.commentDeleteCloud(id)
                                                currentCoroutineContext().ensureActive()
                                                rows = rows?.filterNot { cloudCommentId(it) == id }
                                                load()
                                            } catch (e: CancellationException) { throw e }
                                            catch (e: Exception) { error = "删除结果未确认，请先刷新核对：${e.message}" }
                                            finally { deletingId = null }
                                        }
                                    }, enabled = deletingId == null && cloudCommentId(c).isNotBlank()) {
                                        Text(if (deletingId == cloudCommentId(c)) "删除中…" else "删除")
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}

/** 每日/每周任务页签：tasks[] = {id,label,progress,target,reward,period,claimed,claimable}。 */
@Composable
fun QuestsTab(onClaimed: () -> Unit) {
    var tasks by remember { mutableStateOf<List<JSONObject>?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    var message by remember { mutableStateOf<String?>(null) }
    var claimingId by remember { mutableStateOf<String?>(null) }
    val scope = rememberCoroutineScope()

    fun load() {
        scope.launch {
            try {
                val r = SlgApi.raw("quests.list")
                val arr = r.optJSONObject("quests")?.optJSONArray("tasks")
                    ?: r.optJSONArray("tasks") ?: org.json.JSONArray()
                tasks = (0 until arr.length()).map { arr.getJSONObject(it) }
                error = null
            } catch (e: CancellationException) { throw e }
            catch (e: Exception) { error = e.message }
        }
    }
    LaunchedEffect(Unit) { load() }

    Text("每日任务北京时间 04:00 刷新；每周任务周一 04:00 刷新。",
        style = MaterialTheme.typography.labelSmall)
    message?.let {
        Text(it, style = MaterialTheme.typography.labelMedium,
            color = MaterialTheme.colorScheme.primary)
        Spacer(Modifier.height(6.dp))
    }
    when {
        error != null -> Text("任务读取失败：$error", color = MaterialTheme.colorScheme.error)
        tasks == null -> CircularProgressIndicator()
        tasks!!.isEmpty() -> Text("暂无任务", color = MaterialTheme.colorScheme.onSurfaceVariant)
        else -> LazyColumn(verticalArrangement = Arrangement.spacedBy(8.dp)) {
            items(tasks!!, key = { it.optString("id") }) { t ->
                val claimed = t.optBoolean("claimed")
                val claimable = t.optBoolean("claimable")
                val progress = t.optInt("progress")
                val target = t.optInt("target")
                val reward = t.optInt("reward")
                val weekly = t.optString("period").lowercase() in listOf("week", "weekly")
                Card {
                    Row(Modifier.padding(12.dp), verticalAlignment = Alignment.CenterVertically) {
                        Column(Modifier.weight(1f)) {
                            Text("${if (weekly) "每周" else "每日"} ${t.optString("label", t.optString("id"))}",
                                style = MaterialTheme.typography.bodyMedium)
                            Text("$progress/$target · 奖励 +$reward 积分",
                                style = MaterialTheme.typography.labelSmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                        when {
                            claimed -> Text("已领取",
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                                style = MaterialTheme.typography.labelMedium)
                            claimable || (target > 0 && progress >= target) -> Button(
                                onClick = {
                                    if (claimingId != null) return@Button
                                    claimingId = t.optString("id")
                                    scope.launch {
                                        try {
                                            SlgApi.raw("quests.claim",
                                                JSONObject().put("quest_id", t.optString("id")))
                                            currentCoroutineContext().ensureActive()
                                            message = "已领取 +$reward 积分"
                                            load()
                                            onClaimed()
                                        } catch (e: CancellationException) { throw e }
                                        catch (e: Exception) {
                                            message = e.message
                                        } finally { claimingId = null }
                                    }
                                },
                                enabled = claimingId == null,
                            ) {
                                Text(if (claimingId == t.optString("id")) "领取中…"
                                else if (claimable) "领取" else "已完成")
                            }
                            else -> Text("未完成",
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                                style = MaterialTheme.typography.labelMedium)
                        }
                    }
                }
            }
        }
    }
}
