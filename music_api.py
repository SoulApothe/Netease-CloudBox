"""网易云音乐API模块

提供网易云音乐相关API接口的封装，包括：
- 音乐URL获取
- 歌曲详情获取
- 歌词获取
- 搜索功能
- 歌单和专辑详情
- 二维码登录
"""

import json
import os
import re
import urllib.parse
import time
from random import randrange
from typing import Dict, List, Optional, Tuple, Any
from hashlib import md5
from enum import Enum

import requests
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from config import COOKIE_FILE, BASE_DIR


def loads_loose(text: str) -> Dict[str, Any]:
    """容错解析 eapi 响应。

    网易云 eapi 偶发返回拼接/多段 JSON（json.loads 会报 Extra data），
    这里退化为「取第一个完整 JSON 对象」，避免批量任务被偶发响应打断。
    """
    text = (text or "").strip()
    if not text:
        raise APIException("接口返回空内容")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        obj, _ = json.JSONDecoder().raw_decode(text.lstrip("\ufeff\n\r\t "))
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    # 多段 JSON 时，优先取带 data 字段的那一段
    for match in re.finditer(r"\{", text):
        try:
            obj, _ = json.JSONDecoder().raw_decode(text[match.start():])
            if isinstance(obj, dict) and obj.get("data"):
                return obj
        except json.JSONDecodeError:
            continue
    raise APIException(f"接口响应无法解析: {text[:80]!r}")


class QualityLevel(Enum):
    """音质等级枚举"""
    STANDARD = "standard"      # 标准音质
    EXHIGH = "exhigh"          # 极高音质
    LOSSLESS = "lossless"      # 无损音质
    HIRES = "hires"            # Hi-Res音质
    SKY = "sky"                # 沉浸环绕声
    JYEFFECT = "jyeffect"      # 高清环绕声
    JYMASTER = "jymaster"      # 超清母带
    DOLBY = "dolby"      # 杜比全景声


# 常量定义
class APIConstants:
    """API相关常量"""
    AES_KEY = b"e82ckenh8dichen8"
    USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Safari/537.36 Chrome/91.0.4472.164 NeteaseMusicDesktop/2.10.2.200154'
    REFERER = 'https://music.163.com/'
    
    # API URLs
    SONG_URL_V1 = "https://interface3.music.163.com/eapi/song/enhance/player/url/v1"
    SONG_DETAIL_V3 = "https://interface3.music.163.com/api/v3/song/detail"
    LYRIC_API = "https://interface3.music.163.com/api/song/lyric"
    SEARCH_API = 'https://music.163.com/api/cloudsearch/pc'
    PLAYLIST_DETAIL_API = 'https://music.163.com/api/v6/playlist/detail'
    ALBUM_DETAIL_API = 'https://music.163.com/api/v1/album/'
    QR_UNIKEY_API = 'https://interface3.music.163.com/eapi/login/qrcode/unikey'
    QR_LOGIN_API = 'https://interface3.music.163.com/eapi/login/qrcode/client/login'
    
    # 默认配置
    DEFAULT_CONFIG = {
        "os": "pc",
        "appver": "",
        "osver": "",
        "deviceId": "pyncm!"
    }
    
    DEFAULT_COOKIES = {
        "os": "pc",
        "appver": "",
        "osver": "",
        "deviceId": "pyncm!"
    }


