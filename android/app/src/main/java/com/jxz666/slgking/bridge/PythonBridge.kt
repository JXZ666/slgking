package com.jxz666.slgking.bridge

import com.chaquo.python.Python
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.coroutines.CancellationException
import org.json.JSONObject

/**
 * Kotlin↔Python 进程内 JSON 桥的唯一通道。
 * 契约见 slg_android_api.call：成功 {"ok":true,...}，失败 {"ok":false,"error":...}。
 */
object PythonBridge {

    /** 阻塞版（必须在 IO 线程调用）。 */
    fun callBlocking(method: String, params: JSONObject = JSONObject()): JSONObject {
        val py = Python.getInstance()
        val api = py.getModule("slg_android_api")
        val payload = JSONObject(params.toString()).put("method", method).toString()
        return JSONObject(api.callAttr("call", payload).toString())
    }

    /** 挂起版：桥层或运行时异常统一转为 ok:false，绝不向 UI 抛。 */
    suspend fun call(method: String, params: JSONObject = JSONObject()): JSONObject =
        withContext(Dispatchers.IO) {
            try {
                callBlocking(method, params)
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                JSONObject().put("ok", false).put("error", "${e.javaClass.simpleName}: ${e.message}")
            }
        }

    inline fun build(block: JSONObject.() -> Unit): JSONObject =
        JSONObject().apply(block)
}
