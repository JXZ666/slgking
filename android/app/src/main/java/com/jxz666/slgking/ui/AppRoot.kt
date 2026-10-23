package com.jxz666.slgking.ui

import android.widget.Toast
import androidx.activity.compose.BackHandler
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.AutoStories
import androidx.compose.material.icons.filled.Construction
import androidx.compose.material.icons.filled.Person
import androidx.compose.material.icons.filled.Search
import androidx.compose.material.icons.filled.VideoLibrary
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.saveable.rememberSaveableStateHolder
import androidx.compose.runtime.saveable.listSaver
import androidx.compose.runtime.toMutableStateList
import androidx.compose.runtime.snapshots.SnapshotStateList
import java.util.UUID
import org.json.JSONObject
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import com.jxz666.slgking.SlgApp
import com.jxz666.slgking.ui.catalog.CatalogScreen
import com.jxz666.slgking.ui.library.LibraryScreen
import com.jxz666.slgking.ui.profile.ProfileScreen
import com.jxz666.slgking.ui.profile.ProfileViewModel
import com.jxz666.slgking.ui.profile.KeyRevealScreen
import com.jxz666.slgking.ui.profile.findProfileActivity
import com.jxz666.slgking.ui.tools.ToolsScreen

/** 覆盖层（二级页），本地返回栈。 */
sealed interface Overlay {
    data object Messages : Overlay
    data class Detail(val gameId: Int) : Overlay
    data class Comments(val slug: String, val title: String) : Overlay
}

internal data class OverlayEntry(val overlay: Overlay, val id: String = UUID.randomUUID().toString())

internal val OverlayStackSaver = listSaver<SnapshotStateList<OverlayEntry>, String>(
    save = { stack -> stack.map { entry ->
        JSONObject().put("id", entry.id).apply {
            when (val value = entry.overlay) {
                is Overlay.Detail -> put("kind", "detail").put("game", value.gameId)
                is Overlay.Comments -> put("kind", "comments").put("slug", value.slug).put("title", value.title)
                Overlay.Messages -> put("kind", "messages")
            }
        }.toString()
    } },
    restore = { rows -> rows.map { row ->
        val value = JSONObject(row)
        val overlay = when (value.getString("kind")) {
            "detail" -> Overlay.Detail(value.getInt("game"))
            "comments" -> Overlay.Comments(value.getString("slug"), value.getString("title"))
            else -> Overlay.Messages
        }
        OverlayEntry(overlay, value.getString("id"))
    }.toMutableStateList() },
)

private enum class Tab(val label: String) {
    Catalog("目录"), Library("追踪"), Profile("个人"), Tools("工具")
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun AppRoot() {
    val context = LocalContext.current
    val prefs = remember { context.getSharedPreferences("slgking_shell", 0) }

    var bootError by remember { mutableStateOf<String?>(null) }
    var ready by remember { mutableStateOf(false) }
    var ageOk by rememberSaveable { mutableStateOf(prefs.getBoolean("age_ok", false)) }
    var welcomeDone by rememberSaveable { mutableStateOf(prefs.getBoolean("welcome_done", false)) }

    LaunchedEffect(Unit) {
        try {
            SlgApp.ready.await()
            ready = true
        } catch (e: Exception) {
            bootError = "${e.javaClass.simpleName}: ${e.message}"
        }
    }

    when {
        bootError != null -> BootError(bootError!!)
        !ready -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
            CircularProgressIndicator()
        }
        !ageOk -> AgeGate(onConfirm = {
            prefs.edit().putBoolean("age_ok", true).apply()
            ageOk = true
        }, onExit = { (context as? android.app.Activity)?.finish() })
        !welcomeDone -> Welcome(onDone = {
            prefs.edit().putBoolean("welcome_done", true).apply()
            welcomeDone = true
        })
        else -> MainScreen()
    }
}

@Composable
private fun BootError(message: String) {
    Box(Modifier.fillMaxSize().padding(24.dp), contentAlignment = Alignment.Center) {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            Text("初始化失败", style = MaterialTheme.typography.titleMedium)
            Spacer(Modifier.height(8.dp))
            Text(message, color = MaterialTheme.colorScheme.error, textAlign = TextAlign.Center)
        }
    }
}

