# Chaquopy runtime
-keep class com.chaquo.python.** { *; }

# Python 互操作桥（jclass 访问的静态方法）
-keep class com.jxz666.slgking.PlatformCrypto { *; }

# Compose
-dontwarn androidx.compose.**

# Coil
-dontwarn coil.**
