"""云音盒子 · 多平台歌单导入

目标：把其它音乐平台的歌单链接（或用户复制的歌单文本）转成
「网易云歌曲列表」，从而能在本软件里播放、收藏、批量下载。

设计要点：
1. 只做「曲目名 + 歌手」的搬运，不下载任何平台的受版权保护音频。
2. 全程匿名请求，不读取 cookie.txt，也不使用软件账号信息。
3. 各平台网页结构随时可能变化；抓不到时明确返回 needs_text，
   引导用户把歌单文本粘贴进来（这个通道适用于所有平台）。
4. 匹配到网易云时同样使用匿名搜索接口；Cookie 只用于下载。
"""
from __future__ import annotations

import difflib
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

import requests

from music_api import APIConstants, NeteaseAPI, loads_loose

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1")

# 平台识别：(key, 展示名, 域名特征, 是否原生支持)
PLATFORMS = [
    ("netease", "网易云音乐", ("music.163.com", "163cn.tv"), True),
    ("qq", "QQ 音乐", ("y.qq.com", "qq.com"), True),
    ("kugou", "酷狗音乐", ("kugou.com",), True),
    ("kuwo", "酷我音乐", ("kuwo.cn",), False),
    ("migu", "咪咕音乐", ("migu.cn",), False),
    ("bilibili", "哔哩哔哩", ("bilibili.com", "b23.tv"), False),
    ("spotify", "Spotify", ("spotify.com",), False),
    ("apple", "Apple Music", ("music.apple.com",), False),
    ("netease_podcast", "其它平台", (), False),
]

TEXT_HINT = ("这个平台的歌单链接暂时无法直接读取。请把歌单里的曲目列表复制粘贴到"
             "下面的文本框（每行一首，格式「歌名 - 歌手」），即可继续导入。")


class ImportError(Exception):
    """可预期的导入错误，直接展示给用户。"""


def detect_platform(url: str) -> Tuple[str, str, bool]:
    host = (urlparse(url).hostname or "").lower()
    for key, label, patterns, native in PLATFORMS:
        if any(p in host for p in patterns):
            return key, label, native
    return "unknown", "未知平台", False


def _clean(text: str) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    text = re.sub(r"\.(mp3|flac|m4a|wav|aac|ogg|ape)$", "", text, flags=re.I)
    return text.strip(" -–—_·\t")


def _entry(name: str, artists: str = "", raw: str = "") -> Dict[str, str]:
    return {"name": _clean(name), "artists": _clean(artists), "raw": raw or name}


def parse_text(text: str) -> List[Dict[str, str]]:
    """解析用户粘贴的歌单文本：每行「歌名 - 歌手」，兼容序号、制表符、多分隔符。"""
    result: List[Dict[str, str]] = []
    seen = set()
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line or len(line) < 2:
            continue
        # 去掉常见序号前缀：01. / 1、/ #2 / 1)
        line = re.sub(r"^\s*(?:#?\d{1,3}\s*[\.、\)．:：]\s*|[-*•]\s*)", "", line).strip()
        if not line:
            continue
        parts = re.split(r"\s+[-–—~]\s+|\t+|\s{3,}", line, maxsplit=1)
        if len(parts) == 2:
            name, artists = parts[0], parts[1]
        else:
            name, artists = line, ""
        # 「歌手 - 歌名」这类反序无法自动判断，交给匹配阶段的模糊搜索兜底
        name, artists = _clean(name), _clean(artists)
        if not name:
            continue
        key = (name.lower(), artists.lower())
        if key in seen:
            continue
        seen.add(key)
        result.append(_entry(name, artists, line))
    return result


# ---------------- 各平台原生解析 ----------------

