"""云音盒子 · 桌面版全局配置

设计要点：
1. 所有路径基于「程序根目录」解析，双击启动 / exe 启动 / 从任意 CWD 启动都不会跑偏。
2. 下载的歌曲默认输出到根目录下的 music 文件夹（不建 downloads 子目录）。
3. 设置持久化到根目录 settings.json，Cookie 复用原有 cookie.txt。
"""
from __future__ import annotations

import copy
import json
import os
import re
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, parse_qs


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _app_root() -> Path:
    """用户可见的程序根目录：源码模式 = 项目目录；打包模式 = exe 所在目录。"""
    if _is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _resource_root() -> Path:
    """只读资源（模板等）所在目录：打包模式 = 解包临时目录。"""
    if _is_frozen():
        return Path(getattr(sys, "_MEIPASS", _app_root()))
    return _app_root()


BASE_DIR = _app_root()
RESOURCE_DIR = _resource_root()
TEMPLATE_DIR = RESOURCE_DIR / "templates"

COOKIE_FILE = BASE_DIR / "cookie.txt"
SETTINGS_FILE = BASE_DIR / "settings.json"
LOG_FILE = BASE_DIR / "music_api.log"

APP_NAME = "云音盒子"
APP_SUBTITLE = "NetEase Cloud Music Desktop"
VERSION = "3.5.0"

# 音质选项（前端与后端共用）
QUALITY_OPTIONS = [
    {"value": "standard", "label": "标准音质", "hint": "128 kbps · MP3"},
    {"value": "exhigh", "label": "极高音质", "hint": "320 kbps · MP3"},
    {"value": "lossless", "label": "无损音质", "hint": "FLAC · 需会员"},
    {"value": "hires", "label": "Hi-Res", "hint": "24bit / 96kHz · 需会员"},
    {"value": "jyeffect", "label": "高清环绕声", "hint": "需会员"},
    {"value": "sky", "label": "沉浸环绕声", "hint": "需 SVIP"},
    {"value": "jymaster", "label": "超清母带", "hint": "需 SVIP"},
    {"value": "dolby", "label": "杜比全景声", "hint": "需设备支持"},
]
QUALITY_VALUES = [q["value"] for q in QUALITY_OPTIONS]
QUALITY_LABELS = {q["value"]: q["label"] for q in QUALITY_OPTIONS}

# 文件名「简单模式」可选的连接符（值直接拼进文件名，所以带空格）
NAMING_SEPARATORS = [
    {"value": " - ", "label": "短横线　歌曲 - 歌手"},
    {"value": " – ", "label": "长横线　歌曲 – 歌手"},
    {"value": " · ", "label": "间隔号　歌曲 · 歌手"},
    {"value": " _ ", "label": "下划线　歌曲 _ 歌手"},
    {"value": " ", "label": "空格　歌曲 歌手"},
]
SEPARATOR_VALUES = [item["value"] for item in NAMING_SEPARATORS]

# 文件名里显示的短音质标签
QUALITY_SHORT = {
    "standard": "标准", "exhigh": "极高", "lossless": "无损", "hires": "Hi-Res",
    "jyeffect": "环绕", "sky": "沉浸", "jymaster": "母带", "dolby": "杜比",
}

# 「专辑内顺序」序号与后面内容之间的连接符（值直接拼进文件名）
TRACK_SEPARATORS = [
    {"value": ". ", "label": "点 + 空格　01. 歌曲名"},
    {"value": " - ", "label": "短横线　01 - 歌曲名"},
    {"value": "_", "label": "下划线　01_歌曲名"},
    {"value": " ", "label": "空格　01 歌曲名"},
    {"value": "、", "label": "顿号　01、歌曲名"},
    {"value": "]", "label": "方括号　[01] 歌曲名"},
]
TRACK_SEPARATOR_VALUES = [item["value"] for item in TRACK_SEPARATORS]
# 序号位数（不足补零，超过不截断）
TRACK_DIGITS_OPTIONS = [
    {"value": 1, "label": "1 位　1. 歌曲名"},
    {"value": 2, "label": "2 位　01. 歌曲名（推荐）"},
    {"value": 3, "label": "3 位　001. 歌曲名"},
]
TRACK_DIGITS_VALUES = [item["value"] for item in TRACK_DIGITS_OPTIONS]