class CryptoUtils:
    """加密工具类"""
    
    @staticmethod
    def hex_digest(data: bytes) -> str:
        """将字节数据转换为十六进制字符串"""
        return "".join([hex(d)[2:].zfill(2) for d in data])
    
    @staticmethod
    def hash_digest(text: str) -> bytes:
        """计算MD5哈希值"""
        return md5(text.encode("utf-8")).digest()
    
    @staticmethod
    def hash_hex_digest(text: str) -> str:
        """计算MD5哈希值并转换为十六进制字符串"""
        return CryptoUtils.hex_digest(CryptoUtils.hash_digest(text))
    
    @staticmethod
    def encrypt_params(url: str, payload: Dict[str, Any]) -> str:
        """加密请求参数"""
        url_path = urllib.parse.urlparse(url).path.replace("/eapi/", "/api/")
        digest = CryptoUtils.hash_hex_digest(f"nobody{url_path}use{json.dumps(payload)}md5forencrypt")
        params = f"{url_path}-36cd479b6b5-{json.dumps(payload)}-36cd479b6b5-{digest}"
        
        # AES加密
        padder = padding.PKCS7(algorithms.AES(APIConstants.AES_KEY).block_size).padder()
        padded_data = padder.update(params.encode()) + padder.finalize()
        cipher = Cipher(algorithms.AES(APIConstants.AES_KEY), modes.ECB())
        encryptor = cipher.encryptor()
        enc = encryptor.update(padded_data) + encryptor.finalize()
        
        return CryptoUtils.hex_digest(enc)


class HTTPClient:
    """HTTP客户端类"""
    
    @staticmethod
    def post_request(url: str, params: str, cookies: Dict[str, str]) -> str:
        """发送POST请求并返回文本响应"""
        headers = {
            'User-Agent': APIConstants.USER_AGENT,
            'Referer': APIConstants.REFERER,
        }
        
        request_cookies = APIConstants.DEFAULT_COOKIES.copy()
        request_cookies.update(cookies)
        
        try:
            response = requests.post(url, headers=headers, cookies=request_cookies, 
                                   data={"params": params}, timeout=30)
            response.raise_for_status()
            return response.text
        except requests.RequestException as e:
            raise APIException(f"HTTP请求失败: {e}")
    
    @staticmethod
    def post_request_full(url: str, params: str, cookies: Dict[str, str]) -> requests.Response:
        """发送POST请求并返回完整响应对象"""
        headers = {
            'User-Agent': APIConstants.USER_AGENT,
            'Referer': APIConstants.REFERER,
        }
        
        request_cookies = APIConstants.DEFAULT_COOKIES.copy()
        request_cookies.update(cookies)
        
        try:
            response = requests.post(url, headers=headers, cookies=request_cookies, 
                                   data={"params": params}, timeout=30)
            response.raise_for_status()
            return response
        except requests.RequestException as e:
            raise APIException(f"HTTP请求失败: {e}")


class APIException(Exception):
    """API异常类"""
    pass


