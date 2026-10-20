package com.jxz666.slgking.ui.profile

import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.Checkbox
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.key
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import com.jxz666.slgking.ui.gamify.signinMessage
import com.jxz666.slgking.ui.gamify.referralCode
import com.jxz666.slgking.ui.gamify.canBindReferral
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import com.jxz666.slgking.bridge.SlgApi
import com.jxz666.slgking.bridge.SlgApiException
import com.jxz666.slgking.ui.gamify.LedgerSheet
import com.jxz666.slgking.ui.gamify.LeaderboardSheet
import com.jxz666.slgking.ui.gamify.LotterySheet
import com.jxz666.slgking.ui.gamify.RedeemDialog
import com.jxz666.slgking.ui.gamify.ShopSheet
import com.jxz666.slgking.ui.gamify.WardrobeSheet
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.launch
import org.json.JSONObject

/** 个人 Tab：登录态 / 账号 / 游戏化入口。 */
@Composable
fun ProfileScreen(onOpenMessages: () -> Unit = {}) {
    val vm: ProfileViewModel = androidx.lifecycle.viewmodel.compose.viewModel(
        viewModelStoreOwner = androidx.compose.ui.platform.LocalContext.current.findProfileActivity())
    val scope = rememberCoroutineScope()
    var loading by vm.loading
    var loggedIn by vm.loggedIn
    var me by vm.me
    var error by vm.error
    var loginOpen by remember { mutableStateOf(false) }
    var createOpen by remember { mutableStateOf(false) }
    var sheet by remember { mutableStateOf<String?>(null) }
    var devicesOpen by remember { mutableStateOf(false) }
    var rotateOpen by remember { mutableStateOf(false) }
    var myCommentsOpen by remember { mutableStateOf(false) }
    var renameOpen by remember { mutableStateOf(false) }
    var messageOpen by remember { mutableStateOf(false) }
    var referralOpen by remember { mutableStateOf(false) }
    var compensationOpen by remember { mutableStateOf(false) }
    var busy by remember { mutableStateOf(false) }
    var opMessage by remember { mutableStateOf<String?>(null) }

    var accountId by vm.accountId
    fun reload(after: () -> Unit = {}) { vm.refresh(after) }
    LaunchedEffect(com.jxz666.slgking.bridge.AccountChanges.revision) {
        val changed = vm.readLocalIdentity()
        if (changed) {
            sheet = null; opMessage = null; referralOpen = false; myCommentsOpen = false
            devicesOpen = false; compensationOpen = false; renameOpen = false; messageOpen = false
            rotateOpen = false; loginOpen = false; busy = false
        }
    }

    run {
        LazyColumn(
            Modifier.fillMaxSize(),
            state = vm.listState,
            contentPadding = androidx.compose.foundation.layout.PaddingValues(16.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            item {
                Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                    Text("个人", style = MaterialTheme.typography.headlineSmall, modifier = Modifier.weight(1f))
                    com.jxz666.slgking.ui.community.ActivityMessageButton(onClick = onOpenMessages)
                    TextButton(onClick = { reload() }, enabled = !loading) {
                        Text(if (loading) "刷新中…" else "刷新资料")
                    }
                }
            }
            if (error != null) {
                item {
                    Card {
                        Column(Modifier.padding(14.dp)) {
                            Text("加载失败：$error", color = MaterialTheme.colorScheme.error,
                                style = MaterialTheme.typography.bodySmall)
                            TextButton(onClick = { reload() }) { Text("重试") }
                        }
                    }
                }
            }
            if (!loggedIn) {
                item { LocalAvatarCard(accountId = "local-guest", nickname = "未登录") }
                item {
                    Card {
                        Column(Modifier.padding(16.dp)) {
                            Text("未登录", style = MaterialTheme.typography.titleMedium)
                            Spacer(Modifier.height(6.dp))
                            Text("登录后可使用签到、积分商城、头衔装扮、排行榜与公开评论。未登录也能浏览目录。",
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant)
                            Spacer(Modifier.height(12.dp))
                            Button(onClick = { loginOpen = true }, modifier = Modifier.fillMaxWidth()) {
                                Text("登录")
                            }
                            Spacer(Modifier.height(6.dp))
                            OutlinedButton(onClick = { createOpen = true }, modifier = Modifier.fillMaxWidth()) {
                                Text("创建账号")
                            }
                            TextButton(onClick = { sheet = "leaderboard" }) { Text("社区排行榜") }
                            Spacer(Modifier.height(6.dp))
                            Text("账号不使用邮箱注册：创建时生成登录密钥与恢复码，请务必妥善保存。",
                                style = MaterialTheme.typography.labelSmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                    }
                }
            } else {
                item { LocalAvatarCard(accountId = accountId, nickname = me?.optString("nickname") ?: "玩家") }
                item { ProfileCard(me = me, busy = busy,
                    onPrivacy = { vm.updatePrivacy(it) },
                    onSignin = {
                        if (!busy) {
                            busy = true
                            val identity = vm.identityState.value
                            scope.launch {
                                try {
                                    val r = SlgApi.raw("points.signin")
                                    vm.readLocalIdentity()
                                    if (identity != vm.identityState.value) return@launch
                                    currentCoroutineContext().ensureActive()
                                    val signin = r.optJSONObject("signin") ?: JSONObject()
                                    opMessage = signinMessage(signin)
                                    me = me?.let { JSONObject(it.toString()).put("signed_today", true) }
                                    reload()
                                } catch (e: CancellationException) { throw e }
                                catch (e: Exception) { if (identity == vm.identityState.value) opMessage = e.message }
                                finally { if (identity == vm.identityState.value || !vm.loggedIn.value) busy = false }
                            }
                        }
                    },
                    onRename = { renameOpen = true },
                    onMessage = { messageOpen = true },
                    onLogout = {
                        if (!busy) {
                            busy = true
                            val identity = vm.identityState.value
                            scope.launch {
                                try {
                                    SlgApi.accountLogout()
                                    vm.readLocalIdentity()
                                    if (identity != vm.identityState.value && vm.loggedIn.value) return@launch
                                    currentCoroutineContext().ensureActive()
                                    sheet = null
                                    me = null
                                    loggedIn = false
                                    accountId = null
                                    reload()
                                } catch (e: CancellationException) { throw e }
                                catch (e: Exception) { if (identity == vm.identityState.value) opMessage = e.message }
                                finally { if (identity == vm.identityState.value || !vm.loggedIn.value) busy = false }
                            }
                        }
                    }) }
                item { EntriesCard(onOpen = { sheet = it }, onDevices = { devicesOpen = true },
                    onRotate = { rotateOpen = true }, onMyComments = { myCommentsOpen = true },
                    onReferral = { referralOpen = true },
                    onCompensation = { compensationOpen = true }) }
            }
            opMessage?.let { msg ->
                item {
                    Card {
                        Column(Modifier.padding(12.dp)) {
                            Text(msg, style = MaterialTheme.typography.bodySmall)
                            TextButton(onClick = { opMessage = null }) { Text("关闭") }
                        }
                    }
                }
            }
        }
    }

    key(vm.identityState.value) {
    if (loginOpen) {
        LoginDialog(
            onDone = { loginOpen = false; reload() },
            onDismiss = { loginOpen = false },
        )
    }
    if (createOpen) {
        AlertDialog(
            onDismissRequest = { if (!busy) createOpen = false },
            title = { Text("创建账号") },
            text = { Text("将生成一串登录密钥和一串恢复码（只显示一次，丢失无法找回）。换设备登录需要这两串密钥。确定创建吗？") },
            confirmButton = {
                TextButton(onClick = {
                    if (busy) return@TextButton
                    createOpen = false
                    vm.createAccount()
                }, enabled = !busy) { Text(if (busy) "创建中…" else "创建") }
            },
            dismissButton = { TextButton(onClick = { createOpen = false }, enabled = !busy) { Text("取消") } },
        )
    }
    key(accountId) {
        when (sheet) {
            "ledger" -> LedgerSheet(me = me, onDismiss = { sheet = null })
            "shop" -> ShopSheet(me = me, onChanged = { sheet = null; reload() }, onDismiss = { sheet = null })
            "wardrobe" -> WardrobeSheet(me = me, onChanged = { reload() }, onDismiss = { sheet = null })
            "leaderboard" -> LeaderboardSheet(onDismiss = { sheet = null })
            "redeem" -> RedeemDialog(onDone = { sheet = null; reload() }, onDismiss = { sheet = null })
            "lottery" -> LotterySheet(onDismiss = { sheet = null }, onChanged = { reload() })
        }
    }
    if (devicesOpen) {
        com.jxz666.slgking.ui.gamify.DevicesSheet(onDismiss = { devicesOpen = false })
    }
    if (rotateOpen) {
        com.jxz666.slgking.ui.gamify.RotateDialog(
            onDone = { rotateOpen = false },
            onSubmit = { loginKey -> rotateOpen = false; vm.rotateAccount(loginKey) },
        )
    }
    if (myCommentsOpen) {
        com.jxz666.slgking.ui.gamify.MyCommentsSheet(onDismiss = { myCommentsOpen = false })
    }
    if (renameOpen) {
        RenameDialog(me = me, onDone = { renameOpen = false; reload() }, onDismiss = { renameOpen = false })
    }
    if (messageOpen) {
        ProfileMessageDialog(me = me,
            onDone = { messageOpen = false; reload() }, onDismiss = { messageOpen = false })
    }
    if (referralOpen) {
        ReferralSheet(onDismiss = { referralOpen = false })
    }
    if (compensationOpen) {
        CompensationSheet(onDismiss = { compensationOpen = false },
            onClaimed = { reload() })
    }
}
}

/** 名片寄语编辑。 */
@Composable
private fun ProfileMessageDialog(me: JSONObject?, onDone: () -> Unit, onDismiss: () -> Unit) {
    var text by remember { mutableStateOf(me?.optString("profile_message") ?: "") }
    var error by remember { mutableStateOf<String?>(null) }
    var busy by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()

    AlertDialog(
        onDismissRequest = { if (!busy) onDismiss() },
        title = { Text("名片寄语") },
        text = {
            Column {
                OutlinedTextField(value = text, onValueChange = { if (it.length <= 40) text = it },
                    singleLine = true, modifier = Modifier.fillMaxWidth(),
                    placeholder = { Text("一句话介绍自己") })
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
                        SlgApi.raw("account.update_profile",
                            JSONObject().put("profile_message", text.trim()))
                        currentCoroutineContext().ensureActive()
                        onDone()
                    } catch (e: CancellationException) { throw e }
                    catch (e: Exception) { error = e.message }
                    busy = false
                }
            }, enabled = !busy) { Text("保存") }
        },
        dismissButton = { TextButton(onClick = onDismiss, enabled = !busy) { Text("取消") } },
    )
}