@Composable
private fun AgeGate(onConfirm: () -> Unit, onExit: () -> Unit) {
    Column(
        Modifier.fillMaxSize().padding(32.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = androidx.compose.foundation.layout.Arrangement.Center,
    ) {
        Text("黄油之王", style = MaterialTheme.typography.headlineMedium)
        Spacer(Modifier.height(16.dp))
        Text(
            "本应用面向 18 周岁及以上用户，包含成人向游戏目录信息。\n进入即表示你已年满 18 周岁并自愿使用。",
            textAlign = TextAlign.Center,
            style = MaterialTheme.typography.bodyMedium,
        )
        Spacer(Modifier.height(24.dp))
        Button(onClick = onConfirm, modifier = Modifier.fillMaxWidth()) {
            Text("我已满 18 岁，继续使用")
        }
        Spacer(Modifier.height(8.dp))
        OutlinedButton(onClick = onExit, modifier = Modifier.fillMaxWidth()) {
            Text("退出")
        }
    }
}

@Composable
private fun Welcome(onDone: () -> Unit) {
    Column(
        Modifier.fillMaxSize().padding(32.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = androidx.compose.foundation.layout.Arrangement.Center,
    ) {
        Text("欢迎使用", style = MaterialTheme.typography.headlineSmall)
        Spacer(Modifier.height(16.dp))
        Text("① 目录：SLG、DLsite 与全部游戏，检索和查看来源\n\n② 追踪：想玩、版本更新与收藏；我的记录可手动记录移植版\n\n③ 个人：登录云账号，积分/头衔/评论\n\n④ 工具：更新所选目录、翻译与设置", style = MaterialTheme.typography.bodyMedium)
        Spacer(Modifier.height(24.dp))
        Button(onClick = onDone, modifier = Modifier.fillMaxWidth()) {
            Text("开始使用")
        }
        Spacer(Modifier.height(8.dp))
        OutlinedButton(onClick = onDone) { Text("跳过") }
    }
}

@Composable
private fun MainScreen() {
    val context = LocalContext.current
    val profileVm: ProfileViewModel = androidx.lifecycle.viewmodel.compose.viewModel(
        viewModelStoreOwner = context.findProfileActivity())
    val secretState = profileVm.secretState.value
    val navigationLocked = secretState.locked
    var tab by rememberSaveable { mutableStateOf(Tab.Catalog) }
    val overlays = rememberSaveable(saver = OverlayStackSaver) { androidx.compose.runtime.mutableStateListOf<OverlayEntry>() }
    var catalogEpoch by rememberSaveable { mutableStateOf(0) }
    var libraryEpoch by rememberSaveable { mutableStateOf(0) }
    var profileEpoch by rememberSaveable { mutableStateOf(0) }
    var toolsEpoch by rememberSaveable { mutableStateOf(0) }
    var lastBack by remember { mutableStateOf(0L) }
    val stateHolder = rememberSaveableStateHolder()
    val overlayStateHolder = rememberSaveableStateHolder()
    fun push(overlay: Overlay) { overlays.add(OverlayEntry(overlay)) }
    fun pop() { if (overlays.isNotEmpty()) overlayStateHolder.removeState(overlays.removeAt(overlays.lastIndex).id) }
    fun clearOverlays() { overlays.forEach { overlayStateHolder.removeState(it.id) }; overlays.clear() }
    fun selectTab(target: Tab) {
        if (tab == target) return
        when (target) {
            Tab.Catalog -> catalogEpoch++
            Tab.Library -> libraryEpoch++
            Tab.Profile -> profileEpoch++
            Tab.Tools -> toolsEpoch++
        }
        clearOverlays()
        tab = target
    }
    // 详情页标签点选 → 返回目录并应用该标签为筛选
    var pendingTag by remember { mutableStateOf<String?>(null) }
    var openTranslationSettings by remember { mutableStateOf(false) }

    BackHandler(enabled = !navigationLocked && overlays.isNotEmpty()) { pop() }
    BackHandler(enabled = !navigationLocked && overlays.isEmpty()) {
        val now = System.currentTimeMillis()
        if (now - lastBack < 2000) {
            (context as? android.app.Activity)?.finish()
        } else {
            lastBack = now
            Toast.makeText(context, "再按一次退出", Toast.LENGTH_SHORT).show()
        }
    }

    LaunchedEffect(navigationLocked) {
        if (navigationLocked) { tab = Tab.Profile; clearOverlays() }
    }
    BackHandler(enabled = navigationLocked) {
        Toast.makeText(context, "请等待密钥操作完成并保存密钥", Toast.LENGTH_SHORT).show()
    }
    if (navigationLocked) {
        androidx.compose.material3.Surface(Modifier.fillMaxSize()) {
            val credentials = secretState.pending
            if (credentials != null) {
                KeyRevealScreen(account = credentials, onDone = { profileVm.acknowledgeSecret() })
            } else {
                Column(Modifier.fillMaxSize().padding(24.dp), horizontalAlignment = Alignment.CenterHorizontally,
                    verticalArrangement = androidx.compose.foundation.layout.Arrangement.Center) {
                    CircularProgressIndicator()
                    Spacer(Modifier.height(16.dp))
                    Text(if (secretState.operation == "account.rotate") "正在换绑密钥，请等待结果…" else "正在创建账号，请等待结果…")
                    Text("请勿关闭进程。成功后会显示一次性密钥供你保存。", style = MaterialTheme.typography.bodySmall)
                }
            }
        }
        return
    }

    Scaffold(
        bottomBar = {
            NavigationBar {
                Tab.entries.forEach { t ->
                    NavigationBarItem(
                        selected = tab == t,
                        enabled = !navigationLocked,
                        onClick = { selectTab(t) },
                        icon = {
                            Icon(
                                when (t) {
                                    Tab.Catalog -> Icons.Filled.Search
                                    Tab.Library -> Icons.Filled.VideoLibrary
                                    Tab.Profile -> Icons.Filled.Person
                                    Tab.Tools -> Icons.Filled.Construction
                                },
                                contentDescription = t.label,
                            )
                        },
                        label = { Text(t.label) },
                    )
                }
            }
        },
    ) { padding ->
        Box(Modifier.fillMaxSize().padding(padding)) {
            // 保留筛选/输入状态；主动切换主Tab只消费一次滚动重置。
            stateHolder.SaveableStateProvider(tab.name) {
                when (tab) {
                    Tab.Catalog -> CatalogScreen(
                        openDetail = { push(Overlay.Detail(it)) },
                        pendingTag = pendingTag,
                        scrollResetEpoch = catalogEpoch,
                        onConsumePendingTag = { pendingTag = null },
                    )
                    Tab.Library -> LibraryScreen(openDetail = { push(Overlay.Detail(it)) }, scrollResetEpoch = libraryEpoch)
                    Tab.Profile -> ProfileScreen(onOpenMessages = { push(Overlay.Messages) }, scrollResetEpoch = profileEpoch)
                    Tab.Tools -> ToolsScreen(openTranslationSettings = openTranslationSettings, scrollResetEpoch = toolsEpoch,
                        onTranslationSettingsConsumed = { openTranslationSettings = false })
                }
            }
            overlays.lastOrNull()?.let { entry ->
                androidx.compose.runtime.key(entry.id) {
                    overlayStateHolder.SaveableStateProvider(entry.id) {
                when (val overlay = entry.overlay) {
                    is Overlay.Detail -> com.jxz666.slgking.ui.game.GameDetailScreen(
                        gameId = overlay.gameId,
                        onBack = { pop() },
                        openComments = { slug, title ->
                            push(Overlay.Comments(slug, title))
                        },
                        onTagPicked = { tag ->
                            pendingTag = tag
                            selectTab(Tab.Catalog)
                            clearOverlays()
                        },
                        onOpenTranslationSettings = {
                            clearOverlays()
                            selectTab(Tab.Tools)
                            openTranslationSettings = true
                        },
                    )
                    is Overlay.Comments -> com.jxz666.slgking.ui.comments.CommentsScreen(
                        slug = overlay.slug,
                        title = overlay.title,
                        onBack = { pop() },
                    )
                    Overlay.Messages -> com.jxz666.slgking.ui.community.MessageCenter(
                        onClose = { pop() })
                }
                    }
                }
            }
        }
    }
}
