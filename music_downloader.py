"""网易云音乐下载器 · 兼容层

v3 起下载与元数据逻辑已迁移到 downloader.py + tags.py（纯 Python / mutagen，
不再依赖 ffmpeg、aiohttp、aiofiles）。本模块保留旧的类名与函数签名，
确保旧脚本 `from music_downloader import MusicDownloader` 仍可正常工作。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from config import build_filename, format_size, output_dir, settings
from downloader import download_one, extension_for, service
from tags import inspect_tags


class AudioFormat(Enum):
    MP3 = "mp3"
    FLAC = "flac"
    M4A = "m4a"
    MP4 = "mp4"
    UNKNOWN = "unknown"


class QualityLevel(Enum):
    STANDARD = "standard"
    EXHIGH = "exhigh"
    LOSSLESS = "lossless"
    HIRES = "hires"
    SKY = "sky"
    JYEFFECT = "jyeffect"
    JYMASTER = "jymaster"
    DOLBY = "dolby"


@dataclass
class MusicInfo:
    id: int
    name: str
    artists: str
    album: str
    pic_url: str
    duration: int
    track_number: int
    download_url: str
    file_type: str
    file_size: int
    quality: str
    lyric: str = ""
    tlyric: str = ""
    year: str = ""


@dataclass
class DownloadResult:
    success: bool
    file_path: Optional[str] = None
    file_size: int = 0
    error_message: str = ""
    music_info: Optional[MusicInfo] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


class DownloadException(Exception):
    pass


class MusicDownloader:
    """兼容旧接口的下载器；输出目录统一为设置中的根目录。"""

    def __init__(self, download_dir: str = "downloads", max_concurrent: int = 3,
                 ffmpeg_path: Optional[str] = None):
        self.download_dir = output_dir()  # v3: 始终输出到根目录
        self.max_concurrent = max_concurrent
        self.ffmpeg_path = ffmpeg_path  # 保留参数，实际已不需要

    @property
    def api(self):
        return service.api

    def get_music_info(self, music_id: int, quality: str = "standard") -> MusicInfo:
        try:
            detail = service.api.get_song_details([music_id]).get(str(music_id))
            if not detail:
                raise DownloadException(f"无法获取音乐ID {music_id} 的详细信息")
            info = service.api.normalize_song(detail)
            url = service.url_map([str(music_id)], quality).get(str(music_id), {})
            lyric, tlyric = service.lyric(str(music_id))
            return MusicInfo(
                id=int(music_id),
                name=info["name"],
                artists=info["artists"],
                album=info["album"],
                pic_url=info["picUrl"],
                duration=int(info["duration"] or 0) // 1000,
                track_number=int(info["track_no"] or 0),
                download_url=url.get("url", ""),
                file_type=url.get("type", "mp3"),
                file_size=int(url.get("size") or 0),
                quality=quality,
                lyric=lyric,
                tlyric=tlyric,
                year=info.get("year", ""),
            )
        except DownloadException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DownloadException(f"获取音乐信息时发生错误: {exc}")

    def download_music_file(self, music_id: int, quality: str = "standard") -> DownloadResult:
        try:
            info = self.get_music_info(music_id, quality)
            result = download_one(service, {
                "id": music_id, "name": info.name,
                "artist_list": [a for a in info.artists.split("/") if a],
                "album": info.album, "picUrl": info.pic_url,
                "track_no": info.track_number, "year": info.year,
            }, quality)
            status = result.get("status")
            success = status in ("done", "skipped")
            return DownloadResult(
                success=success,
                file_path=result.get("file"),
                file_size=int(result.get("size") or 0),
                error_message="" if success else result.get("error", "下载失败"),
                music_info=info,
                metadata={"fields": result.get("fields", []), "status": status},
            )
        except DownloadException as exc:
            return DownloadResult(success=False, error_message=str(exc))

    async def download_music_file_async(self, music_id: int,
                                        quality: str = "standard") -> DownloadResult:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.download_music_file, music_id, quality)

    async def download_batch_async(self, music_ids: List[int],
                                   quality: str = "standard") -> List[DownloadResult]:
        return list(await asyncio.gather(*[
            self.download_music_file_async(mid, quality) for mid in music_ids
        ]))

    def download_music_to_memory(self, music_id: int,
                                 quality: str = "standard") -> Tuple[bool, BytesIO, MusicInfo]:
        import requests
        info = self.get_music_info(music_id, quality)
        if not info.download_url:
            raise DownloadException("无可用的下载链接")
        response = requests.get(info.download_url, timeout=60)
        response.raise_for_status()
        return True, BytesIO(response.content), info

    def get_download_progress(self, music_id: int,
                              quality: str = "standard") -> Dict[str, Any]:
        info = self.get_music_info(music_id, quality)
        ext = extension_for({'type': info.file_type})
        filename = build_filename(
            {"name": info.name, "artists": info.artists, "album": info.album, "id": music_id},
            settings.as_dict(), quality, ext,
        )
        path = output_dir() / filename
        current = path.stat().st_size if path.exists() else 0
        total = info.file_size or current
        return {
            "music_id": music_id,
            "filename": path.name,
            "total_size": total,
            "current_size": current,
            "progress": min(100, int(current / total * 100)) if total else 0,
            "completed": path.exists(),
        }

    @staticmethod
    def inspect(path: str) -> Dict[str, Any]:
        """读取已写入的标签，便于验证元数据。"""
        return inspect_tags(path)


if __name__ == "__main__":
    import sys
    from dataclasses import asdict

    downloader = MusicDownloader()
    if len(sys.argv) < 2:
        print("云音盒子下载器（兼容层）")
        print(f"输出目录: {output_dir()}")
        print("元数据: 内置 mutagen 写入，无需 ffmpeg")
        print("用法: python music_downloader.py <music_id> [quality] [--download]")
        sys.exit(0)
    try:
        mid = int(sys.argv[1])
    except ValueError:
        print("错误: music_id 必须是整数")
        sys.exit(1)
    quality = sys.argv[2] if len(sys.argv) > 2 and not sys.argv[2].startswith("--") else "lossless"
    info = downloader.get_music_info(mid, quality)
    for key, value in asdict(info).items():
        print(f"  {key:<14}: {str(value)[:120]}")
    if "--download" in sys.argv:
        result = downloader.download_music_file(mid, quality)
        if result.success:
            print(f"\n完成: {result.file_path} ({format_size(result.file_size)})")
            print(f"元数据: {result.metadata}")
        else:
            print(f"\n失败: {result.error_message}")
            sys.exit(1)
