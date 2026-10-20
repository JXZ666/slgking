"""Bounded, read-only local identification evidence; never execute game files."""
import ast
import configparser
import json
import os
import re
from pathlib import Path

from slg_scan_identity import product_ids

MAX_FILE_BYTES = 256 * 1024
MAX_FILES = 8
_VNDB = re.compile(r"https?://(?:www\.)?vndb\.org/(v[1-9][0-9]*)(?=[/?#\s\"']|$)", re.I)
_NAME = re.compile(r"^\s*(?:define\s+)?config\.name\s*=\s*(?:_\(\s*)?((?:\"(?:[^\"\\]|\\.)*\")|(?:'(?:[^'\\]|\\.)*'))", re.M)
_GENERIC = {"game", "unity player", "unityplayer", "ue4", "ue5", "unreal engine", "nw", "nw.js", "renpy", "ren'py", "rpg maker", "application", "game launcher"}
_HELPER = re.compile(r"^(?:opensavefolder|resetconfig|uninstall(?:er)?|install(?:er)?|setup|patch(?:er)?|update(?:r)?|安装(?:程序|器)?|卸载(?:程序|器)?|补丁(?:安装程序|程序|工具)?)(?:[ _.-]*[0-9.]+)?$", re.I)


def auxiliary_title(value):
    """Only known helper labels, never arbitrary game titles containing 'patch'."""
    return isinstance(value, str) and bool(_HELPER.fullmatch(value.strip()))


