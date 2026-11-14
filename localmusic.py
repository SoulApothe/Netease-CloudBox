"""云音盒子 · 本地音乐扫描与播放

- 只扫描「输出目录 / 程序根目录 / 用户额外添加的目录」，做严格的路径校验，
  避免 /api/local/stream 变成任意文件读取。
- 元数据用 mutagen 读取（只读标签区，不改动文件），封面按需从文件里提取。
- 返回的曲目结构与在线 Track 兼容，并额外带 local=True / path 字段，
  播放器的 src 会切到 /api/local/stream。
"""
from __future__ import annotations

import base64
import hashlib
import io
import mimetypes
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from mutagen import File as MutagenFile

from config import BASE_DIR, output_dir, settings

AUDIO_EXTS = {
    ".mp3", ".flac", ".m4a", ".mp4", ".aac", ".ogg", ".opus",
    ".wav", ".wma", ".aif", ".aiff", ".alac",
}
SKIP_DIRS = {
    "__pycache__", "node_modules", ".git", ".venv", "System Volume Information",
    "$RECYCLE.BIN", ".workbuddy", "AppData", "build", "dist",
}
MAX_FILES = 3000

_MIME_BY_EXT = {
    ".mp3": "audio/mpeg", ".flac": "audio/flac", ".m4a": "audio/mp4",
    ".mp4": "audio/mp4", ".aac": "audio/aac", ".ogg": "audio/ogg",
    ".opus": "audio/ogg", ".wav": "audio/wav", ".wma": "audio/x-ms-wma",
    ".aif": "audio/aiff", ".aiff": "audio/aiff", ".alac": "audio/mp4",
}


def extra_dirs() -> List[str]:
    raw = settings.get("local_dirs") or []
    if not isinstance(raw, list):
        return []
    return [str(x).strip() for x in raw if str(x).strip()][:12]


def allowed_roots() -> List[Path]:
    roots: List[Path] = []
    candidates = [output_dir(), BASE_DIR] + [Path(x).expanduser() for x in extra_dirs()]
    for item in candidates:
        try:
            resolved = item.resolve()
            if resolved.is_dir() and resolved not in roots:
                roots.append(resolved)
        except OSError:
            continue
    return roots


def safe_path(raw: str) -> Optional[Path]:
    """把请求里的路径解析成允许范围内的真实文件；越界一律拒绝。"""
    try:
        path = Path(str(raw or "")).expanduser().resolve()
    except (OSError, ValueError):
        return None
    if not path.is_file() or path.suffix.lower() not in AUDIO_EXTS:
        return None
    for root in allowed_roots():
        try:
            path.relative_to(root)
            return path
        except ValueError:
            continue
    return None


def mime_for(path: Path) -> str:
    ext = path.suffix.lower()
    return _MIME_BY_EXT.get(ext) or mimetypes.guess_type(str(path))[0] or "application/octet-stream"


def _first(tags: Any, keys: Tuple[str, ...]) -> str:
    for key in keys:
        try:
            value = tags.get(key) if tags is not None else None
        except Exception:  # noqa: BLE001
            continue
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            value = value[0] if value else None
        if value is None:
            continue
        if isinstance(value, bytes):
            value = value.decode("utf-8", "ignore")
        text = str(value).strip()
        if text:
            return text[:200]
    return ""


def _open(path: Path):
    return MutagenFile(str(path))


def has_cover(audio: Any, tags: Any) -> bool:
    try:
        if tags is not None and hasattr(tags, "getall") and tags.getall("APIC"):
            return True
        if getattr(audio, "pictures", None):
            return True
        if tags is not None:
            if tags.get("covr"):
                return True
            if tags.get("metadata_block_picture"):
                return True
    except Exception:  # noqa: BLE001
        return False
    return False


