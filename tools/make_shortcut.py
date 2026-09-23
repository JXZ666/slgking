"""Puts Desktop shortcuts to the built exes on the Desktop.

    python tools\\make_shortcut.py                     # 全部（默认）
    python tools\\make_shortcut.py slgking_admin.exe   # 只建某几个

Two shortcuts exist because there are two exes with very different audiences:
the public slgking.exe, and slgking_admin.exe (作者本机运维工具，从不分发).
The admin exe was building fine but had no shortcut, so it was effectively
invisible - the whole reason this script learned about a second target.

Not a pure PowerShell script, because two things break on this machine and
both were measured, not guessed:

  * [Environment]::GetFolderPath('Desktop') AND the WScript.Shell
    SpecialFolders collection both return an empty string. CodePilot shadows
    the user profile, and the shell resolves the Desktop relative to it. The
    real path is HOMEDRIVE+HOMEPATH - the same fallback slg_db.home_dir()
    uses for the same reason.
  * A Chinese shortcut name inside a .ps1 depends on PowerShell 5.1 guessing
    the file encoding correctly, and it does not always. Passing the name
    through an environment variable sidesteps that: environment variables
    cross the process boundary as UTF-16, so there is nothing to guess.

So Python resolves every path and PowerShell only drives the COM object.
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import slg_db  # noqa: E402

DIST = os.path.join(ROOT, "dist")
MAIN_EXE = "slgking.exe"

# (exe 文件名, 快捷方式名, 说明)。第一个是主程序，它没打包就算整个脚本失败。
TARGETS = (
    (MAIN_EXE, "SLG黄游之王.lnk", "SLG黄游之王 - dikgames 游戏库管理"),
    ("slgking_admin.exe", "SLG黄游之王-管理工具.lnk",
     "SLG黄游之王 - 作者运维工具（仅本机使用，勿分发）"),
)

# Pure ASCII: every value it needs arrives through the environment.
PS = r"""
$ErrorActionPreference = 'Stop'
$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($env:SLGK_LINK)
$sc.TargetPath = $env:SLGK_TARGET
$sc.WorkingDirectory = $env:SLGK_WORKDIR
$sc.IconLocation = $env:SLGK_TARGET + ',0'
$sc.Description = $env:SLGK_DESC
$sc.Save()
Write-Output $env:SLGK_LINK
"""


def make_link(desktop, exe, name, desc):
    """一个快捷方式。成功返回 0，失败返回 1（并且已经打印了原因）。"""
    link = os.path.join(desktop, name)
    env = dict(os.environ)
    env.update({
        "SLGK_LINK": link,
        "SLGK_TARGET": exe,
        "SLGK_WORKDIR": os.path.dirname(exe),
        "SLGK_DESC": desc,
    })

    proc = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-Command", PS],
        env=env, capture_output=True, text=True, encoding="utf-8",
        errors="replace")
    if proc.returncode != 0:
        print("%s 创建失败：\n%s%s" % (name, proc.stdout, proc.stderr))
        return 1

    if not os.path.isfile(link):
        print("PowerShell 说成功了，但文件不在：%s" % link)
        return 1

    print("已创建：%s\n  指向：%s" % (link, exe))
    return 0


def main(argv):
    wanted = [os.path.basename(a).lower() for a in argv]
    targets = [t for t in TARGETS if not wanted or t[0].lower() in wanted]

    main_exe = os.path.join(DIST, MAIN_EXE)
    if not wanted and not os.path.isfile(main_exe):
        print("还没打包：%s\n先跑 build_exe.bat。" % main_exe)
        return 1

    desktop = os.path.join(slg_db.home_dir(), "Desktop")
    if not os.path.isdir(desktop):
        print("找不到桌面目录：%s" % desktop)
        return 1

    failed = 0
    for exe_name, name, desc in targets:
        exe = os.path.join(DIST, exe_name)
        if not os.path.isfile(exe):
            print("跳过（还没打包）：%s" % exe)
            continue
        failed += make_link(desktop, exe, name, desc)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
