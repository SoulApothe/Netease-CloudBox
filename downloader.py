"""云音盒子 · 下载引擎

职责：
1. NeteaseService：对 music_api 的高层封装（搜索 / 解析 / 批量详情 / 批量直链 / 封面 / 歌词）
2. download_track：单曲下载 + 写入元数据 + 封面 + 歌词（纯 Python，无需 ffmpeg）
3. BatchManager：专辑 / 歌单 / 自选歌曲的并发批量下载任务（进度、取消、跳过、重试）
"""
from __future__ import annotations

import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

import tags as tagger
from config import (
    BASE_DIR, QUALITY_VALUES, build_filename, format_size, output_dir,
    safe_filename, settings,
)
from music_api import APIException, NeteaseAPI, load_cookies

EXT_MAP = {
    "mp3": ".mp3", "flac": ".flac", "m4a": ".m4a",
    "mp4": ".mp4", "ogg": ".ogg", "opus": ".opus", "aac": ".m4a",
}
COVER_CACHE_LIMIT = 240


def extension_for(info: Dict[str, Any]) -> str:
    api_type = str(info.get("type") or "").lower()
    if api_type in EXT_MAP:
        return EXT_MAP[api_type]
    url = str(info.get("url") or "").lower()
    for ext in (".flac", ".mp3", ".m4a", ".mp4", ".ogg", ".opus"):
        if ext in url:
            return ext
    # 高码率一般是 flac，普通码率是 mp3
    try:
        return ".flac" if int(info.get("br") or 0) >= 700000 else ".mp3"
    except (TypeError, ValueError):
        return ".mp3"


def cover_url(url: str, size: int = 800) -> str:
    if not url:
        return ""
    if "param=" in url:
        return re.sub(r"param=\d+y\d+", f"param={size}y{size}", url)
    return url + ("&" if "?" in url else "?") + f"param={size}y{size}"


def strip_lrc_timecodes(lrc: str) -> str:
    """把 LRC 时间轴去掉，得到适合写入标签的纯歌词文本。"""
    lines = []
    for raw in (lrc or "").splitlines():
        line = re.sub(r"\[\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?\]", "", raw).strip()
        if line and not line.startswith(("作词", "作曲", "编曲", "制作人")):
            if not line.lower().startswith(("lyric", "lrc")):
                lines.append(line)
    return "\n".join(lines).strip()


def merge_translation(lrc: str, tlyric: str) -> str:
    """把翻译歌词按时间轴合并进原 LRC（用于 .lrc 文件）。"""
    if not tlyric or not tlyric.strip():
        return lrc or ""
    trans: Dict[str, str] = {}
    for line in tlyric.splitlines():
        match = re.match(r"\[(\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?)\](.+)", line.strip())
        if match:
            key = match.group(1).split(".")[0].split(":")[0] + ":" + match.group(1).split(":")[1][:2]
            trans.setdefault(key, match.group(2).strip())
    merged = []
    for line in (lrc or "").splitlines():
        match = re.match(r"\[(\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?)\](.+)", line.strip())
        if match:
            key = match.group(1).split(".")[0].split(":")[0] + ":" + match.group(1).split(":")[1][:2]
            extra = trans.get(key)
            if extra and extra != match.group(2).strip():
                line = f"{match.group(0)}  ({extra})"
        merged.append(line)
    return "\n".join(merged)


