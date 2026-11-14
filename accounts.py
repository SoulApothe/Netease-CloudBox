"""云音盒子 · 本机档案（无登录）

设计原则（与网易云账号彻底解耦）：
- 不再有注册 / 登录 / 密码流程：启动时自动建一个固定的「本机档案」用户，
  收藏、歌手收藏、自建歌单、本地音乐收藏全部直接写入本地 SQLite。
- 数据只存在程序根目录 cloudbox.db，不读取也不写入 cookie.txt，
  不向任何服务上传个人信息。
- 三类收藏彼此分开：
    favorites        在线（流媒体）歌曲，按网易云歌曲 ID
    favorite_artists 收藏的歌手，按歌手 ID
    local_favorites  本地音频文件，按绝对路径
- 网易云 Cookie 只在「下载歌曲」时用于解锁会员音质，
  推荐 / 在线播放 / 跨平台匹配都走匿名公开接口。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from config import BASE_DIR

DB_FILE = BASE_DIR / "cloudbox.db"

# 固定的本机档案（不再有注册 / 登录）
LOCAL_USERNAME = "local"
LOCAL_DISPLAY_NAME = "本机档案"

# 收藏 / 歌单里保存的曲目字段（精简，避免把直链等易失效数据写进数据库）
SONG_FIELDS = (
    "id", "name", "artists", "artist_list", "album", "picUrl",
    "duration", "track_no", "alia", "album_id",
)


class AccountError(Exception):
    """账号操作的可预期错误（直接展示给用户）。"""


def normalize_song(raw: Dict[str, Any]) -> Dict[str, Any]:
    """把前端 / API 的曲目对象规范化成数据库存储结构。"""
    raw = raw or {}
    song_id = str(raw.get("id") or "").strip()
    if not song_id:
        raise AccountError("缺少歌曲 ID")
    artists = raw.get("artist_list") or []
    if isinstance(artists, str):
        artists = [x.strip() for x in artists.replace("、", "/").split("/") if x.strip()]
    if not artists and raw.get("artists"):
        artists = [x.strip() for x in str(raw["artists"]).replace("、", "/").split("/") if x.strip()]
    artists = [str(x) for x in artists if x][:12]
    result = {
        "id": song_id,
        "name": str(raw.get("name") or "未知歌曲")[:180],
        "artists": str(raw.get("artists") or "/".join(artists))[:180],
        "artist_list": artists,
        "album": str(raw.get("album") or "")[:180],
        "picUrl": str(raw.get("picUrl") or raw.get("pic") or "")[:500],
        "duration": int(raw.get("duration") or 0) if str(raw.get("duration") or "").isdigit() else 0,
        "track_no": int(raw.get("track_no") or 0) if str(raw.get("track_no") or "").isdigit() else 0,
        "alia": str(raw.get("alia") or "")[:180],
        "album_id": raw.get("album_id") or "",
    }
    return result


def normalize_artist(raw: Dict[str, Any]) -> Dict[str, Any]:
    """歌手收藏的存储结构。"""
    raw = raw or {}
    artist_id = str(raw.get("id") or "").strip()
    if not artist_id:
        raise AccountError("缺少歌手 ID")
    alias = raw.get("alias") or raw.get("alia") or []
    if isinstance(alias, str):
        alias = [alias]
    return {
        "id": artist_id,
        "name": str(raw.get("name") or "未知歌手")[:120],
        "picUrl": str(raw.get("picUrl") or raw.get("pic") or "")[:500],
        "alias": [str(x)[:60] for x in alias if x][:6],
        "musicSize": int(raw.get("musicSize") or 0) if str(raw.get("musicSize") or "").isdigit() else 0,
        "albumSize": int(raw.get("albumSize") or 0) if str(raw.get("albumSize") or "").isdigit() else 0,
    }


def normalize_local(raw: Dict[str, Any]) -> Dict[str, Any]:
    """本地音频收藏的存储结构（按绝对路径去重）。"""
    raw = raw or {}
    path = str(raw.get("path") or "").strip()
    if not path:
        raise AccountError("缺少本地文件路径")
    return {
        "path": path[:1000],
        "id": str(raw.get("id") or ("local:" + path))[:200],
        "name": str(raw.get("name") or Path(path).stem)[:180],
        "artists": str(raw.get("artists") or raw.get("artist") or "")[:180],
        "album": str(raw.get("album") or "")[:180],
        "duration": int(raw.get("duration") or 0) if str(raw.get("duration") or "").isdigit() else 0,
        "size": int(raw.get("size") or 0) if str(raw.get("size") or "").isdigit() else 0,
        "ext": str(raw.get("ext") or Path(path).suffix.lower())[:12],
        "local": True,
    }


def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                               bytes.fromhex(salt), 120_000).hex()


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class AccountStore:
    """本地账号仓储；每次操作独立连接，天然适配 Flask 的多线程。"""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path or DB_FILE)
        self.init_db()
        self._local: Dict[str, Any] = self._ensure_local_user()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def init_db(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    salt TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS favorites (
                    user_id INTEGER NOT NULL,
                    song_id TEXT NOT NULL,
                    data TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(user_id, song_id),
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS playlists (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    cover TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS playlist_songs (
                    playlist_id INTEGER NOT NULL,
                    song_id TEXT NOT NULL,
                    data TEXT NOT NULL,
                    sort INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(playlist_id, song_id),
                    FOREIGN KEY(playlist_id) REFERENCES playlists(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS favorite_artists (
                    user_id INTEGER NOT NULL,
                    artist_id TEXT NOT NULL,
                    data TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(user_id, artist_id),
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS local_favorites (
                    user_id INTEGER NOT NULL,
                    path TEXT NOT NULL,
                    data TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(user_id, path),
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_fav_user ON favorites(user_id);
                CREATE INDEX IF NOT EXISTS idx_pl_user ON playlists(user_id);
                CREATE INDEX IF NOT EXISTS idx_pls_pl ON playlist_songs(playlist_id, sort);
                CREATE INDEX IF NOT EXISTS idx_artist_user ON favorite_artists(user_id);
                CREATE INDEX IF NOT EXISTS idx_local_user ON local_favorites(user_id);
            """)

    def _ensure_local_user(self) -> Dict[str, Any]:
        """免登录：数据库里始终存在一个固定的本机档案用户。"""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE username=?",
                               (LOCAL_USERNAME,)).fetchone()
            if not row:
                salt = secrets.token_hex(16)
                # 随机密码，任何人都不会用它登录（本机模式不使用登录流程）
                digest = _hash_password(secrets.token_urlsafe(32), salt)
                conn.execute(
                    "INSERT OR IGNORE INTO users(username,display_name,password_hash,salt,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (LOCAL_USERNAME, LOCAL_DISPLAY_NAME, digest, salt, _now()),
                )
                row = conn.execute("SELECT * FROM users WHERE username=?",
                                   (LOCAL_USERNAME,)).fetchone()
        return self.public_user(row)

    def local_user(self) -> Dict[str, Any]:
        return dict(self._local)

    # ---------- 账号 ----------

    @staticmethod
    def public_user(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "username": row["username"],
            "display_name": row["display_name"],
            "created_at": row["created_at"],
        }

    def register(self, username: str, password: str,
                 display_name: str = "") -> Dict[str, Any]:
        username = str(username or "").strip().lower()
        password = str(password or "")
        if len(username) < 2 or len(username) > 32:
            raise AccountError("用户名需为 2–32 个字符")
        if not all(c.isalnum() or c in "_-" for c in username):
            raise AccountError("用户名只能包含字母、数字、下划线和短横线")
        if len(password) < 6 or len(password) > 128:
            raise AccountError("密码需为 6–128 位")
        salt = secrets.token_hex(16)
        digest = _hash_password(password, salt)
        name = str(display_name or "").strip()[:32] or username
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "INSERT INTO users(username, display_name, password_hash, salt, created_at) "
                    "VALUES(?,?,?,?,?)",
                    (username, name, digest, salt, _now()),
                )
                uid = cur.lastrowid
        except sqlite3.IntegrityError:
            raise AccountError("该用户名已存在")
        return {"id": uid, "username": username, "display_name": name, "created_at": _now()}

    def login(self, username: str, password: str) -> Dict[str, Any]:
        username = str(username or "").strip().lower()
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if not row or not hmac.compare_digest(
            _hash_password(str(password or ""), row["salt"]), row["password_hash"]
        ):
            raise AccountError("用户名或密码不正确")
        token = secrets.token_urlsafe(28)
        with self._connect() as conn:
            conn.execute("INSERT INTO sessions(token,user_id,created_at) VALUES(?,?,?)",
                         (token, row["id"], _now()))
            # 只保留最近 10 个会话
            conn.execute(
                "DELETE FROM sessions WHERE user_id=? AND token NOT IN "
                "(SELECT token FROM sessions WHERE user_id=? ORDER BY created_at DESC, rowid DESC LIMIT 10)",
                (row["id"], row["id"]),
            )
        return {"token": token, "user": self.public_user(row)}

    def logout(self, token: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM sessions WHERE token=?", (token or "",))
            return cur.rowcount > 0

    def user_by_token(self, token: str) -> Optional[Dict[str, Any]]:
        if not token:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT u.* FROM users u JOIN sessions s ON s.user_id=u.id WHERE s.token=?",
                (token,),
            ).fetchone()
        return self.public_user(row) if row else None

    def stats(self, user_id: int) -> Dict[str, int]:
        with self._connect() as conn:
            fav = conn.execute("SELECT COUNT(*) c FROM favorites WHERE user_id=?", (user_id,)).fetchone()["c"]
            pls = conn.execute("SELECT COUNT(*) c FROM playlists WHERE user_id=?", (user_id,)).fetchone()["c"]
            artists = conn.execute("SELECT COUNT(*) c FROM favorite_artists WHERE user_id=?", (user_id,)).fetchone()["c"]
            local = conn.execute("SELECT COUNT(*) c FROM local_favorites WHERE user_id=?", (user_id,)).fetchone()["c"]
        return {"favorites": fav, "playlists": pls,
                "artists": artists, "local_favorites": local}

    # ---------- 收藏 ----------

    def favorite_ids(self, user_id: int) -> Set[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT song_id FROM favorites WHERE user_id=?", (user_id,)).fetchall()
        return {str(r["song_id"]) for r in rows}

    def favorites(self, user_id: int) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT data, created_at FROM favorites WHERE user_id=? ORDER BY created_at DESC, rowid DESC",
                (user_id,),
            ).fetchall()
        result = []
        for row in rows:
            try:
                item = json.loads(row["data"])
                item["favorited_at"] = row["created_at"]
                result.append(item)
            except (ValueError, TypeError):
                continue
        return result

    def add_favorite(self, user_id: int, raw_song: Dict[str, Any]) -> Dict[str, Any]:
        song = normalize_song(raw_song)
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO favorites(user_id,song_id,data,created_at) VALUES(?,?,?,?)",
                (user_id, song["id"], json.dumps(song, ensure_ascii=False), _now()),
            )
        return song

    def remove_favorite(self, user_id: int, song_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM favorites WHERE user_id=? AND song_id=?",
                               (user_id, str(song_id)))
            return cur.rowcount > 0

    # ---------- 收藏的歌手（在线） ----------

    def favorite_artist_ids(self, user_id: int) -> Set[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT artist_id FROM favorite_artists WHERE user_id=?",
                                (user_id,)).fetchall()
        return {str(r["artist_id"]) for r in rows}

    def favorite_artists(self, user_id: int) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT data, created_at FROM favorite_artists WHERE user_id=? "
                "ORDER BY created_at DESC, rowid DESC", (user_id,),
            ).fetchall()
        result = []
        for row in rows:
            try:
                item = json.loads(row["data"])
                item["favorited_at"] = row["created_at"]
                result.append(item)
            except (ValueError, TypeError):
                continue
        return result

    def add_favorite_artist(self, user_id: int, raw: Dict[str, Any]) -> Dict[str, Any]:
        artist = normalize_artist(raw)
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO favorite_artists(user_id,artist_id,data,created_at) "
                "VALUES(?,?,?,?)",
                (user_id, artist["id"], json.dumps(artist, ensure_ascii=False), _now()),
            )
        return artist

    def remove_favorite_artist(self, user_id: int, artist_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM favorite_artists WHERE user_id=? AND artist_id=?",
                               (user_id, str(artist_id)))
            return cur.rowcount > 0

    # ---------- 本地音乐收藏（与在线收藏完全分开） ----------

    def local_favorite_paths(self, user_id: int) -> Set[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT path FROM local_favorites WHERE user_id=?",
                                (user_id,)).fetchall()
        return {str(r["path"]) for r in rows}

    def local_favorites(self, user_id: int) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT data, created_at FROM local_favorites WHERE user_id=? "
                "ORDER BY created_at DESC, rowid DESC", (user_id,),
            ).fetchall()
        result = []
        for row in rows:
            try:
                item = json.loads(row["data"])
                item["favorited_at"] = row["created_at"]
                result.append(item)
            except (ValueError, TypeError):
                continue
        return result

    def add_local_favorite(self, user_id: int, raw: Dict[str, Any]) -> Dict[str, Any]:
        item = normalize_local(raw)
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO local_favorites(user_id,path,data,created_at) "
                "VALUES(?,?,?,?)",
                (user_id, item["path"], json.dumps(item, ensure_ascii=False), _now()),
            )
        return item

    def remove_local_favorite(self, user_id: int, path: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM local_favorites WHERE user_id=? AND path=?",
                               (user_id, str(path)))
            return cur.rowcount > 0

    # ---------- 自建歌单 ----------

    def playlists(self, user_id: int) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT p.*, (SELECT COUNT(*) FROM playlist_songs s WHERE s.playlist_id=p.id) AS song_count "
                "FROM playlists p WHERE p.user_id=? ORDER BY p.created_at DESC, p.id DESC",
                (user_id,),
            ).fetchall()
        return [{
            "id": r["id"], "name": r["name"], "description": r["description"],
            "cover": r["cover"], "created_at": r["created_at"],
            "song_count": r["song_count"],
        } for r in rows]

    def _owned_playlist(self, conn: sqlite3.Connection, user_id: int, playlist_id: int):
        row = conn.execute("SELECT * FROM playlists WHERE id=? AND user_id=?",
                           (playlist_id, user_id)).fetchone()
        if not row:
            raise AccountError("歌单不存在或不属于当前账号")
        return row

    def create_playlist(self, user_id: int, name: str,
                        description: str = "", cover: str = "") -> Dict[str, Any]:
        name = str(name or "").strip()
        if not name:
            raise AccountError("请输入歌单名称")
        if len(name) > 60:
            raise AccountError("歌单名称不能超过 60 个字符")
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO playlists(user_id,name,description,cover,created_at) VALUES(?,?,?,?,?)",
                (user_id, name, str(description or "")[:500], str(cover or "")[:500], _now()),
            )
            pid = cur.lastrowid
        return {"id": pid, "name": name, "description": description, "cover": cover, "song_count": 0}

    def rename_playlist(self, user_id: int, playlist_id: int, name: str) -> Dict[str, Any]:
        name = str(name or "").strip()
        if not name:
            raise AccountError("请输入歌单名称")
        with self._connect() as conn:
            self._owned_playlist(conn, user_id, int(playlist_id))
            conn.execute("UPDATE playlists SET name=? WHERE id=?", (name[:60], playlist_id))
        return {"id": int(playlist_id), "name": name[:60]}

    def delete_playlist(self, user_id: int, playlist_id: int) -> bool:
        with self._connect() as conn:
            self._owned_playlist(conn, user_id, int(playlist_id))
            conn.execute("DELETE FROM playlist_songs WHERE playlist_id=?", (playlist_id,))
            conn.execute("DELETE FROM playlists WHERE id=?", (playlist_id,))
        return True

    def playlist_detail(self, user_id: int, playlist_id: int) -> Dict[str, Any]:
        with self._connect() as conn:
            row = self._owned_playlist(conn, user_id, int(playlist_id))
            songs = conn.execute(
                "SELECT data FROM playlist_songs WHERE playlist_id=? ORDER BY sort ASC, rowid ASC",
                (playlist_id,),
            ).fetchall()
        tracks = []
        for item in songs:
            try:
                tracks.append(json.loads(item["data"]))
            except (ValueError, TypeError):
                continue
        return {
            "id": row["id"], "name": row["name"], "description": row["description"],
            "cover": row["cover"], "created_at": row["created_at"], "tracks": tracks,
        }

    def add_to_playlist(self, user_id: int, playlist_id: int,
                        raw_songs: List[Dict[str, Any]]) -> Dict[str, Any]:
        songs = [normalize_song(x) for x in (raw_songs or [])]
        if not songs:
            raise AccountError("没有可添加的歌曲")
        added = 0
        with self._connect() as conn:
            self._owned_playlist(conn, user_id, int(playlist_id))
            next_sort = conn.execute(
                "SELECT COALESCE(MAX(sort),0)+1 n FROM playlist_songs WHERE playlist_id=?",
                (playlist_id,),
            ).fetchone()["n"]
            for offset, song in enumerate(songs):
                cur = conn.execute(
                    "INSERT OR IGNORE INTO playlist_songs(playlist_id,song_id,data,sort,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (playlist_id, song["id"], json.dumps(song, ensure_ascii=False),
                     next_sort + offset, _now()),
                )
                added += cur.rowcount
        return {"added": added, "total": len(songs)}

    def remove_from_playlist(self, user_id: int, playlist_id: int, song_id: str) -> bool:
        with self._connect() as conn:
            self._owned_playlist(conn, user_id, int(playlist_id))
            cur = conn.execute("DELETE FROM playlist_songs WHERE playlist_id=? AND song_id=?",
                               (playlist_id, str(song_id)))
            return cur.rowcount > 0


account_store = AccountStore()
