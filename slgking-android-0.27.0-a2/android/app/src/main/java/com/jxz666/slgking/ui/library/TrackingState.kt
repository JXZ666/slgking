package com.jxz666.slgking.ui.library

/** Monotonic request ownership: stale callbacks cannot replace a newer section result. */
internal class TrackingRequestGate {
    private var generation = 0L
    fun begin(): Long = ++generation
    fun owns(request: Long): Boolean = request == generation
    fun invalidate() { generation++ }
}

/** A count is factual only after a successful result; retain known counts during refresh. */
internal fun trackingCountLabel(loaded: Boolean, loading: Boolean, error: String?, count: Int): String = when {
    loaded -> count.coerceAtLeast(0).toString()
    loading -> "…"
    error != null -> "—"
    else -> "…"
}

/** Atomic admission guard shared by UI disabling and ViewModel action handling. */
internal class TrackingMarkGate {
    private val pending = mutableSetOf<Int>()
    fun begin(id: Int): Boolean = pending.add(id)
    fun finish(id: Int) { pending.remove(id) }
    fun snapshot(): Set<Int> = pending.toSet()
}
