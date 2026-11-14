"""音频元数据写入（纯 Python / mutagen，不依赖 ffmpeg）

支持的标签字段：
    标题 / 歌手（多值） / 专辑 / 专辑艺术家 / 音轨号 / 总音轨数 / 碟号 /
    发行年份 / 歌词 / 注释 / 网易云歌曲 ID / 专辑封面

支持格式：
    MP3   -> ID3v2.3 (TIT2/TPE1/TALB/TRCK/TDRC/USLT/APIC ...)
    FLAC  -> Vorbis Comment + METADATA_BLOCK_PICTURE
    M4A/MP4 -> iTunes atoms (©nam/©ART/©alb/trkn/©lyr/covr ...)
    OGG/Opus -> Vorbis Comment + METADATA_BLOCK_PICTURE(base64)

mutagen 只改动标签区，不重新编码音频，音质零损失。
"""
from __future__ import annotations

import base64
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SUPPORTED_SUFFIXES = {".mp3", ".flac", ".m4a", ".mp4", ".ogg", ".opus"}


@dataclass
class TagInfo:
    """写入音频文件所需的全部元信息。"""

    title: str = ""
    artists: List[str] = field(default_factory=list)
    album: str = ""
    album_artist: str = ""
    track_no: int = 0
    track_total: int = 0
    disc_no: int = 0
    disc_total: int = 0
    year: str = ""
    lyrics: str = ""
    comment: str = ""
    source_id: str = ""
    cover: Optional[bytes] = None
    cover_mime: str = "image/jpeg"

    @property
    def artist_string(self) -> str:
        return " / ".join(a for a in self.artists if a)


def _image_size(data: bytes) -> Tuple[int, int, int]:
    """从 JPEG / PNG / WEBP 头里读出宽高与色深（用于 FLAC PICTURE 块）。"""
    if not data:
        return 0, 0, 0
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
            w, h = struct.unpack(">II", data[16:24])
            return w, h, 24
        if data[:2] == b"\xff\xd8":
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xD8, 0xD9, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                length = struct.unpack(">H", data[i + 2 : i + 4])[0]
                # SOF0..SOF15 (排除 DHT/JPG/DAC)
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                              0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack(">HH", data[i + 5 : i + 9])
                    depth = data[i + 4] * 8
                    return w, h, depth or 24
                i += 2 + length
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            if data[12:16] == b"VP8X" and len(data) >= 30:
                w = int.from_bytes(data[24:27], "little") + 1
                h = int.from_bytes(data[27:30], "little") + 1
                return w, h, 24
    except Exception:
        pass
    return 0, 0, 24


