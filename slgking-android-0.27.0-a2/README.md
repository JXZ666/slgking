# 黄油之王 Android 0.27.0-a2 源码

这是官方 Android 测试版的独立源码，来源提交 `0f7a8ba3db81dcc871672701b582058eaf8b4f1f`。
应用名：黄油之王；包名 `com.jxz666.slgking`；versionName `0.27.0-a2`；versionCode `10`；minSdk 24 / targetSdk 35；ABI `arm64-v8a`、`x86_64`。
Windows 与网页源码另附，GitHub 发行标签的自动源码不替代本包。

## 构建

需要 JDK 17、Android SDK/build-tools 35、Python 3.13；Gradle Wrapper 8.11.1、AGP 8.7.3、Kotlin 2.0.21、Chaquopy 17 与 Pillow 11.0.0。
设置 JAVA_HOME、ANDROID_HOME，进入 android/：

```powershell
./gradlew.bat -PslgBuildPython=python :app:testReleaseUnitTest :app:assembleDebug
```

发行构建使用 `:app:assembleRelease`。必须提供自己的签名配置（见 `android/keystore.properties.example`），复制为本机 `android/keystore.properties`，或通过 SLGKING_KEYSTORE / SLGKING_STORE_PASSWORD / SLGKING_KEY_ALIAS / SLGKING_KEY_PASSWORD 环境变量提供。示例只含占位值；官方私钥不随源码分发。自签名包不能直接覆盖官方包，请先备份个人资料。

`syncUpstreamPython` 从根目录同步全部 slg_*.py；`syncSeedAssets` 同步 assets/seed、assets/dlsite 与 assets/tag_zh.json。本包保留完整输入；不要将其他端的 Python 文件覆盖本包。生成的 android/app/src/main/python 与 assets 目录无需手动维护。

## 功能与资料

AI 翻译新增公司／模型预置和高级接口设置，旧设置保留，换服务商或接口须填新 Key。Key 输入遮罩、不会从桥接口回传原文；目前保存于应用私有 SQLite，不宣称 Keystore 加密。
追踪分为版本更新、想玩、收藏夹，个人记录为次级入口。支持 SLG / DLsite / 全部目录、同步停止、个人分类与 JSON 备份迁移。账号共享云端业务，评分、收藏、备注、个人游戏与翻译设置属于本机资料，登录不会自动同步。
正常覆盖升级保留数据；卸载可能删除未备份资料。DLsite 种子151款不是实时全站目录，在线采集仍可能因来源拒绝暂停。

## 验证与许可

a1/code9 → a2/code10 在 Android 9/x86_64 设备覆盖升级验证通过；0.26 → a2 采用历史核心 SQLite 迁移验证，未做设备降级；未完成 arm64 真机验证。没有使用真实付费翻译 Key。
可运行 `python tools/smoke_android_api.py` 及 Android JVM 测试；离线升级用例依赖历史 Git 对象，本源码 ZIP 不含原 Git 历史。
上游 Python 核心 GPL-3.0（根 LICENSE）；Android 移植 MIT（android/LICENSE、NOTICE）。Android 由 vegetable dog（工程署名 zeratulzhou）贡献。