def _qq_playlist(url: str) -> Dict[str, Any]:
    match = re.search(r"(?:disstid|id)[=/](\d{6,})", url) or re.search(r"/playlist/(\d+)", url)
    if not match:
        raise ImportError("没能从 QQ 音乐链接里识别出歌单 ID")
    tid = match.group(1)
    api = "https://c.y.qq.com/qzone/fcg-bin/fcg_ucc_getcdinfo_byids_cp.fcg"
    params = {
        "type": 1, "json": 1, "utf8": 1, "onlysong": 0, "disstid": tid,
        "format": "json", "inCharset": "utf8", "outCharset": "utf-8",
        "platform": "yqq", "notice": 0, "needNewCode": 0,
    }
    r = requests.get(api, params=params,
                     headers={"User-Agent": UA, "Referer": "https://y.qq.com/"},
                     cookies={}, timeout=15)
    r.raise_for_status()
    data = loads_loose(r.text)
    cd = (data.get("cdlist") or [{}])[0]
    songs = cd.get("songlist") or []
    if not songs:
        raise ImportError("QQ 音乐没有返回曲目（歌单可能不存在或已设为私密）")
    entries = []
    for s in songs:
        singers = s.get("singer") or []
        artists = "/".join(str(x.get("name") or "") for x in singers if isinstance(x, dict))
        entries.append(_entry(s.get("songname") or s.get("name") or "", artists))
    return {"title": str(cd.get("dissname") or "QQ 音乐歌单"), "entries": entries}


def _kugou_playlist(url: str) -> Dict[str, Any]:
    match = (re.search(r"/plist/list/(\d+)", url)
             or re.search(r"special/single/(\d+)", url)
             or re.search(r"[?&](?:id|pid|specialid)=(\d+)", url))
    if not match:
        raise ImportError("没能从酷狗链接里识别出歌单 ID")
    pid = match.group(1)
    headers = {"User-Agent": MOBILE_UA, "Referer": "https://m.kugou.com/"}
    entries: List[Dict[str, str]] = []
    total = None
    for page in range(1, 6):  # 最多 5 页 / 100 首，避免拖慢界面
        r = requests.get(f"https://m.kugou.com/plist/list/{pid}",
                         params={"json": "true", "page": page},
                         headers=headers, cookies={}, timeout=15)
        r.raise_for_status()
        data = loads_loose(r.text)
        node = ((data.get("list") or {}).get("list") or {})
        info = node.get("info") or []
        total = node.get("total") or total
        if not info:
            break
        for item in info:
            # 酷狗 filename 一般是「歌手 - 歌名」
            filename = _clean(item.get("filename") or item.get("songname") or "")
            if " - " in filename:
                left, right = filename.split(" - ", 1)
                if len(left) <= 40:
                    entries.append(_entry(right, left, filename))
                    continue
            entries.append(_entry(filename, item.get("singername") or "", filename))
        if total and len(entries) >= int(total):
            break
    if not entries:
        raise ImportError("酷狗没有返回曲目（歌单可能不存在或已下架）")
    return {"title": f"酷狗歌单 {pid}", "entries": entries}


def _generic_html_titles(url: str, label: str) -> Dict[str, Any]:
    """对暂未有稳定公开接口的平台做一次尽力而为的 HTML 抓取。

    很多站点会返回 403 / 需要登录，此时直接返回 needs_text 让文本通道兜底。
    """
    r = requests.get(url, headers={"User-Agent": UA}, cookies={}, timeout=12)
    r.raise_for_status()
    html = r.text[:400000]
    title = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    if m:
        title = re.sub(r"\s+", " ", m.group(1)).strip()
    # 尝试从 __NUXT__ / __INITIAL_STATE__ 里找 "name":"..." 与 "artist":"..."
    pairs = re.findall(r'"(?:song|music)?name"\s*:\s*"([^"]{1,80})"[^{}]{0,400}?"artist(?:Name)?"\s*:\s*"([^"]{1,80})"', html)
    entries = [_entry(n, a) for n, a in pairs if n]
    if not entries:
        return {"title": title or label + " 歌单", "entries": [],
                "needs_text": True, "note": TEXT_HINT}
    return {"title": title or label + " 歌单", "entries": entries[:200]}


NATIVE = {"qq": _qq_playlist, "kugou": _kugou_playlist}