class NeteaseAPI:
    """网易云音乐API主类"""
    
    def __init__(self):
        self.http_client = HTTPClient()
        self.crypto_utils = CryptoUtils()
    
    def get_song_url(self, song_id, quality: str, cookies: Dict[str, str]) -> Dict[str, Any]:
        """获取歌曲播放/下载直链。

        song_id 可以是单个 ID，也可以是 ID 列表（批量，一次最多 ~100 个），
        批量模式大幅减少专辑 / 歌单下载时的请求数。
        """
        try:
            ids = song_id if isinstance(song_id, (list, tuple, set)) else [song_id]
            ids = [int(i) for i in ids if str(i).strip()]
            if not ids:
                raise APIException("歌曲 ID 列表为空")

            config = APIConstants.DEFAULT_CONFIG.copy()
            config["requestId"] = str(randrange(20000000, 30000000))

            payload = {
                'ids': ids,
                'level': quality,
                'encodeType': 'mp4' if quality == 'dolby' else 'flac',
                'header': json.dumps(config),
            }

            if quality == 'sky':
                payload['immerseType'] = 'c51'

            params = self.crypto_utils.encrypt_params(APIConstants.SONG_URL_V1, payload)
            response_text = self.http_client.post_request(APIConstants.SONG_URL_V1, params, cookies)

            result = loads_loose(response_text)
            # 部分情况下响应体不带 code，但 data 列表有效
            if result.get('code') not in (None, 200) and not result.get('data'):
                raise APIException(f"获取歌曲URL失败: {result.get('message', '未知错误')}")

            return result
        except APIException:
            raise
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            raise APIException(f"解析响应数据失败: {e}")
    
    def get_song_detail(self, song_id: int) -> Dict[str, Any]:
        """获取单首歌曲详细信息（保持旧接口形状）。"""
        songs = self.get_song_details([song_id])
        return {"code": 200, "songs": [songs[str(song_id)]] if str(song_id) in songs else []}

    def get_song_details(self, song_ids, cookies: Optional[Dict[str, str]] = None,
                         batch_size: int = 100) -> Dict[str, Dict[str, Any]]:
        """批量获取歌曲详情，返回 {id字符串: 原始歌曲字典}。

        一次最多 batch_size 首，避免大歌单被接口截断。
        """
        ids = [str(i).strip() for i in song_ids if str(i).strip()]
        headers = {
            'User-Agent': APIConstants.USER_AGENT,
            'Referer': APIConstants.REFERER,
        }
        result: Dict[str, Dict[str, Any]] = {}
        for start in range(0, len(ids), batch_size):
            chunk = ids[start:start + batch_size]
            try:
                payload = {'c': json.dumps([{'id': int(sid), 'v': 0} for sid in chunk])}
                response = requests.post(
                    APIConstants.SONG_DETAIL_V3, data=payload,
                    headers=headers, cookies=cookies or {}, timeout=30,
                )
                response.raise_for_status()
                data = loads_loose(response.text)
                for song in data.get('songs', []):
                    result[str(song.get('id'))] = song
            except (requests.RequestException, ValueError, APIException) as exc:
                # 单批失败不影响其它批次，缺失的歌曲由调用方兜底
                print(f"[music_api] 批量详情第 {start // batch_size + 1} 批失败: {exc}")
                continue
        return result
    
    def get_lyric(self, song_id: int, cookies: Dict[str, str]) -> Dict[str, Any]:
        """获取歌词信息
        
        Args:
            song_id: 歌曲ID
            cookies: 用户cookies
            
        Returns:
            包含歌词信息的字典
            
        Raises:
            APIException: API调用失败时抛出
        """
        try:
            data = {
                'id': song_id, 
                'cp': 'false', 
                'tv': '0', 
                'lv': '0', 
                'rv': '0', 
                'kv': '0', 
                'yv': '0', 
                'ytv': '0', 
                'yrv': '0'
            }
            
            headers = {
                'User-Agent': APIConstants.USER_AGENT,
                'Referer': APIConstants.REFERER
            }
            
            response = requests.post(APIConstants.LYRIC_API, data=data, 
                                   headers=headers, cookies=cookies, timeout=30)
            response.raise_for_status()
            
            result = response.json()
            if result.get('code') != 200:
                raise APIException(f"获取歌词失败: {result.get('message', '未知错误')}")
            
            return result
        except requests.RequestException as e:
            raise APIException(f"获取歌词请求失败: {e}")
        except json.JSONDecodeError as e:
            raise APIException(f"解析歌词响应失败: {e}")
    
    def search_music(self, keywords: str, cookies: Dict[str, str], limit: int = 10,
                     search_type: int = 1, offset: int = 0) -> List[Dict[str, Any]]:
        """搜索歌曲 / 专辑 / 歌单。

        search_type: 1=歌曲, 100=歌手, 10=专辑, 1000=歌单
        统一返回结构: id / name / artists / artist_list / album / picUrl /
                      duration / type / subtitle / extra
        """
        try:
            data = {'s': keywords, 'type': search_type, 'limit': limit, 'offset': offset}
            headers = {
                'User-Agent': APIConstants.USER_AGENT,
                'Referer': APIConstants.REFERER
            }

            response = requests.post(APIConstants.SEARCH_API, data=data,
                                   headers=headers, cookies=cookies, timeout=30)
            response.raise_for_status()

            result = loads_loose(response.text)
            if result.get('code') != 200 and not result.get('result'):
                raise APIException(f"搜索失败: {result.get('message', '未知错误')}")

            bucket = result.get('result', {}) or {}
            items: List[Dict[str, Any]] = []

            if search_type == 100:
                for a in bucket.get('artists') or []:
                    alias = a.get('alias') or a.get('alia') or []
                    items.append({
                        'id': a.get('id'), 'name': a.get('name', ''),
                        'artists': '/'.join(alias[:1]) if alias else '',
                        'artist_list': [], 'album': '',
                        'picUrl': a.get('picUrl') or a.get('img1v1Url') or '',
                        'duration': 0, 'type': 'artist',
                        'subtitle': ('别名：' + ' / '.join(alias[:2])) if alias else '歌手',
                        'extra': f"{a.get('albumSize', 0)} 张专辑 · {a.get('musicSize', 0)} 首",
                        'alias': alias, 'musicSize': a.get('musicSize', 0),
                        'albumSize': a.get('albumSize', 0),
                        'accountId': a.get('accountId'),
                    })
            elif search_type == 10:
                for a in bucket.get('albums') or []:
                    artist = (a.get('artist') or {}).get('name', '')
                    items.append({
                        'id': a.get('id'), 'name': a.get('name', ''),
                        'artists': artist, 'artist_list': [artist] if artist else [],
                        'album': '', 'picUrl': a.get('picUrl', ''),
                        'duration': 0, 'type': 'album',
                        'subtitle': f"专辑 · {artist}",
                        'extra': f"{a.get('size', 0)} 首",
                    })
            elif search_type == 1000:
                for p in bucket.get('playlists') or []:
                    creator = (p.get('creator') or {}).get('nickname', '')
                    items.append({
                        'id': p.get('id'), 'name': p.get('name', ''),
                        'artists': creator, 'artist_list': [],
                        'album': '', 'picUrl': p.get('coverImgUrl', ''),
                        'duration': 0, 'type': 'playlist',
                        'subtitle': f"歌单 · by {creator}",
                        'extra': f"{p.get('trackCount', 0)} 首",
                    })
            else:
                for item in bucket.get('songs') or []:
                    artist_list = [x.get('name', '') for x in item.get('ar', []) if x.get('name')]
                    items.append({
                        'id': item.get('id'), 'name': item.get('name', ''),
                        'artists': '/'.join(artist_list),
                        'artist_list': artist_list,
                        'album': (item.get('al') or {}).get('name', ''),
                        'picUrl': (item.get('al') or {}).get('picUrl', ''),
                        'album_id': (item.get('al') or {}).get('id'),
                        'duration': item.get('dt') or item.get('duration') or 0,
                        'type': 'song',
                        'subtitle': '/'.join(artist_list),
                        'extra': (item.get('al') or {}).get('name', ''),
                    })
            return items
        except requests.RequestException as e:
            raise APIException(f"搜索请求失败: {e}")
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            raise APIException(f"解析搜索响应失败: {e}")
    
    def normalize_song(self, song: Dict[str, Any], index: int = 0,
                       total: int = 0) -> Dict[str, Any]:
        """把网易云原始歌曲结构统一成桌面版使用的 Track 结构。

        兼容两种返回格式：
        - 搜索 / 详情接口：al / ar / dt / no
        - 歌手歌曲接口：album / artists / duration / position（封面在 album.picUrl）
        """
        album = song.get('al') or song.get('album') or {}
        if not isinstance(album, dict):
            album = {'name': str(album)}
        raw_artists = song.get('ar') or song.get('artists') or []
        if not isinstance(raw_artists, list):
            raw_artists = [raw_artists]
        artists = [a.get('name', '') for a in raw_artists if isinstance(a, dict) and a.get('name')]
        pic = album.get('picUrl') or self.get_pic_url(album.get('pic'))
        published = song.get('publishTime') or album.get('publishTime')
        year = ""
        if published:
            try:
                year = time.strftime('%Y', time.localtime(int(published) / 1000))
            except (ValueError, OSError, OverflowError):
                pass
        alia = song.get('alia') or song.get('alias') or []
        disc = song.get('disc') or song.get('cd') or "1"
        return {
            'id': song.get('id'),
            'name': song.get('name', '') or '',
            'artists': '/'.join(artists),
            'artist_list': artists,
            'album': album.get('name', '') or '',
            'album_id': album.get('id'),
            'picUrl': pic or '',
            'duration': song.get('dt') or song.get('duration') or 0,
            'track_no': song.get('no') or song.get('position') or (index + 1),
            'track_total': total,
            'disc': str(disc),
            'year': year,
            'alia': ' / '.join(alia) if isinstance(alia, list) else str(alia),
        }

    def get_playlist_detail(self, playlist_id: int, cookies: Dict[str, str]) -> Dict[str, Any]:
        """获取歌单详情（含完整曲目元数据，用于批量下载写入标签）。"""
        try:
            data = {'id': playlist_id, 'n': 100000, 's': 0}
            headers = {
                'User-Agent': APIConstants.USER_AGENT,
                'Referer': APIConstants.REFERER
            }

            response = requests.post(APIConstants.PLAYLIST_DETAIL_API, data=data,
                                   headers=headers, cookies=cookies, timeout=30)
            response.raise_for_status()

            result = loads_loose(response.text)
            playlist = result.get('playlist') or {}
            if not playlist:
                raise APIException(f"获取歌单详情失败: {result.get('message') or result.get('msg') or '歌单不存在'}")

            info = {
                'id': playlist.get('id'),
                'name': playlist.get('name') or '',
                'coverImgUrl': playlist.get('coverImgUrl') or '',
                'creator': (playlist.get('creator') or {}).get('nickname', ''),
                'trackCount': playlist.get('trackCount') or 0,
                'description': playlist.get('description') or '',
                'type': 'playlist',
                'tracks': [],
            }

            track_ids = [str(t['id']) for t in (playlist.get('trackIds') or []) if t.get('id')]
            if not track_ids:
                track_ids = [str(t['id']) for t in (playlist.get('tracks') or []) if t.get('id')]

            details = self.get_song_details(track_ids, cookies)
            total = len(track_ids)
            for index, sid in enumerate(track_ids):
                raw = details.get(sid)
                if raw:
                    info['tracks'].append(self.normalize_song(raw, index, total))
            info['trackCount'] = len(info['tracks']) or info['trackCount']
            for i, track in enumerate(info['tracks']):
                track['track_total'] = len(info['tracks'])
            return info
        except requests.RequestException as e:
            raise APIException(f"获取歌单详情请求失败: {e}")
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            raise APIException(f"解析歌单详情响应失败: {e}")
    
    def get_album_detail(self, album_id: int, cookies: Dict[str, str]) -> Dict[str, Any]:
        """获取专辑详情（含曲目号、碟号、发行年份，用于元数据写入）。"""
        try:
            url = f'{APIConstants.ALBUM_DETAIL_API}{album_id}'
            headers = {
                'User-Agent': APIConstants.USER_AGENT,
                'Referer': APIConstants.REFERER
            }

            response = requests.get(url, headers=headers, cookies=cookies, timeout=30)
            response.raise_for_status()

            result = loads_loose(response.text)
            album = result.get('album') or {}
            if not album:
                raise APIException(f"获取专辑详情失败: {result.get('message') or result.get('msg') or '专辑不存在'}")

            published = album.get('publishTime')
            year = ""
            if published:
                try:
                    year = time.strftime('%Y', time.localtime(int(published) / 1000))
                except (ValueError, OSError, OverflowError):
                    pass

            cover = album.get('picUrl') or self.get_pic_url(album.get('pic'))
            artist = (album.get('artist') or {}).get('name', '')
            info = {
                'id': album.get('id'),
                'name': album.get('name') or '',
                'coverImgUrl': cover,
                'artist': artist,
                'creator': artist,
                'publishTime': published,
                'year': year,
                'company': album.get('company') or '',
                'description': album.get('description') or '',
                'type': 'album',
                'tracks': [],
                'songs': [],  # 兼容旧调用方
            }

            songs = result.get('songs') or []
            total = len(songs)
            for index, song in enumerate(songs):
                normalized = self.normalize_song(song, index, total)
                normalized['year'] = normalized['year'] or year
                normalized['album'] = normalized['album'] or album.get('name', '')
                normalized['album_id'] = album.get('id')
                normalized['picUrl'] = normalized['picUrl'] or cover
                if artist and not normalized['artist_list']:
                    normalized['artist_list'] = [artist]
                    normalized['artists'] = artist
                info['tracks'].append(normalized)
            info['trackCount'] = total
            info['songs'] = info['tracks']
            return info
        except requests.RequestException as e:
            raise APIException(f"获取专辑详情请求失败: {e}")
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            raise APIException(f"解析专辑详情响应失败: {e}")
    
    def netease_encrypt_id(self, id_str: str) -> str:
        """网易云加密图片ID算法
        
        Args:
            id_str: 图片ID字符串
            
        Returns:
            加密后的字符串
        """
        import base64
        import hashlib
        
        magic = list('3go8&$8*3*3h0k(2)2')
        song_id = list(id_str)
        
        for i in range(len(song_id)):
            song_id[i] = chr(ord(song_id[i]) ^ ord(magic[i % len(magic)]))
        
        m = ''.join(song_id)
        md5_bytes = hashlib.md5(m.encode('utf-8')).digest()
        result = base64.b64encode(md5_bytes).decode('utf-8')
        result = result.replace('/', '_').replace('+', '-')
        
        return result
    
    def get_pic_url(self, pic_id: Optional[int], size: int = 300) -> str:
        """获取网易云加密歌曲/专辑封面直链
        
        Args:
            pic_id: 封面ID
            size: 图片尺寸
            
        Returns:
            图片URL
        """
        if pic_id is None:
            return ''
        
        enc_id = self.netease_encrypt_id(str(pic_id))
        return f'https://p3.music.126.net/{enc_id}/{pic_id}.jpg?param={size}y{size}'