class NeteaseService:
    """桌面版业务层：所有网络请求都收敛到这里，方便统一容错。"""

    def __init__(self) -> None:
        self.api = NeteaseAPI()
        self._cover_cache: Dict[str, Tuple[bytes, str]] = {}
        self._cover_lock = threading.Lock()

    @property
    def cookies(self) -> Dict[str, str]:
        return load_cookies()

    # ---------- 查询 ----------

    def search(self, keyword: str, limit: int = 30, search_type: int = 1,
               offset: int = 0) -> List[Dict[str, Any]]:
        limit = max(1, min(100, int(limit)))
        return self.api.search_music(keyword, self.cookies, limit, search_type, offset)

    def song(self, song_id: str, level: str = "lossless") -> Dict[str, Any]:
        """单曲解析：详情 + 直链 + 歌词，供界面展示。"""
        details = self.api.get_song_details([song_id], self.cookies)
        raw = details.get(str(song_id))
        if not raw:
            raise APIException("未找到该歌曲，请检查 ID / 链接是否正确")
        track = self.api.normalize_song(raw, 0, 1)
        url_map = self.url_map([song_id], level)
        info = url_map.get(str(song_id), {})
        track.update({
            "quality": level,
            "download_url": info.get("url", ""),
            "file_type": info.get("type") or "",
            "file_size": info.get("size") or 0,
            "file_size_text": format_size(info.get("size") or 0),
            "bitrate": info.get("br") or 0,
            "available": bool(info.get("url")),
            "reason": info.get("reason", ""),
            "level": info.get("level") or level,
        })
        try:
            lyric = self.api.get_lyric(int(song_id), self.cookies)
            track["lyric"] = (lyric.get("lrc") or {}).get("lyric", "")
            track["tlyric"] = (lyric.get("tlyric") or {}).get("lyric", "")
        except Exception:
            track["lyric"] = track["tlyric"] = ""
        return track

    def playlist(self, playlist_id: str) -> Dict[str, Any]:
        return self.api.get_playlist_detail(playlist_id, self.cookies)

    def album(self, album_id: str) -> Dict[str, Any]:
        return self.api.get_album_detail(album_id, self.cookies)

    def resolve(self, kind: str, resource_id: str) -> Dict[str, Any]:
        """按类型解析歌单 / 专辑，统一返回 {title, cover, subtitle, tracks}。"""
        if kind == "album":
            data = self.album(resource_id)
            return {
                "kind": "album",
                "title": data.get("name", ""),
                "cover": data.get("coverImgUrl", ""),
                "subtitle": data.get("artist", ""),
                "description": data.get("description", ""),
                "year": data.get("year", ""),
                "tracks": data.get("tracks", []),
            }
        data = self.playlist(resource_id)
        return {
            "kind": "playlist",
            "title": data.get("name", ""),
            "cover": data.get("coverImgUrl", ""),
            "subtitle": f"by {data.get('creator', '')}",
            "description": data.get("description", ""),
            "year": "",
            "tracks": data.get("tracks", []),
        }

    def details(self, ids: List[str]) -> Dict[str, Dict[str, Any]]:
        """批量补全歌曲元数据（音轨号、专辑、发行年份、封面）。"""
        raw = self.api.get_song_details(ids, self.cookies)
        return {
            str(sid): self.api.normalize_song(raw_song, index, len(ids))
            for index, (sid, raw_song) in enumerate(raw.items())
        }

    def url_map(self, ids: List[str], level: str, batch_size: int = 50) -> Dict[str, Dict[str, Any]]:
        """批量取下载直链，返回 {id: {url, type, size, br, level, reason}}。"""
        level = level if level in QUALITY_VALUES else "lossless"
        result: Dict[str, Dict[str, Any]] = {}
        unique = [str(i) for i in dict.fromkeys(str(x) for x in ids if str(x).strip())]
        for start in range(0, len(unique), batch_size):
            chunk = unique[start:start + batch_size]
            try:
                response = self.api.get_song_url([int(x) for x in chunk], level, self.cookies)
                for item in response.get("data") or []:
                    sid = str(item.get("id"))
                    url = item.get("url") or ""
                    reason = "" if url else self._url_failure_reason(item, level)
                    result[sid] = {
                        "url": url,
                        "type": (item.get("type") or "").lower(),
                        "size": item.get("size") or 0,
                        "br": item.get("br") or 0,
                        "level": item.get("level") or level,
                        "code": item.get("code"),
                        "reason": reason,
                    }
            except Exception as exc:  # noqa: BLE001
                for sid in chunk:
                    result.setdefault(sid, {
                        "url": "", "type": "", "size": 0, "br": 0,
                        "level": level, "code": -1,
                        "reason": f"获取直链失败: {exc}",
                    })
        for sid in unique:
            result.setdefault(sid, {
                "url": "", "type": "", "size": 0, "br": 0,
                "level": level, "code": -1, "reason": "接口未返回该歌曲",
            })
        return result

    @staticmethod
    def _url_failure_reason(item: Dict[str, Any], level: str) -> str:
        code = item.get("code")
        if code == 404:
            return "无版权 / 歌曲下架"
        if code in (301, 302) or item.get("fee") == 1:
            return "该音质需要黑胶会员，请更换音质或登录会员账号"
        if not item.get("url"):
            return "当前音质不可用（版权或会员限制），试试标准 / 极高音质"
        return "无法获取下载链接"

    def lyric(self, song_id: str) -> Tuple[str, str]:
        try:
            data = self.api.get_lyric(int(song_id), self.cookies)
            return (data.get("lrc") or {}).get("lyric", ""), (data.get("tlyric") or {}).get("lyric", "")
        except Exception:
            return "", ""

    def fetch_cover(self, url: str, size: int = 800) -> Tuple[Optional[bytes], str]:
        """下载封面并缓存（同一专辑的封面只请求一次）。"""
        if not url:
            return None, "image/jpeg"
        target = cover_url(url, size)
        with self._cover_lock:
            cached = self._cover_cache.get(target)
        if cached:
            return cached
        try:
            response = requests.get(
                target,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                    "Referer": "https://music.163.com/",
                },
                timeout=12,
            )
            response.raise_for_status()
            data = response.content
            if len(data) < 100:
                return None, "image/jpeg"
            mime = tagger.guess_mime(data)
            with self._cover_lock:
                if len(self._cover_cache) >= COVER_CACHE_LIMIT:
                    self._cover_cache.clear()
                self._cover_cache[target] = (data, mime)
            return data, mime
        except Exception:
            return None, "image/jpeg"

    def profile(self) -> Dict[str, Any]:
        from music_api import QRLoginManager
        try:
            return QRLoginManager().get_user_profile(self.cookies)
        except Exception:
            return {"logged_in": False}


