package com.jxz666.slgking.ui.profile

import android.content.Context
import android.content.ContextWrapper
import androidx.activity.ComponentActivity
import androidx.compose.runtime.mutableStateOf
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.jxz666.slgking.bridge.SlgApi
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.launch
import org.json.JSONObject

internal fun Context.findProfileActivity(): ComponentActivity {
    var current = this
    while (current is ContextWrapper) {
        if (current is ComponentActivity) return current
        current = current.baseContext
    }
    error("Profile requires an Activity")
}

class ProfileViewModel : ViewModel() {
    val identityState = mutableStateOf("")
    val listState = androidx.compose.foundation.lazy.LazyListState()
    val loading = mutableStateOf(false)
    val loggedIn = mutableStateOf(false)
    val me = mutableStateOf<JSONObject?>(null)
    val error = mutableStateOf<String?>(null)
    val accountId = mutableStateOf<String?>(null)
    private val fence = ProfileFence()

    // Deliberately not SavedStateHandle/rememberSaveable: account secrets stay in RAM only.
    internal val secretState = mutableStateOf(SecretOperationState<JSONObject>())
    fun createAccount() = runSecretOperation("account.create", JSONObject())
    fun rotateAccount(loginKey: String) = runSecretOperation("account.rotate", JSONObject().put("login_key", loginKey))
    fun acknowledgeSecret() {
        secretState.value = secretState.value.acknowledged()
        refresh()
    }
    private fun runSecretOperation(method: String, params: JSONObject) {
        val started = secretState.value.begin(method) ?: return
        secretState.value = started
        error.value = null
        viewModelScope.launch {
            try {
                val result = SlgApi.raw(method, params)
                val credentials = result.optJSONObject("account")
                    ?: kotlin.error("服务器未返回密钥，请核对账号状态后再操作")
                // Retain the response before any further suspend/read, including partial credentials.
                secretState.value = secretState.value.succeeded(credentials)
                readLocalIdentity()
            } catch (e: CancellationException) { throw e }
            catch (e: Exception) { error.value = e.message ?: "密钥操作未能确认，请先核对账号状态" }
            finally { secretState.value = secretState.value.failed() }
        }
    }

    suspend fun readLocalIdentity(): Boolean {
        return try {
            val status = SlgApi.raw("account.status")
            val id = status.optString("account_id").takeIf { it.isNotBlank() && it != "null" }
            val active = status.optBoolean("logged_in")
            val changed = fence.select("${if (active) id else "guest"}:${status.optString("session_epoch")}")
            identityState.value = fence.identity
            if (changed) { me.value = null; error.value = null; loading.value = false }
            accountId.value = if (active) id else null
            loggedIn.value = active
            changed
        } catch (e: CancellationException) { throw e }
        catch (e: Exception) { error.value = e.message; false }
    }

    fun refresh(after: () -> Unit = {}) {
        if (loading.value) return
        loading.value = true
        viewModelScope.launch {
            var ticket: Pair<String, Long>? = null
            try {
                readLocalIdentity()
                if (!loggedIn.value) return@launch
                ticket = fence.request()
                loading.value = true
                val result = SlgApi.raw("account.profile")
                readLocalIdentity()
                if (fence.accepts(ticket)) {
                    me.value = result.optJSONObject("me")
                    error.value = null
                }
            } catch (e: CancellationException) { throw e }
            catch (e: Exception) { if (ticket == null || fence.accepts(ticket)) error.value = e.message }
            finally {
                if (ticket == null || fence.accepts(ticket)) { loading.value = false; after() }
            }
        }
    }

    fun updatePrivacy(visible: Boolean) {
        if (loading.value || !loggedIn.value) return
        val ticket = fence.request()
        loading.value = true
        viewModelScope.launch {
            try {
                SlgApi.raw("account.update_profile", JSONObject().put("profile_visible", visible))
                readLocalIdentity()
                if (fence.accepts(ticket)) {
                    // No optimistic toggle: only reflect a confirmed server response.
                    val response = SlgApi.raw("account.profile")
                    readLocalIdentity()
                    if (fence.accepts(ticket)) me.value = response.optJSONObject("me")
                }
            } catch (e: CancellationException) { throw e }
            catch (e: Exception) { if (fence.accepts(ticket)) error.value = e.message }
            finally { if (fence.accepts(ticket)) loading.value = false }
        }
    }
}
