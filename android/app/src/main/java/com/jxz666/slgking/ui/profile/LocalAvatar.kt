package com.jxz666.slgking.ui.profile

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.Image
import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.ByteArrayOutputStream
import java.io.File

private fun avatarFile(context: Context, identity: String): File =
    File(File(context.filesDir, "private_avatars"), AvatarPolicy.filename(identity))

private fun cropped(source: Bitmap, zoom: Float, x: Float, y: Float): Bitmap {
    val side = (minOf(source.width, source.height) / zoom).toInt().coerceAtLeast(1)
    val left = ((source.width - side) * x).toInt().coerceIn(0, source.width - side)
    val top = ((source.height - side) * y).toInt().coerceIn(0, source.height - side)
    return Bitmap.createScaledBitmap(Bitmap.createBitmap(source, left, top, side, side),
        AvatarPolicy.SIDE, AvatarPolicy.SIDE, true)
}

@Composable
internal fun LocalAvatarCard(accountId: String?, nickname: String) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var image by remember(accountId) { mutableStateOf<Bitmap?>(null) }
    var candidate by remember(accountId) { mutableStateOf<Bitmap?>(null) }
    var error by remember(accountId) { mutableStateOf<String?>(null) }
    var busy by remember(accountId) { mutableStateOf(false) }
    var active by remember(accountId) { mutableStateOf(true) }
    DisposableEffect(accountId) { active = true; onDispose { active = false } }
    LaunchedEffect(accountId) {
        image = accountId?.let { withContext(Dispatchers.IO) { BitmapFactory.decodeFile(avatarFile(context, it).path) } }
    }
    val picker = rememberLauncherForActivityResult(ActivityResultContracts.GetContent()) { uri ->
        if (uri != null && accountId != null) scope.launch {
            busy = true
            try {
                val bitmap = withContext(Dispatchers.IO) {
                    val data = context.contentResolver.openInputStream(uri)?.use { stream ->
                        val out = ByteArrayOutputStream()
                        val buf = ByteArray(8192)
                        while (true) {
                            val n = stream.read(buf)
                            if (n < 0) break
                            require(out.size() + n <= 10 * 1024 * 1024) { "图片不得超过10MB" }
                            out.write(buf, 0, n)
                        }
                        out.toByteArray()
                    } ?: kotlin.error("无法读取图片")
                    val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
                    BitmapFactory.decodeByteArray(data, 0, data.size, bounds)
                    require(bounds.outWidth > 0 && bounds.outHeight > 0) { "无法识别图片" }
                    var sample = 1
                    while (maxOf(bounds.outWidth, bounds.outHeight) / sample > 1024) sample *= 2
                    BitmapFactory.decodeByteArray(data, 0, data.size,
                        BitmapFactory.Options().apply { inSampleSize = sample }) ?: kotlin.error("图片解码失败")
                }
                if (active) { candidate = bitmap; error = null }
            } catch (e: CancellationException) { throw e }
            catch (e: Exception) { if (active) error = e.message }
            finally { if (active) busy = false }
        }
    }
    Card {
        Column(Modifier.fillMaxWidth().padding(14.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                image?.let { Image(it.asImageBitmap(), "本机个人头像", Modifier.size(56.dp)) }
                    ?: Text(AvatarPolicy.initial(nickname), style = MaterialTheme.typography.headlineLarge)
                Column(Modifier.weight(1f)) {
                    Text("本机头像", style = MaterialTheme.typography.titleSmall)
                    Text("仅保存在此设备，不上传、不随备份导出。公开名片仍使用昵称首字。",
                        style = MaterialTheme.typography.bodySmall)
                }
            }
            Row {
                TextButton(onClick = { picker.launch("image/*") }, enabled = !busy && accountId != null) { Text("选择图片") }
                TextButton(onClick = {
                    val id = accountId ?: return@TextButton
                    scope.launch {
                        try {
                            withContext(Dispatchers.IO) {
                                val file = avatarFile(context, id)
                                check(!file.exists() || file.delete()) { "无法恢复默认头像" }
                            }
                            if (active) image = null
                        } catch (e: CancellationException) { throw e }
                        catch (e: Exception) { if (active) error = e.message }
                    }
                }, enabled = !busy && image != null) { Text("恢复昵称首字") }
            }
            error?.let { Text(it, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall) }
        }
    }
    candidate?.let { source ->
        var zoom by remember(source) { mutableStateOf(1f) }
        var x by remember(source) { mutableStateOf(0.5f) }
        var y by remember(source) { mutableStateOf(0.5f) }
        val preview = remember(source, zoom, x, y) { cropped(source, zoom, x, y) }
        AlertDialog(onDismissRequest = { if (!busy) candidate = null }, title = { Text("裁切本机头像") }, text = {
            Column(horizontalAlignment = Alignment.CenterHorizontally) {
                Image(preview.asImageBitmap(), "裁切预览", Modifier.size(192.dp))
                Text("缩放"); Slider(zoom, { zoom = it }, valueRange = 1f..4f, enabled = !busy)
                Text("左右位置"); Slider(x, { x = it }, enabled = !busy)
                Text("上下位置"); Slider(y, { y = it }, enabled = !busy)
            }
        }, confirmButton = {
            TextButton(enabled = !busy, onClick = {
                val id = accountId ?: return@TextButton
                busy = true
                scope.launch {
                    try {
                        withContext(Dispatchers.IO) {
                            var bytes = ByteArray(0)
                            for (quality in listOf(90, 80, 65, 45)) {
                                val out = ByteArrayOutputStream()
                                check(preview.compress(Bitmap.CompressFormat.JPEG, quality, out)) { "头像编码失败" }
                                bytes = out.toByteArray()
                                if (bytes.size <= AvatarPolicy.MAX_BYTES) break
                            }
                            require(bytes.size <= AvatarPolicy.MAX_BYTES) { "头像超过64KiB，请更换图片" }
                            val target = avatarFile(context, id)
                            check(target.parentFile!!.exists() || target.parentFile!!.mkdirs()) { "头像目录不可写" }
                            val atomic = android.util.AtomicFile(target)
                            var stream: java.io.FileOutputStream? = null
                            try {
                                stream = atomic.startWrite()
                                stream.write(bytes)
                                atomic.finishWrite(stream)
                            } catch (e: Exception) { atomic.failWrite(stream); throw e }
                        }
                        if (active) { image = preview; candidate = null; error = null }
                    } catch (e: CancellationException) { throw e }
                    catch (e: Exception) { if (active) error = e.message }
                    finally { if (active) busy = false }
                }
            }) { Text(if (busy) "保存中…" else "确认") }
        }, dismissButton = { TextButton(onClick = { candidate = null }, enabled = !busy) { Text("取消") } })
    }
}
