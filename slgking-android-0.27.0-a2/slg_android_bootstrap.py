"""slgking 安卓端运行时引导。

职责（在导入任何上游 slg_* 核心模块之前完成）：
  1. 把上游期望的数据目录环境变量 LOCALAPPDATA 指到安卓应用私有目录，
     使 slg_db.app_dir() 落在 <filesDir>/slgking（零改动上游）。
  2. 预置 ctypes.wintypes 兜底（非 Windows 平台导入失败时提供最小替身，
     slg_account 在模块级 from ctypes import wintypes）。
  3. 用安卓 Keystore 实现替换 slg_account._dpapi（经 Chaquopy Java 互操作，
     以 base64 字符串过桥）；桌面冒烟时可用 "stub" 明文实现。
  4. 首启安装种子库（db + 封面）并导入标签翻译种子（均为幂等操作）。

桌面冒烟用法（不触碰真实用户数据、不发网络请求）：
  init(files_dir=<临时目录>, asset_root=<仓库 assets>, crypto="stub")

本文件属于安卓移植新增代码（MIT，见 android/LICENSE），不属于上游；
上游 slg_*.py 保持逐字节不变。
"""

import base64
import json
import os
import sys
import types

_state = {
    "initialized": False,
    "files_dir": None,
    "asset_root": None,
    "seed_installed": False,
    "tag_seed_imported": 0,
}


def _ensure_wintypes_shim():
    """slg_account 顶层 import wintypes；非 Windows 下若失败则注入替身。"""
    try:
        from ctypes import wintypes  # noqa: F401
        return
    except Exception:
        pass
    import ctypes

    shim = types.ModuleType("ctypes.wintypes")
    shim.BOOL = ctypes.c_int
    shim.BYTE = ctypes.c_ubyte
    shim.WORD = ctypes.c_ushort
    shim.DWORD = ctypes.c_ulong
    shim.QWORD = ctypes.c_ulonglong
    shim.HANDLE = ctypes.c_void_p
    shim.LPVOID = ctypes.c_void_p
    shim.LPCSTR = ctypes.c_char_p
    shim.LPSTR = ctypes.c_char_p
    shim.UINT = ctypes.c_uint
    shim.ULONG = ctypes.c_ulong
    shim.LONG = ctypes.c_long
    shim.INT = ctypes.c_int
    sys.modules["ctypes.wintypes"] = shim


def _load_platform_crypto():
    """返回 (protect_b64, unprotect_b64) 两个 str->str 函数；无 Java 环境则 None。"""
    try:
        from java import jclass  # Chaquopy 提供
    except ImportError:
        return None
    try:
        cls = jclass("com.jxz666.slgking.PlatformCrypto")
        return cls.protectB64, cls.unprotectB64
    except Exception:
        return None


def _install_dpapi_replacement(crypto):
    """把 slg_account._dpapi(data: bytes, protect: bool) -> bytes 换成平台实现。"""
    import slg_account

    if crypto == "java":
        bridge = _load_platform_crypto()
        if bridge is None:
            raise RuntimeError("java 加密后端不可用：仅安卓进程内可用")

        def _dpapi(data, protect):
            b64 = base64.b64encode(data).decode("ascii")
            out = (bridge[0] if protect else bridge[1])(b64)
            return base64.b64decode(out)

    elif crypto == "stub":
        # 仅桌面冒烟：可逆明文实现，绝不用于真实设备。
        def _dpapi(data, protect):
            return data

    else:
        raise ValueError("未知 crypto 后端: %r" % crypto)

    slg_account._dpapi = _dpapi


def init(files_dir, asset_root=None, crypto="java"):
    """安卓进程内初始化。Kotlin 在 Python.start() 后、任何 API 调用前执行。

    files_dir   应用私有目录（Context.getFilesDir() 的绝对路径）
    asset_root  Kotlin 预先从 APK assets 复制出的种子目录（含 seed/ 与
                tag_zh.json）；桌面冒烟直接传仓库 assets/ 路径
    crypto      "java"（安卓 Keystore）或 "stub"（桌面冒烟）
    """
    _ensure_wintypes_shim()
    os.environ["LOCALAPPDATA"] = str(files_dir)

    import slg_db

    if crypto != "none":
        _install_dpapi_replacement(crypto)

    seed_installed = False
    if asset_root and not os.path.exists(slg_db.db_path()):
        db_src = os.path.join(str(asset_root), "seed", "slgking.db")
        covers_src = os.path.join(str(asset_root), "seed", "covers")
        if os.path.exists(db_src):
            slg_db.install_seed_db(
                db_src, covers_src if os.path.isdir(covers_src) else None)
            seed_installed = True

    tag_seed_imported = 0
    dlsite_imported = None
    dlsite_root = os.path.join(str(asset_root or ""), "dlsite")
    if os.path.isfile(os.path.join(dlsite_root, "manifest.json")):
        import slg_sync_server
        with slg_db.session() as conn:
            dlsite_imported = slg_sync_server.import_local_catalogue(conn,
                os.path.join(dlsite_root, "slgking.db"), os.path.join(dlsite_root, "manifest.json"))
            dlsite_labels = os.path.join(dlsite_root, "tag_zh.json")
            if os.path.isfile(dlsite_labels):
                slg_db.import_seed_tag_translations(conn, dlsite_labels)
    tag_seed = os.path.join(str(asset_root or ""), "tag_zh.json")
    if os.path.exists(tag_seed):
        conn = slg_db.connect()
        try:
            tag_seed_imported = slg_db.import_seed_tag_translations(conn, tag_seed)
        finally:
            conn.close()

    _state.update(initialized=True, files_dir=str(files_dir),
                  asset_root=str(asset_root) if asset_root else None,
                  seed_installed=seed_installed, tag_seed_imported=tag_seed_imported)
    return {
        "ok": True,
        "db_path": slg_db.db_path(),
        "app_dir": slg_db.app_dir(),
        "seed_installed": seed_installed,
        "tag_seed_imported": tag_seed_imported,
        "dlsite_imported": dlsite_imported,
    }


def status():
    """当前引导状态（供诊断/冒烟）。"""
    out = dict(_state)
    if out["initialized"]:
        import slg_db
        out["app_dir"] = slg_db.app_dir()
        out["db_exists"] = os.path.exists(slg_db.db_path())
    return out
