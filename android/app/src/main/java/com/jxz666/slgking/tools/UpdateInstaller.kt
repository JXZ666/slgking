package com.jxz666.slgking.tools

import android.app.DownloadManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.provider.Settings
import androidx.core.content.FileProvider
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.File

/** Persist downloads across navigation/process death. Installation always requires a user action. */
object UpdateInstaller {
    data class Download(val id: Long, val url: String, val fileName: String,
                        val status: Int, val received: Long = 0, val total: Long = -1,
                        val error: String? = null)

    private fun prefs(context: Context) = context.getSharedPreferences("apk_update", Context.MODE_PRIVATE)
    private fun manager(context: Context) = context.getSystemService(Context.DOWNLOAD_SERVICE) as DownloadManager

    @Synchronized
    fun download(context: Context, url: String): Download {
        val uri = Uri.parse(url)
        require(uri.scheme == "https" && !uri.host.isNullOrBlank()) { "更新包地址无效" }
        current(context)?.let { old ->
            if (old.status in listOf(DownloadManager.STATUS_PENDING, DownloadManager.STATUS_RUNNING,
                    DownloadManager.STATUS_PAUSED)) {
                require(old.url == url) { "已有更新包正在下载，请等待完成" }
                return old
            }
            if (old.url == url && old.status == DownloadManager.STATUS_SUCCESSFUL) return old
            manager(context).remove(old.id)
            downloadedFile(context, old.fileName).delete()
        }
        val name = "slgking-update-${System.currentTimeMillis()}.apk"
        val request = DownloadManager.Request(uri)
            .setTitle("黄油之王 更新包").setDescription(name)
            .setDestinationInExternalFilesDir(context, Environment.DIRECTORY_DOWNLOADS, name)
            .setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
            .setMimeType("application/vnd.android.package-archive")
        val id = manager(context).enqueue(request)
        prefs(context).edit().putLong("id", id).putString("url", url).putString("filename", name).commit()
        return Download(id, url, name, DownloadManager.STATUS_PENDING)
    }

    fun current(context: Context): Download? {
        val pref = prefs(context)
        val id = pref.getLong("id", -1)
        if (id < 0) return null
        val url = pref.getString("url", "") ?: ""
        val name = pref.getString("filename", "") ?: ""
        manager(context).query(DownloadManager.Query().setFilterById(id)).use { cursor ->
            if (!cursor.moveToFirst()) return Download(id, url, name, DownloadManager.STATUS_FAILED,
                error = "下载记录已被移除，请重新下载")
            val status = cursor.getInt(cursor.getColumnIndexOrThrow(DownloadManager.COLUMN_STATUS))
            val reason = cursor.getInt(cursor.getColumnIndexOrThrow(DownloadManager.COLUMN_REASON))
            return Download(id, url, name, status,
                cursor.getLong(cursor.getColumnIndexOrThrow(DownloadManager.COLUMN_BYTES_DOWNLOADED_SO_FAR)),
                cursor.getLong(cursor.getColumnIndexOrThrow(DownloadManager.COLUMN_TOTAL_SIZE_BYTES)),
                if (status == DownloadManager.STATUS_FAILED) "下载失败（$reason），请重试" else null)
        }
    }

    @Synchronized
    fun discard(context: Context) {
        val pref = prefs(context)
        val id = pref.getLong("id", -1)
        if (id >= 0) manager(context).remove(id)
        pref.getString("filename", null)?.let { downloadedFile(context, it).delete() }
        pref.edit().clear().commit()
    }

    fun canInstall(context: Context): Boolean = Build.VERSION.SDK_INT < 26 ||
        context.packageManager.canRequestPackageInstalls()

    fun requestInstallPermission(context: Context) {
        if (Build.VERSION.SDK_INT >= 26) context.startActivity(
            Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:${context.packageName}"))
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
    }

    /** Validate the package and signing certificate before exposing it to the system installer. */
    @Suppress("DEPRECATION")
    fun validate(context: Context, fileName: String) {
        val file = downloadedFile(context, fileName)
        require(file.isFile && file.length() > 0) { "更新包不存在或为空，请重新下载" }
        val pm = context.packageManager
        val flags = if (Build.VERSION.SDK_INT >= 28) PackageManager.GET_SIGNING_CERTIFICATES else PackageManager.GET_SIGNATURES
        val candidate = pm.getPackageArchiveInfo(file.absolutePath, flags)
            ?: error("更新包损坏或不是有效 APK")
        val installed = pm.getPackageInfo(context.packageName, flags)
        require(candidate.packageName == context.packageName) { "更新包的应用标识不匹配" }
        val newCode = if (Build.VERSION.SDK_INT >= 28) candidate.longVersionCode else candidate.versionCode.toLong()
        val oldCode = if (Build.VERSION.SDK_INT >= 28) installed.longVersionCode else installed.versionCode.toLong()
        require(newCode > oldCode) { "更新包版本不高于当前版本" }
        val incoming = if (Build.VERSION.SDK_INT >= 28) candidate.signingInfo?.apkContentsSigners else candidate.signatures
        val active = if (Build.VERSION.SDK_INT >= 28) installed.signingInfo?.apkContentsSigners else installed.signatures
        require(!incoming.isNullOrEmpty() && !active.isNullOrEmpty() &&
            incoming.map { it.toCharsString() }.toSet() == active.map { it.toCharsString() }.toSet()) {
            "更新包签名不匹配，已阻止安装"
        }
    }

    suspend fun install(context: Context, fileName: String) {
        check(canInstall(context)) { "请先允许本应用安装更新包" }
        withContext(Dispatchers.IO) { validate(context, fileName) }
        val uri = FileProvider.getUriForFile(context, context.packageName + ".fileprovider",
            downloadedFile(context, fileName))
        context.startActivity(Intent(Intent.ACTION_VIEW).apply {
            setDataAndType(uri, "application/vnd.android.package-archive")
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
        })
    }

    fun downloadedFile(context: Context, fileName: String): File =
        File(context.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS), fileName)

    fun openReleasesPage(context: Context) {
        try {
            context.startActivity(Intent(Intent.ACTION_VIEW,
                Uri.parse("https://github.com/JXZ666/slgking/releases/latest"))
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        } catch (_: Exception) { }
    }
}