class QRLoginManager:
    """二维码登录管理器"""
    
    def __init__(self):
        self.http_client = HTTPClient()
        self.crypto_utils = CryptoUtils()
    
    def generate_qr_key(self) -> Optional[str]:
        """生成二维码的key
        
        Returns:
            成功返回unikey，失败返回None
            
        Raises:
            APIException: API调用失败时抛出
        """
        try:
            config = APIConstants.DEFAULT_CONFIG.copy()
            config["requestId"] = str(randrange(20000000, 30000000))
            
            payload = {
                'type': 1,
                'header': json.dumps(config)
            }
            
            params = self.crypto_utils.encrypt_params(APIConstants.QR_UNIKEY_API, payload)
            response = self.http_client.post_request_full(APIConstants.QR_UNIKEY_API, params, {})
            
            result = loads_loose(response.text)
            if result.get('code') == 200:
                return result.get('unikey')
            else:
                raise APIException(f"生成二维码key失败: {result.get('message', '未知错误')}")
        except (json.JSONDecodeError, KeyError) as e:
            raise APIException(f"解析二维码key响应失败: {e}")

    @staticmethod
    def login_qr_url(unikey: str) -> str:
        """二维码内容（网易云 App 扫码后跳转的链接）。"""
        return f'https://music.163.com/login?codekey={unikey}'

    @staticmethod
    def qr_matrix(text: str, border: int = 2):
        """生成二维码布尔矩阵，交给桌面端 canvas 绘制（无需 Pillow / 图片文件）。"""
        import qrcode
        from qrcode.constants import ERROR_CORRECT_M
        qr = qrcode.QRCode(error_correction=ERROR_CORRECT_M, box_size=1, border=border)
        qr.add_data(text)
        qr.make(fit=True)
        return [[bool(cell) for cell in row] for row in qr.get_matrix()]

    @staticmethod
    def format_cookie_string(cookies: Dict[str, str]) -> str:
        """把 cookie dict 拼成可写入 cookie.txt 的字符串。"""
        cookies = dict(cookies or {})
        cookies.setdefault('os', 'pc')
        cookies.setdefault('appver', '8.9.70')
        return '; '.join(f'{k}={v}' for k, v in cookies.items() if v) + ';'

    def get_user_profile(self, cookies: Dict[str, str]) -> Dict[str, Any]:
        """查询当前 Cookie 对应的账号昵称（用于桌面版显示登录状态）。"""
        try:
            response = requests.get(
                'https://music.163.com/api/v1/user/detail',
                headers={'User-Agent': APIConstants.USER_AGENT, 'Referer': APIConstants.REFERER},
                cookies=cookies, timeout=12,
            )
            data = loads_loose(response.text)
            profile = data.get('profile') or {}
            if profile.get('userId'):
                return {
                    'logged_in': True,
                    'nickname': profile.get('nickname', ''),
                    'user_id': profile.get('userId'),
                    'avatar': profile.get('avatarUrl', ''),
                }
        except Exception:
            pass
        return {'logged_in': False}
    
    def create_qr_login(self) -> Optional[str]:
        """创建登录二维码并在控制台显示
        
        Returns:
            成功返回unikey，失败返回None
        """
        try:
            import qrcode
            
            unikey = self.generate_qr_key()
            if not unikey:
                print("生成二维码key失败")
                return None
            
            # 创建二维码
            qr = qrcode.QRCode()
            qr.add_data(f'https://music.163.com/login?codekey={unikey}')
            qr.make(fit=True)
            
            # 在控制台显示二维码
            qr.print_ascii(tty=True)
            print("\n请使用网易云音乐APP扫描上方二维码登录")
            return unikey
        except ImportError:
            print("请安装qrcode库: pip install qrcode")
            return None
        except Exception as e:
            print(f"创建二维码失败: {e}")
            return None
    
    def check_qr_login(self, unikey: str) -> Tuple[int, Dict[str, str]]:
        """检查二维码登录状态
        
        Args:
            unikey: 二维码key
            
        Returns:
            (登录状态码, cookie字典)
            
        Raises:
            APIException: API调用失败时抛出
        """
        try:
            config = APIConstants.DEFAULT_CONFIG.copy()
            config["requestId"] = str(randrange(20000000, 30000000))
            
            payload = {
                'key': unikey,
                'type': 1,
                'header': json.dumps(config)
            }
            
            params = self.crypto_utils.encrypt_params(APIConstants.QR_LOGIN_API, payload)
            response = self.http_client.post_request_full(APIConstants.QR_LOGIN_API, params, {})
            
            result = loads_loose(response.text)
            code = result.get('code', -1)
            cookie_dict: Dict[str, str] = {}

            if code == 803:
                # 优先用 requests 已解析好的 cookie jar（比手工切 Set-Cookie 可靠）
                try:
                    cookie_dict = dict(response.cookies.get_dict())
                except Exception:
                    cookie_dict = {}
                if not cookie_dict.get('MUSIC_U'):
                    raw_cookies = response.headers.get('Set-Cookie', '')
                    for key, value in re.findall(r'([A-Za-z0-9_]+)=([^;,\s]+)', raw_cookies):
                        cookie_dict.setdefault(key, value)
                cookie_dict.setdefault('os', 'pc')

            return code, cookie_dict
        except (json.JSONDecodeError, KeyError) as e:
            raise APIException(f"解析登录状态响应失败: {e}")
    
    def qr_login(self) -> Optional[str]:
        """完整的二维码登录流程
        
        Returns:
            成功返回cookie字符串，失败返回None
        """
        try:
            unikey = self.create_qr_login()
            if not unikey:
                return None
            
            while True:
                code, cookies = self.check_qr_login(unikey)
                
                if code == 803:
                    print("\n登录成功！")
                    return f"MUSIC_U={cookies['MUSIC_U']};os=pc;appver=8.9.70;"
                elif code == 801:
                    print("\r等待扫码...", end='')
                elif code == 802:
                    print("\r扫码成功，请在手机上确认登录...", end='')
                else:
                    print(f"\n登录失败，错误码：{code}")
                    return None
                
                time.sleep(2)
        except KeyboardInterrupt:
            print("\n用户取消登录")
            return None
        except Exception as e:
            print(f"\n登录过程中发生错误: {e}")
            return None


