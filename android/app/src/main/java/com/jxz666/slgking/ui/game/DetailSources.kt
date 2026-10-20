package com.jxz666.slgking.ui.game

import android.content.Intent
import android.net.Uri
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.unit.dp
import com.jxz666.slgking.bridge.GameDetail
import com.jxz666.slgking.bridge.GameSource

@Composable
internal fun SourceLinks(detail: GameDetail) {
    val context = LocalContext.current
    val clipboard = LocalClipboardManager.current
    var message by remember(detail.item.id) { mutableStateOf<String?>(null) }
    // Public fallback is the bridge's responsibility: do not bypass its source allowlist.
    val links = detail.sources.filter { validWebUrl(it.url) != null }.distinctBy { it.sourceId }
        .ifEmpty {
            if (detail.origin == "user") detail.url?.let { validWebUrl(it) }?.let {
                listOf(GameSource("user", sourceDomain(it), it))
            } ?: emptyList() else emptyList()
        }
    if (links.isEmpty()) {
        Text("暂无可跳转来源", style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant)
        return
    }
    Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
        Text("来源网页", style = MaterialTheme.typography.titleSmall)
        links.forEach { source ->
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedButton(modifier = Modifier.weight(1f), onClick = {
                    val url = validWebUrl(source.url)
                    if (url == null) message = "来源网址无效，请复制后核对。"
                    else try {
                        context.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)).addCategory(Intent.CATEGORY_BROWSABLE))
                        message = null
                    } catch (_: Exception) { message = "无法打开网页。请安装或启用浏览器，也可复制链接。" }
                }) { Text("打开 ${source.label.ifBlank { sourceDomain(source.url) }}") }
                TextButton(onClick = {
                    clipboard.setText(AnnotatedString(source.url))
                    message = "已复制 ${source.label.ifBlank { sourceDomain(source.url) }} 链接"
                }) { Text("复制") }
            }
        }
        message?.let { Text(it, style = MaterialTheme.typography.bodySmall) }
    }
}

@Composable
internal fun DlsiteFacts(detail: GameDetail) {
    val facts = detail.dlsiteFacts
    fun fact(key: String): String = facts?.opt(key)?.takeUnless {
        it == org.json.JSONObject.NULL || it.toString().isBlank() || it.toString() == "null"
    }?.let {
        if (it is org.json.JSONArray) (0 until it.length()).joinToString("、") { i -> it.optString(i) }.ifBlank { "未知" }
        else it.toString()
    } ?: "未知"
    Column(Modifier.fillMaxWidth().padding(vertical = 4.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
        Text("DLsite 商品资料", style = MaterialTheme.typography.titleSmall)
        Text("商品编号：${fact("product_id")}")
        Text("开发者：${fact("developer")}")
        Text("发行日期：${fact("release_date")} · 更新：${fact("updated_date")}")
        Text("语言：${fact("languages")} · 作品形式：${fact("work_type")}")
        val value = fact("rating_value")
        val scale = fact("rating_scale_max")
        Text(if (value == "未知") "DLsite 原生评分：未知"
            else "DLsite 原生评分：$value / ${if (scale == "未知") "?（尺度未知）" else scale}（${fact("rating_count")} 人）",
            color = MaterialTheme.colorScheme.primary)
    }
}
