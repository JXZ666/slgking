package com.jxz666.slgking.ui

import android.content.Context
import android.os.Build
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.dynamicDarkColorScheme
import androidx.compose.material3.dynamicLightColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext

/** 主题偏好（system/light/dark），持久化在应用 SharedPreferences。 */
object ThemePrefs {
    var mode by mutableStateOf("system")
        private set

    fun load(context: Context) {
        mode = context.getSharedPreferences("slgking_shell", 0).getString("theme", "system")
            ?: "system"
    }

    fun set(context: Context, value: String) {
        mode = value
        context.getSharedPreferences("slgking_shell", 0)
            .edit().putString("theme", value).apply()
    }
}

private val LightColors = lightColorScheme(
    primary = Color(0xFF2F6FD0),
    secondary = Color(0xFF526070),
    surface = Color(0xFFF7F8FA),
    background = Color(0xFFF7F8FA),
)

private val DarkColors = darkColorScheme(
    primary = Color(0xFF9EC1F5),
    secondary = Color(0xFFB9C4D4),
    surface = Color(0xFF16181D),
    background = Color(0xFF101216),
)

@Composable
fun SlgkingTheme(
    darkTheme: Boolean = when (ThemePrefs.mode) {
        "light" -> false
        "dark" -> true
        else -> isSystemInDarkTheme()
    },
    content: @Composable () -> Unit,
) {
    val context = LocalContext.current
    val scheme = when {
        Build.VERSION.SDK_INT >= Build.VERSION_CODES.S ->
            if (darkTheme) dynamicDarkColorScheme(context) else dynamicLightColorScheme(context)
        darkTheme -> DarkColors
        else -> LightColors
    }
    MaterialTheme(colorScheme = scheme, content = content)
}
