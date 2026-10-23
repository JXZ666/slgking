# 黄油之王 0.28.0 · Windows 源码

这是 Windows 0.28.0 正式修复包对应的客户端源码快照。内容包括从 0.27.0 以来的工作区、收藏夹、头像、键盘浏览、帮助、讨论消息及反馈修复，本次另补充《深渊回响》联动兑换及页面滚动修复。联动头衔为史诗样式，每个云端账号合计领取一次并获得100积分；Windows与Android共享领取记录。兑换码由游戏项目提供，不包含在客户端源码中。

## 运行和构建

在 Windows 中安装带 Tcl/Tk 的 Python 3.13；创建虚拟环境后执行：

```powershell
python -m pip install -r requirements-build.txt
python slg_main.py --version
python slg_main.py
```

正式包使用 PyInstaller 单文件、无控制台配置。运行 `build_exe.bat` 输出 `dist/slgking.exe`；`build_exe.bat test` 生成测试渠道包。`requirements.txt`、批处理、spec、Python模块和种子均与本次冻结的构建提交保持原字节；`requirements-build.txt` 是准备本快照时核对的参考环境，不保证不同机器重建得到相同 EXE 哈希。

源码启动会建立应用自己的用户资料目录。已有用户应先备份，自动验证使用独立临时资料；不要把私人数据库、API Key、登录凭证或个人封面提交进仓库。仅构建不会执行游戏或扫描用户磁盘。

## 内容与数据边界

- 目录资料由服务器公开快照提供；本机评分、收藏、路径和私人记录独立保存在用户数据目录，不因登录自动跨端同步。
- 本机头像免费、按身份保存且不上传；JSON 备份不包含头像图片，应另外备份。
- 原评论下的讨论、账号消息和隐私设置需要配套服务；本源码树只包含 Windows 客户端，不是服务器部署包。
- 已附客户端实际使用的公开目录种子及封面，个人记录表为空；它们是构建输入，不是当前服务器收录数量或采集恢复的证明。
- 官方资料及封面归原权利人所有；项目不提供游戏安装文件。项目许可证见 [LICENSE](LICENSE)。

## 验证与来源

[PUBLIC_SOURCE.json](PUBLIC_SOURCE.json) 记录正式 EXE 的构建提交、大小、哈希和种子隐私检查。[SOURCE_MANIFEST.json](SOURCE_MANIFEST.json) 列出每个原始输入的大小与 SHA-256。实际构建提交为 `dd5dc891810d275baadaf5828b59316e686e79bd`，EXE SHA-256为 `404a084f37a0639edcf952797e9dca767ce019dab7c0eb7011e317dc1b33c7ea`。本快照后续提交只更新发行说明与元数据，不改变已构建的运行时输入。

联动活动需要已部署的配套账号服务；同账号重复兑换或双端重试不会重复发奖。主动切换主页面和其他游戏时置顶，同页余额刷新保留位置；本机私人资料不因切页被清空。

`tests/` 保留一组不依赖生产账号的客户端回归及其夹具。本次定向验收可运行 `python -m unittest discover -s tests -p test_echoes_campaign_navigation.py -v`；测试会使用临时目录与本地模拟服务，不需要真实付费 API Key。GUI、实际 EXE 升级和系统 DPI 的既有验收不由这组纯测试替代。

正式下载及反馈：[GitHub Releases](https://github.com/JXZ666/slgking/releases) · [项目首页](https://github.com/JXZ666/slgking)。