def guess_mime(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _make_picture(data: bytes, mime: str):
    from mutagen.flac import Picture

    pic = Picture()
    pic.type = 3  # Front Cover
    pic.mime = mime
    pic.desc = "Cover"
    pic.width, pic.height, pic.depth = _image_size(data)
    pic.data = data
    return pic


def _write_mp3(path: Path, t: TagInfo, written: List[str]) -> None:
    from mutagen.id3 import (
        ID3, ID3NoHeaderError, TIT2, TPE1, TPE2, TALB, TRCK, TPOS,
        TDRC, USLT, COMM, APIC, TXXX, TCON,
    )

    try:
        tags = ID3(str(path))
    except ID3NoHeaderError:
        tags = ID3()

    def set_frame(key, frame):
        tags.setall(key, [frame])

    if t.title:
        set_frame("TIT2", TIT2(encoding=3, text=[t.title]))
        written.append("标题")
    if t.artists:
        set_frame("TPE1", TPE1(encoding=3, text=t.artists))
        written.append("歌手")
    if t.album_artist or t.artists:
        set_frame("TPE2", TPE2(encoding=3, text=[t.album_artist or t.artist_string]))
        written.append("专辑艺术家")
    if t.album:
        set_frame("TALB", TALB(encoding=3, text=[t.album]))
        written.append("专辑")
    number = str(t.track_no) if t.track_no else ""
    if number:
        number += f"/{t.track_total}" if t.track_total else ""
        set_frame("TRCK", TRCK(encoding=3, text=[number]))
        written.append("音轨号")
    if t.disc_no:
        disc = f"{t.disc_no}" + (f"/{t.disc_total}" if t.disc_total else "")
        set_frame("TPOS", TPOS(encoding=3, text=[disc]))
    if t.year:
        set_frame("TDRC", TDRC(encoding=3, text=[str(t.year)]))
        written.append("年份")
    if t.lyrics:
        tags.setall("USLT", [USLT(encoding=3, lang="und", desc="", text=t.lyrics)])
        written.append("歌词")
    if t.comment:
        set_frame("COMM", COMM(encoding=3, lang="eng", desc="", text=[t.comment]))
    if t.source_id:
        tags.setall("TXXX:NETEASE_ID", [TXXX(encoding=3, desc="NETEASE_ID", text=[t.source_id])])
    if t.cover:
        set_frame("APIC", APIC(encoding=3, mime=t.cover_mime, type=3, desc="Cover", data=t.cover))
        written.append("封面")
    tags.save(str(path), v2_version=3)


def _apply_vorbis(audio, t: TagInfo, written: List[str]) -> None:
    if audio.tags is None:
        audio.add_tags()
    v = audio.tags
    mapping = {
        "title": t.title,
        "artist": t.artists,
        "album": t.album,
        "albumartist": t.album_artist or t.artist_string,
        "tracknumber": str(t.track_no) if t.track_no else "",
        "tracktotal": str(t.track_total) if t.track_total else "",
        "discnumber": str(t.disc_no) if t.disc_no else "",
        "disctotal": str(t.disc_total) if t.disc_total else "",
        "date": str(t.year),
        "lyrics": t.lyrics,
        "comment": t.comment,
        "genre": "",
        "netease_id": t.source_id,
    }
    friendly = {
        "title": "标题", "artist": "歌手", "album": "专辑",
        "albumartist": "专辑艺术家", "tracknumber": "音轨号",
        "date": "年份", "lyrics": "歌词",
    }
    for key, value in mapping.items():
        if not value:
            continue
        v[key] = value if isinstance(value, list) else [str(value)]
        if key in friendly:
            written.append(friendly[key])


def _write_flac(path: Path, t: TagInfo, written: List[str]) -> None:
    from mutagen.flac import FLAC

    audio = FLAC(str(path))
    _apply_vorbis(audio, t, written)
    if t.cover:
        audio.clear_pictures()
        audio.add_picture(_make_picture(t.cover, t.cover_mime))
        written.append("封面")
    audio.save()


def _write_ogg(path: Path, t: TagInfo, written: List[str], opus: bool) -> None:
    if opus:
        from mutagen.oggopus import OggOpus as OggType
    else:
        from mutagen.oggvorbis import OggVorbis as OggType

    audio = OggType(str(path))
    _apply_vorbis(audio, t, written)
    if t.cover:
        pic = _make_picture(t.cover, t.cover_mime)
        audio["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]
        written.append("封面")
    audio.save()


def _write_mp4(path: Path, t: TagInfo, written: List[str]) -> None:
    from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm

    audio = MP4(str(path))
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    if t.title:
        tags["\xa9nam"] = [t.title]
        written.append("标题")
    if t.artists:
        tags["\xa9ART"] = t.artists
        written.append("歌手")
    if t.album_artist or t.artists:
        tags["aART"] = [t.album_artist or t.artist_string]
        written.append("专辑艺术家")
    if t.album:
        tags["\xa9alb"] = [t.album]
        written.append("专辑")
    if t.track_no:
        tags["trkn"] = [(t.track_no, t.track_total or 0)]
        written.append("音轨号")
    if t.disc_no:
        tags["disk"] = [(t.disc_no, t.disc_total or 0)]
    if t.year:
        tags["\xa9day"] = [str(t.year)]
        written.append("年份")
    if t.lyrics:
        tags["\xa9lyr"] = [t.lyrics]
        written.append("歌词")
    if t.comment:
        tags["\xa9cmt"] = [t.comment]
    if t.source_id:
        tags["----:com.apple.iTunes:NETEASE_ID"] = [
            MP4FreeForm(str(t.source_id).encode("utf-8"))
        ]
    if t.cover:
        fmt = MP4Cover.FORMAT_PNG if t.cover_mime == "image/png" else MP4Cover.FORMAT_JPEG
        tags["covr"] = [MP4Cover(t.cover, imageformat=fmt)]
        written.append("封面")
    audio.save()


def write_tags(path: str | Path, info: TagInfo) -> Dict[str, Any]:
    """把元信息写进音频文件，返回写入结果（永不抛异常，失败不影响主流程）。"""
    file_path = Path(path)
    result: Dict[str, Any] = {
        "ok": False, "fields": [], "cover": bool(info.cover),
        "lyrics": bool(info.lyrics), "error": "",
    }
    suffix = file_path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        result["error"] = f"暂不支持写入 {suffix or '未知'} 格式的标签"
        return result
    if not file_path.is_file():
        result["error"] = "音频文件不存在"
        return result

    written: List[str] = []
    try:
        if suffix == ".mp3":
            _write_mp3(file_path, info, written)
        elif suffix == ".flac":
            _write_flac(file_path, info, written)
        elif suffix in (".m4a", ".mp4"):
            _write_mp4(file_path, info, written)
        elif suffix == ".ogg":
            _write_ogg(file_path, info, written, opus=False)
        elif suffix == ".opus":
            _write_ogg(file_path, info, written, opus=True)
        result["ok"] = True
        result["fields"] = list(dict.fromkeys(written))
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def inspect_tags(path: str | Path) -> Dict[str, Any]:
    """读回标签，用于「元数据是否写入成功」的验证与展示。"""
    from mutagen import File

    file_path = Path(path)
    out: Dict[str, Any] = {"path": str(file_path), "format": file_path.suffix.lower()}
    try:
        audio = File(str(file_path))
        if audio is None:
            return out
        tags = getattr(audio, "tags", None)
        if not tags:
            return out
        out["length"] = round(getattr(audio.info, "length", 0), 2)
        if file_path.suffix.lower() == ".mp3":
            def first(key):
                frame = tags.get(key)
                return str(frame.text[0]) if frame else ""
            out.update({
                "title": first("TIT2"), "artist": first("TPE1"),
                "album": first("TALB"), "track": first("TRCK"),
                "year": first("TDRC"), "cover": bool(tags.getall("APIC")),
                "lyrics": bool(tags.getall("USLT")),
                "cover_bytes": len(tags.getall("APIC")[0].data) if tags.getall("APIC") else 0,
            })
        elif file_path.suffix.lower() in (".m4a", ".mp4"):
            def m4(key):
                values = tags.get(key)
                return values[0] if values else ""
            out.update({
                "title": m4("\xa9nam"), "artist": m4("\xa9ART"),
                "album": m4("\xa9alb"), "track": str(m4("trkn")),
                "year": m4("\xa9day"), "cover": bool(tags.get("covr")),
                "lyrics": bool(tags.get("\xa9lyr")),
                "cover_bytes": len(tags["covr"][0]) if tags.get("covr") else 0,
            })
        else:
            def vorbis(key, idx=0):
                values = tags.get(key)
                return values[idx] if values else ""
            out.update({
                "title": vorbis("title"), "artist": vorbis("artist"),
                "album": vorbis("album"), "track": vorbis("tracknumber"),
                "year": vorbis("date"),
                "cover": bool(getattr(audio, "pictures", None)) or bool(tags.get("metadata_block_picture")),
                "lyrics": bool(tags.get("lyrics")),
            })
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
    return out
