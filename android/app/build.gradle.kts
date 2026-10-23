import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
    id("com.chaquo.python")
}

android {
    namespace = "com.jxz666.slgking"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.jxz666.slgking"
        minSdk = 24
        targetSdk = 35
        versionCode = 15
        versionName = "0.28.0"
        ndk {
            abiFilters += listOf("arm64-v8a", "x86_64")
        }
    }

    // 种子库与翻译种子从仓库 assets/ 构建期同步，避免在 android/ 里复制一份
    val seedSrc = file("../../assets")
    val assetDst = file("src/main/assets")
    val syncSeed = tasks.register<Sync>("syncSeedAssets") {
        from(seedSrc) {
            include("seed/**", "dlsite/**", "tag_zh.json")
        }
        into(assetDst)
    }
    tasks.named("preBuild") { dependsOn(syncSeed) }

    // 构建期从仓库根同步上游 .py（含安卓桥模块）到 src/main/python，
    // 避免 Chaquopy 扫描 .git/tests/tools/android 自身
    val syncPy = tasks.register<Sync>("syncUpstreamPython") {
        from(file("../../")) {
            include("slg_*.py")
        }
        into(file("src/main/python"))
    }
    tasks.named("preBuild") { dependsOn(syncPy) }
    // 同步任务写入的目录被 Chaquopy/assets 合并任务消费，显式声明依赖
    tasks.matching { it.name == "mergeDebugPythonSources" || it.name == "mergeReleasePythonSources" }
        .configureEach { dependsOn(syncPy) }
    tasks.matching { it.name == "mergeDebugAssets" || it.name == "mergeReleaseAssets" }
        .configureEach { dependsOn(syncSeed) }

    // 发布签名：凭据走 android/keystore.properties（gitignored）或环境变量；
    // release 签名缺失时失败，不交付未签名产物。
    val keystorePropsFile = rootProject.file(providers.gradleProperty("slgSigningProperties").getOrElse("keystore.properties"))
    val keystoreProps = Properties().apply {
        if (keystorePropsFile.exists()) keystorePropsFile.inputStream().use { load(it) }
    }
    val storeFilePath: String? =
        (keystoreProps.getProperty("storeFile") ?: System.getenv("SLGKING_KEYSTORE"))?.takeIf { it.isNotBlank() }
    val releaseStorePassword = keystoreProps.getProperty("storePassword") ?: System.getenv("SLGKING_STORE_PASSWORD")
    val releaseKeyAlias = keystoreProps.getProperty("keyAlias") ?: System.getenv("SLGKING_KEY_ALIAS")
    val releaseKeyPassword = keystoreProps.getProperty("keyPassword") ?: System.getenv("SLGKING_KEY_PASSWORD")
    val validateSigning = tasks.register("validateReleaseCredentials") {
        doLast {
            check(!storeFilePath.isNullOrBlank() && file(storeFilePath).isFile &&
                !releaseStorePassword.isNullOrBlank() && !releaseKeyAlias.isNullOrBlank() &&
                !releaseKeyPassword.isNullOrBlank()) {
                "Release signing credentials missing; configure local properties or SLGKING_* environment."
            }
        }
    }
    tasks.matching { it.name == "preReleaseBuild" || it.name == "validateSigningRelease" }
        .configureEach { dependsOn(validateSigning) }

    signingConfigs {
        create("release") {
            if (storeFilePath != null) {
                storeFile = file(storeFilePath)
                storePassword = releaseStorePassword
                keyAlias = releaseKeyAlias
                keyPassword = releaseKeyPassword
            }
        }
    }

    buildTypes {
        debug {
            isMinifyEnabled = false
        }
        release {
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
            if (storeFilePath != null) {
                signingConfig = signingConfigs.getByName("release")
            }
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
    buildFeatures {
        compose = true
        buildConfig = true
    }
    packaging {
        resources.excludes += "META-INF/**"
        jniLibs.useLegacyPackaging = false
    }
}

chaquopy {
    defaultConfig {
        version = "3.13"
        pip { install("Pillow==11.0.0") }
        // buildPython 指向你本机的 Python 3.13（需与目标机同大版本），按需修改
        buildPython(providers.gradleProperty("slgBuildPython").orElse(
            providers.environmentVariable("SLGKING_BUILD_PYTHON")).getOrElse("python"))
    }
    // Python 源来自默认 src/main/python（构建期由 syncUpstreamPython 从仓库根同步）
}

dependencies {
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.json:json:20240303")
    implementation(platform("androidx.compose:compose-bom:2024.10.01"))
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.material:material-icons-extended")
    implementation("androidx.activity:activity-compose:1.9.3")
    implementation("androidx.navigation:navigation-compose:2.8.4")
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.8.7")
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.8.7")
    implementation("io.coil-kt:coil-compose:2.7.0")
    debugImplementation("androidx.compose.ui:ui-tooling")
}
