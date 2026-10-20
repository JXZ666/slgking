package com.jxz666.slgking

import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey

/**
 * 云端设备令牌的 Android Keystore 加密后端。
 * Python 侧经 Chaquopy 调用（jclass + @JvmStatic），以 base64 字符串过桥，
 * 替代上游 Windows DPAPI（见 slg_android_bootstrap._install_dpapi_replacement）。
 */
object PlatformCrypto {

    private const val ANDROID_KEYSTORE = "AndroidKeyStore"
    private const val ALIAS = "slgking_device_token"
    private const val GCM_TAG_BITS = 128
    private const val GCM_IV_BYTES = 12

    @JvmStatic
    fun protectB64(plainB64: String): String {
        val plain = Base64.decode(plainB64, Base64.NO_WRAP)
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, key())
        val encrypted = cipher.doFinal(plain)
        // iv || ciphertext 一并输出，unprotect 时自取
        val out = ByteArray(GCM_IV_BYTES + encrypted.size)
        System.arraycopy(cipher.iv, 0, out, 0, GCM_IV_BYTES)
        System.arraycopy(encrypted, 0, out, GCM_IV_BYTES, encrypted.size)
        return Base64.encodeToString(out, Base64.NO_WRAP)
    }

    @JvmStatic
    fun unprotectB64(sealedB64: String): String {
        val sealed = Base64.decode(sealedB64, Base64.NO_WRAP)
        require(sealed.size > GCM_IV_BYTES) { "密文过短" }
        val iv = sealed.copyOfRange(0, GCM_IV_BYTES)
        val body = sealed.copyOfRange(GCM_IV_BYTES, sealed.size)
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.DECRYPT_MODE, key(), javax.crypto.spec.GCMParameterSpec(GCM_TAG_BITS, iv))
        val plain = cipher.doFinal(body)
        return Base64.encodeToString(plain, Base64.NO_WRAP)
    }

    private fun key(): SecretKey {
        val ks = KeyStore.getInstance(ANDROID_KEYSTORE).apply { load(null) }
        (ks.getKey(ALIAS, null) as? SecretKey)?.let { return it }
        val generator = KeyGenerator.getInstance(
            KeyProperties.KEY_ALGORITHM_AES, ANDROID_KEYSTORE)
        generator.init(
            KeyGenParameterSpec.Builder(
                ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .build())
        return generator.generateKey()
    }
}
