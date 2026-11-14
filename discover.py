"""云音盒子 · 公开推荐内容

严格不使用 cookie.txt：这里的每个请求都显式传空 Cookie，也不读取软件账号
的任何信息。推荐结果由公开榜单 / 新歌 / 公开检索（按本机偏好关键词）组成。

动态偏好：推荐页的播放 / 收藏 / 下载会累积「曲风 / 语言 / 歌手」权重，
权重只存在本机 settings.json，用来调整检索关键词的优先级与次数；
请求本身依旧不带 Cookie，也不关联任何账号。
"""
from __future__ import annotations

import json
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Iterable, List, Optional, Set

import requests

from music_api import APIConstants, loads_loose

TOPLIST_API = "https://music.163.com/api/toplist/detail"
NEWSONG_API = "https://music.163.com/api/personalized/newsong"
NEWALBUM_API = "https://music.163.com/api/album/new"
PLAYLIST_API = "https://music.163.com/api/playlist/detail"
SEARCH_API = APIConstants.SEARCH_API

# 不携带任何用户 Cookie；也不使用登录态接口
ANON_HEADERS = {
    "User-Agent": APIConstants.USER_AGENT,
    "Referer": APIConstants.REFERER,
}
CACHE_TTL = 600.0
# 歌曲标签（曲风 / 语言 / 歌手）缓存时长；只在本机内存里，重启即清空
TAG_TTL = 6 * 3600.0
TAG_MAX = 4000

# 首页固定展示的几张公开榜单（当榜单接口不可用时的兜底）
FALLBACK_CHARTS = [
    {"id": "19723756", "name": "飙升榜", "cover": ""},
    {"id": "3779629", "name": "新歌榜", "cover": ""},
    {"id": "3778678", "name": "热歌榜", "cover": ""},
    {"id": "2884035", "name": "原创榜", "cover": ""},
]

# 「换一批」轮换使用的公开榜单，避免每次都是同一批热歌
ROTATING_CHARTS = ("3778678", "19723756", "2884035", "3779629")

# {歌曲ID: {"tags": set, "artists": list, "at": 时间戳}}，供点击回传时查标签
_SONG_TAGS: Dict[str, Dict[str, Any]] = {}
_TAGS_LOCK = threading.Lock()


def _get(url: str, params: Optional[Dict[str, Any]] = None, timeout: int = 12) -> Dict[str, Any]:
    """匿名 GET —— cookies={} 明确表示不携带 cookie.txt 的任何内容。"""
    response = requests.get(url, params=params or {}, headers=ANON_HEADERS,
                            cookies={}, timeout=timeout)
    response.raise_for_status()
    return loads_loose(response.text)


def _artists(raw: Dict[str, Any]) -> List[str]:
    values = raw.get("ar") or raw.get("artists") or []
    result = []
    for item in values:
        if isinstance(item, dict) and item.get("name"):
            result.append(str(item["name"]))
        elif isinstance(item, str) and item.strip():
            result.append(item.strip())
    return result


def normalize_song(raw: Dict[str, Any], index: int = 0) -> Dict[str, Any]:
    """把公开接口的歌曲结构统一成前端 Track 结构。"""
    raw = raw or {}
    song = raw.get("song") if isinstance(raw.get("song"), dict) else raw
    album = song.get("al") or song.get("album") or {}
    if not isinstance(album, dict):
        album = {}
    artists = _artists(song)
    pic = (album.get("picUrl") or album.get("blurPicUrl")
           or song.get("picUrl") or raw.get("picUrl") or "")
    return {
        "id": song.get("id") or raw.get("id"),
        "name": song.get("name") or "",
        "artists": "/".join(artists),
        "artist_list": artists,
        "album": album.get("name") or "",
        "album_id": album.get("id") or "",
        "picUrl": pic,
        "duration": song.get("dt") or song.get("duration") or 0,
        "track_no": song.get("no") or index + 1,
        "alia": "",
        "type": "song",
    }