# 在线播放可选音质：与下载音质相互独立；无损 / Hi-Res 需要 cookie.txt 里的会员登录态
PLAY_QUALITY_OPTIONS = [
    {"value": "standard", "label": "标准音质", "hint": "128 kbps · 无需会员"},
    {"value": "exhigh", "label": "极高音质", "hint": "320 kbps · 无需会员"},
    {"value": "lossless", "label": "无损音质", "hint": "FLAC · 需会员 Cookie"},
    {"value": "hires", "label": "Hi-Res", "hint": "24bit · 需会员 Cookie"},
]
PLAY_QUALITY_VALUES = [item["value"] for item in PLAY_QUALITY_OPTIONS]

# 播放模式
PLAY_MODES = [
    {"value": "order", "label": "顺序播放", "hint": "列表放完就停"},
    {"value": "one", "label": "单曲循环", "hint": "一直重复当前歌曲"},
    {"value": "shuffle", "label": "随机播放", "hint": "随机挑下一首"},
]
PLAY_MODE_VALUES = [item["value"] for item in PLAY_MODES]

# 首次使用推荐偏好（可跳过，仅存本机 settings.json）
PREFERENCE_GENRES = [
    "流行", "摇滚", "民谣", "电子", "说唱", "R&B", "爵士", "古典", "二次元", "轻音乐",
]
PREFERENCE_LANGUAGES = ["华语", "欧美", "日语", "韩语", "粤语", "纯音乐"]

DEFAULTS: Dict[str, Any] = {
    # 默认输出到程序根目录下的 music 文件夹；相对路径基于程序根目录解析
    # 想直接输出到根目录可以填 "."（或绝对路径）
    "output_dir": "music",
    # 默认「极高音质」：320kbps MP3，不需要会员 Cookie 也能完整下载
    "quality": "exhigh",
    "concurrency": 3,
    # ---- 文件命名 ----
    # simple：勾选式（可读、易操作）；template：{变量} 模板（高度自定义）
    "naming_mode": "simple",
    "naming_song_first": True,
    "naming_include_artist": True,
    "naming_include_album": False,
    "naming_include_track": False,     # 是否包含专辑内顺序
    "naming_track_separator": ". ",    # 序号与后面内容的连接符
    "naming_track_digits": 2,          # 序号位数
    "naming_include_id": False,
    "naming_include_quality": False,
    "naming_artist_mode": "all",
    "naming_separator": " - ",
    "filename_template": "{artist} - {name}",
    "embed_lyrics": True,
    "save_lrc": False,
    "skip_existing": True,
    "group_by_album": False,
    "batch_in_folder": False,          # 批量下载时按任务标题建一个文件夹
    "local_dirs": [],                  # 本地音乐附加扫描目录（绝对路径列表）
    "cover_size": 800,
    "theme": "light",
    # ---- 在线播放（与下载音质分开） ----
    "play_quality": "exhigh",
    "play_mode": "order",
    # ---- 推荐偏好（首次使用引导，可跳过） ----
    "pref_genres": [],
    "pref_languages": [],
    "pref_done": False,
    # 推荐页点击累积出的动态权重：{"genres": {...}, "languages": {...}, "artists": {...}}
    "pref_weights": {"genres": {}, "languages": {}, "artists": {}},
}

_FILENAME_TEMPLATE_VARS = (
    "{name}", "{artist}", "{album}", "{id}", "{track}", "{quality}", "{year}",
)
# 命名相关设置项
NAMING_KEYS = (
    "naming_mode", "naming_song_first", "naming_include_artist",
    "naming_include_album", "naming_include_track", "naming_track_separator",
    "naming_track_digits", "naming_include_id", "naming_include_quality",
    "naming_artist_mode", "naming_separator", "filename_template",
)


# 动态偏好权重：只影响推荐排序 / 检索关键词，不影响下载与播放
PREF_WEIGHT_MIN = 0.2
PREF_WEIGHT_MAX = 6.0
PREF_WEIGHT_DEFAULT = 1.0
PREF_WEIGHT_BUCKETS = ("genres", "languages", "artists")
# 不同行为对权重的贡献：收藏 > 下载 > 播放
PREF_FEEDBACK_GAIN = {"play": 0.3, "favorite": 1.0, "download": 0.5}


