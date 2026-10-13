# 黄油之王 Windows 0.27.0 源码

本包包含正式 EXE 的业务源码、构建资源、公共种子和回归测试。

构建来源：0ea10aaa3be14f1b1381a02f179cebd61b7d3403；冻结来源：769c126278652eded98e8403cf63aaabd34747e9（之后只有交付文档）。35 个关键模块已经与正式 EXE 离线比对一致。

安装 Python 3.13 与 requirements.txt 中的依赖，然后运行 build_exe.bat。成品为 dist/slgking.exe。运行 python slg_main.py 启动源码版；用户资料位于用户数据目录，不包含在本包。

网页和 Android 使用各自独立源码附件，不能覆盖合并其共享模块来复现此 EXE。许可证见 LICENSE。
