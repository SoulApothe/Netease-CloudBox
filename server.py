"""云音盒子 · 桌面版本地服务（Flask）

只监听 127.0.0.1，由 desktop.py 启动原生窗口后加载；也可 `--server` 作为纯 Web 服务。
新 API 统一返回 {ok, data, message}；保留全部旧 API 路由以兼容原有文档 / 脚本。
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import quote, urlparse

import requests
from flask import (Flask, Response, jsonify, render_template, request, send_file,
                   stream_with_context)

from config import (
    APP_NAME, BASE_DIR, COOKIE_FILE, DEFAULTS, LOG_FILE, NAMING_SEPARATORS,
    PLAY_MODES, PLAY_QUALITY_OPTIONS, PLAY_QUALITY_VALUES,
    PREF_FEEDBACK_GAIN, PREF_WEIGHT_DEFAULT, PREF_WEIGHT_MAX, PREF_WEIGHT_MIN,
    PREFERENCE_GENRES, PREFERENCE_LANGUAGES, clean_pref_weights,
    QUALITY_OPTIONS, QUALITY_VALUES, SETTINGS_FILE, TEMPLATE_DIR,
    TRACK_DIGITS_OPTIONS, TRACK_SEPARATORS, VERSION, build_filename,
    detect_kind, extract_id, format_size, naming_sample, output_dir, settings,
)
from downloader import download_one, download_track, service, batch_manager
from music_api import (APIConstants, APIException, NeteaseAPI, load_cookies,
                       loads_loose, parse_cookie_string)
import tags as tagger
from accounts import AccountError, account_store, normalize_song as normalize_account_song
from discover import discovery
import importer as playlist_importer
import localmusic

app = Flask(__name__, template_folder=str(TEMPLATE_DIR), static_folder=None)
app.json.ensure_ascii = False
app.config["JSON_AS_ASCII"] = False

# 轻量日志：只写文件，不污染桌面窗口
logger = logging.getLogger("cloudbox")
logger.setLevel(logging.INFO)
if not logger.handlers:
    try:
        handler = logging.FileHandler(str(LOG_FILE), encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    except OSError:
        logger.addHandler(logging.NullHandler())

_profile_cache: Dict[str, Any] = {"at": 0.0, "data": {}}


def ok(data: Any = None, message: str = "success") -> Tuple[Response, int]:
    return jsonify({"ok": True, "data": data, "message": message}), 200


def fail(message: str, status: int = 400) -> Tuple[Response, int]:
    return jsonify({"ok": False, "data": None, "message": message}), status


def legacy(data: Any = None, message: str = "success", status: int = 200):
    return jsonify({
        "status": status, "success": status < 400, "message": message,
        "data": data,
    }), status


def body() -> Dict[str, Any]:
    if request.is_json:
        return request.get_json(silent=True) or {}
    return dict(request.form) or dict(request.args)


# ---------------- 本机档案（免登录，与网易云 Cookie 无关） ----------------

def account_token() -> str:
    """保留参数读取以兼容旧前端，但本机模式不再需要 token。"""
    return (request.headers.get("X-Account-Token") or "").strip()


def current_user() -> Dict[str, Any]:
    return account_store.local_user()


def need_user():
    """本机模式永远可用：返回 (本机档案, None)。"""
    return current_user(), None


def account_error(exc: Exception):
    if isinstance(exc, AccountError):
        return fail(str(exc), 400)
    logger.exception("account operation failed")
    return fail(f"操作失败: {exc}", 500)


# ---------------- 基础 ----------------

@app.route("/")
def index():
    return render_template("index.html", app_name=APP_NAME, version=VERSION)


@app.get("/api/state")
def api_state():
    cookies = load_cookies()
    account_user = current_user()
    cookie_file = Path(COOKIE_FILE)
    exists = cookie_file.is_file()
    has_music_u = bool(cookies.get("MUSIC_U"))
    updated = ""
    if exists:
        updated = time.strftime("%Y-%m-%d %H:%M", time.localtime(cookie_file.stat().st_mtime))

    # 账号信息查询较慢，缓存 60 秒
    profile = {"logged_in": False}
    if has_music_u:
        if time.time() - _profile_cache["at"] < 60:
            profile = _profile_cache["data"]
        else:
            try:
                profile = service.profile()
            except Exception:  # noqa: BLE001
                profile = {"logged_in": False}
            _profile_cache.update({"at": time.time(), "data": profile})

    return ok({
        "app": {
            "name": APP_NAME, "version": VERSION,
            "base_dir": str(BASE_DIR), "output_dir": str(output_dir()),
            "settings_file": str(SETTINGS_FILE), "cookie_file": str(COOKIE_FILE),
        },
        "cookie": {
            "exists": exists, "valid": has_music_u, "count": len(cookies),
            "updated_at": updated, "profile": profile,
        },
        "deps": {
            "mutagen": True,
            "ffmpeg": shutil.which("ffmpeg"),
            "webview": _has_webview(),
            "qrcode": _has_qrcode(),
            "note": "元数据由内置 mutagen 写入，无需安装 ffmpeg",
        },
        "settings": settings.as_dict(),
        "qualities": QUALITY_OPTIONS,
        "play_qualities": PLAY_QUALITY_OPTIONS,
        "play_modes": PLAY_MODES,
        "preference_options": {
            "genres": PREFERENCE_GENRES,
            "languages": PREFERENCE_LANGUAGES,
        },
        "naming_separators": NAMING_SEPARATORS,
        "track_separators": TRACK_SEPARATORS,
        "track_digits_options": TRACK_DIGITS_OPTIONS,
        "account": {
            "logged_in": True, "local": True,
            "user": account_user,
            "stats": account_store.stats(account_user["id"]),
            "db_file": str(account_store.path),
            "note": "免登录本机档案：在线收藏 / 歌手收藏 / 本地收藏 / 自建歌单都只保存在本机",
        },
    })


@app.post("/api/naming/preview")
def api_naming_preview():
    """按当前设置（可带未保存的前端改动）生成文件名示例。"""
    payload = body()
    options = settings.as_dict()
    options.update({k: v for k, v in payload.items() if k in DEFAULTS})
    quality = str(payload.get("quality") or options.get("quality") or "lossless")
    if quality not in QUALITY_VALUES:
        quality = "lossless"
    samples = naming_sample(payload.get("sample") if isinstance(payload.get("sample"), dict) else None)
    names = [build_filename(item, options, quality, ".flac") for item in samples]
    return ok({"names": names, "mode": options.get("naming_mode") or "simple"})


def _has_webview() -> bool:
    try:
        import webview  # noqa: F401
        return True
    except ImportError:
        return False


def _has_qrcode() -> bool:
    try:
        import qrcode  # noqa: F401
        return True
    except ImportError:
        return False


@app.post("/api/settings")
def api_settings():
    patch = {k: v for k, v in body().items() if k in DEFAULTS}
    saved = settings.update(patch)
    return ok({"settings": saved, "output_dir": str(output_dir())}, "设置已保存")


@app.post("/api/folder/open")
def api_folder_open():
    payload = body()
    raw = str(payload.get("path") or "").strip()
    target = Path(raw).expanduser() if raw else output_dir()
    if not target.is_dir():
        target = output_dir()
    try:
        if sys.platform == "win32":
            os.startfile(str(target))  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target)])
        return ok(str(target), "已打开目录")
    except Exception as exc:  # noqa: BLE001
        return fail(f"打开目录失败: {exc}")


@app.post("/api/folder/reveal")
def api_folder_reveal():
    """在资源管理器中定位已下载的文件。"""
    path = Path(str(body().get("path") or ""))
    if not path.is_file():
        return fail("文件不存在")
    try:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", str(path)])
        else:
            return ok(str(path.parent), "已定位目录")
        return ok(str(path), "已定位文件")
    except Exception as exc:  # noqa: BLE001
        return fail(f"定位失败: {exc}")


@app.get("/api/cover")
def api_cover():
    """封面代理：解决外链防盗链 / 混合内容，并复用下载器的内存缓存。"""
    url = request.args.get("url", "")
    host = urlparse(url).hostname or ""
    if not host.endswith((".music.126.net", ".music.163.com")):
        return Response(status=404)
    try:
        size = int(request.args.get("size") or 300)
    except ValueError:
        size = 300
    data, mime = service.fetch_cover(url, size)
    if not data:
        return Response(status=404)
    response = Response(data, mimetype=mime)
    response.headers["Cache-Control"] = "public, max-age=86400"
    return response


@app.post("/api/inspect")
def api_inspect():
    path = str(body().get("path") or "")
    if not path:
        return fail("缺少文件路径")
    return ok(tagger.inspect_tags(path))


# ---------------- 推荐（公开数据 + 本机动态权重，不使用 Cookie） ----------------

@app.get("/api/recommend")
def api_recommend():
    """首页推荐：新歌、热歌、公开榜单、新碟。请求不带 cookie.txt。

    「换一批」会轮换公开榜单窗口与检索页码，保证结果真的变化。
    若用户填过偏好或有动态权重，会按「曲风 / 语言 / 歌手」权重排序检索关键词，
    依旧不读取 Cookie、不关联任何账号。
    """
    force = str(request.args.get("refresh") or "") in ("1", "true", "yes")
    prefs = {
        "genres": settings.get("pref_genres") or [],
        "languages": settings.get("pref_languages") or [],
        "weights": settings.get("pref_weights") or {},
    } if settings.get("pref_done") else None
    try:
        return ok(discovery.overview(force, prefs))
    except Exception as exc:  # noqa: BLE001
        logger.exception("recommend failed")
        return fail(f"获取推荐失败: {exc}", 500)


# ---------------- 动态偏好权重（本机设置，不上传） ----------------

_feedback_lock = threading.Lock()
_feedback_seen: Dict[str, float] = {}


def _bump_weight(weights: Dict[str, Dict[str, float]], bucket: str,
                 name: str, gain: float) -> None:
    name = str(name or "").strip()[:80]
    if not name:
        return
    bucket_map = weights.setdefault(bucket, {})
    current = float(bucket_map.get(name) or PREF_WEIGHT_DEFAULT)
    # 越接近上限增量越小，避免单一标签很快顶到 6.0 后失去区分度
    room = max(0.05, 1 - (current - PREF_WEIGHT_MIN) / (PREF_WEIGHT_MAX - PREF_WEIGHT_MIN))
    bucket_map[name] = round(
        max(PREF_WEIGHT_MIN, min(PREF_WEIGHT_MAX, current + float(gain) * room)), 3
    )


def _pref_summary(weights: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    data = (clean_pref_weights(weights) if weights is not None
            else clean_pref_weights(settings.get("pref_weights") or {}))

    def top(bucket: str, limit: int = 4) -> list:
        items = sorted((data.get(bucket) or {}).items(), key=lambda kv: -float(kv[1] or 0))
        return [{"name": name, "weight": round(float(value), 2)}
                for name, value in items[:limit] if float(value or 0) > 1.0]

    return {
        "enabled": bool(settings.get("pref_done")),
        "genres": top("genres"),
        "languages": top("languages"),
        "artists": top("artists"),
    }


@app.get("/api/preferences")
def api_preferences():
    """当前生效的动态权重摘要（供设置页 / 推荐页展示）。"""
    return ok(_pref_summary())


@app.post("/api/preferences/feedback")
def api_preferences_feedback():
    """推荐页播放 / 收藏 / 下载时调用：按歌曲标签累加权重。

    权重只写本机 settings.json，随后清空推荐缓存，让下一批立即体现偏好。
    """
    payload = body()
    song_id = extract_id(str(payload.get("song_id") or ""))
    action = str(payload.get("action") or "play").strip().lower()
    if action not in PREF_FEEDBACK_GAIN:
        action = "play"
    if not song_id or not song_id.isdigit():
        return fail("缺少有效的歌曲 ID", 400)

    now = time.time()
    token = f"{song_id}:{action}"
    with _feedback_lock:
        if now - float(_feedback_seen.get(token) or 0) < 20:
            return ok(_pref_summary(), "这次点击刚刚记录过了")
        _feedback_seen[token] = now
        if len(_feedback_seen) > 2000:
            _feedback_seen.clear()

    entry = discovery.tags_for(song_id)
    tags = list(entry.get("tags") or [])
    artists = [str(a).strip() for a in entry.get("artists") or [] if str(a).strip()]
    if not artists:
        # 队列里的老歌可能没登记过标签，用匿名详情补一次歌手
        try:
            raw = service.api.get_song_details([song_id]) or {}
            song = raw.get(str(song_id)) or {}
            artists = [str((item or {}).get("name") or "").strip()
                       for item in (song.get("ar") or song.get("artists") or [])]
            artists = [name for name in artists if name]
        except Exception:  # noqa: BLE001
            artists = []
    if not tags and not artists:
        return ok(_pref_summary(), "这首歌没有可学习的标签，已跳过")

    gain = float(PREF_FEEDBACK_GAIN[action])
    with _feedback_lock:
        weights = clean_pref_weights(settings.get("pref_weights") or {})
        for name in tags:
            bucket = "languages" if name in PREFERENCE_LANGUAGES else "genres"
            _bump_weight(weights, bucket, name, gain)
        for name in artists:
            _bump_weight(weights, "artists", name, gain * 0.8)
        settings.update({"pref_weights": weights})
    discovery.clear_cache()
    return ok(_pref_summary(weights), "已根据这次点击调整推荐权重")


@app.post("/api/preferences/reset")
def api_preferences_reset():
    """清空点击累积的权重，回到「所选偏好各 1.0」的初始状态。"""
    genres = [str(x) for x in settings.get("pref_genres") or []]
    languages = [str(x) for x in settings.get("pref_languages") or []]
    weights = {
        "genres": {name: PREF_WEIGHT_DEFAULT for name in genres},
        "languages": {name: PREF_WEIGHT_DEFAULT for name in languages},
        "artists": {},
    }
    settings.update({"pref_weights": weights})
    discovery.clear_cache()
    return ok(_pref_summary(weights), "已重置动态推荐权重")


@app.get("/api/recommend/chart")
def api_recommend_chart():
    """展开某个公开榜单的曲目。"""
    chart_id = extract_id(request.args.get("id") or "")
    if not chart_id:
        return fail("缺少榜单 ID")
    force = str(request.args.get("refresh") or "") in ("1", "true", "yes")
    try:
        data = discovery.chart(chart_id, force)
    except Exception as exc:  # noqa: BLE001
        return fail(f"获取榜单失败: {exc}", 500)
    if not data.get("tracks"):
        return fail("榜单暂时不可用，请稍后重试", 404)
    return ok(data)


# ---------------- 在线播放（Cookie 优先音频流代理，支持 Range） ----------------

# 匿名仍可播放的兜底档位
STREAM_LEVELS = ("exhigh", "standard")
# 可尝试的音质梯度：请求档位拿不到就往下降，保证尽量出声
PLAY_LADDER = ("jymaster", "hires", "lossless", "exhigh", "standard")

# 直链后缀 / 接口 type -> 浏览器可识别的 MIME
AUDIO_MIME = {
    "mp3": "audio/mpeg",
    "flac": "audio/flac",
    "m4a": "audio/mp4",
    "mp4": "audio/mp4",
    "aac": "audio/mp4",
    "ogg": "audio/ogg",
    "opus": "audio/ogg",
    "wav": "audio/wav",
}


def _play_levels(level: str) -> list:
    """把用户选择的播放音质展开成「从该档位往下」的降级链。"""
    ladder = [item for item in PLAY_LADDER if item in PLAY_QUALITY_VALUES]
    if level in ladder:
        return ladder[ladder.index(level):]
    return list(STREAM_LEVELS)


def _resolve_stream(song_id: str, level: str = "") -> Dict[str, Any]:
    """解析可播放直链，返回 {url, level, source, type, size, br}。

    优先带 cookie.txt 里的登录态请求：会员歌曲、无损 / Hi-Res 只有带
    Cookie 才拿得到直链（否则 data[0].url 为空，表现为「会员歌不能播」）。
    Cookie 失效或该曲无权限时，再退回匿名公开音质，尽量让用户听到声音。
    """
    cookies = load_cookies()
    attempts = []
    if cookies.get("MUSIC_U"):
        attempts.append((cookies, _play_levels(level), "cookie"))
    anon_levels = [item for item in STREAM_LEVELS if item in PLAY_QUALITY_VALUES]
    attempts.append(({}, anon_levels or list(STREAM_LEVELS), "anonymous"))

    for cred, levels, source in attempts:
        for item_level in levels:
            try:
                response = service.api.get_song_url([int(song_id)], item_level, cred)
                for item in response.get("data") or []:
                    if str(item.get("id")) == str(song_id) and item.get("url"):
                        return {
                            "url": str(item["url"]),
                            "level": item.get("level") or item_level,
                            "source": source,
                            "type": (item.get("type") or "").lower(),
                            "size": int(item.get("size") or 0),
                            "br": int(item.get("br") or 0),
                        }
            except Exception:  # noqa: BLE001
                continue
    return {}


@app.get("/api/stream")
def api_stream():
    """把网易云直链代理成本地音频流，供 <audio> 播放，支持拖动进度（206）。

    直链解析优先使用本机 Cookie（可播会员歌曲、可用无损 / Hi-Res），
    Cookie 不可用时退回匿名公开音质；具体档位由「播放音质」设置（?level=）决定。
    """
    song_id = extract_id(request.args.get("id") or "")
    if not song_id or not song_id.isdigit():
        return fail("缺少有效的歌曲 ID", 400)

    level = str(request.args.get("level") or settings.get("play_quality") or "")
    info = _resolve_stream(song_id, level)
    url = info.get("url") or ""
    if not url:
        return fail("该歌曲暂无可播放音质：可能受版权限制，或 cookie.txt 已过期", 404)

    base_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Referer": "https://music.163.com/",
        "Accept": "*/*",
    }
    browser_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
    }
    range_header = request.headers.get("Range")
    upstream = None
    for attempt in range(3):
        headers = dict(base_headers if attempt == 0 else browser_headers)
        if range_header:
            headers["Range"] = range_header
        target = url.replace("http://", "https://", 1) if attempt == 2 else url
        if attempt == 2:
            fresh = _resolve_stream(song_id, level)
            if fresh.get("url"):
                target = str(fresh["url"]).replace("http://", "https://", 1)
                info = fresh
        try:
            response = requests.get(target, headers=headers, stream=True,
                                    timeout=(10, 45), allow_redirects=True)
            if response.status_code < 400:
                upstream = response
                break
            response.close()
        except requests.RequestException:
            continue
    if upstream is None:
        return fail("音频源暂时不可用，请稍后重试", 502)

    def generate():
        try:
            for chunk in upstream.iter_content(chunk_size=64 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    ctype = (upstream.headers.get("Content-Type") or "").split(";")[0].strip()
    # 会员无损直链常被上游标成 audio/mpeg，实际是 FLAC，按真实格式纠正
    suffix = urlparse(url).path.rsplit(".", 1)[-1].lower()
    mime = AUDIO_MIME.get(str(info.get("type") or "").lower()) or AUDIO_MIME.get(suffix)
    ctype = mime or ctype or "audio/mpeg"
    resp = Response(stream_with_context(generate()), status=upstream.status_code,
                    content_type=ctype)
    resp.headers["Accept-Ranges"] = "bytes"
    for key in ("Content-Length", "Content-Range", "ETag", "Last-Modified"):
        value = upstream.headers.get(key)
        if value:
            resp.headers[key] = value
    resp.headers["Cache-Control"] = "private, max-age=3600"
    resp.headers["X-Stream-Level"] = str(info.get("level") or "")
    resp.headers["X-Stream-Source"] = str(info.get("source") or "")
    return resp


@app.get("/api/stream/info")
def api_stream_info():
    """返回某首歌能否播放、实际音质档位与来源（会员 Cookie / 匿名公开）。

    前端用来在播放器上标注「会员 · 无损」等信息，也用来在播放失败时
    给出准确原因，而不是笼统的「无法播放」。
    """
    song_id = extract_id(request.args.get("id") or "")
    if not song_id or not song_id.isdigit():
        return fail("缺少有效的歌曲 ID", 400)

    level = str(request.args.get("level") or settings.get("play_quality") or "")
    cookies = load_cookies()
    has_login = bool(cookies.get("MUSIC_U"))
    info = _resolve_stream(song_id, level)
    if not info.get("url"):
        message = "该歌曲暂无可播放音质"
        if has_login:
            message += "：Cookie 可能已过期，或该曲受版权 / 下架限制"
        else:
            message += "：它可能是会员专享曲目，配置会员 Cookie 后可播放"
        return fail(message, 404)

    return ok({
        "level": info.get("level"),
        "source": info.get("source"),
        "vip": info.get("source") == "cookie",
        "type": info.get("type"),
        "size": info.get("size"),
        "br": info.get("br"),
        "cookie": has_login,
    })


# ---------------- 本机档案 / 收藏 / 自建歌单（全部免登录） ----------------

def _local_profile() -> Dict[str, Any]:
    user = current_user()
    uid = user["id"]
    return {
        "logged_in": True, "local": True, "user": user,
        "stats": account_store.stats(uid),
        "favorite_ids": sorted(account_store.favorite_ids(uid)),
        "favorite_artist_ids": sorted(account_store.favorite_artist_ids(uid)),
        "local_favorite_paths": sorted(account_store.local_favorite_paths(uid)),
    }


# 旧版登录接口保留为兼容空操作，避免外部脚本 / 旧前端报错
@app.post("/api/account/register")
@app.post("/api/account/login")
def api_account_noop_auth():
    return ok(_local_profile(), "本机档案模式：无需登录，收藏直接保存在这台电脑")


@app.post("/api/account/logout")
def api_account_logout():
    return ok(_local_profile(), "本机档案模式没有登录态，数据仍保留在本机")


@app.get("/api/account/me")
def api_account_me():
    return ok(_local_profile())


# ---- 收藏的歌手（在线） ----

@app.get("/api/account/artists")
def api_account_artists():
    user, error = need_user()
    if error:
        return error
    return ok({"items": account_store.favorite_artists(user["id"])})


@app.post("/api/account/artists/add")
def api_account_artist_add():
    user, error = need_user()
    if error:
        return error
    try:
        artist = account_store.add_favorite_artist(user["id"], body().get("artist") or {})
        return ok({"artist": artist, "stats": account_store.stats(user["id"])}, "已收藏歌手")
    except Exception as exc:  # noqa: BLE001
        return account_error(exc)


@app.post("/api/account/artists/remove")
def api_account_artist_remove():
    user, error = need_user()
    if error:
        return error
    aid = str(body().get("artist_id") or "")
    if not aid:
        return fail("缺少歌手 ID")
    removed = account_store.remove_favorite_artist(user["id"], aid)
    return ok({"removed": removed, "stats": account_store.stats(user["id"])},
              "已取消收藏歌手" if removed else "该歌手不在收藏中")


# ---- 本地音乐收藏（与在线收藏分开） ----

@app.get("/api/account/local-favorites")
def api_account_local_favorites():
    user, error = need_user()
    if error:
        return error
    return ok({"items": account_store.local_favorites(user["id"])})


@app.post("/api/account/local-favorites/add")
def api_account_local_favorite_add():
    user, error = need_user()
    if error:
        return error
    try:
        item = account_store.add_local_favorite(user["id"], body().get("track") or {})
        return ok({"item": item, "stats": account_store.stats(user["id"])}, "已收藏本地音乐")
    except Exception as exc:  # noqa: BLE001
        return account_error(exc)


@app.post("/api/account/local-favorites/remove")
def api_account_local_favorite_remove():
    user, error = need_user()
    if error:
        return error
    path = str(body().get("path") or "")
    if not path:
        return fail("缺少文件路径")
    removed = account_store.remove_local_favorite(user["id"], path)
    return ok({"removed": removed, "stats": account_store.stats(user["id"])},
              "已取消本地收藏" if removed else "该文件不在本地收藏中")


@app.get("/api/account/favorites")
def api_account_favorites():
    user, error = need_user()
    if error:
        return error
    return ok({"items": account_store.favorites(user["id"])})


@app.post("/api/account/favorites/add")
def api_account_favorite_add():
    user, error = need_user()
    if error:
        return error
    try:
        song = account_store.add_favorite(user["id"], body().get("song") or {})
        return ok({"song": song, "stats": account_store.stats(user["id"])}, "已收藏")
    except Exception as exc:  # noqa: BLE001
        return account_error(exc)


@app.post("/api/account/favorites/remove")
def api_account_favorite_remove():
    user, error = need_user()
    if error:
        return error
    sid = str(body().get("song_id") or "")
    if not sid:
        return fail("缺少歌曲 ID")
    removed = account_store.remove_favorite(user["id"], sid)
    return ok({"removed": removed, "stats": account_store.stats(user["id"])},
              "已取消收藏" if removed else "该歌曲不在收藏中")


@app.get("/api/account/playlists")
def api_account_playlists():
    user, error = need_user()
    if error:
        return error
    return ok({"items": account_store.playlists(user["id"])})


@app.post("/api/account/playlists/create")
def api_account_playlist_create():
    user, error = need_user()
    if error:
        return error
    payload = body()
    try:
        item = account_store.create_playlist(user["id"], payload.get("name"),
                                             payload.get("description"))
        return ok({"playlist": item, "items": account_store.playlists(user["id"])}, "歌单已创建")
    except Exception as exc:  # noqa: BLE001
        return account_error(exc)


@app.post("/api/account/playlists/rename")
def api_account_playlist_rename():
    user, error = need_user()
    if error:
        return error
    payload = body()
    try:
        item = account_store.rename_playlist(user["id"], payload.get("id"), payload.get("name"))
        return ok({"playlist": item}, "已重命名")
    except Exception as exc:  # noqa: BLE001
        return account_error(exc)


@app.post("/api/account/playlists/delete")
def api_account_playlist_delete():
    user, error = need_user()
    if error:
        return error
    try:
        account_store.delete_playlist(user["id"], body().get("id"))
        return ok({"items": account_store.playlists(user["id"])}, "歌单已删除")
    except Exception as exc:  # noqa: BLE001
        return account_error(exc)


@app.get("/api/account/playlists/detail")
def api_account_playlist_detail():
    user, error = need_user()
    if error:
        return error
    pid = str(request.args.get("id") or "")
    try:
        return ok(account_store.playlist_detail(user["id"], pid))
    except Exception as exc:  # noqa: BLE001
        return account_error(exc)


@app.post("/api/account/playlists/add")
def api_account_playlist_add():
    user, error = need_user()
    if error:
        return error
    payload = body()
    songs = payload.get("songs")
    if not isinstance(songs, list):
        songs = [payload.get("song")] if payload.get("song") else []
    try:
        result = account_store.add_to_playlist(user["id"], payload.get("id"), songs)
        return ok(result, f"已添加 {result['added']} 首到歌单")
    except Exception as exc:  # noqa: BLE001
        return account_error(exc)


@app.post("/api/account/playlists/remove")
def api_account_playlist_remove():
    user, error = need_user()
    if error:
        return error
    payload = body()
    try:
        removed = account_store.remove_from_playlist(
            user["id"], payload.get("id"), payload.get("song_id"))
        return ok({"removed": removed}, "已从歌单移除" if removed else "歌曲不在该歌单中")
    except Exception as exc:  # noqa: BLE001
        return account_error(exc)


# ---------------- 查询 ----------------

@app.get("/api/search")
def api_search():
    keyword = (request.args.get("keyword") or request.args.get("keywords") or "").strip()
    if not keyword:
        return fail("请输入搜索关键词")
    try:
        limit = int(request.args.get("limit") or 30)
        search_type = int(request.args.get("type") or 1)
        offset = int(request.args.get("offset") or 0)
    except ValueError:
        return fail("参数格式错误")
    try:
        results = service.search(keyword, limit, search_type, offset)
        return ok({"keyword": keyword, "type": search_type, "items": results})
    except APIException as exc:
        return fail(str(exc), 500)
    except Exception as exc:  # noqa: BLE001
        logger.exception("search failed")
        return fail(f"搜索失败: {exc}", 500)


@app.get("/api/song")
def api_song():
    raw = request.args.get("id") or request.args.get("url") or ""
    song_id = extract_id(raw)
    level = request.args.get("level") or request.args.get("quality") or "lossless"
    if not song_id:
        return fail("请输入歌曲 ID 或链接")
    if level not in QUALITY_VALUES:
        return fail("不支持的音质")
    try:
        return ok(service.song(song_id, level))
    except APIException as exc:
        return fail(str(exc), 404)
    except Exception as exc:  # noqa: BLE001
        logger.exception("song failed")
        return fail(f"解析失败: {exc}", 500)


@app.get("/api/resolve")
def api_resolve():
    """解析歌单 / 专辑，返回可批量下载的完整曲目列表。"""
    raw = request.args.get("id") or ""
    resource_id = extract_id(raw)
    kind = request.args.get("kind") or detect_kind(raw)
    if kind not in ("album", "playlist"):
        kind = "playlist"
    if not resource_id:
        return fail("请输入歌单 / 专辑 ID 或链接")
    try:
        data = service.resolve(kind, resource_id)
        return ok(data)
    except APIException as exc:
        return fail(str(exc), 404)
    except Exception as exc:  # noqa: BLE001
        logger.exception("resolve failed")
        return fail(f"解析失败: {exc}", 500)


# ---------------- 歌手搜索的展开：热门歌曲 ----------------

@app.get("/api/artist/songs")
def api_artist_songs():
    """匿名获取歌手热门歌曲；失败时退回按歌手名搜索。"""
    raw = str(request.args.get("id") or "").strip()
    found = re.search(r"(\d{2,})", raw)
    if not found:
        return fail("缺少有效的歌手 ID")
    artist_id = found.group(1)
    name = str(request.args.get("name") or "").strip()
    tracks: list = []
    try:
        response = requests.get(
            "https://music.163.com/api/v1/artist/songs",
            params={"id": artist_id, "limit": 50, "offset": 0},
            headers={"User-Agent": APIConstants.USER_AGENT,
                     "Referer": APIConstants.REFERER},
            cookies={}, timeout=15,
        )
        data = loads_loose(response.text)
        raw_songs = data.get("songs") or []
        tracks = [service.api.normalize_song(s, i, len(raw_songs))
                  for i, s in enumerate(raw_songs)]
    except Exception:  # noqa: BLE001
        logger.info("artist songs api unavailable, fallback to search")
    if not tracks and name:
        try:
            tracks = service.api.search_music(name, {}, 50, 1, 0)
        except Exception:  # noqa: BLE001
            tracks = []
    return ok({
        "artist_id": artist_id, "name": name or "歌手",
        "tracks": tracks,
        "fallback": bool(name and not raw),
    })


# ---------------- 多平台歌单导入 ----------------

@app.post("/api/import/resolve")
def api_import_resolve():
    """把其它平台歌单链接 / 粘贴文本转换成网易云曲目列表。"""
    payload = body()
    url = str(payload.get("url") or "").strip()
    text = str(payload.get("text") or "").strip()
    meta: Dict[str, Any] = {
        "platform": "text", "platform_label": "歌单文本",
        "title": "粘贴的歌单文本", "needs_text": False, "note": "",
        "native": False, "tracks": [], "unmatched": [],
    }
    entries: list = []

    if url:
        try:
            parsed = playlist_importer.parse_url(url)
        except playlist_importer.ImportError as exc:
            return fail(str(exc))
        meta.update({
            "platform": parsed.get("platform"),
            "platform_label": parsed.get("platform_label"),
            "title": parsed.get("title") or "导入的歌单",
            "needs_text": bool(parsed.get("needs_text")),
            "note": parsed.get("note") or "",
            "url": parsed.get("url"),
        })
        if parsed.get("native_netease"):
            # 网易云自家链接走原生解析：曲目完全精确，无需模糊匹配
            resource_id = extract_id(url)
            kind = detect_kind(url)
            if kind not in ("album", "playlist"):
                kind = "playlist"
            if not resource_id:
                return fail("没能从网易云链接里识别出歌单 / 专辑 ID")
            try:
                data = service.resolve(kind, resource_id)
            except Exception as exc:  # noqa: BLE001
                return fail(f"解析网易云歌单失败: {exc}")
            meta.update({
                "native": True, "title": data.get("title") or "网易云歌单",
                "tracks": data.get("tracks") or [], "unmatched": [],
            })
            return ok(meta)
        entries = parsed.get("entries") or []
        if not entries:
            meta["tracks"] = []
            return ok(meta)

    if not entries and text:
        entries = playlist_importer.parse_text(text)
        meta.update({"title": "粘贴的歌单文本", "platform_label": "歌单文本",
                     "needs_text": False, "note": ""})
    if not entries:
        return fail("没有读到任何曲目。请粘贴歌单链接，或把曲目列表粘贴到文本框。")

    tracks, unmatched = playlist_importer.match_entries(entries)
    meta.update({
        "tracks": tracks, "unmatched": unmatched,
        "total": len(entries),
        "note": meta.get("note") or f"已匹配 {len(tracks)} / {len(entries)} 首；匹配使用匿名公开搜索，不使用 Cookie。",
    })
    return ok(meta)


# ---------------- 本地音乐（扫描 / 播放 / 封面 / 附加目录） ----------------

@app.get("/api/local/scan")
def api_local_scan():
    return ok(localmusic.scan())


@app.get("/api/local/dirs")
def api_local_dirs():
    return ok({
        "output_dir": str(output_dir()),
        "roots": [str(x) for x in localmusic.allowed_roots()],
        "extra_dirs": localmusic.extra_dirs(),
    })


@app.post("/api/local/dirs")
def api_local_dirs_save():
    dirs = body().get("dirs")
    if not isinstance(dirs, list):
        return fail("目录列表格式错误")
    settings.update({"local_dirs": dirs})
    return ok({"extra_dirs": localmusic.extra_dirs()}, "本地音乐目录已保存")


@app.get("/api/local/stream")
def api_local_stream():
    path = localmusic.safe_path(request.args.get("path") or "")
    if not path:
        return fail("文件不存在，或不在允许的本地音乐目录内", 404)
    return send_file(str(path), mimetype=localmusic.mime_for(path),
                     conditional=True, as_attachment=False,
                     download_name=path.name)


@app.get("/api/local/cover")
def api_local_cover():
    path = localmusic.safe_path(request.args.get("path") or "")
    if not path:
        return fail("文件不存在", 404)
    result = localmusic.cover_bytes(path)
    if not result:
        return fail("该音频没有内嵌封面", 404)
    data, mime = result
    resp = Response(data, mimetype=mime)
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


# ---------------- 下载 ----------------

@app.post("/api/download")
def api_download():
    """单曲下载：直接保存到根目录（不再走浏览器文件流）。"""
    payload = body()
    song_id = extract_id(payload.get("id") or payload.get("url") or "")
    if not song_id:
        return fail("请输入歌曲 ID 或链接")
    quality = payload.get("quality") or settings.get("quality")
    if quality not in QUALITY_VALUES:
        quality = "lossless"
    try:
        track = service.song(song_id, quality)
        url_info = {
            "url": track.get("download_url", ""),
            "type": track.get("file_type", ""),
            "size": track.get("file_size", 0),
            "br": track.get("bitrate", 0),
            "reason": track.get("reason", ""),
        }
        result = download_track(service, track, url_info, settings.as_dict() | {"quality": quality})
        result["track"] = {
            "id": song_id, "name": track.get("name"), "artists": track.get("artists"),
            "album": track.get("album"), "picUrl": track.get("picUrl"),
        }
        if result["status"] == "failed":
            return fail(result["error"], 500)
        return ok(result, "已保存到下载目录")
    except Exception as exc:  # noqa: BLE001
        logger.exception("download failed")
        return fail(f"下载失败: {exc}", 500)


@app.post("/api/batch")
def api_batch():
    """创建批量下载任务。

    支持两种入参：
      {tracks: [...], title, kind, quality, ...}             前端已解析好的列表
      {source: "album"|"playlist", id: "..."}               由服务端解析
    """
    payload = body()
    tracks = payload.get("tracks")
    kind = payload.get("kind") or "manual"
    title = payload.get("title") or "批量下载"

    if not tracks and payload.get("source") in ("album", "playlist"):
        source = payload["source"]
        resource_id = extract_id(payload.get("id") or "")
        if not resource_id:
            return fail("缺少歌单 / 专辑 ID")
        try:
            resolved = service.resolve(source, resource_id)
        except Exception as exc:  # noqa: BLE001
            return fail(f"解析失败: {exc}", 500)
        tracks = resolved["tracks"]
        title = resolved["title"] or title
        kind = source

    if not isinstance(tracks, list) or not tracks:
        return fail("下载列表为空")
    if len(tracks) > 1000:
        return fail("单次最多 1000 首，请分批下载")

    options = {k: v for k, v in settings.as_dict().items()}
    options.update({k: v for k, v in payload.items() if k in DEFAULTS})
    options["quality"] = payload.get("quality") or options.get("quality")
    if options["quality"] not in QUALITY_VALUES:
        options["quality"] = "lossless"

    try:
        job_id = batch_manager.start(title, kind, tracks, options)
    except ValueError as exc:
        return fail(str(exc))
    return ok({"job_id": job_id}, "批量下载已开始")


@app.get("/api/batch/status")
def api_batch_status():
    job_id = request.args.get("id") or ""
    status = batch_manager.status(job_id)
    if status is None:
        return fail("任务不存在", 404)
    return ok(status)


@app.post("/api/batch/cancel")
def api_batch_cancel():
    job_id = str(body().get("id") or "")
    if batch_manager.cancel(job_id):
        return ok({"id": job_id}, "正在取消任务")
    return fail("任务不存在或已结束")


@app.get("/api/batch/list")
def api_batch_list():
    return ok(batch_manager.list_jobs())


# ---------------- 登录 / Cookie ----------------

@app.get("/api/login/status")
def api_login_status():
    cookies = load_cookies()
    has = bool(cookies.get("MUSIC_U"))
    profile = {"logged_in": False}
    if has:
        profile = service.profile()
        _profile_cache.update({"at": time.time(), "data": profile})
    return ok({
        "exists": Path(COOKIE_FILE).is_file(),
        "count": len(cookies),
        "valid": has,
        "profile": profile,
    })


@app.get("/api/login/qr")
def api_login_qr():
    if not _has_qrcode():
        return fail("缺少 qrcode 依赖，请在设置里手动粘贴 Cookie")
    try:
        from music_api import QRLoginManager
        manager = QRLoginManager()
        key = manager.generate_qr_key()
        if not key:
            return fail("生成二维码失败，请重试", 500)
        url = manager.login_qr_url(key)
        matrix = manager.qr_matrix(url)
        return ok({"key": key, "url": url, "matrix": matrix})
    except Exception as exc:  # noqa: BLE001
        logger.exception("qr failed")
        return fail(f"生成二维码失败: {exc}", 500)


@app.get("/api/login/check")
def api_login_check():
    from music_api import QRLoginManager

    key = request.args.get("key") or ""
    if not key:
        return fail("缺少 key")
    try:
        code, cookies = QRLoginManager().check_qr_login(key)
        labels = {
            801: ("waiting", "等待扫码"),
            802: ("scanned", "已扫码，请在手机上确认"),
            800: ("expired", "二维码已过期，请刷新"),
            803: ("success", "登录成功"),
        }
        state, label = labels.get(code, ("error", f"登录状态异常 ({code})"))
        if code == 803:
            content = QRLoginManager.format_cookie_string(cookies)
            if "MUSIC_U=" not in content:
                return ok({"state": "error", "message": "登录成功但未取得 MUSIC_U，请重试"})
            Path(COOKIE_FILE).write_text(content.strip() + "\n", encoding="utf-8")
            _profile_cache["at"] = 0.0
            return ok({"state": "success", "message": "登录成功，Cookie 已保存", "profile": service.profile()})
        return ok({"state": state, "message": label, "code": code})
    except Exception as exc:  # noqa: BLE001
        return fail(f"检查登录状态失败: {exc}", 500)


@app.post("/api/login/cookie")
def api_login_cookie():
    raw = str(body().get("cookie") or "").strip()
    if not raw:
        return fail("请粘贴 Cookie")
    cookies = parse_cookie_string(raw)
    if not cookies.get("MUSIC_U"):
        return fail("Cookie 里没有 MUSIC_U，请确认复制完整（登录网页版后从 Network 复制）")
    try:
        if Path(COOKIE_FILE).is_file():
            backup = Path(COOKIE_FILE).with_suffix(f".bak.{int(time.time())}.txt")
            shutil.copy2(COOKIE_FILE, backup)
        content = '; '.join(f"{k}={v}" for k, v in cookies.items()) + ';'
        Path(COOKIE_FILE).write_text(content + "\n", encoding="utf-8")
        _profile_cache["at"] = 0.0
        return ok({"count": len(cookies), "profile": service.profile()}, "Cookie 已保存")
    except OSError as exc:
        return fail(f"保存失败: {exc}")


@app.post("/api/login/logout")
def api_login_logout():
    path = Path(COOKIE_FILE)
    try:
        if path.is_file():
            shutil.copy2(path, path.with_suffix(f".logout.{int(time.time())}.txt"))
            path.unlink()
        _profile_cache["at"] = 0.0
        return ok(None, "已退出登录（原 Cookie 已备份）")
    except OSError as exc:
        return fail(f"退出失败: {exc}")


# ---------------- 旧版兼容 API ----------------

@app.route("/health", methods=["GET"])
def health_check():
    cookies = load_cookies()
    return legacy({
        "service": "running", "timestamp": int(time.time()),
        "cookie_status": "valid" if cookies else "invalid",
        "cookie_count": len(cookies),
        "downloads_dir": str(output_dir()), "version": VERSION,
    }, "API服务运行正常")


def _legacy_song_info(song_id: str, level: str, info_type: str):
    if info_type == "url":
        item = service.url_map([song_id], level).get(song_id, {})
        if not item.get("url"):
            return None
        return {
            "id": song_id, "url": item["url"], "level": item.get("level", level),
            "size": item.get("size", 0), "size_formatted": format_size(item.get("size", 0)),
            "type": item.get("type"), "bitrate": item.get("br"),
        }
    detail = service.api.get_song_details([song_id]).get(song_id)
    if not detail:
        return None
    track = service.api.normalize_song(detail)
    if info_type == "name":
        return track
    if info_type == "lyric":
        lrc, trans = service.lyric(song_id)
        return {"lrc": {"lyric": lrc}, "tlyric": {"lyric": trans}}
    # json
    item = service.url_map([song_id], level).get(song_id, {})
    lrc, trans = service.lyric(song_id)
    return {
        "id": song_id, "name": track["name"], "ar_name": track["artists"],
        "al_name": track["album"], "pic": track["picUrl"], "level": level,
        "lyric": lrc, "tlyric": trans, "url": item.get("url", ""),
        "size": format_size(item.get("size", 0)) if item.get("size") else "获取失败",
    }


@app.route("/song", methods=["GET", "POST"])
@app.route("/Song_V1", methods=["GET", "POST"])
def legacy_song():
    payload = body()
    raw = payload.get("ids") or payload.get("id") or payload.get("url") or ""
    song_id = extract_id(raw)
    level = payload.get("level", "lossless")
    info_type = payload.get("type", "url")
    if not song_id:
        return legacy(None, "必须提供 ids / id / url 参数", 400)
    if info_type not in ("url", "name", "lyric", "json"):
        return legacy(None, "无效的 type 参数", 400)
    try:
        result = _legacy_song_info(song_id, level, info_type)
        if result is None:
            return legacy(None, "获取音乐信息失败", 404)
        return legacy(result, "获取歌曲信息成功")
    except Exception as exc:  # noqa: BLE001
        return legacy(None, f"服务器错误: {exc}", 500)


@app.route("/search", methods=["GET", "POST"])
@app.route("/Search", methods=["GET", "POST"])
def legacy_search():
    payload = body()
    keyword = payload.get("keyword") or payload.get("keywords") or payload.get("q") or ""
    if not keyword:
        return legacy(None, "参数 keyword 不能为空", 400)
    try:
        limit = min(100, int(payload.get("limit", 30)))
        results = service.search(keyword, limit)
        return legacy(results, "搜索完成")
    except Exception as exc:  # noqa: BLE001
        return legacy(None, f"搜索失败: {exc}", 500)


@app.route("/playlist", methods=["GET", "POST"])
@app.route("/Playlist", methods=["GET", "POST"])
def legacy_playlist():
    payload = body()
    pid = extract_id(payload.get("id") or "")
    if not pid:
        return legacy(None, "参数 id 不能为空", 400)
    try:
        return legacy({"status": "success", "playlist": service.playlist(pid)}, "获取歌单详情成功")
    except Exception as exc:  # noqa: BLE001
        return legacy(None, f"获取歌单失败: {exc}", 500)


@app.route("/album", methods=["GET", "POST"])
@app.route("/Album", methods=["GET", "POST"])
def legacy_album():
    payload = body()
    aid = extract_id(payload.get("id") or "")
    if not aid:
        return legacy(None, "参数 id 不能为空", 400)
    try:
        return legacy({"status": 200, "album": service.album(aid)}, "获取专辑详情成功")
    except Exception as exc:  # noqa: BLE001
        return legacy(None, f"获取专辑失败: {exc}", 500)


@app.route("/download", methods=["GET", "POST"])
@app.route("/Download", methods=["GET", "POST"])
def legacy_download():
    payload = body()
    song_id = extract_id(payload.get("id") or payload.get("url") or "")
    quality = payload.get("quality", "lossless")
    return_format = payload.get("format", "file")
    if not song_id:
        return legacy(None, "参数 id 不能为空", 400)
    if quality not in QUALITY_VALUES:
        return legacy(None, "无效的音质参数", 400)
    try:
        track = service.song(song_id, quality)
        url_info = {
            "url": track.get("download_url", ""),
            "type": track.get("file_type", ""),
            "size": track.get("file_size", 0),
            "br": track.get("bitrate", 0),
            "reason": track.get("reason", ""),
        }
        result = download_track(service, track, url_info,
                               settings.as_dict() | {"quality": quality})
        if result["status"] == "failed":
            return legacy(None, f"下载失败: {result['error']}", 500)
        if return_format == "json":
            return legacy({
                "music_id": song_id, "name": track["name"],
                "artist": track["artists"], "album": track["album"],
                "quality": quality, "file_type": track.get("file_type"),
                "file_size": result["size"], "file_path": result["file"],
                "filename": result["filename"], "metadata": result.get("fields", []),
            }, "下载完成")
        response = send_file(
            result["file"], as_attachment=True,
            download_name=result["filename"],
            mimetype="audio/mpeg" if result["filename"].endswith(".mp3") else "audio/flac",
        )
        response.headers["X-Download-Message"] = "Download completed successfully"
        response.headers["X-Download-Filename"] = quote(result["filename"], safe="")
        return response
    except Exception as exc:  # noqa: BLE001
        logger.exception("legacy download failed")
        return legacy(None, f"下载异常: {exc}", 500)


@app.get("/api/info")
def api_info():
    return legacy({
        "name": "云音盒子 · 桌面版", "version": VERSION,
        "description": "云音盒子桌面版：公开推荐 / 流媒体与本地播放 / 免登录本机档案 / 多平台歌单导入 / 完整元数据 / 批量下载",
        "endpoints": [
            "/api/state", "/api/settings", "/api/search", "/api/song",
            "/api/resolve", "/api/artist/songs", "/api/download",
            "/api/naming/preview", "/api/recommend", "/api/recommend/chart",
            "/api/stream", "/api/import/resolve",
            "/api/local/scan", "/api/local/stream", "/api/local/cover",
            "/api/account/me", "/api/account/favorites",
            "/api/account/artists", "/api/account/local-favorites",
            "/api/account/playlists", "/api/batch", "/api/batch/status",
            "/api/batch/cancel", "/api/login/qr", "/api/login/check",
            "/api/login/cookie",
        ],
        "supported_qualities": QUALITY_VALUES,
        "output_dir": str(output_dir()),
    }, "API信息获取成功")


@app.errorhandler(404)
def not_found(_error):
    if request.path.startswith("/api/"):
        return fail("接口不存在", 404)
    return legacy(None, "请求的资源不存在", 404)


@app.errorhandler(500)
def internal_error(error):
    logger.error("internal error: %s", error)
    if request.path.startswith("/api/"):
        return fail(f"服务器内部错误: {error}", 500)
    return legacy(None, f"服务器内部错误: {error}", 500)


@app.after_request
def no_cache(response: Response) -> Response:
    # 音频流与封面允许浏览器缓存，其余 API（含 /api/stream/info 探测）一律不缓存
    cacheable = ("/api/stream", "/api/cover", "/api/local/stream", "/api/local/cover")
    if request.path.startswith(cacheable) and request.path != "/api/stream/info":
        return response
    if request.path.startswith("/api/") or request.path == "/":
        response.headers["Cache-Control"] = "no-store"
    return response