def clean_pref_weights(value: Any) -> Dict[str, Dict[str, float]]:
    """把任意输入整理成 {genres/languages/artists: {名称: 权重}}。"""
    cleaned: Dict[str, Dict[str, float]] = {bucket: {} for bucket in PREF_WEIGHT_BUCKETS}
    if not isinstance(value, dict):
        return cleaned
    limits = {"genres": 32, "languages": 32, "artists": 80}
    for bucket in PREF_WEIGHT_BUCKETS:
        source = value.get(bucket)
        if not isinstance(source, dict):
            continue
        items = []
        for raw_name, raw_weight in source.items():
            name = str(raw_name).strip()[:80]
            if not name:
                continue
            try:
                weight = float(raw_weight)
            except (TypeError, ValueError):
                continue
            if weight != weight:  # NaN
                continue
            items.append((name, round(max(PREF_WEIGHT_MIN, min(PREF_WEIGHT_MAX, weight)), 3)))
        items.sort(key=lambda kv: -kv[1])
        cleaned[bucket] = dict(items[:limits[bucket]])
    return cleaned


def write_settings_file(data: Dict[str, Any]) -> bool:
    """原子写入 settings.json。

    先写临时文件再 os.replace，断电 / 崩溃 / 强制关窗都不会留下半截 JSON，
    确保「下次打开设置不变」。返回是否写入成功。
    """
    tmp = SETTINGS_FILE.with_name(SETTINGS_FILE.name + ".tmp")
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, SETTINGS_FILE)
        return True
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