/** 邀请返利：我的邀请码 + 绑定他人邀请码。 */
@OptIn(androidx.compose.material3.ExperimentalMaterial3Api::class)
@Composable
private fun ReferralSheet(onDismiss: () -> Unit) {
    var info by remember { mutableStateOf<JSONObject?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    var message by remember { mutableStateOf<String?>(null) }
    var code by remember { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }
    val clipboard = LocalClipboardManager.current
    val scope = rememberCoroutineScope()

    fun load() {
        if (busy) return
        busy = true
        scope.launch {
            try {
                info = SlgApi.referralMe()
                currentCoroutineContext().ensureActive()
                error = null
            } catch (e: CancellationException) { throw e }
            catch (e: Exception) { info = null; error = e.message }
            finally { busy = false }
        }
    }
    LaunchedEffect(Unit) { load() }

    ModalBottomSheet(onDismissRequest = { if (!busy) onDismiss() }) {
        Column(Modifier.padding(horizontal = 16.dp).padding(bottom = 28.dp)) {
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                Text("邀请码 / 邀请返利", style = MaterialTheme.typography.titleMedium, modifier = Modifier.weight(1f))
                TextButton(onClick = { load() }, enabled = !busy) { Text("刷新") }
            }
            Spacer(Modifier.height(8.dp))
            when {
                error != null -> Column {
                    Text(error!!, color = MaterialTheme.colorScheme.error)
                    TextButton(onClick = { load() }, enabled = !busy) { Text("重新查询状态") }
                }
                info == null -> CircularProgressIndicator()
                else -> {
                    val myCode = referralCode(info!!)
                    if (myCode.isNotBlank()) {
                        Text("我的邀请码", style = MaterialTheme.typography.labelMedium)
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Text(myCode, style = MaterialTheme.typography.titleLarge,
                                modifier = Modifier.weight(1f))
                            TextButton(onClick = {
                                clipboard.setText(AnnotatedString(myCode))
                                message = "✓ 邀请码已复制"
                            }) { Text("复制") }
                        }
                        Text("已邀请 ${info!!.optInt("invite_count", 0)} 人",
                            style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                    if (myCode.isBlank()) Text("服务器暂未提供邀请码，请重新查询。")
                    info!!.optBoolean("redeemed", false).let { bound ->
                        if (bound) {
                            Spacer(Modifier.height(6.dp))
                            Text("已绑定邀请人", style = MaterialTheme.typography.labelSmall,
                                color = MaterialTheme.colorScheme.primary)
                        }
                    }
                }
            }
            Spacer(Modifier.height(12.dp))
            if (info != null && !canBindReferral(info!!)) Text(
                if (info!!.optBoolean("redeemed")) "此账号已绑定邀请人" else "此账号不符合新账号绑定资格",
                style = MaterialTheme.typography.bodySmall)
            Text("绑定邀请码（新账号一次性）", style = MaterialTheme.typography.labelMedium)
            Row(verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedTextField(value = code, onValueChange = { code = it },
                    singleLine = true, modifier = Modifier.weight(1f),
                    enabled = !busy && info?.let(::canBindReferral) == true,
                    placeholder = { Text("对方的邀请码") })
                Button(onClick = {
                    if (busy || info?.let(::canBindReferral) != true) return@Button
                    busy = true
                    scope.launch {
                        try {
                            SlgApi.referralRedeem(code.trim())
                            currentCoroutineContext().ensureActive()
                            message = "✓ 绑定成功"
                            info = null
                            try { info = SlgApi.referralMe(); error = null }
                            catch (e: CancellationException) { throw e }
                            catch (e: Exception) { error = "绑定已完成，但状态刷新失败，请重新查询。" }
                        } catch (e: CancellationException) { throw e }
                        catch (e: Exception) {
                            message = "绑定结果尚未确认：${e.message}"
                            info = null
                            try {
                                info = SlgApi.referralMe()
                                error = null
                                if (info!!.optBoolean("redeemed")) message = "✓ 已绑定邀请人"
                            } catch (refresh: CancellationException) { throw refresh }
                            catch (refresh: Exception) { error = "无法核对绑定结果，请重新查询状态后再操作。" }
                        } finally { busy = false }
                    }
                }, enabled = code.isNotBlank() && !busy && info?.let(::canBindReferral) == true) { Text("绑定") }
            }
            message?.let {
                Spacer(Modifier.height(6.dp))
                Text(it, style = MaterialTheme.typography.labelMedium,
                    color = if (it.startsWith("✓")) MaterialTheme.colorScheme.primary
                    else MaterialTheme.colorScheme.error)
            }
        }
    }
}

/** 维护补偿：有活动时可领取。 */
@OptIn(androidx.compose.material3.ExperimentalMaterial3Api::class)
@Composable
private fun CompensationSheet(onDismiss: () -> Unit, onClaimed: () -> Unit = {}) {
    var status by remember { mutableStateOf<JSONObject?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    var message by remember { mutableStateOf<String?>(null) }
    var busy by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()

    LaunchedEffect(Unit) {
        scope.launch {
            try { status = SlgApi.maintenanceRewardStatus(); error = null }
            catch (e: CancellationException) { throw e }
            catch (e: Exception) { error = e.message }
        }
    }

    ModalBottomSheet(onDismissRequest = { if (!busy) onDismiss() }) {
        Column(Modifier.padding(horizontal = 16.dp).padding(bottom = 28.dp)) {
            Text("维护补偿", style = MaterialTheme.typography.titleMedium)
            Spacer(Modifier.height(8.dp))
            when {
                error != null -> Text(error!!, color = MaterialTheme.colorScheme.error)
                status == null -> CircularProgressIndicator()
                else -> {
                    val camp = status!!.optJSONObject("campaign")
                    val campaign = camp?.optString("id") ?: ""
                    val available = (camp?.optBoolean("active", false) == true)
                    val claimed = status!!.optBoolean("claimed", false)
                    val note = if (camp != null)
                        "${camp.optString("title")}（+${camp.optInt("amount", 0)} 积分）" else ""
                    if (note.isNotBlank()) {
                        Text(note, style = MaterialTheme.typography.bodyMedium)
                        Spacer(Modifier.height(8.dp))
                    }
                    when {
                        claimed -> Text("本次补偿已领取",
                            color = MaterialTheme.colorScheme.primary)
                        available -> Button(
                            onClick = {
                                if (busy) return@Button
                                busy = true
                                scope.launch {
                                    try {
                                        SlgApi.maintenanceRewardClaim(campaign)
                                        currentCoroutineContext().ensureActive()
                                        status = status?.put("claimed", true)
                                        message = "✓ 已领取"
                                        onClaimed()
                                    } catch (e: CancellationException) { throw e }
                                    catch (e: Exception) { message = e.message }
                                    busy = false
                                }
                            },
                            enabled = !busy && campaign.isNotBlank(),
                        ) { Text("领取补偿") }
                        else -> Text("当前没有进行中的补偿活动",
                            color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                }
            }
            message?.let {
                Spacer(Modifier.height(6.dp))
                Text(it, style = MaterialTheme.typography.labelMedium,
                    color = if (it.startsWith("✓")) MaterialTheme.colorScheme.primary
                    else MaterialTheme.colorScheme.error)
            }
        }
    }
}

@Composable
private fun Box_(content: @Composable () -> Unit) {
    androidx.compose.foundation.layout.Box(
        Modifier.fillMaxSize(), contentAlignment = Alignment.Center) { content() }
}

@Composable
private fun ProfileCard(
    me: JSONObject?,
    busy: Boolean,
    onPrivacy: (Boolean) -> Unit,
    onSignin: () -> Unit,
    onRename: () -> Unit,
    onMessage: () -> Unit,
    onLogout: () -> Unit,
) {
    Card {
        Column(Modifier.padding(16.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) {
                    Text(me?.optString("nickname")?.takeIf { it.isNotBlank() } ?: "玩家",
                        style = MaterialTheme.typography.titleLarge)
                    me?.optString("title_name")?.takeIf { it.isNotBlank() }?.let {
                        Text(it, style = MaterialTheme.typography.labelMedium,
                            color = MaterialTheme.colorScheme.primary)
                    }
                    me?.optJSONObject("appearance_names")?.let { names ->
                        listOf("avatar_frame" to "头像框", "comment_frame" to "评论框").forEach { (slot, label) ->
                            names.optString(slot).takeIf { it.isNotBlank() }?.let { name ->
                                Text("$label：$name", style = MaterialTheme.typography.labelSmall,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant)
                            }
                        }
                    }
                    me?.optString("profile_message")?.takeIf { it.isNotBlank() }?.let {
                        Text("寄语：$it", style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            maxLines = 1)
                    }
                }
                TextButton(onClick = onRename, enabled = !busy) { Text("改名") }
                TextButton(onClick = onMessage, enabled = !busy) { Text("寄语") }
            }
            Spacer(Modifier.height(6.dp))
            Text(if (me?.has("balance") == true) "积分 ${me.optInt("balance")}" else "资料尚未获取，请点击刷新资料",
                style = MaterialTheme.typography.titleMedium)
            if (me?.has("profile_visible") == true) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text("公开个人名片", modifier = Modifier.weight(1f))
                    androidx.compose.material3.Switch(checked = me.optBoolean("profile_visible"),
                        onCheckedChange = onPrivacy, enabled = !busy)
                }
            }
            Spacer(Modifier.height(10.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(onClick = onSignin, enabled = !busy && me?.optBoolean("signed_today", false) != true, modifier = Modifier.weight(1f)) {
                    Text(if (me?.optBoolean("signed_today", false) == true) "今日已签到" else "每日签到 +10")
                }
                OutlinedButton(onClick = onLogout, enabled = !busy, modifier = Modifier.weight(1f)) {
                    Text("退出登录")
                }
            }
            Text("签到与抽奖按北京时间每日 00:00 刷新。", style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}

@Composable
private fun EntriesCard(
    onOpen: (String) -> Unit,
    onDevices: () -> Unit,
    onRotate: () -> Unit,
    onMyComments: () -> Unit,
    onReferral: () -> Unit,
    onCompensation: () -> Unit,
) {
    Card {
        Column(Modifier.padding(vertical = 6.dp)) {
            Entry("本机积分流水") { onOpen("ledger") }
            Entry("积分商城 / 任务") { onOpen("shop") }
            Entry("兑换码") { onOpen("redeem") }
            Entry("头衔 / 衣橱") { onOpen("wardrobe") }
            Entry("排行榜") { onOpen("leaderboard") }
            Entry("每日抽奖") { onOpen("lottery") }
            Entry("邀请码 / 邀请返利") { onReferral() }
            Entry("维护补偿") { onCompensation() }
            Entry("我的评论") { onMyComments() }
            Entry("设备管理") { onDevices() }
            Entry("密钥换绑") { onRotate() }
        }
    }
}

@Composable
private fun Entry(label: String, onClick: () -> Unit) {
    androidx.compose.material3.Surface(onClick = onClick, color = androidx.compose.ui.graphics.Color.Transparent) {
        Row(Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 13.dp),
            verticalAlignment = Alignment.CenterVertically) {
            Text(label, modifier = Modifier.weight(1f))
            Text("›", color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}

@Composable
private fun LoginDialog(onDone: () -> Unit, onDismiss: () -> Unit) {
    var key by remember { mutableStateOf("") }
    var code by remember { mutableStateOf("") }
    var error by remember { mutableStateOf<String?>(null) }
    var busy by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()

    AlertDialog(
        onDismissRequest = { if (!busy) onDismiss() },
        title = { Text("登录") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedTextField(value = key, onValueChange = { key = it },
                    label = { Text("登录密钥") }, singleLine = true)
                OutlinedTextField(value = code, onValueChange = { code = it },
                    label = { Text("恢复码（新设备必填）") }, singleLine = true)
                error?.let { Text(it, color = MaterialTheme.colorScheme.error,
                    style = MaterialTheme.typography.labelSmall) }
                Text("受信任设备 7 天内免恢复码；密钥丢失无法找回。",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        },
        confirmButton = {
            Button(
                onClick = {
                    if (busy) return@Button
                    busy = true
                    scope.launch {
                        try {
                            SlgApi.accountLogin(key, code.ifBlank { null })
                            currentCoroutineContext().ensureActive()
                            onDone()
                        } catch (e: SlgApiException) {
                            error = e.message
                        } catch (e: CancellationException) { throw e }
                        catch (e: Exception) {
                            error = "${e.javaClass.simpleName}: ${e.message}"
                        } finally {
                            busy = false
                        }
                    }
                },
                enabled = key.isNotBlank() && !busy,
            ) { Text(if (busy) "登录中…" else "登录") }
        },
        dismissButton = { TextButton(onClick = onDismiss, enabled = !busy) { Text("取消") } },
    )
}

/** 一次性密钥展示：勾选「已保存」前拦截返回。 */
@Composable
fun KeyRevealScreen(account: JSONObject, onDone: () -> Unit) {
    val clipboard = LocalClipboardManager.current
    var saved by remember { mutableStateOf(false) }
    var showSaveReminder by remember { mutableStateOf(false) }

    androidx.activity.compose.BackHandler { showSaveReminder = true }

    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(24.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center,
    ) {
        Text("密钥只显示这一次", style = MaterialTheme.typography.headlineSmall,
            color = MaterialTheme.colorScheme.error)
        Spacer(Modifier.height(8.dp))
        Text("请立即复制并妥善保存。丢失后账号无法找回，也没有邮箱可以重置。",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.height(16.dp))
        KeyBlock("登录密钥", account.optString("login_key"), clipboard)
        Spacer(Modifier.height(10.dp))
        KeyBlock("恢复码", account.optString("recovery_code"), clipboard)
        account.optString("session_error").takeIf { it.isNotBlank() }?.let {
            Spacer(Modifier.height(6.dp))
            Text(it, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.labelSmall)
        }
        Spacer(Modifier.height(8.dp))
        Row(verticalAlignment = Alignment.CenterVertically) {
            Checkbox(checked = saved, onCheckedChange = { saved = it })
            Text("我已把两串密钥保存到安全的地方")
        }
        Spacer(Modifier.height(12.dp))
        Button(onClick = onDone, enabled = saved, modifier = Modifier.fillMaxWidth()) {
            Text("完成")
        }
    }

    if (showSaveReminder) {
        AlertDialog(
            onDismissRequest = { showSaveReminder = false },
            title = { Text("请先保存密钥") },
            text = { Text("复制并保存登录密钥和恢复码，勾选确认后点击完成。密钥不会写入本机存档，关闭进程后无法再次显示。") },
            confirmButton = { TextButton(onClick = { showSaveReminder = false }) { Text("继续保存") } },
        )
    }
}

@Composable
private fun KeyBlock(label: String, value: String, clipboard: androidx.compose.ui.platform.ClipboardManager) {
    Column(Modifier.fillMaxWidth()) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(label, style = MaterialTheme.typography.labelLarge, modifier = Modifier.weight(1f))
            TextButton(onClick = { clipboard.setText(AnnotatedString(value)) }) { Text("复制") }
        }
        Card {
            SelectionContainer {
                Text(
                    value,
                    fontFamily = FontFamily.Monospace,
                    style = MaterialTheme.typography.bodyLarge,
                    modifier = Modifier.padding(12.dp).fillMaxWidth(),
                )
            }
        }
    }
}

@Composable
private fun RenameDialog(me: JSONObject?, onDone: () -> Unit, onDismiss: () -> Unit) {
    var name by remember { mutableStateOf(me?.optString("nickname") ?: "") }
    var error by remember { mutableStateOf<String?>(null) }
    var busy by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()

    AlertDialog(
        onDismissRequest = { if (!busy) onDismiss() },
        title = { Text("修改昵称") },
        text = {
            Column {
                OutlinedTextField(value = name, onValueChange = { name = it }, singleLine = true)
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
                        SlgApi.raw("account.update_profile",
                            JSONObject().put("nickname", name.trim()))
                        currentCoroutineContext().ensureActive()
                        onDone()
                    } catch (e: CancellationException) { throw e }
                    catch (e: Exception) { error = e.message }
                    busy = false
                }
            }, enabled = name.isNotBlank() && !busy) { Text("保存") }
        },
        dismissButton = { TextButton(onClick = onDismiss, enabled = !busy) { Text("取消") } },
    )
}
