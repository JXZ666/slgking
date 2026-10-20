# 黄油之王 Android 0.28.0

Android 正式版独立源码。应用名黄油之王，包名 `com.jxz666.slgking`，versionName `0.28.0`，versionCode `13`，Android 7.0+，ABI `arm64-v8a` / `x86_64`。Android 移植由 vegetable dog（工程署名 zeratulzhou）贡献。

## 构建

需要 JDK 17、Android SDK / build-tools 35、Python 3.13。工程使用 Gradle Wrapper 8.11.1、AGP 8.7.3、Kotlin 2.0.21、Chaquopy 17、Pillow 11.0.0。设置 `JAVA_HOME`、`ANDROID_HOME` 后进入 `android/`：

```powershell
./gradlew.bat -PslgBuildPython=python :app:testReleaseUnitTest :app:assembleDebug
```

正式构建用 `:app:clean :app:testReleaseUnitTest :app:lintVitalRelease :app:assembleRelease`，须提供自己的签名配置。可复制 `android/keystore.properties.example` 为本机 `android/keystore.properties` 后填写，或使用 `-PslgSigningProperties=<本机配置绝对路径>` 引用。缺失签名时构建失败。官方私钥与密码不随源码分发；自签名包不能覆盖官方包。

构建任务从根目录同步 `slg_*.py` 与 `assets/seed`、`assets/dlsite`、`assets/tag_zh.json`。`android/app/src/main/python` 和 `assets` 是生成目录，不必手动复制。Windows 与网页源码独立，勿覆盖此源码中的共享模块。

## 本版功能

- 个人页先展示本机状态框架，摘要与资源按需加载，身份变更后拒绝迟到响应。
- 本机免费头像选图、裁切与恢复；公开名片按服务器确认的隐私状态展示。头像不上云，也不包含在 JSON 备份。
- 详情原文/中文切换，标题与简介逐字段翻译；人工译文和标签可在本机编辑，简介、备注、标签独立展开。
- 近7/30/90天及自定义日期与其它筛选组合；隐藏与恢复、收藏成员筛选和移出当前收藏夹。
- 按需追评和消息中心，保留输入与审核状态；无后台轮询。
- 同步展示真实来源、阶段和数量，可停止并保留已提交目录。
- 保留 AI 公司/模型预置、追踪三区和个人记录；支持 RJ/VJ 目录身份及官方封面索引。官方封面由设备按需直连，失败回退旧封面。VJ 实际可用数据取决于服务器目录发布，本包不保证来源已完成采集。

## 升级与资料

正常同证书覆盖升级保留本机数据。早期本地 `0.28.0 / code11` 与本版同名，需从发行页手动下载新版覆盖；`0.28.0-a2 / code12` 可升至本版。不要卸载或清除资料来升级，先导出备份。

评分、收藏、备注、自建记录和翻译设置属于应用私有存储，登录不会自动与 PC 同步；可通过 JSON 备份迁移部分资料。备份恢复可能替换个人表，先保留原备份。PC 的扫描、启动、存档管理为平台专属功能。

## 验证与许可

离线桥：`python tools/smoke_android_api.py`；Android 契约：`python -m unittest discover -s tests -p "test_android*.py"`；JVM：`:app:testReleaseUnitTest`。测试使用隔离数据库和网络 mock，不使用真实账号或付费 Key。具体本次结果及设备未覆盖项见 `QA-0.28.0.md`。

Python 核心 GPL-3.0（根 `LICENSE`）；Android 移植 MIT（`android/LICENSE` 与 `android/NOTICE`）。`SOURCE_MANIFEST.json` 记录实际公开源码输入，GitHub 标签自动源码不替代该独立 Android 源码。
