# 黄油之王网页 0.27 源码

本包来自实际生产只读源码及公开页面资源，包括 /m/ 前端、官网和共享 HTTP 服务依赖。SOURCE_MANIFEST.json 逐文件列出哈希与导出来源。没有用户数据库、账号会话、服务器密钥、采集账本或生产运行配置。

前端无需构建。共享服务入口为 serve_catalog.py，可用 --port、--publish-dir、--data-dir 指定隔离环境。安装 requirements-server.txt 是管理员推送的可选依赖；基本服务只使用 Python 标准库。

此源码包不自动部署或启动生产采集；需要自行提供公开目录快照、封面和隔离运行目录才能展示游戏数据。采集 worker 子目录保留依赖源码，但没有来源运行账本，切勿直接对外站运行。

slg_titles 中 slg_account 是有 ImportError 兜底的桌面专用可选导入，不是服务器必需项。签名、推送密钥和用户私有资料不包含在附件中。

公开网站：https://slg-king.com/m/。许可证见 LICENSE。
