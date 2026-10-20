package com.jxz666.slgking.ui.profile

import java.security.MessageDigest

/** Pure identity/request fence: slow responses from A never populate B. */
internal class ProfileFence {
    var identity = ""
        private set
    var generation = 0L
        private set
    fun select(identity: String): Boolean {
        if (this.identity == identity) return false
        this.identity = identity
        generation++
        return true
    }
    fun request(): Pair<String, Long> = identity to ++generation
    fun accepts(ticket: Pair<String, Long>) = ticket.first == identity && ticket.second == generation
}

internal object AvatarPolicy {
    const val MAX_BYTES = 65536
    const val SIDE = 256
    fun filename(identity: String): String = MessageDigest.getInstance("SHA-256")
        .digest(identity.toByteArray(Charsets.UTF_8)).joinToString("") { "%02x".format(it) } + ".jpg"
    fun initial(nickname: String): String = nickname.trim().firstOrNull()?.toString() ?: "玩"
}


/** Memory-only operation state. UI recreation never owns or discards credentials. */
internal data class SecretOperationState<T>(val operation: String? = null, val pending: T? = null) {
    val locked: Boolean get() = operation != null || pending != null
    fun begin(kind: String): SecretOperationState<T>? = if (locked) null else copy(operation = kind)
    fun succeeded(value: T): SecretOperationState<T> = copy(operation = null, pending = value)
    fun failed(): SecretOperationState<T> = copy(operation = null)
    fun acknowledged(): SecretOperationState<T> = if (operation == null) copy(pending = null) else this
}
