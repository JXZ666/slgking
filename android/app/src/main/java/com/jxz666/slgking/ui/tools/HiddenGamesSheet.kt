package com.jxz666.slgking.ui.tools

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.jxz666.slgking.bridge.GameItem
import com.jxz666.slgking.bridge.SlgApi
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.launch
import org.json.JSONObject

@OptIn(ExperimentalMaterial3Api::class)
@Composable
internal fun HiddenGamesSheet(onDismiss: () -> Unit) {
    val scope = rememberCoroutineScope()
    var items by remember { mutableStateOf(emptyList<GameItem>()) }
    var total by remember { mutableStateOf(0) }
    var page by remember { mutableStateOf(0) }
    var busy by remember { mutableStateOf(false) }
    var restoring by remember { mutableStateOf<Int?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    var generation by remember { mutableStateOf(0) }
    fun load(reset: Boolean = false) {
        if (busy) return
        val request = ++generation
        busy = true
        error = null
        scope.launch {
            try {
                val next = if (reset) 1 else page + 1
                val result = SlgApi.catalogList(catalogId = "all", hiddenMode = "only", sort = "title", page = next, pageSize = 50)
                currentCoroutineContext().ensureActive()
                if (request != generation) return@launch
                items = if (reset) result.items else (items + result.items).distinctBy { it.id }
                total = result.total
                page = result.page
            } catch (e: CancellationException) { throw e }
            catch (e: Exception) { if (request == generation) error = e.message ?: "读取失败" }
            finally { if (request == generation) busy = false }
        }
    }
    LaunchedEffect(Unit) { load(true) }
    ModalBottomSheet(onDismissRequest = onDismiss) {
        LazyColumn(Modifier.fillMaxWidth().fillMaxHeight(0.85f).padding(horizontal = 16.dp),
            contentPadding = PaddingValues(bottom = 28.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            item { Row(verticalAlignment = Alignment.CenterVertically) {
                Text("隐藏条目管理", style = MaterialTheme.typography.titleMedium, modifier = Modifier.weight(1f))
                TextButton(onClick = { load(true) }, enabled = !busy && restoring == null) { Text("刷新") }
            } }
            item { Text("隐藏不会删除个人记录。恢复后可在目录与个人清单重新找到。", style = MaterialTheme.typography.bodySmall) }
            item { Text("已加载 ${items.size} / $total 条", style = MaterialTheme.typography.labelMedium) }
            error?.let { message -> item {
                Text(message, color = MaterialTheme.colorScheme.error)
                TextButton(onClick = { load(page == 0) }, enabled = !busy && restoring == null) { Text("重新读取") }
            } }
            if (!busy && error == null && items.isEmpty()) item { Text("没有隐藏条目") }
            items.forEach { game -> item(key = game.id) {
                Card { Row(Modifier.fillMaxWidth().padding(12.dp), verticalAlignment = Alignment.CenterVertically) {
                    Text(game.displayTitle, modifier = Modifier.weight(1f))
                    TextButton(enabled = !busy && restoring == null, onClick = {
                        if (restoring != null) return@TextButton
                        restoring = game.id
                        error = null
                        scope.launch {
                            try {
                                SlgApi.raw("game.hidden.set", JSONObject().put("id", game.id).put("hidden", false))
                                currentCoroutineContext().ensureActive()
                                restoring = null
                                load(true)
                            } catch (e: CancellationException) { throw e }
                            catch (e: Exception) { error = e.message ?: "恢复失败，请重试" }
                            finally { restoring = null }
                        }
                    }) { Text(if (restoring == game.id) "恢复中…" else "恢复") }
                } }
            } }
            if (busy) item { LinearProgressIndicator(Modifier.fillMaxWidth()) }
            if (items.size < total && !busy) item {
                OutlinedButton(onClick = { load() }, enabled = restoring == null, modifier = Modifier.fillMaxWidth()) { Text("加载更多") }
            }
        }
    }
}
