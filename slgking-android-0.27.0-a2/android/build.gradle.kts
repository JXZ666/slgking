plugins {
    id("com.android.application") version "8.7.3" apply false
    id("org.jetbrains.kotlin.android") version "2.0.21" apply false
    id("org.jetbrains.kotlin.plugin.compose") version "2.0.21" apply false
    // Chaquopy 12.0.1 起为 MIT 完全开源；此处使用 17.0.0（AGP 7.3–9.2 / Python 3.8–3.14 / minSdk 24+）
    id("com.chaquo.python") version "17.0.0" apply false
}
