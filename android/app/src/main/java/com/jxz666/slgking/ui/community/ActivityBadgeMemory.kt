package com.jxz666.slgking.ui.community

/** Pure memory-only policy; the caller uses a monotonic clock. No account or network access. */
internal class ActivityBadgeMemory(private val lifetimeMs: Long = 5 * 60 * 1000L) {
    private var accountRevision: Int? = null
    private var confirmedAt: Long? = null
    fun confirm(revision: Int, eligible: Boolean, nowMs: Long) {
        clear()
        if (eligible) { accountRevision = revision; confirmedAt = nowMs }
    }
    fun clear() { accountRevision = null; confirmedAt = null }
    fun remaining(revision: Int, nowMs: Long): Long {
        val start = confirmedAt ?: return 0
        val age = nowMs - start
        if (accountRevision != revision || age < 0 || age >= lifetimeMs) { clear(); return 0 }
        return lifetimeMs - age
    }
    fun visible(revision: Int, nowMs: Long): Boolean = remaining(revision, nowMs) > 0
}
