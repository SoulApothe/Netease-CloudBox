# 云音盒子 · CloudBox

<div align="center">

**网易云音乐桌面版 · 本地运行 · 源码公开**

搜索推荐 · 在线播放 · 收藏与自建歌单 · 无损下载 · 完整元数据

![License](https://img.shields.io/badge/license-MIT-blue.svg)
![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)
![Platform](https://img.shields.io/badge/platform-Windows%2010%20%2F%2011-lightgrey.svg)
![Version](https://img.shields.io/badge/version-3.5.0-green.svg)

[下载](#-下载) · [快速开始](#-快速开始) · [隐私与安全](#-隐私与安全) · [许可证](#-许可证) · [问题反馈](https://github.com/SoulApothe/Netease-CloudBox/issues)

</div>

---

## 📌 来源与开源声明

本项目**基于开源项目 [Suxiaoqinx/Netease_url](https://github.com/Suxiaoqinx/Netease_url)（MIT License，Copyright © 2024 Suxiaoqinx）二次开发**，在其网易云音乐解析内核之上，重写为可在 Windows 上直接运行的桌面应用，并新增在线播放、动态推荐、本机档案、批量下载与元数据写入等能力。

上游协议的解析思路、接口封装与早期实现来自原作者，**版权归原作者所有**；本项目仅对新增与改造部分主张权利，并同样以 **MIT License** 开源。衍生关系已在 [NOTICE](./NOTICE) 中写明，原始版权声明保留在 [LICENSE](./LICENSE) 中。

> **本仓库源码完全公开，接受大众监督。**
> 如果你发现隐私风险、安全缺陷、异常网络请求或任何不当行为，请在 [Issues](https://github.com/SoulApothe/Netease-CloudBox/issues) 提出，或按 [SECURITY.md](./SECURITY.md) 私下报告。所有反馈都会被认真处理，核实后及时修复并在 Release Notes 中说明。

---

## ✨ 功能

| 模块 | 能力 |
| --- | --- |
| 🎧 推荐 | 公开榜单 / 新歌 / 新碟聚合；「换一批」会轮换榜单窗口与检索页；按本机点击权重动态重排 |
| ▶️ 播放 | 底部常驻播放器，支持进度拖动（Range / 206）、顺序 / 单曲循环；会员 Cookie 优先解析高音质直链，失败自动降级 |
| 🔍 搜索 | 单曲、歌手、专辑、歌单站内搜索，结果可直接播放、收藏、下载 |
| ⬇️ 下载 | 单曲 / 整张专辑 / 整个歌单批量下载，多线程并发、逐曲进度、可取消、跳过已存在 |
| 🏷️ 元数据 | 标题、歌手、专辑、专辑艺术家、音轨号、碟号、年份、歌词、封面、网易云 ID；MP3 / FLAC / M4A / MP4 / Ogg 全覆盖，纯 Python 实现，**不需要 ffmpeg** |
| 🗂️ 命名 | 简单模式（歌曲 - 歌手）与模板模式（`{artist} - {name}` 等占位符），实时预览 |
| 👤 本机档案 | 本地 SQLite 免注册档案：收藏歌曲、收藏歌手、本地收藏、自建歌单 |
| 🔑 登录 | 窗口内扫码登录或手动粘贴 Cookie，登录态实时显示 |
| 🎨 界面 | 完全自包含的桌面 UI，浅色 / 深色主题，零 CDN 依赖，离线可用 |

---

## 📥 下载

前往 **[Releases](https://github.com/SoulApothe/Netease-CloudBox/releases)** 下载最新 `CloudBox.exe`：

- Windows 10 / 11，单文件、免安装，**不需要 Python 或任何依赖**
- 双击即用，原生窗口（WebView2）
- 首次启动会在 exe 同级目录生成 `settings.json`、`cloudbox.db` 与 `music` 文件夹
- 安装包内**不含任何 Cookie 与本机数据**，是一个干净的空白包

> exe 只作为 Release 附件提供，不放进源码仓库，避免仓库膨胀。想核实构建来源，可以按下方「方式三」用同一份源码自行打包，或用 `SECURITY.md` 里的方法检查可执行文件内的外链域名。

> ⚠️ exe 未做代码签名，首次运行 Windows SmartScreen 可能提示「未知发布者」。源码在本仓库完全公开，你可以核对后选择「仍要运行」，或直接按下方步骤从源码运行。

### 版本与更新

| Release | 说明 |
| --- | --- |
| [v3.5.0-r2](https://github.com/SoulApothe/Netease-CloudBox/releases/tag/v3.5.0-r2) | **最新**：第二版 exe。新增桌面歌词悬浮窗、均衡器、推荐页批量下载、歌单导入命名；修复设置页勾选命名项导致整个界面位移、多选操作条遮挡列表等问题。 |
| [v3.5.0](https://github.com/SoulApothe/Netease-CloudBox/releases/tag/v3.5.0) | 首个开源版本。 |

完整的逐项更新内容见 [CHANGELOG.md](./CHANGELOG.md) 与对应 Release 的说明。

> GitHub 上发布的 exe **始终不含任何 Cookie / 登录凭证**，首次运行不会生成 `cookie.txt`；请在「设置 → 下载凭证」扫码登录或粘贴自己的 Cookie。
> 程序内版本标识仍为 `3.5.0`，`r2` 仅用于区分构建批次。本仓库 `main` 分支源码仍是 v3.5.0 首个发布时的快照，上述新增功能尚未同步，如需核对实现请等候源码同步。

---

## 🚀 快速开始

### 方式一：直接运行 exe

1. 从 [Releases](https://github.com/SoulApothe/Netease-CloudBox/releases) 下载 `CloudBox.exe`，放到一个空文件夹；
2. 双击运行。首次启动自动创建配置与 `music` 目录；
3. 想下载会员曲目 / 无损音质时，在「设置 → 账号」扫码登录，或粘贴自己的 Cookie。

### 方式二：从源码运行

```bash
git clone https://github.com/SoulApothe/Netease-CloudBox.git
cd Netease-CloudBox
```

Windows 上双击 **`启动桌面版.bat`**：脚本会自动创建 `.venv`、安装依赖并打开桌面窗口（仅首次较慢）。

也可以手动执行：

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python desktop.py
```

纯 Web 模式（不弹桌面窗口，用浏览器访问）：

```bash
python desktop.py --server --port 5000
```

打开 <http://127.0.0.1:5000>。

> 需要 Python 3.10 及以上。桌面窗口依赖 Microsoft Edge WebView2 Runtime（Windows 11 自带；Windows 10 若缺失，程序会自动回退到 Edge / Chrome 的 `--app` 模式，再回退默认浏览器）。

### 方式三：自己打包 exe

```bash
pip install pyinstaller
python -m PyInstaller app.spec --noconfirm --clean
```

产物为 `dist/CloudBox.exe`。也可直接双击 **`打包EXE.bat`**，它会完成打包并组装 `release-package/` 分发包。

---

## 🧭 目录结构

```
Netease-CloudBox/
├── desktop.py            # 桌面入口：本地服务 + pywebview 窗口
├── main.py               # 兼容入口（旧版 python main.py）
├── server.py             # Flask 路由与 API
├── config.py             # 全局配置、设置持久化、Cookie 路径
├── discover.py           # 推荐：公开榜单、偏好检索、动态权重
├── accounts.py           # 本机档案（SQLite）：收藏、歌单、会话
├── downloader.py         # 下载与批量下载
├── importer.py           # 从歌单链接导入（网易云 / QQ 音乐 / 酷狗）
├── localmusic.py         # 本地音乐扫描与匹配
├── tags.py               # 元数据与封面写入（mutagen）
├── music_api.py          # 网易云接口封装（解析、搜索、取链）
├── music_downloader.py   # 上游下载实现
├── qr_login.py           # 扫码登录
├── templates/index.html  # 单文件前端（内联 CSS / JS）
├── assets/cloudbox.ico   # 应用图标
├── docs/                 # 使用文档与开发记录
├── app.spec              # PyInstaller 打包配置
├── pack_dist.py          # 组装分发包
├── requirements.txt
├── 启动桌面版.bat / 打包EXE.bat
├── LICENSE
├── SECURITY.md
├── CHANGELOG.md          # 版本更新记录
└── PRIVACY.md
```

---

## 🔒 隐私与安全

这一节是本项目最想讲清楚的部分。**所有结论都可以在源码里逐行核对**，不依赖任何口头承诺。

### 1. 你的数据只在本机

| 文件 / 目录 | 内容 | 是否离开本机 |
| --- | --- | --- |
| `cookie.txt` | 你粘贴或扫码得到的网易云 Cookie | **否** |
| `settings.json` | 音质、命名、主题、推荐权重等设置 | **否** |
| `cloudbox.db` | 本机档案：收藏、歌手、歌单 | **否** |
| `music/` | 下载的音频与歌词 | **否** |
| `music_api.log` | 启动信息与错误堆栈 | **否** |

程序**没有服务器**，作者也**没有收集任何数据**。

### 2. 没有遥测、没有后门

- **无统计上报**：不含任何埋点、UV / PV 统计、设备指纹或崩溃上报 SDK。
- **无自动更新**：不会在后台静默下载或替换任何可执行文件。
- **无远程开关**：不存在从远端拉取配置、指令或脚本的逻辑。
- **无账号体系联网**：「本机档案」是纯本地 SQLite，不需要注册，也不会同步到任何地方。
- **本地服务仅监听 `127.0.0.1`**（`desktop.py` 中 `bind(("127.0.0.1", 0))`），端口随机，同局域网的其他设备也访问不到。
- **前端零外部资源**：`templates/index.html` 内联全部样式与脚本，不加载任何 CDN、字体或第三方 JS。

### 3. Cookie 怎么被使用

Cookie 是你网易云账号的**登录凭证**，等同于账号密码，本项目对它的处理是：

- 只保存在本机 `cookie.txt`，文件名与位置固定，方便你随时检查、替换、删除；
- 只作为 HTTP 请求头，**直接发往网易云官方域名**（`music.163.com` / `interface3.music.163.com` 等），**不经过任何第三方服务器、不经作者中转**；
- **不写入日志、不写入数据库、不上传到任何地方**；
- 本仓库**不含任何真实 Cookie**，`.gitignore` 已屏蔽 `cookie.txt`、`cookie*.bak*` 等文件；分发包同样不带 Cookie。

> 你可以用一条命令自查：`grep -rn "MUSIC_U" --include="*.py" .` —— 命中的只有「读取你本地 Cookie 并拼进请求头」的代码，没有任何外发逻辑。

### 4. 网络请求清单（完整）

程序运行期间可能访问的域名，只有以下几类：

| 域名 | 触发时机 | 携带 Cookie |
| --- | --- | --- |
| `music.163.com`、`interface3.music.163.com` | 搜索、推荐、取直链、歌单详情 | 仅在你已登录时携带，用于解锁会员音质 |
| `p*.music.126.net` 等网易云 CDN | 播放音频、下载封面 | 否 |
| `y.qq.com`、`c.y.qq.com`、`m.kugou.com` | **仅当你主动粘贴 QQ 音乐 / 酷狗歌单链接做导入时** | 否 |
| `127.0.0.1` | 本地界面与后端通信 | 否 |

除此之外没有任何出站连接。

### 5. 你需要自己注意的风险

- **Cookie 泄露等于账号被登录**。不要把 `cookie.txt` 发给任何人，不要截图，不要提交到 Git 仓库，不要打包进分发包。怀疑泄露时，请在网易云 App 里退出所有设备并重新登录。
- 下载的音乐受**版权法**保护，请仅用于个人学习与已购买内容的备份，不要传播或商用。
- 第三方依赖（Flask、requests、pywebview、mutagen 等）由各自作者维护，请关注其安全公告。
- exe 由 PyInstaller 打包且**未签名**，无法从系统层面证明来源；请以本仓库源码为准，或自行编译。

更多细节见 [PRIVACY.md](./PRIVACY.md) 与 [SECURITY.md](./SECURITY.md)。

---

## 📄 许可证

本项目以 **MIT License** 开源，允许自由使用、修改、分发（包括商用），但须保留版权声明与许可声明。

```
MIT License

Copyright (c) 2024 Suxiaoqinx            <- 上游项目 Netease_url
Copyright (c) 2026 SoulApothe             <- 本项目新增与改造部分
```

完整文本见 [LICENSE](./LICENSE)。

需要特别说明的几点：

- 上游 [Netease_url](https://github.com/Suxiaoqinx/Netease_url) 同样采用 MIT License，本项目**保留其原始版权声明**，并在 [NOTICE](./NOTICE) 中注明衍生关系。
- **MIT 只授予代码层面的权利**：不授予「云音盒子 / CloudBox」名称与图标的商标权，也不授予任何音乐、封面、歌词的版权。音乐内容的权利属于版权方与网易云音乐。
- 若你基于本项目再分发，请同样保留上游与本项目的版权声明，并**不要把你的 Cookie 一起发出去**。

---

## ⚠️ 免责声明

- 本项目仅供**个人学习、技术研究与已购内容的本地备份**使用，不得用于商业用途或大规模传播。
- 本项目与网易云音乐（NetEase Cloud Music）**没有任何关联**，未获其授权或认可，亦不代表其立场。
- 使用本工具下载、缓存的内容，其版权归原权利人所有；使用者应自行确保行为符合当地法律法规与平台服务条款。
- 因使用本软件产生的任何直接或间接损失、账号风险、法律责任，由使用者自行承担，作者不承担责任。
- 若权利人认为本项目侵犯其权益，请通过 Issues 或邮件联系，核实后将及时调整或移除相关代码。

---

## 🐞 反馈与贡献

- **Bug / 功能建议**：[提交 Issue](https://github.com/SoulApothe/Netease-CloudBox/issues)
- **安全或隐私问题**：请勿公开提交，按 [SECURITY.md](./SECURITY.md) 的方式私下联系
- **Pull Request**：欢迎。请保持代码风格一致，并在描述里说明改动动机与验证方式

反馈时请附上：系统版本、软件版本、复现步骤、`music_api.log` 相关片段（**请先删除其中的 Cookie 与个人信息**）。

---

## 🙏 致谢

- [Suxiaoqinx/Netease_url](https://github.com/Suxiaoqinx/Netease_url) —— 本项目的上游与起点
- [Flask](https://flask.palletsprojects.com/)、[pywebview](https://pywebview.flowrl.com/)、[mutagen](https://mutagen.readthedocs.io/)、[requests](https://requests.readthedocs.io/) —— 支撑本项目的开源组件
- 所有提交 Issue、反馈问题的使用者

---

<div align="center">

**代码公开，欢迎监督。发现问题，请积极反馈。**

</div>