def _alice_values(data):
    """Decode each literal INI value independently; legacy files mix encodings."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff", b"\xef\xbb\xbf")):
        lines = _text(data).splitlines()
        pairs = [(line, "unicode") for line in lines]
    else:
        pairs = []
        for raw in data.splitlines():
            if b"=" not in raw:
                continue
            key, value = raw.split(b"=", 1)
            try:
                key = key.decode("ascii").strip().casefold()
            except UnicodeError:
                continue
            if key not in ("gamename", "bootname", "codename"):
                continue
            decoded = []
            for encoding in ("utf-8", "gb18030", "cp932"):
                try:
                    item = value.decode(encoding).strip().strip('"')
                except UnicodeError:
                    continue
                if item and item not in [v[0] for v in decoded]:
                    decoded.append((item, encoding))
                if encoding == "utf-8" and decoded:
                    break
            for value, encoding in decoded:
                yield key, value, encoding, len(decoded) > 1
        return
    for line, encoding in pairs:
        if "=" in line:
            key, value = line.split("=", 1)
            yield key.strip().casefold(), value.strip().strip('"'), encoding, False


def _text(data):
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    for encoding in ("utf-8-sig", "cp932", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeError:
            pass
    raise ValueError("无法解码文本")


def windows_executable_titles(path):
    """Read resource version information, with no loading/execution of the EXE."""
    if os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes
    version = ctypes.WinDLL("version", use_last_error=True)
    size_fn = version.GetFileVersionInfoSizeW
    size_fn.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    size_fn.restype = wintypes.DWORD
    info_fn = version.GetFileVersionInfoW
    info_fn.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID]
    info_fn.restype = wintypes.BOOL
    query = version.VerQueryValueW
    query.argtypes = [wintypes.LPCVOID, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT)]
    query.restype = wintypes.BOOL
    ignored = wintypes.DWORD()
    size = size_fn(str(path), ctypes.byref(ignored))
    if not size:
        return []
    if size > MAX_FILE_BYTES:
        raise ValueError("版本信息超过读取上限")
    buf = ctypes.create_string_buffer(size)
    if not info_fn(str(path), 0, size, buf):
        raise OSError("无法读取启动文件版本信息")
    ptr, length = ctypes.c_void_p(), wintypes.UINT()
    translations = []
    if query(buf, "\\VarFileInfo\\Translation", ctypes.byref(ptr), ctypes.byref(length)):
        words = ctypes.cast(ptr, ctypes.POINTER(wintypes.WORD))
        translations = [(words[i], words[i + 1]) for i in range(0, min(length.value // 2, 16) - 1, 2)]
    translations = translations or [(0x0409, 0x04B0)]
    found = []
    for language, codepage in translations:
        for field in ("ProductName", "FileDescription"):
            key = f"\\StringFileInfo\\{language:04x}{codepage:04x}\\{field}"
            if query(buf, key, ctypes.byref(ptr), ctypes.byref(length)) and length.value:
                found.append((ctypes.wstring_at(ptr, min(length.value - 1, 512)), field))
    return found


def extract_evidence(folder, executables=(), should_stop=None, *, enumerate_urls=True):
    """Return identities/titles with relative evidence paths and recoverable warnings.

    At most eight metadata files/resources are read. Directory enumeration is only
    at the game root and bounded to 256 entries; symlinks/junctions are not followed.
    The caller must still require confirmation when evidence conflicts.
    """
    result = {"identities": [], "titles": [], "warnings": []}
    stop = should_stop or (lambda: False)
    root = Path(os.path.abspath(os.fspath(folder)))
    seen_ids, seen_titles = set(), set()
    count = 0

    def cancelled():
        if stop():
            if "识别已取消" not in result["warnings"]:
                result["warnings"].append("识别已取消")
            return True
        return False

    def identity(source, external_id, reason, relative):
        key = (source, external_id, relative)
        if key not in seen_ids:
            seen_ids.add(key)
            result["identities"].append(dict(source=source, external_id=external_id, reason=reason, path=relative))

    def title(value, reason, relative, **details):
        if not isinstance(value, str):
            return
        value = re.sub(r"\s+", " ", value).strip()
        if not value or len(value) > 512 or value.casefold() in _GENERIC or re.search(r"[\x00-\x1f\x7f]", value) or auxiliary_title(value):
            return
        key = (value.casefold(), relative)
        if key not in seen_titles:
            seen_titles.add(key)
            result["titles"].append(dict(text=value, reason=reason, path=relative, **details))

    def safe(path):
        try:
            relative = path.relative_to(root)
            for part in (root, *[root.joinpath(*relative.parts[:i]) for i in range(1, len(relative.parts) + 1)]):
                if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
                    raise ValueError("跳过链接路径")
            return relative.as_posix()
        except ValueError:
            raise ValueError("跳过链接或游戏目录外的路径") from None

    def read(path, raw=False):
        nonlocal count
        if cancelled() or count >= MAX_FILES:
            return None
        count += 1
        relative = safe(path)
        with path.open("rb") as stream:
            data = stream.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise ValueError("文件超过256 KiB读取上限")
        return (data if raw else _text(data)), relative

    if cancelled():
        return result
    for external_id in product_ids(root.name):
        identity("dlsite", external_id, "folder_product_id", ".")
    targets = [("game/options.rpy", "renpy"), ("data/System.json", "rpgmaker"),
               ("www/data/System.json", "rpgmaker"), ("Game.ini", "ini"),
               ("AliceStart.ini", "alice"), ("steam_appid.txt", "steam")]
    for relative, kind in targets:
        if cancelled():
            break
        path = root / relative
        try:
            if not path.exists():
                continue
            loaded = read(path, raw=kind in ("alice", "ini"))
            if loaded is None:
                continue
            text, relative = loaded
            if kind == "renpy":
                for match in _NAME.finditer(text):
                    title(ast.literal_eval(match.group(1)), "renpy_config_name", relative)
            elif kind == "rpgmaker":
                value = json.loads(text)
                if isinstance(value, dict):
                    title(value.get("gameTitle"), "rpgmaker_game_title", relative)
            elif kind == "ini":
                decoded = []
                encodings = ("utf-16",) if text.startswith((b"\xff\xfe", b"\xfe\xff")) else ("utf-8-sig", "cp932", "gb18030")
                for encoding in encodings:
                    try:
                        parser = configparser.ConfigParser(interpolation=None, strict=False)
                        parser.read_string(text.decode(encoding))
                        for section in parser.sections():
                            if section.casefold() == "game":
                                value = parser.get(section, "Title", fallback="")
                                if value and value not in [item[0] for item in decoded]:
                                    decoded.append((value, encoding))
                        if encoding == "utf-8-sig" and decoded:
                            break
                    except (UnicodeError, configparser.Error):
                        continue
                for value, encoding in decoded:
                    title(value, "game_ini_title", relative, encoding=encoding, ambiguous=len(decoded) > 1)
                if len(decoded) > 1:
                    result["warnings"].append("Game.ini: 名称存在多种编码解释，请确认")
            elif kind == "alice":
                ambiguous = False
                for key, value, encoding, uncertain in _alice_values(text):
                    if key == "gamename":
                        title(value, "alice_game_name", relative, encoding=encoding, ambiguous=uncertain)
                        ambiguous |= uncertain
                    elif key in ("bootname", "codename"):
                        suffix = Path(value).suffix.lower()
                        if suffix in (".exe", ".ain"):
                            value = value[:-len(suffix)]
                        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value) and ".." not in value:
                            title(value, "alice_" + key, relative, encoding=encoding, ambiguous=False)
                if ambiguous:
                    result["warnings"].append("AliceStart.ini: 名称存在多种编码解释，请确认；不覆盖目录名称")
            elif re.fullmatch(r"[1-9][0-9]{0,9}", text.strip()):
                identity("steam", text.strip(), "steam_appid", relative)
        except (OSError, ValueError, SyntaxError, configparser.Error) as exc:
            result["warnings"].append(f"{relative}: {type(exc).__name__}: {exc}")
    if enumerate_urls and not cancelled():
        try:
            safe(root)
            with os.scandir(root) as entries:
                urls = []
                for index, entry in enumerate(entries):
                    if cancelled():
                        break
                    if index >= 256:
                        result["warnings"].append("根目录枚举达到256项上限")
                        break
                    if entry.name.lower().endswith(".url"):
                        urls.append(root / entry.name)
                for path in sorted(urls):
                    if cancelled() or count >= MAX_FILES:
                        break
                    try:
                        loaded = read(path)
                        if loaded:
                            text, relative = loaded
                            for line in text.splitlines():
                                if line.strip().lower().startswith("url="):
                                    match = _VNDB.match(line.strip()[4:].strip())
                                    if match:
                                        identity("vndb", match.group(1).lower(), "official_url", relative)
                    except (OSError, ValueError) as exc:
                        result["warnings"].append(f"{path.name}: {type(exc).__name__}: {exc}")
        except (OSError, ValueError) as exc:
            result["warnings"].append(f"目录读取失败: {type(exc).__name__}: {exc}")
    for executable in executables:
        if cancelled():
            break
        path = Path(executable)
        if not path.is_absolute():
            path = root / path
        try:
            relative = safe(path)
            for external_id in product_ids(path.stem):
                identity("dlsite", external_id, "executable_product_id", relative)
            if auxiliary_title(path.stem):
                continue
            if count >= MAX_FILES or not path.is_file():
                continue
            count += 1
            for value, field in windows_executable_titles(path):
                title(value, f"exe_{field}", relative)
        except (OSError, ValueError) as exc:
            result["warnings"].append(f"启动文件: {type(exc).__name__}: {exc}")
    return result