# ---------- 单曲下载 ----------

ProgressCB = Callable[[int, int], None]


def download_track(
    service: NeteaseService,
    track: Dict[str, Any],
    url_info: Dict[str, Any],
    options: Dict[str, Any],
    progress_cb: Optional[ProgressCB] = None,
    cancel_event: Optional[threading.Event] = None,
) -> Dict[str, Any]:
    """下载一首歌 -> 写入元数据 -> 落盘到输出目录。

    返回 {status, file, size, fields, cover, lyrics, error}
    status: done / skipped / cancelled / failed
    """
    # 批量任务若开启「合并到一个文件夹」，_batch_dir 由 BatchManager 预先算好；
    # 单曲下载没有这个键，仍然落到用户设置的 output_dir。
    batch_dir = options.get("_batch_dir")
    out_dir = Path(batch_dir) if batch_dir else output_dir()
    quality = options.get("quality") or "lossless"
    skip_existing = bool(options.get("skip_existing", True))
    embed_lyrics = bool(options.get("embed_lyrics", True))
    save_lrc = bool(options.get("save_lrc", False))
    group_by_album = bool(options.get("group_by_album", False))
    # 「按专辑分目录」只对真正的批量任务生效；单曲下载始终直接落到输出目录
    try:
        batch_total = int(options.get("_batch_total") or 0)
    except (TypeError, ValueError):
        batch_total = 0
    if batch_total <= 1 or options.get("_batch_dir"):
        group_by_album = False
    cover_size = int(options.get("cover_size") or 800)

    if not url_info.get("url"):
        return {"status": "failed", "error": url_info.get("reason") or "无可用下载链接"}

    ext = extension_for(url_info)
    filename = build_filename(track, options, quality, ext)
    if group_by_album:
        album_dir = safe_filename(track.get("album") or "未知专辑", 80)
        target_dir = out_dir / album_dir
    else:
        target_dir = out_dir
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        target_dir = out_dir

    target = target_dir / filename
    if skip_existing and target.exists() and target.stat().st_size > 0:
        return {
            "status": "skipped", "file": str(target), "filename": target.name,
            "size": target.stat().st_size, "fields": [], "cover": False, "lyrics": False,
            "error": "文件已存在，已跳过",
        }

    # 临时文件保留真实扩展名，mutagen 才能识别格式
    temp = target_dir / (target.stem + ".part" + target.suffix)
    total = int(url_info.get("size") or 0)
    downloaded = 0
    last_report = 0.0

    try:
        # 网易云 CDN 直链带时效签名，偶发 403。三次策略：
        #   1) 常规头  2) 浏览器头（去掉 Referer）  3) 重新取一条新直链 + https
        base_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Referer": "https://music.163.com/",
        }
        browser_headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        }
        current_url = url_info["url"]
        last_error = ""
        succeeded = False

        for attempt in range(3):
            headers = base_headers if attempt == 0 else browser_headers
            if attempt == 2:
                try:
                    fresh = service.url_map([str(track.get("id"))], quality).get(
                        str(track.get("id")), {}
                    )
                    if fresh.get("url"):
                        current_url = fresh["url"]
                        url_info = {**url_info, **fresh}
                        total = int(fresh.get("size") or total or 0)
                except Exception:
                    pass
                current_url = current_url.replace("http://", "https://", 1)

            try:
                with requests.get(current_url, stream=True, timeout=(12, 60),
                                  headers=headers) as response:
                    if response.status_code >= 400:
                        last_error = f"HTTP {response.status_code}"
                        temp.unlink(missing_ok=True)
                        time.sleep(0.3)
                        continue
                    if not total:
                        try:
                            total = int(response.headers.get("Content-Length") or 0)
                        except (TypeError, ValueError):
                            total = 0
                    downloaded = 0
                    with open(temp, "wb") as handle:
                        for chunk in response.iter_content(chunk_size=64 * 1024):
                            if cancel_event and cancel_event.is_set():
                                handle.close()
                                temp.unlink(missing_ok=True)
                                return {"status": "cancelled", "error": "已取消"}
                            if not chunk:
                                continue
                            handle.write(chunk)
                            downloaded += len(chunk)
                            now = time.time()
                            if progress_cb and (now - last_report > 0.12 or downloaded == total):
                                progress_cb(downloaded, total)
                                last_report = now
                    if downloaded > 0:
                        succeeded = True
                        break
                    last_error = "服务器返回空文件"
            except requests.RequestException as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                temp.unlink(missing_ok=True)
            except OSError as exc:
                temp.unlink(missing_ok=True)
                return {"status": "failed", "error": f"写入文件失败: {exc}"}

        if not succeeded:
            return {"status": "failed", "error": f"网络错误: {last_error or '下载失败'}"}

        if cancel_event and cancel_event.is_set():
            temp.unlink(missing_ok=True)
            return {"status": "cancelled", "error": "已取消"}

        # ---- 元数据 ----
        artists = track.get("artist_list") or []
        if not artists and track.get("artists"):
            artists = [a.strip() for a in str(track["artists"]).replace("/", "、").split("、") if a.strip()]
        lrc_text, tlyric = "", ""
        if embed_lyrics or save_lrc:
            lrc_text, tlyric = service.lyric(str(track.get("id")))
        cover_bytes, cover_mime = service.fetch_cover(
            track.get("picUrl", ""), cover_size
        )
        info = tagger.TagInfo(
            title=str(track.get("name") or "未知歌曲"),
            artists=artists,
            album=str(track.get("album") or "未知专辑"),
            album_artist=artists[0] if artists else "",
            track_no=int(track.get("track_no") or 0),
            track_total=int(track.get("track_total") or 0),
            year=str(track.get("year") or ""),
            lyrics=strip_lrc_timecodes(lrc_text) if embed_lyrics else "",
            comment="Downloaded with 云音盒子 · Netease Cloud Music",
            source_id=str(track.get("id") or ""),
            cover=cover_bytes,
            cover_mime=cover_mime,
        )
        tag_result = tagger.write_tags(temp, info)

        os.replace(str(temp), str(target))

        if save_lrc and (lrc_text or tlyric):
            try:
                lrc_path = target.with_suffix(".lrc")
                lrc_path.write_text(merge_translation(lrc_text, tlyric), encoding="utf-8")
            except OSError:
                pass

        return {
            "status": "done",
            "file": str(target),
            "filename": target.name,
            "size": target.stat().st_size,
            "fields": tag_result.get("fields", []),
            "tag_ok": tag_result.get("ok", False),
            "tag_error": tag_result.get("error", ""),
            "cover": bool(cover_bytes) and "封面" in tag_result.get("fields", []),
            "lyrics": bool(info.lyrics) and "歌词" in tag_result.get("fields", []),
            "error": "",
        }
    except requests.RequestException as exc:
        temp.unlink(missing_ok=True)
        return {"status": "failed", "error": f"网络错误: {exc}"}
    except OSError as exc:
        temp.unlink(missing_ok=True)
        return {"status": "failed", "error": f"写入文件失败: {exc}"}
    except Exception as exc:  # noqa: BLE001
        temp.unlink(missing_ok=True)
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}


