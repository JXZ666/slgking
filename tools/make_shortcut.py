"""Puts a shortcut to slgking.exe on the Desktop.

    python tools\\make_shortcut.py

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

EXE = os.path.join(ROOT, "dist", "slgking.exe")
NAME = "SLG黄游之王.lnk"
DESC = "SLG黄游之王 - dikgames 游戏库管理"

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


def main():
    if not os.path.isfile(EXE):
        print("还没打包：%s\n先跑 build_exe.bat。" % EXE)
        return 1

    desktop = os.path.join(slg_db.home_dir(), "Desktop")
    if not os.path.isdir(desktop):
        print("找不到桌面目录：%s" % desktop)
        return 1

    link = os.path.join(desktop, NAME)
    env = dict(os.environ)
    env.update({
        "SLGK_LINK": link,
        "SLGK_TARGET": EXE,
        "SLGK_WORKDIR": os.path.dirname(EXE),
        "SLGK_DESC": DESC,
    })

    proc = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-Command", PS],
        env=env, capture_output=True, text=True, encoding="utf-8",
        errors="replace")
    if proc.returncode != 0:
        print("创建失败：\n%s%s" % (proc.stdout, proc.stderr))
        return 1

    if not os.path.isfile(link):
        print("PowerShell 说成功了，但文件不在：%s" % link)
        return 1

    print("已创建：%s" % link)
    print("指向：%s" % EXE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