class Discovery:
    def __init__(self) -> None:
        self._cache: Dict[str, Any] = {}
        self._lock = threading.Lock()
        self._nonce = 0

    def _cached(self, key: str, producer, force: bool = False):
        with self._lock:
            hit = self._cache.get(key)
        if hit and not force and hit[0] > time.time():
            return hit[1]
        data = producer()
        if data:
            with self._lock:
                self._cache[key] = (time.time() + CACHE_TTL, data)
        return data

    @staticmethod
    def _charts() -> List[Dict[str, Any]]:
        try:
            data = _get(TOPLIST_API)
            result = []
            for item in (data.get("list") or [])[:14]:
                if not item.get("id"):
                    continue
                result.append({
                    "id": str(item["id"]),
                    "name": item.get("name") or "榜单",
                    "cover": item.get("coverImgUrl") or "",
                    "update": item.get("updateFrequency") or "",
                    "subtitle": f"{(item.get('trackCount') or 0)} 首 · {item.get('updateFrequency') or '官方榜'}",
                    "type": "chart",
                })
            return result[:10] or list(FALLBACK_CHARTS)
        except Exception:
            return list(FALLBACK_CHARTS)

    @staticmethod
    def _new_songs() -> List[Dict[str, Any]]:
        try:
            data = _get(NEWSONG_API)
            rows = data.get("result") or []
            if isinstance(rows, dict):
                rows = rows.get("songs") or []
            return [normalize_song(item, i) for i, item in enumerate(rows[:12])
                    if normalize_song(item, i).get("id")]
        except Exception:
            return []

    @staticmethod
    def _hot_songs(nonce: int = 0) -> List[Dict[str, Any]]:
        """热门歌曲：在几张公开榜单之间轮换，并在榜内滑动窗口，「换一批」有新鲜内容。"""
        chart_id = ROTATING_CHARTS[int(nonce) % len(ROTATING_CHARTS)]
        try:
            data = _get(PLAYLIST_API, {"id": int(chart_id), "n": 60}, timeout=15)
            rows = (data.get("result") or {}).get("tracks") or []
        except Exception:
            return []
        if not rows:
            return []
        rows = [normalize_song(item, i) for i, item in enumerate(rows)]
        rows = [row for row in rows if row.get("id")]
        if len(rows) <= 12:
            return rows
        start = (int(nonce) * 13) % (len(rows) - 12)
        return rows[start:start + 12]

    @staticmethod
    def _new_albums() -> List[Dict[str, Any]]:
        try:
            data = _get(NEWALBUM_API, {"limit": 12, "offset": 0})
            rows = data.get("albums") or []
            return [{
                "id": a.get("id"), "name": a.get("name") or "",
                "artists": (a.get("artist") or {}).get("name", ""),
                "picUrl": a.get("picUrl") or "",
                "subtitle": "新碟 · " + (a.get("artist") or {}).get("name", ""),
                "extra": f"{a.get('size') or 0} 首",
                "type": "album",
            } for a in rows if a.get("id")]
        except Exception:
            return []

    @staticmethod
    def _fallback_songs() -> List[Dict[str, Any]]:
        """公开接口全挂时用匿名搜索兜底（依旧不携带 Cookie）。"""
        songs: List[Dict[str, Any]] = []
        for keyword in ("热门华语", "流行", "经典老歌"):
            try:
                response = requests.post(
                    SEARCH_API, data={"s": keyword, "type": 1, "limit": 8},
                    headers=ANON_HEADERS, cookies={}, timeout=10,
                )
                data = loads_loose(response.text)
                for i, item in enumerate((data.get("result") or {}).get("songs") or []):
                    songs.append(normalize_song(item, i))
            except Exception:
                continue
            if len(songs) >= 12:
                break
        return songs[:16]

    @staticmethod
    def _search_songs(keyword: str, limit: int = 10, offset: int = 0) -> List[Dict[str, Any]]:
        """匿名搜索一批歌曲（依旧不携带任何 Cookie）；offset 用于「换一批」。"""
        songs: List[Dict[str, Any]] = []
        try:
            response = requests.post(
                SEARCH_API,
                data={"s": keyword, "type": 1, "limit": limit, "offset": max(0, int(offset))},
                headers=ANON_HEADERS, cookies={}, timeout=10,
            )
            data = loads_loose(response.text)
            for i, item in enumerate((data.get("result") or {}).get("songs") or []):
                songs.append(normalize_song(item, i))
        except Exception:
            return []
        return [s for s in songs if s.get("id")]

    # ---------- 动态偏好：标签登记与权重 ----------

    @staticmethod
    def _weight_of(name: str, bucket: str, weights: Dict[str, Any]) -> float:
        raw = ((weights or {}).get(bucket) or {}).get(str(name))
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return 1.0
        return max(0.2, min(6.0, value))

    @staticmethod
    def _remember(songs: Iterable[Dict[str, Any]], tags: Optional[Iterable[str]] = None) -> None:
        """登记歌曲的曲风 / 语言标签与歌手，供点击回传时使用。"""
        tag_set: Set[str] = {str(t).strip() for t in (tags or []) if str(t).strip()}
        now = time.time()
        with _TAGS_LOCK:
            if len(_SONG_TAGS) > TAG_MAX:
                for key in sorted(_SONG_TAGS, key=lambda k: _SONG_TAGS[k]["at"])[:TAG_MAX // 4]:
                    _SONG_TAGS.pop(key, None)
            for item in songs:
                sid = str(item.get("id") or "")
                if not sid:
                    continue
                entry = _SONG_TAGS.setdefault(sid, {"tags": set(), "artists": [], "at": now})
                entry["tags"] |= tag_set
                for artist in item.get("artist_list") or []:
                    artist = str(artist).strip()
                    if artist and artist not in entry["artists"]:
                        entry["artists"].append(artist)
                entry["at"] = now

    @staticmethod
    def tags_for(song_id: str) -> Dict[str, Any]:
        """读取某首歌登记过的标签与歌手；过期或未登记返回空。"""
        sid = str(song_id or "")
        with _TAGS_LOCK:
            entry = _SONG_TAGS.get(sid)
            if not entry:
                return {}
            if time.time() - float(entry.get("at") or 0) > TAG_TTL:
                _SONG_TAGS.pop(sid, None)
                return {}
            return {"tags": sorted(entry.get("tags") or []),
                    "artists": list(entry.get("artists") or [])}

    def clear_cache(self) -> None:
        """权重变化后清空推荐缓存，下一次请求立即生效。"""
        with self._lock:
            self._cache.clear()

    def _pref_songs(self, genres: List[str], languages: List[str],
                    weights: Optional[Dict[str, Any]] = None,
                    artists: Optional[List[str]] = None,
                    nonce: int = 0) -> List[Dict[str, Any]]:
        """按「曲风 / 语言 / 歌手」权重拼关键词检索推荐歌曲。

        - 权重高的标签排前面，并重复检索更多页（offset 不同），自然占据更多席位；
        - nonce 每次「换一批」自增，用于轮换关键词顺序与搜索页码，保证结果真的会变。
        """
        weights = weights or {}
        genres = sorted(genres, key=lambda g: -self._weight_of(g, "genres", weights))[:4]
        languages = sorted(languages, key=lambda l: -self._weight_of(l, "languages", weights))[:3]
        artists = [str(a).strip() for a in (artists or []) if str(a).strip()][:4]

        # (关键词, 标签集合, 权重)
        plans: List[tuple] = []
        for lang in languages[:3]:
            for genre in genres[:2]:
                weight = (self._weight_of(lang, "languages", weights)
                          + self._weight_of(genre, "genres", weights)) / 2
                plans.append((f"{lang} {genre}", {lang, genre}, weight))
        for genre in genres[:3]:
            plans.append((genre, {genre}, self._weight_of(genre, "genres", weights)))
        for lang in languages[:2]:
            plans.append((lang, {lang}, self._weight_of(lang, "languages", weights)))
        for artist in artists:
            plans.append((artist, set(), self._weight_of(artist, "artists", weights)))
        if not plans:
            return []

        # 权重 1.6 / 3.2 以上分别多搜一页、两页，让偏好更集中
        expanded: List[tuple] = []
        for index, (keyword, tags, weight) in enumerate(plans):
            repeat = 1 + (1 if weight >= 1.6 else 0) + (1 if weight >= 3.2 else 0)
            for rep in range(repeat):
                expanded.append((keyword, tags, index, rep))
        # 每批从不同位置开始，避免总拿前几个关键词的结果
        shift = (nonce * 3) % max(1, len(expanded))
        expanded = expanded[shift:] + expanded[:shift]

        base = (nonce * 8) % 48
        jobs = []
        for keyword, tags, index, rep in expanded[:9]:
            offset = (base + rep * 8 + (index % 3) * 4) % 60
            jobs.append((keyword, tags, offset))
        # 并发搜索：9 个关键词串行约 2 秒，并发后通常 1 秒内
        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(
                lambda job: (job[1], self._search_songs(job[0], 8, job[2])), jobs
            ))
        songs: List[Dict[str, Any]] = []
        seen = set()
        for tags, items in results:
            for item in items:
                sid = str(item.get("id") or "")
                if sid and sid not in seen:
                    seen.add(sid)
                    songs.append(item)
                    self._remember([item], tags)
            if len(songs) >= 24:
                break
        return songs[:24]

    @staticmethod
    def _rotate_order(songs: List[Dict[str, Any]], pref_count: int,
                      nonce: int) -> List[Dict[str, Any]]:
        """按 nonce 打乱顺序：换一批时顶部歌曲也会变，而不是固定前 12 首新歌。"""
        if not songs:
            return songs
        rng = random.Random(7919 * (int(nonce) + 1))
        cut = min(max(0, int(pref_count)), len(songs))
        head, tail = songs[:cut], songs[cut:]
        if len(head) > 1:
            rng.shuffle(head)
        if len(tail) > 1:
            rng.shuffle(tail)
        return head + tail

    @staticmethod
    def _weight_summary(weights: Dict[str, Any], genres: List[str],
                        languages: List[str], artists: List[str]) -> Dict[str, Any]:
        """整理出给前端展示的权重摘要（只含当前生效的标签 / 歌手）。"""
        items = []
        for name in genres:
            items.append({"name": name, "kind": "genre",
                          "weight": Discovery._weight_of(name, "genres", weights)})
        for name in languages:
            items.append({"name": name, "kind": "language",
                          "weight": Discovery._weight_of(name, "languages", weights)})
        for name in artists:
            items.append({"name": name, "kind": "artist",
                          "weight": Discovery._weight_of(name, "artists", weights)})
        items.sort(key=lambda x: -x["weight"])
        return {
            "top": items[:8],
            "genres": {name: Discovery._weight_of(name, "genres", weights) for name in genres},
            "languages": {name: Discovery._weight_of(name, "languages", weights) for name in languages},
            "artists": {name: Discovery._weight_of(name, "artists", weights) for name in artists},
        }

    def overview(self, force: bool = False,
                 prefs: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        prefs = prefs or {}
        genres = [str(x) for x in prefs.get("genres") or [] if str(x).strip()]
        languages = [str(x) for x in prefs.get("languages") or [] if str(x).strip()]
        weights = prefs.get("weights") or {}
        weight_artists = sorted(
            (weights.get("artists") or {}).items(), key=lambda kv: -float(kv[1] or 0)
        )
        artists = [name for name, value in weight_artists[:4] if float(value or 0) > 1.05]
        personalized = bool(genres or languages or artists)
        signature = json.dumps(weights, sort_keys=True, ensure_ascii=False)
        cache_key = "overview"
        if personalized:
            cache_key += ":" + "|".join(sorted(genres)) + "/" + "|".join(sorted(languages))
            cache_key += ":" + "|".join(artists) + ":" + signature

        if force:
            with self._lock:
                self._nonce += 1
        with self._lock:
            nonce = self._nonce

        def produce():
            with ThreadPoolExecutor(max_workers=5) as pool:
                f_new = pool.submit(self._new_songs)
                f_hot = pool.submit(self._hot_songs, nonce)
                f_charts = pool.submit(self._charts)
                f_albums = pool.submit(self._new_albums)
                f_pref = (pool.submit(self._pref_songs, genres, languages,
                                      weights, artists, nonce)
                          if personalized else None)
                new_songs, hot_songs = f_new.result(), f_hot.result()
                charts, albums = f_charts.result(), f_albums.result()
                pref_songs = f_pref.result() if f_pref is not None else []
                # 大盘歌曲也登记歌手，点击任意推荐歌曲都能学习
                self._remember(new_songs + hot_songs)

            songs: List[Dict[str, Any]] = []
            seen = set()
            for item in pref_songs + new_songs + hot_songs:
                sid = str(item.get("id") or "")
                if sid and sid not in seen:
                    seen.add(sid)
                    songs.append(item)
            if not songs:
                songs = self._fallback_songs()
            songs = self._rotate_order(songs, len(pref_songs), nonce)

            ranked_genres = sorted(genres, key=lambda g: -self._weight_of(g, "genres", weights))
            ranked_languages = sorted(languages, key=lambda l: -self._weight_of(l, "languages", weights))
            summary = self._weight_summary(weights, ranked_genres, ranked_languages, artists)
            boosted = [item for item in summary["top"] if item["weight"] > 1.05]
            dynamic = bool(boosted)
            based_on = ranked_genres + ranked_languages + artists
            if dynamic:
                note = ("已按你在推荐页的播放 / 收藏 / 下载调整权重："
                        + " / ".join(f"{x['name']} ×{x['weight']:.1f}" for x in boosted[:5]) + "。")
            elif personalized:
                note = ("按你选择的偏好（" + " / ".join(genres + languages) + "）从公开曲库检索，"
                        "不读取 Cookie，也不建立账号画像")
            else:
                note = ("推荐来自公开榜单与新歌；在本页播放 / 收藏会逐渐调整歌手权重，"
                        "不读取 Cookie，也不关联账号")
            return {
                "songs": songs[:24],
                "new_songs": new_songs[:12],
                "hot_songs": hot_songs[:12],
                "charts": charts,
                "albums": albums,
                "updated": time.strftime("%Y-%m-%d %H:%M"),
                "personalized": personalized,
                "dynamic": dynamic,
                "based_on": based_on,
                "weights": summary,
                "note": note,
            }

        data = self._cached(cache_key, produce, force) or {}
        return data or {
            "songs": [], "new_songs": [], "hot_songs": [], "charts": FALLBACK_CHARTS,
            "albums": [], "updated": time.strftime("%Y-%m-%d %H:%M"), "note": "暂时无法获取推荐",
        }

    def chart(self, chart_id: str, force: bool = False) -> Dict[str, Any]:
        chart_id = str(chart_id or "").strip()
        if not chart_id.isdigit():
            return {"tracks": [], "title": "", "cover": ""}

        def produce():
            try:
                data = _get(PLAYLIST_API, {"id": int(chart_id), "n": 100}, timeout=18)
            except Exception:
                return {}
            result = data.get("result") or {}
            tracks = [normalize_song(item, i) for i, item in enumerate(result.get("tracks") or [])]
            tracks = [t for t in tracks if t.get("id")]
            self._remember(tracks)
            return {
                "id": chart_id,
                "title": result.get("name") or "榜单",
                "cover": result.get("coverImgUrl") or "",
                "description": result.get("description") or "公开榜单 · 可直接播放或批量下载",
                "tracks": tracks[:100],
            }

        return self._cached("chart:" + chart_id, produce, force) or {
            "id": chart_id, "title": "", "cover": "", "description": "", "tracks": [],
        }


discovery = Discovery()
