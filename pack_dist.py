"""把 dist/CloudBox.exe 组装成可以直接发给别人的分发包。

分发包只包含这些内容：
  CloudBox.exe        单文件主程序（免安装 Python / 依赖）
  cookie.example.txt  Cookie 占位示例，由使用者自己填写
  使用说明.txt        给收件人的说明（存在才会带上）

**刻意不包含任何本机数据**：settings.json / cloudbox.db / music / music_api.log，
以及真实 cookie.txt —— 把登录态一起发出去，等于把账号交出去。

对方首次双击时，配置与 music 目录会在 exe 同级自动生成（默认极高音质、无偏好、空收藏）。
"""
from __future__ import annotations

import shutil
from pathlib import Path

BASE = Path(__file__).resolve().parent
EXE = BASE / "dist" / "CloudBox.exe"
TARGET = BASE / "release-package"
README = TARGET / "使用说明.txt"
COOKIE_EXAMPLE_SRC = BASE / "cookie.example.txt"

# 绝不能带进分发包的本机文件 / 目录
LOCAL_ONLY = [
    "settings.json",
    "cloudbox.db",
    "cloudbox.db-journal",
    "music_api.log",
    "cookie.txt",
    "music",
]

PLACEHOLDER = """# 把你自己的网易云 Cookie 写在这一行（不是别人的、也不是示例值）
# 格式：MUSIC_U=xxxx; __csrf=xxxx; NMTID=xxxx; os=pc; appver=8.9.70;
# 不填也能用：只能播放与下载公开音质（极高 / 标准），会员曲目无法解锁。
"""


def main() -> int:
    if not EXE.is_file():
        print(f"[打包] 找不到 {EXE}，请先运行 PyInstaller。")
        return 1

    TARGET.mkdir(parents=True, exist_ok=True)

    shutil.copy2(EXE, TARGET / "CloudBox.exe")
    print(f"[打包] 已复制 {EXE.name}")

    # 只放占位示例，绝不复制真实 Cookie
    if COOKIE_EXAMPLE_SRC.is_file():
        shutil.copy2(COOKIE_EXAMPLE_SRC, TARGET / "cookie.example.txt")
    else:
        (TARGET / "cookie.example.txt").write_text(PLACEHOLDER, encoding="utf-8")
    print("[打包] 已写入 cookie.example.txt（占位，不含任何真实凭据）")

    for name in LOCAL_ONLY:
        path = TARGET / name
        if path.is_dir():
            shutil.rmtree(path)
            print(f"[打包] 已移除本机数据目录 {name}")
        elif path.exists():
            path.unlink()
            print(f"[打包] 已移除本机数据文件 {name}")

    print(f"[打包] 分发包已更新：{TARGET}")
    for item in sorted(TARGET.iterdir()):
        if item.is_file():
            size = item.stat().st_size / 1024 / 1024
            print(f"        {item.name:<22} {size:6.1f} MB")
        else:
            print(f"        {item.name:<22}    <dir>")
    if not README.is_file():
        print("[打包] 提示：分发包缺少 使用说明.txt，记得补上。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