def parse_url(url: str) -> Dict[str, Any]:
    """解析一个歌单链接，返回平台、标题与曲目条目。"""
    url = unquote(str(url or "").strip())
    if not url:
        raise ImportError("请先粘贴歌单链接")
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    key, label, native = detect_platform(url)
    result: Dict[str, Any] = {
        "platform": key, "platform_label": label, "url": url,
        "title": label + " 歌单", "entries": [], "needs_text": False, "note": "",
    }
    if key == "netease":
        # 网易云歌单/专辑由 server 走原生 resolve，精确曲目，无需模糊匹配
        result["native_netease"] = True
        return result
    try:
        if native and key in NATIVE:
            result.update(NATIVE[key](url))
        else:
            result.update(_generic_html_titles(url, label))
    except ImportError as exc:
        result.update({"needs_text": True, "note": str(exc) + " " + TEXT_HINT})
    except Exception as exc:  # noqa: BLE001
        result.update({"needs_text": True,
                       "note": f"{label} 暂时无法读取（{type(exc).__name__}）。{TEXT_HINT}"})
    return result


# ---------------- 匹配到网易云歌曲 ----------------

def _norm(text: str) -> str:
    return re.sub(r"[\s\-_·、,，.。()（）\[\]【】'\"!！?？/]+", "", str(text or "")).lower()


_BAD_WORDS = ("remix", "dj版", "1.1x", "0.8x", "伴奏", "纯音乐", "翻唱", "live版", "试听")


def _score(name: str, artists: str, candidate: Dict[str, Any]) -> float:
    n, cn = _norm(name), _norm(candidate.get("name") or "")
    if not n or not cn:
        return -1
    score = difflib.SequenceMatcher(None, n, cn).ratio()
    if n == cn:
        score += 0.55
    elif cn.startswith(n) or n.startswith(cn):
        score += 0.22
    if any(w in cn for w in _BAD_WORDS) and not any(w in n for w in _BAD_WORDS):
        score -= 0.22
    if artists:
        ca = _norm(candidate.get("artists") or "")
        names = [a for a in re.split(r"[/、,，&]", artists) if _norm(a)]
        if any(_norm(a) and _norm(a) in ca for a in names):
            score += 0.24
    return score


def _match_one(entry: Dict[str, str]) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, str]]]:
    name, artists = entry.get("name") or "", entry.get("artists") or ""
    keyword = (name + " " + artists).strip() or entry.get("raw") or ""
    try:
        # 匿名搜索：不使用 cookie.txt
        candidates = NeteaseAPI().search_music(keyword, {}, 8, 1, 0)
    except Exception:  # noqa: BLE001
        return None, entry
    best, best_score = None, 0.52
    for item in candidates or []:
        if not item.get("id"):
            continue
        value = _score(name, artists, item)
        if value > best_score:
            best, best_score = item, value
    if best is None and artists:
        # 再试一次只搜歌名，避免「歌手 - 歌名」顺序造成的漏匹配
        try:
            candidates = NeteaseAPI().search_music(name, {}, 8, 1, 0)
            for item in candidates or []:
                value = _score(name, artists, item)
                if value > best_score:
                    best, best_score = item, value
        except Exception:  # noqa: BLE001
            pass
    if best is None:
        return None, entry
    return {
        "id": best.get("id"), "name": best.get("name") or name,
        "artists": best.get("artists") or artists,
        "artist_list": best.get("artist_list") or [],
        "album": best.get("album") or "", "picUrl": best.get("picUrl") or "",
        "duration": best.get("duration") or 0, "type": "song",
        "matched": True, "source_name": name, "source_artists": artists,
    }, None


def match_entries(entries: List[Dict[str, str]], limit: int = 120) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    """并发把条目匹配为网易云歌曲；保持原始顺序。"""
    items = [e for e in (entries or []) if e.get("name")][:limit]
    if not items:
        return [], []
    workers = min(6, max(2, len(items)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(_match_one, items))
    tracks, unmatched, seen = [], [], set()
    for track, miss in results:
        if track:
            key = str(track["id"])
            if key in seen:
                continue
            seen.add(key)
            tracks.append(track)
        elif miss:
            unmatched.append(miss)
    return tracks, unmatched