def cover_bytes(path: Path) -> Optional[Tuple[bytes, str]]:
    """提取内嵌封面，返回 (数据, MIME)。"""
    try:
        audio = _open(path)
        if audio is None:
            return None
        tags = getattr(audio, "tags", None)
        # MP3 / ID3
        if tags is not None and hasattr(tags, "getall"):
            for pic in tags.getall("APIC") or []:
                data = getattr(pic, "data", None)
                if data:
                    return bytes(data), getattr(pic, "mime", "") or "image/jpeg"
        # FLAC
        for pic in getattr(audio, "pictures", None) or []:
            data = getattr(pic, "data", None)
            if data:
                return bytes(data), getattr(pic, "mime", "") or "image/jpeg"
        if tags is not None:
            # MP4 / M4A
            covr = tags.get("covr")
            if covr:
                item = covr[0] if isinstance(covr, (list, tuple)) else covr
                data = bytes(item)
                fmt = getattr(item, "image_format", None)
                mime = ("image/png" if fmt == 14 or data[:8] == b"\x89PNG\r\n\x1a\n"
                        else "image/jpeg")
                return data, mime
            # Ogg / Opus: base64 编码的 FLAC Picture 块
            blocks = tags.get("metadata_block_picture")
            if blocks:
                raw = blocks[0] if isinstance(blocks, (list, tuple)) else blocks
                from mutagen.flac import Picture
                pic = Picture(base64.b64decode(str(raw)))
                if pic.data:
                    return bytes(pic.data), pic.mime or "image/jpeg"
    except Exception:  # noqa: BLE001
        return None
    return None


def read_track(path: Path) -> Dict[str, Any]:
    """读取单个本地音频，返回前端可用的 Track。"""
    stat = path.stat()
    name, artists, album, duration = path.stem, "", "", 0
    cover_ok = False
    try:
        audio = _open(path)
        if audio is not None:
            tags = getattr(audio, "tags", None)
            name = _first(tags, ("title", "TIT2", "©nam", "TITLE")) or path.stem
            artists = _first(tags, ("artist", "TPE1", "©ART", "ARTIST", "albumartist", "TPE2"))
            album = _first(tags, ("album", "TALB", "©alb", "ALBUM"))
            try:
                duration = int(float(audio.info.length) * 1000)
            except (AttributeError, TypeError, ValueError):
                duration = 0
            cover_ok = has_cover(audio, tags)
    except Exception:  # noqa: BLE001  # 元数据损坏也不影响播放
        pass
    if not name.strip():
        name = path.stem
    # 文件名常写成「歌手 - 歌名」，没有标签时拆一下更好看
    if not artists and " - " in name:
        left, right = name.split(" - ", 1)
        if len(left) <= 40:
            artists, name = left.strip(), right.strip()
    key = "local:" + hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:16]
    from urllib.parse import quote
    return {
        "id": key, "path": str(path), "name": name, "artists": artists,
        "artist_list": [artists] if artists else [], "album": album,
        "duration": duration, "size": stat.st_size, "ext": path.suffix.lower(),
        "local": True, "type": "local",
        "picUrl": ("/api/local/cover?path=" + quote(str(path), safe="")) if cover_ok else "",
        "pic": ("/api/local/cover?path=" + quote(str(path), safe="")) if cover_ok else "",
        "track_no": 0,
    }


def scan() -> Dict[str, Any]:
    """扫描允许目录下的音频文件（去重、限量）。"""
    roots = allowed_roots()
    seen: set[str] = set()
    files: List[Path] = []
    for root in roots:
        try:
            for path in root.rglob("*"):
                if len(files) >= MAX_FILES:
                    break
                try:
                    if not path.is_file() or path.suffix.lower() not in AUDIO_EXTS:
                        continue
                    if any(part.startswith(".") or part in SKIP_DIRS for part in path.relative_to(root).parts[:-1]):
                        continue
                    key = str(path).lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    files.append(path)
                except (OSError, ValueError):
                    continue
        except OSError:
            continue
    items = [read_track(p) for p in files]
    items.sort(key=lambda x: (x.get("artists") or "", x.get("album") or "", x.get("name") or ""))
    return {
        "items": items, "count": len(items),
        "truncated": len(files) >= MAX_FILES,
        "roots": [str(x) for x in roots],
        "extra_dirs": extra_dirs(),
    }