class Settings:
    """线程安全的轻量设置存储。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data = copy.deepcopy(DEFAULTS)
        self.load()
        # 首次运行（解压后第一次启动）就把默认值落盘，
        # 这样即便用户一项都没改，settings.json 也存在于 exe 同级目录。
        if not SETTINGS_FILE.is_file():
            self.save()

    def load(self) -> Dict[str, Any]:
        with self._lock:
            try:
                if SETTINGS_FILE.is_file():
                    saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
                    if isinstance(saved, dict):
                        for key, value in saved.items():
                            if key in DEFAULTS:
                                self._data[key] = value
            except Exception:
                pass
            # 旧版本的空值表示「根目录」，v3.1 起默认输出到 music 文件夹
            if not str(self._data.get("output_dir") or "").strip():
                self._data["output_dir"] = DEFAULTS["output_dir"]
            return dict(self._data)

    def as_dict(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._data)

    def get(self, key: str, default: Any = None) -> Any:
        return self.as_dict().get(key, DEFAULTS.get(key, default))

    def update(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            for key, value in patch.items():
                if key not in DEFAULTS:
                    continue
                if key in ("concurrency", "cover_size", "naming_track_digits"):
                    try:
                        value = int(value)
                    except (TypeError, ValueError):
                        continue
                if key == "quality" and value not in QUALITY_VALUES:
                    continue
                if key == "play_quality" and value not in PLAY_QUALITY_VALUES:
                    continue
                if key == "play_mode" and value not in PLAY_MODE_VALUES:
                    continue
                if key in ("pref_genres", "pref_languages"):
                    if not isinstance(value, list):
                        continue
                    pool = PREFERENCE_GENRES if key == "pref_genres" else PREFERENCE_LANGUAGES
                    value = [str(x).strip() for x in value if str(x).strip() in pool][:12]
                if key == "pref_weights":
                    value = clean_pref_weights(value)
                if key == "theme" and value not in ("light", "dark"):
                    continue
                if key == "filename_template" and not isinstance(value, str):
                    continue
                if key == "naming_mode" and value not in ("simple", "template"):
                    continue
                if key == "naming_artist_mode" and value not in ("all", "first"):
                    continue
                if key == "naming_separator" and value not in SEPARATOR_VALUES:
                    continue
                if key == "naming_track_separator" and value not in TRACK_SEPARATOR_VALUES:
                    continue
                if key == "naming_track_digits" and value not in TRACK_DIGITS_VALUES:
                    continue
                if key == "local_dirs":
                    if not isinstance(value, list):
                        continue
                    value = [str(x).strip() for x in value if str(x).strip()][:12]
                if key == "output_dir" and not isinstance(value, str):
                    continue
                if key == "output_dir":
                    value = value.strip() or DEFAULTS["output_dir"]
                if isinstance(DEFAULTS[key], bool):
                    value = bool(value)
                if key == "concurrency":
                    value = max(1, min(8, value))
                if key == "cover_size":
                    value = max(100, min(2000, value))
                self._data[key] = value
            snapshot = dict(self._data)
        write_settings_file(snapshot)
        return snapshot

    def save(self) -> bool:
        """立即把当前设置写盘（供启动时落默认值使用）。"""
        with self._lock:
            snapshot = dict(self._data)
        return write_settings_file(snapshot)


settings = Settings()


def output_dir() -> Path:
    """当前生效的输出目录。

    - 空值 / 未设置 → 程序根目录下的 music 文件夹
    - 相对路径 → 相对程序根目录解析（跨盘符、任意 CWD 启动都不会跑偏）
    - "." → 程序根目录本身
    """
    raw = str(settings.get("output_dir") or "").strip()
    if not raw:
        raw = DEFAULTS["output_dir"]
    if raw in (".", "./", ".\\"):
        path = BASE_DIR
    else:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = BASE_DIR / path
    try:
        path = path.resolve()
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        path = BASE_DIR
    return path


def set_output_dir(path: str) -> Path:
    settings.update({"output_dir": str(path or "")})
    return output_dir()


# ---------- 通用工具 ----------

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_MULTI_SPACE = re.compile(r"\s+")


def safe_filename(name: str, max_len: int = 120) -> str:
    """生成 Windows / macOS / Linux 都安全的文件名。"""
    name = _ILLEGAL.sub("_", str(name or ""))
    name = _MULTI_SPACE.sub(" ", name).strip(" .")
    name = name.replace("\u200b", "").replace("\ufeff", "")
    if len(name) > max_len:
        name = name[:max_len].rstrip(" .")
    return name or "unknown"


def artist_list(info: Dict[str, Any]) -> List[str]:
    """从曲目信息里取出歌手列表，兼容 artists 字符串 / artist_list 数组。"""
    artists = info.get("artist_list") or info.get("artists") or []
    if isinstance(artists, str):
        artists = [a.strip() for a in re.split(r"\s*/\s*", artists) if a.strip()]
    result = []
    for item in artists:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def simple_filename(info: Dict[str, Any], options: Dict[str, Any], quality: str) -> str:
    """「简单模式」命名：按勾选项拼出可读文件名（不含扩展名）。

    组合顺序： [音轨号. ]歌曲名 <分隔符> 歌手 (专辑) [歌曲ID] [音质]
    """
    name = str(info.get("name") or "未知歌曲").strip() or "未知歌曲"
    album = str(info.get("album") or "").strip()
    artists = artist_list(info)
    if str(options.get("naming_artist_mode") or "all") == "first":
        artists = artists[:1]
    artist_str = "、".join(artists)

    separator = str(options.get("naming_separator") or " - ")
    if separator not in SEPARATOR_VALUES:
        separator = " - "

    song_first = bool(options.get("naming_song_first", True))
    if options.get("naming_include_artist", True) and artist_str:
        core = f"{name}{separator}{artist_str}" if song_first else f"{artist_str}{separator}{name}"
    else:
        core = name

    prefix = ""
    if options.get("naming_include_track"):
        try:
            track_no = int(info.get("track_no") or 0)
        except (TypeError, ValueError):
            track_no = 0
        if track_no > 0:
            try:
                digits = int(options.get("naming_track_digits") or 2)
            except (TypeError, ValueError):
                digits = 2
            digits = max(1, min(3, digits))
            number = f"{track_no:0{digits}d}"
            track_sep = str(options.get("naming_track_separator") or ". ")
            if track_sep not in TRACK_SEPARATOR_VALUES:
                track_sep = ". "
            # 方括号样式： [01] 歌曲名
            prefix = f"[{number}] " if track_sep == "]" else f"{number}{track_sep}"

    result = prefix + core
    if options.get("naming_include_album") and album:
        result += f" ({album})"
    if options.get("naming_include_id") and info.get("id"):
        result += f" [{info['id']}]"
    if options.get("naming_include_quality") and quality:
        result += f" [{QUALITY_SHORT.get(quality, quality)}]"
    return result


def template_filename(info: Dict[str, Any], template: str, quality: str) -> str:
    """「模板模式」命名：按 {name} {artist} {album} {track} {id} {year} {quality} 替换。

    音轨号支持补零写法，例如 {track:02d} → 03。
    """
    artists = artist_list(info)
    artist_str = "、".join(artists) or "未知歌手"
    try:
        track_no = int(info.get("track_no") or 0)
    except (TypeError, ValueError):
        track_no = 0
    result = str(template or DEFAULTS["filename_template"])
    result = re.sub(
        r"\{track:(\d+)d\}",
        lambda match: str(track_no).zfill(int(match.group(1))) if track_no else "",
        result,
    )
    values = {
        "{name}": str(info.get("name") or "未知歌曲"),
        "{artist}": artist_str,
        "{album}": str(info.get("album") or "未知专辑"),
        "{id}": str(info.get("id") or ""),
        "{track}": str(track_no or ""),
        "{year}": str(info.get("year") or ""),
        "{quality}": quality,
    }
    for key, value in values.items():
        result = result.replace(key, value)
    # 模板里可能带未替换的 JSON 花括号，交给 safe_filename 统一清洗
    return result


def build_filename(info: Dict[str, Any], options: Any = None,
                   quality: str = "lossless", ext: str = "") -> str:
    """生成最终文件名（含扩展名）。

    options 支持两种传法，便于兼容旧代码：
      - dict：设置快照（含 naming_mode 等），缺失项回落到全局设置
      - str ：直接当作旧版模板字符串
    """
    if isinstance(options, str):
        opts: Dict[str, Any] = {"naming_mode": "template", "filename_template": options}
    else:
        opts = settings.as_dict()
        if isinstance(options, dict):
            opts.update(options)
    quality = quality or str(opts.get("quality") or "lossless")

    mode = str(opts.get("naming_mode") or "simple")
    if mode == "template":
        result = template_filename(info, str(opts.get("filename_template") or ""), quality)
    else:
        result = simple_filename(info, opts, quality)
    return safe_filename(result) + ext


def naming_sample(sample: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """给前端预览用的示例曲目（两首，便于对比单曲 / 有音轨号的情况）。"""
    base = {
        "name": "起风了", "artists": ["买辣椒也用券"], "album": "起风了",
        "track_no": 1, "id": "1330348068", "year": "2017",
    }
    second = {
        "name": "晴天", "artists": ["周杰伦"], "album": "叶惠美",
        "track_no": 3, "id": "186016", "year": "2003",
    }
    third = {
        "name": "刀马旦", "artists": ["周杰伦", "李玟"], "album": "Partners 拍档",
        "track_no": 1, "id": "186064", "year": "2002",
    }
    if isinstance(sample, dict) and sample.get("name"):
        for key in ("name", "artists", "album", "track_no", "id", "year"):
            if key in sample and sample[key] not in (None, ""):
                base[key] = sample[key]
    return [base, second, third]


def format_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} GB"


def format_duration(ms: int) -> str:
    try:
        seconds = int(ms) // 1000
    except (TypeError, ValueError):
        return "--:--"
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def extract_id(text: str) -> str:
    """从纯 ID、网易云链接、短链接、分享文案中提取资源 ID。

    支持：
      185668
      https://music.163.com/song?id=185668
      https://music.163.com/#/playlist?id=123&...
      分享文案里夹杂的链接
    """
    raw = str(text or "").strip()
    if not raw:
        return ""
    url_match = re.search(r"https?://[^\s\u4e00-\u9fff]+", raw)
    if url_match:
        url = url_match.group(0).rstrip("),.;\"'）】，。")
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        if "id" in qs and qs["id"]:
            return qs["id"][0]
        # hash 路由： /#/song?id=xxx
        if parsed.fragment:
            frag_qs = parse_qs(urlparse(parsed.fragment).query)
            if "id" in frag_qs and frag_qs["id"]:
                return frag_qs["id"][0]
        tail = re.search(r"/(?:song|album|playlist|artist)/(\d+)", url)
        if tail:
            return tail.group(1)
    ids = re.findall(r"\d{4,}", raw)
    return ids[0] if ids else ""


def detect_kind(text: str) -> str:
    """根据链接猜测资源类型：song / playlist / album。"""
    raw = str(text or "").lower()
    if "playlist" in raw:
        return "playlist"
    if "album" in raw:
        return "album"
    return "song"