def download_one(service: NeteaseService, track: Dict[str, Any], quality: str,
                 options: Optional[Dict[str, Any]] = None,
                 progress_cb: Optional[ProgressCB] = None) -> Dict[str, Any]:
    """同步下载单曲（供 CLI / 测试 / 快速调用）。"""
    opts = {**settings.as_dict(), **(options or {})}
    opts["quality"] = quality
    url_info = service.url_map([str(track.get("id"))], quality).get(str(track.get("id")), {})
    details = service.details([str(track.get("id"))])
    merged = {**track, **(details.get(str(track.get("id"))) or {})}
    return download_track(service, merged, url_info, opts, progress_cb)


# ---------- 批量任务 ----------

class BatchManager:
    """并发批量下载任务管理（专辑 / 歌单 / 搜索结果 / 自选）。"""

    def __init__(self, service: NeteaseService) -> None:
        self.service = service
        self._lock = threading.Lock()
        self._jobs: Dict[str, Dict[str, Any]] = {}

    def start(self, title: str, kind: str, tracks: List[Dict[str, Any]],
              options: Dict[str, Any]) -> str:
        if not tracks:
            raise ValueError("下载列表为空")
        job_id = uuid.uuid4().hex[:12]
        items = []
        for raw in tracks:
            sid = str(raw.get("id") or "").strip()
            if not sid:
                continue
            artists = raw.get("artist_list") or []
            if not artists and raw.get("artists"):
                artists = [str(raw["artists"])]
            items.append({
                "id": sid,
                "name": str(raw.get("name") or "未知歌曲"),
                "artist": " / ".join(artists),
                "album": str(raw.get("album") or ""),
                "pic": raw.get("picUrl") or raw.get("pic") or "",
                "status": "pending",
                "progress": 0,
                "error": "",
                "file": "",
                "filename": "",
                "size": 0,
                "fields": [],
            })
        if not items:
            raise ValueError("没有有效的歌曲 ID")

        merged_options: Dict[str, Any] = {**settings.as_dict(), **options}
        merged_options["_batch_total"] = len(items)
        # 目录规则（v3.4）：
        # 1. 整张专辑 / 整张歌单 / 榜单 / 歌手 / 收藏 这类「内容型」批量下载，
        #    默认落到以内容名命名的子文件夹（如 music/叶惠美/）；
        # 2. 单曲下载直接放 music 根目录，不建文件夹；
        # 3. 勾选「合并到一个文件夹」时，任何任务（含单曲）都建子目录。
        folder_kinds = {"album", "playlist", "chart", "recommend", "artist",
                        "import", "favorites", "myplaylist"}
        batch_folder = ""
        effective_dir = output_dir()
        wants_folder = bool(merged_options.get("batch_in_folder"))
        if not wants_folder and len(items) > 1 and str(kind or "") in folder_kinds:
            wants_folder = True
        if wants_folder:
            folder = safe_filename(title or "批量下载", 80)
            try:
                effective_dir = effective_dir / folder
                effective_dir.mkdir(parents=True, exist_ok=True)
                merged_options["_batch_dir"] = str(effective_dir)
                # 已经在内容文件夹里，再按专辑分目录会变成两层嵌套
                merged_options["group_by_album"] = False
                batch_folder = folder
            except OSError:
                effective_dir = output_dir()
                merged_options.pop("_batch_dir", None)

        job = {
            "id": job_id,
            "title": title or "批量下载",
            "kind": kind or "manual",
            "status": "running",
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "total": len(items),
            "finished": 0,
            "success": 0,
            "failed": 0,
            "skipped": 0,
            "progress": 0,
            "bytes": 0,
            "quality": merged_options.get("quality", "lossless"),
            "output_dir": str(effective_dir),
            "batch_folder": batch_folder,
            "items": items,
            "_cancel": threading.Event(),
            "_tracks": tracks,
            "_options": merged_options,
            "message": "正在准备下载...",
        }
        with self._lock:
            self._jobs[job_id] = job
            # 只保留最近 20 个任务，避免长时间运行内存堆积
            if len(self._jobs) > 20:
                oldest = sorted(
                    (k for k, v in self._jobs.items() if v["status"] != "running"),
                    key=lambda k: self._jobs[k]["created_at"],
                )
                for key in oldest[:len(self._jobs) - 20]:
                    self._jobs.pop(key, None)
        threading.Thread(target=self._run, args=(job_id,), daemon=True).start()
        return job_id

    def _update(self, job_id: str, **patch) -> None:
        with self._lock:
            if job_id in self._jobs:
                self._jobs[job_id].update(patch)

    def _run(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
        if not job:
            return
        cancel: threading.Event = job["_cancel"]
        options: Dict[str, Any] = job["_options"]
        items: List[Dict[str, Any]] = job["items"]
        ids = [item["id"] for item in items]

        # 1) 批量补全元数据（音轨号 / 专辑 / 年份 / 封面）
        self._update(job_id, message="正在获取歌曲信息...")
        details = self.service.details(ids)
        by_id = {str(raw.get("id")): raw for raw in job["_tracks"] if raw.get("id")}
        full_tracks: Dict[str, Dict[str, Any]] = {}
        for index, sid in enumerate(ids):
            source = by_id.get(sid) or {}
            detail = details.get(sid) or {}
            merged = {**source, **detail}
            merged["id"] = sid
            merged.setdefault("name", items[index]["name"])
            merged["track_total"] = len(ids)
            if not merged.get("artist_list") and merged.get("artists"):
                merged["artist_list"] = [str(merged["artists"])]
            if detail.get("picUrl"):
                merged["picUrl"] = detail["picUrl"]
            full_tracks[sid] = merged

        # 2) 批量取直链
        self._update(job_id, message="正在获取下载链接...")
        url_map = self.service.url_map(ids, options.get("quality", "lossless"))
        for item in items:
            info = url_map.get(item["id"], {})
            if not info.get("url"):
                item["status"] = "failed"
                item["error"] = info.get("reason") or "无可用下载链接"

        pending = [item for item in items if item["status"] == "pending"]
        self._update(job_id, message=f"正在下载 0/{job['total']} 首...")

        workers = max(1, min(8, int(options.get("concurrency") or 3)))
        completed = 0
        total_count = job["total"]

        def work(item: Dict[str, Any]) -> Dict[str, Any]:
            if cancel.is_set():
                return {"status": "cancelled", "error": "已取消"}
            item["status"] = "running"

            def on_progress(done: int, total: int) -> None:
                item["progress"] = min(99, int(done / total * 100)) if total else 0

            return download_track(
                self.service, full_tracks[item["id"]], url_map[item["id"]],
                options, on_progress, cancel,
            )

        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(work, item): item for item in pending}
                for future in as_completed(futures):
                    item = futures[future]
                    try:
                        result = future.result()
                    except Exception as exc:  # noqa: BLE001
                        result = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
                    status = result.get("status", "failed")
                    item["status"] = status
                    item["error"] = result.get("error", "")
                    item["file"] = result.get("file", "")
                    item["filename"] = result.get("filename", "")
                    item["size"] = result.get("size", 0)
                    item["fields"] = result.get("fields", [])
                    item["progress"] = 100 if status in ("done", "skipped") else 0
                    with self._lock:
                        job["bytes"] += int(item["size"] or 0)
                    completed += 1
                    with self._lock:
                        job["finished"] = sum(
                            1 for x in items if x["status"] in
                            ("done", "failed", "skipped", "cancelled")
                        )
                        job["success"] = sum(1 for x in items if x["status"] == "done")
                        job["failed"] = sum(1 for x in items if x["status"] == "failed")
                        job["skipped"] = sum(1 for x in items if x["status"] == "skipped")
                        job["progress"] = int(
                            sum(x["progress"] for x in items) / max(1, total_count)
                        )
                        job["message"] = f"已完成 {job['finished']}/{total_count} 首"
        finally:
            with self._lock:
                if cancel.is_set():
                    job["status"] = "cancelled"
                    job["message"] = "任务已取消"
                else:
                    job["status"] = "done"
                    job["message"] = (
                        f"完成：{job['success']} 成功 · {job['skipped']} 跳过 · {job['failed']} 失败"
                    )
                job["progress"] = 100
                for item in items:
                    if item["status"] in ("pending", "running"):
                        item["status"] = "cancelled" if cancel.is_set() else "failed"
                        item["error"] = item["error"] or ("已取消" if cancel.is_set() else "未完成")

    def status(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            return {
                key: value for key, value in job.items()
                if not key.startswith("_")
            }

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job["status"] != "running":
                return False
            job["_cancel"].set()
            job["message"] = "正在取消..."
            return True

    def list_jobs(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [
                {"id": j["id"], "title": j["title"], "kind": j["kind"],
                 "status": j["status"], "total": j["total"], "progress": j["progress"],
                 "created_at": j["created_at"]}
                for j in sorted(self._jobs.values(), key=lambda x: x["created_at"], reverse=True)
            ]


service = NeteaseService()
batch_manager = BatchManager(service)
