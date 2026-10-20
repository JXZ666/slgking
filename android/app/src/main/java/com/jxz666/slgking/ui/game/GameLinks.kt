package com.jxz666.slgking.ui.game

import java.net.URI

/** A webpage only: never hand an application, file or script URI to Android. */
fun validWebUrl(raw: String): String? = try {
    val value = raw.trim()
    val uri = URI(value)
    if (value.any { it.isWhitespace() || it.isISOControl() } ||
        uri.scheme?.lowercase() !in setOf("http", "https") ||
        uri.host.isNullOrBlank() || uri.userInfo != null || uri.port < -1 || uri.port > 65535) null
    else value
} catch (_: Exception) { null }

fun sourceDomain(raw: String): String = try { URI(raw).host ?: "来源网页" } catch (_: Exception) { "来源网页" }
