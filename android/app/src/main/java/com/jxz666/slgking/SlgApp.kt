package com.jxz666.slgking

import android.app.Application
import android.util.Log
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import kotlinx.coroutines.Deferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.async
import java.io.File

/**
 * 应用入口：启动 Python 运行时（Chaquopy）、从 APK assets 铺种子文件、
 * 执行 slg_android_bootstrap.init，然后桥才可用。
 */
class SlgApp : Application() {

    override fun onCreate() {
        super.onCreate()
        ready = appScope.async { bootstrap() }
    }

    private suspend fun bootstrap(): String {
        if (!Python.isStarted()) Python.start(AndroidPlatform(this))
        stageSeedAssets()
        val py = Python.getInstance()
        val boot = py.getModule("slg_android_bootstrap")
        val result = boot.callAttr(
            "init",
            filesDir.absolutePath,
            File(filesDir, "seed_staging").absolutePath,
            "java"
        ).toString()
        Log.i(TAG, "bootstrap: $result")
        // 匿名启动遥测（fire-and-forget，与桌面口径一致：随机设备号 + 事件名）
        try {
            py.getModule("slg_android_api").callAttr(
                "call",
                "{\"method\":\"telemetry.report\",\"event\":\"startup\"," +
                    "\"data\":{\"platform\":\"android\",\"app\":\"slgking\"}}"
            )
        } catch (_: Exception) { }
        return result
    }

    /** 把 APK assets 里的 seed/ 与 tag_zh.json 复制到私有目录（仅首启一次）。 */
    private fun stageSeedAssets() {
        val staging = File(filesDir, "seed_staging")
        val marker = File(staging, ".staged")
        if (marker.exists() && marker.readText() == BuildConfig.VERSION_NAME) return
        staging.mkdirs()
        copyAssetDir("seed", File(staging, "seed"))
        copyAssetDir("dlsite", File(staging, "dlsite"))
        assets.open("tag_zh.json").use { input ->
            File(staging, "tag_zh.json").outputStream().use { input.copyTo(it) }
        }
        marker.writeText(BuildConfig.VERSION_NAME)
        Log.i(TAG, "seed staged at $staging")
    }

    private fun copyAssetDir(path: String, dst: File) {
        val children = assets.list(path) ?: return
        dst.mkdirs()
        for (child in children) {
            val childPath = "$path/$child"
            val grandChildren = assets.list(childPath)
            if (grandChildren != null && grandChildren.isNotEmpty()) {
                copyAssetDir(childPath, File(dst, child))
            } else {
                assets.open(childPath).use { input ->
                    File(dst, child).outputStream().use { input.copyTo(it) }
                }
            }
        }
    }

    companion object {
        private const val TAG = "SlgApp"
        lateinit var ready: Deferred<String>
            private set
        val appScope = kotlinx.coroutines.CoroutineScope(SupervisorJob() + Dispatchers.IO)
    }
}
