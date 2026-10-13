package com.jxz666.slgking.ui.common

import androidx.compose.foundation.background
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Bookmark
import androidx.compose.material.icons.filled.DownloadDone
import androidx.compose.material.icons.filled.Star
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.material3.TextButton
import androidx.compose.material3.CircularProgressIndicator
import com.jxz666.slgking.bridge.SlgApi
import kotlinx.coroutines.CancellationException
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import coil.compose.AsyncImage
import com.jxz666.slgking.bridge.GameItem
import java.io.File

/** 目录封面卡片：横版封面 + 两行标题 + 评分/状态角标。 */
@OptIn(ExperimentalFoundationApi::class)
@Composable
fun GameCard(
    item: GameItem,
    onClick: () -> Unit,
    onLongClick: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Column(modifier = modifier
        .clip(RoundedCornerShape(10.dp))
        .combinedClickable(onClick = onClick, onLongClick = onLongClick)
    ) {
        Box(
            Modifier
                .fillMaxWidth()
                .aspectRatio(1.6f)
                .clip(RoundedCornerShape(10.dp))
                .background(MaterialTheme.colorScheme.surfaceVariant)
        ) {
            GameCover(item, Modifier.fillMaxSize())
            Row(
                Modifier
                    .align(Alignment.TopStart)
                    .padding(6.dp),
                horizontalArrangement = Arrangement.spacedBy(4.dp),
            ) {
                Badge(icon = Icons.Filled.Bookmark, visible = item.status == "want",
                    tint = Color(0xFFE0A800))
                Badge(icon = Icons.Filled.DownloadDone, visible = item.status == "downloaded",
                    tint = Color(0xFF3DD68C))
            }
            item.rating?.let { rating ->
                Surface(
                    color = Color(0xCC1C1E24),
                    contentColor = Color(0xFFF2C94C),
                    shape = RoundedCornerShape(topStart = 8.dp, bottomEnd = 8.dp),
                    modifier = Modifier.align(Alignment.BottomStart),
                ) {
                    Row(
                        verticalAlignment = Alignment.CenterVertically,
                        modifier = Modifier.padding(start = 6.dp, end = 6.dp, top = 2.dp, bottom = 2.dp),
                    ) {
                        Icon(Icons.Filled.Star, null, Modifier.padding(end = 2.dp).size(12.dp))
                        Text(cardRatingLabel(item), style = MaterialTheme.typography.labelSmall)
                    }
                }
            }
        }
        if (item.catalogId == "dlsite") {
            Text(if (item.rating == null) "DLsite · 原生评分未知" else "DLsite · ${item.ratingCount?.let { "$it 人评分" } ?: "评分人数未知"}",
                style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(top = 4.dp, start = 2.dp))
        }
        Text(
            item.displayTitle,
            style = MaterialTheme.typography.bodySmall,
            maxLines = 2,
            overflow = TextOverflow.Ellipsis,
            modifier = Modifier.padding(top = 6.dp, start = 2.dp, end = 2.dp),
        )
    }
}

@Composable
private fun Badge(icon: androidx.compose.ui.graphics.vector.ImageVector, visible: Boolean, tint: Color) {
    if (!visible) return
    Surface(color = Color(0xCC1C1E24), contentColor = tint, shape = RoundedCornerShape(6.dp)) {
        Icon(icon, null, Modifier.padding(3.dp))
    }
}

internal fun cardRatingLabel(item: GameItem): String {
    val rating = item.rating ?: return "未知"
    val value = "%.1f".format(java.util.Locale.ROOT, rating)
    if (item.catalogId != "dlsite") return value
    val scale = item.ratingScaleMax?.let { "%.0f".format(java.util.Locale.ROOT, it) } ?: "?"
    return "$value / $scale"
}

/** Visible-image loader shared by cards and details. Cancelling composition stops stale UI writes. */
@Composable
fun GameCover(item: GameItem, modifier: Modifier = Modifier, contentScale: ContentScale = ContentScale.Crop) {
    var coverPath by remember(item.id, item.cover) { mutableStateOf(if (item.catalogId == "dlsite") null else item.cover) }
    var coverLoading by remember(item.id) { mutableStateOf(false) }
    var coverFailed by remember(item.id) { mutableStateOf(false) }
    var retry by remember(item.id) { mutableStateOf(0) }
    LaunchedEffect(item.id, item.cover, retry) {
        if (coverPath != null && !coverFailed) return@LaunchedEffect
        coverLoading = true
        coverFailed = false
        try {
            coverPath = SlgApi.coverGet(item.id, retry = retry > 0)
            coverFailed = coverPath == null
        } catch (e: CancellationException) { throw e }
        catch (_: Exception) { coverFailed = true }
        finally { coverLoading = false }
    }
    Box(modifier.background(MaterialTheme.colorScheme.surfaceVariant)) {
            coverPath?.let { path ->
                AsyncImage(
                    model = File(path),
                    onError = { coverFailed = true },
                    contentDescription = item.displayTitle,
                    contentScale = contentScale,
                    modifier = Modifier.fillMaxSize(),
                )
            }
            if (coverLoading) CircularProgressIndicator(Modifier.align(Alignment.Center).size(24.dp))
            if (coverFailed) TextButton(onClick = { retry++ }, modifier = Modifier.align(Alignment.Center)) { Text("封面重试") }
    }
}
