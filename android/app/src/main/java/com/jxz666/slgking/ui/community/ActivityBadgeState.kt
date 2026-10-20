package com.jxz666.slgking.ui.community

import android.os.SystemClock
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Notifications
import androidx.compose.material3.Badge
import androidx.compose.material3.BadgedBox
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.runtime.*
import com.jxz666.slgking.bridge.AccountChanges
import kotlinx.coroutines.delay
import org.json.JSONObject

internal object ActivityBadgeState {
    private val memory = ActivityBadgeMemory()
    var changeRevision by mutableIntStateOf(0)
        private set
    fun clear() { memory.clear(); changeRevision++ }
    fun confirm(revision: Int, status: JSONObject, session: JSONObject) {
        if (revision != AccountChanges.revision) return
        memory.confirm(revision, rewardClaimable(status, session), SystemClock.elapsedRealtime())
        changeRevision++
    }
    fun remaining(revision: Int): Long = memory.remaining(revision, SystemClock.elapsedRealtime())
}

/** One local expiry timer; no polling, prefetch, unread-notification or announcement badge. */
@Composable
fun ActivityMessageButton(onClick: () -> Unit) {
    val accountRevision = AccountChanges.revision
    val changed = ActivityBadgeState.changeRevision
    var clockTick by remember { mutableIntStateOf(0) }
    val visible = remember(accountRevision, changed, clockTick) {
        ActivityBadgeState.remaining(accountRevision) > 0
    }
    LaunchedEffect(accountRevision, changed) {
        val expires = ActivityBadgeState.remaining(accountRevision)
        if (expires > 0) { delay(expires); clockTick++ }
    }
    IconButton(onClick = onClick) {
        BadgedBox(badge = { if (visible) Badge() }) {
            Icon(Icons.Filled.Notifications, contentDescription = "消息")
        }
    }
}