# 向后兼容的函数接口
def url_v1(song_id: int, level: str, cookies: Dict[str, str]) -> Dict[str, Any]:
    """获取歌曲URL（向后兼容）"""
    api = NeteaseAPI()
    return api.get_song_url(song_id, level, cookies)


def name_v1(song_id: int) -> Dict[str, Any]:
    """获取歌曲详情（向后兼容）"""
    api = NeteaseAPI()
    return api.get_song_detail(song_id)


def lyric_v1(song_id: int, cookies: Dict[str, str]) -> Dict[str, Any]:
    """获取歌词（向后兼容）"""
    api = NeteaseAPI()
    return api.get_lyric(song_id, cookies)


def search_music(keywords: str, cookies: Dict[str, str], limit: int = 10,
                 search_type: int = 1, offset: int = 0) -> List[Dict[str, Any]]:
    """搜索音乐（向后兼容）"""
    api = NeteaseAPI()
    return api.search_music(keywords, cookies, limit, search_type, offset)


def playlist_detail(playlist_id: int, cookies: Dict[str, str]) -> Dict[str, Any]:
    """获取歌单详情（向后兼容）"""
    api = NeteaseAPI()
    return api.get_playlist_detail(playlist_id, cookies)


def album_detail(album_id: int, cookies: Dict[str, str]) -> Dict[str, Any]:
    """获取专辑详情（向后兼容）"""
    api = NeteaseAPI()
    return api.get_album_detail(album_id, cookies)


def get_pic_url(pic_id: Optional[int], size: int = 300) -> str:
    """获取图片URL（向后兼容）"""
    api = NeteaseAPI()
    return api.get_pic_url(pic_id, size)


def qr_login() -> Optional[str]:
    """二维码登录（向后兼容）"""
    manager = QRLoginManager()
    return manager.qr_login()


# ============ Cookie 工具 ============

DEFAULT_COOKIE_FILE = str(COOKIE_FILE)


def load_cookies(path=None) -> Dict[str, str]:
    """从 cookie.txt 直接读取并解析为 dict

    默认读取「程序根目录」下的 cookie.txt（不再依赖当前工作目录）。
    支持标准 ``NMTID=xxx; _ntes_nnid=xxx; ...`` 格式。
    文件不存在或解析失败返回空 dict（不抛异常，调用方决定如何处理）。
    """
    target = os.fspath(path or DEFAULT_COOKIE_FILE)
    if not os.path.isfile(target):
        return {}
    try:
        with open(target, "r", encoding="utf-8") as f:
            content = f.read().strip()
    except (OSError, UnicodeDecodeError):
        return {}
    return parse_cookie_string(content)


def parse_cookie_string(cookie_string: str) -> Dict[str, str]:
    """把 ``k1=v1; k2=v2`` 格式字符串解析为 dict

    支持 ``;`` 或 ``\\n`` 作为分隔符。
    """
    cookies: Dict[str, str] = {}
    if not cookie_string:
        return cookies
    cookie_string = cookie_string.strip()
    if not cookie_string:
        return cookies
    for sep in (";", "\n"):
        if sep in cookie_string:
            pairs = cookie_string.split(sep)
            break
    else:
        pairs = [cookie_string]
    for pair in pairs:
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        key, value = pair.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key and value:
            cookies[key] = value
    return cookies


if __name__ == "__main__":
    # 测试代码
    print("网易云音乐API模块")
    print("支持的功能:")
    print("- 歌曲URL获取")
    print("- 歌曲详情获取")
    print("- 歌词获取")
    print("- 音乐搜索")
    print("- 歌单详情")
    print("- 专辑详情")
    print("- 二维码登录")
