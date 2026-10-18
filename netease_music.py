import os
import re
from urllib.parse import parse_qs, urljoin, urlparse

import requests


BASE_URL = "https://music.163.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
    "Referer": "https://music.163.com/",
}
TIMEOUT = 15
_URL_BOUNDARY = r"(?<![A-Za-z0-9._/-])"
_URL_SCHEME = r"(?:https?\\?://)?"
_SONG_LINK = re.compile(
    _URL_BOUNDARY
    + _URL_SCHEME
    + r"(?:m\.)?music\.163\.com/(?:\#/)?song\?"
    + r"(?:[A-Za-z0-9_.~%+-]+=[A-Za-z0-9_.~%+-]*&)*"
    + r"id=([1-9][0-9]*)(?![0-9A-Za-z_])",
    re.IGNORECASE,
)
_SHORT_LINK = re.compile(
    _URL_BOUNDARY + _URL_SCHEME + r"163cn\.tv/([A-Za-z0-9]{5,32})(?![A-Za-z0-9])",
    re.IGNORECASE,
)


class MusicAPIError(Exception):
    pass

def _cookie_header(cookie: str | None) -> str:
    value = (cookie if cookie is not None else os.getenv("NETEASE_MUSIC_COOKIE", "")).strip()
    if not value:
        return ""
    if "=" not in value:
        if not re.fullmatch(r"[0-9a-fA-F]+", value):
            raise ValueError("Cookie 应为 MUSIC_U 值或完整的 Cookie 字符串")
        return f"MUSIC_U={value}"
    if "\r" in value or "\n" in value:
        raise ValueError("Cookie 不得包含换行符")
    return value


def _post(path: str, data: dict, cookie: str | None = None) -> dict:
    headers = HEADERS.copy()
    cookie_value = _cookie_header(cookie)
    if cookie_value:
        headers["Cookie"] = cookie_value
    try:
        response = requests.post(BASE_URL + path, data=data, headers=headers, timeout=TIMEOUT)
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise MusicAPIError("网易云音乐接口请求失败，请稍后重试") from exc
    if not isinstance(payload, dict):
        raise MusicAPIError("网易云音乐返回了无效数据")
    if payload.get("code") != 200:
        raise MusicAPIError(f"接口返回错误码 {payload.get('code', '未知')}")
    return payload


def match_music_link(text: str) -> dict[str, str] | None:
    matches = []
    for pattern, link_type in ((_SONG_LINK, "song_id"), (_SHORT_LINK, "short_code")):
        found = pattern.search(text)
        if found:
            matches.append((found.start(), link_type, found))
    if not matches:
        return None
    _, link_type, found = min(matches, key=lambda item: item[0])
    return {
        "type": link_type,
        "value": found.group(1),
        "url": found.group(0).replace(r"\:", ":"),
    }


def resolve_short_link(code: str) -> int:
    if not re.fullmatch(r"[A-Za-z0-9]{5,32}", code):
        raise ValueError("短链编码无效")
    short_url = f"https://163cn.tv/{code}"
    try:
        response = requests.get(short_url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=False)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise MusicAPIError("短链解析失败，请稍后重试") from exc
    if response.status_code not in {301, 302, 303, 307, 308}:
        raise MusicAPIError("短链没有返回歌曲页面")
    target = urlparse(urljoin(short_url, response.headers.get("Location", "")))
    if target.scheme != "https" or target.hostname not in {
        "music.163.com", "m.music.163.com", "y.music.163.com"
    } or not target.path.endswith("/song"):
        raise MusicAPIError("短链没有指向网易云歌曲页面")
    ids = parse_qs(target.query).get("id", [])
    if not ids or not ids[0].isdecimal() or int(ids[0]) <= 0:
        raise MusicAPIError("短链目标没有有效的歌曲 ID")
    return int(ids[0])


def parse_song_id(value: str | int) -> int:
    text = str(value).strip()
    if text.isdecimal() and int(text) > 0:
        return int(text)
    link = match_music_link(text)
    if link is None:
        raise ValueError("请提供歌曲 ID 或网易云音乐歌曲链接")
    if link["type"] == "song_id":
        return int(link["value"])
    return resolve_short_link(link["value"])


def search_songs(keyword: str, cookie: str | None = None, limit: int = 10) -> list[dict]:
    """按关键词搜索单曲，返回包含 id、name、artists、album 的列表。"""
    if not keyword.strip():
        raise ValueError("搜索关键词不能为空")
    if not 1 <= limit <= 50:
        raise ValueError("limit 必须介于 1 到 50")
    payload = _post("/api/cloudsearch/pc", {"s": keyword, "type": 1, "limit": limit}, cookie)
    result = payload.get("result") or {}
    songs = result.get("songs") or []
    return [
        {
            "id": item["id"],
            "name": item.get("name", ""),
            "artists": "/".join(artist.get("name", "") for artist in item.get("ar", [])),
            "album": (item.get("al") or {}).get("name", ""),
        }
        for item in songs
    ]


def get_song_url(song_id: int, cookie: str | None = None, level: str = "standard") -> str:
    song_id = parse_song_id(song_id)
    if level not in {"standard", "exhigh", "lossless", "hires"}:
        raise ValueError("不支持的音质等级")
    payload = _post(
        "/api/song/enhance/player/url/v1",
        {"ids": f"[{song_id}]", "level": level, "encodeType": "mp3"},
        cookie,
    )
    rows = payload.get("data") or []
    url = rows[0].get("url") if rows else None
    if not url:
        raise MusicAPIError("没有可用直链，可能受版权或账号权限限制")
    return url


def get_lyrics(song_id: int, cookie: str | None = None) -> dict[str, str]:
    song_id = parse_song_id(song_id)
    payload = _post(
        "/api/song/lyric",
        {"id": song_id, "lv": -1, "tv": -1, "rv": -1, "kv": -1},
        cookie,
    )
    return {
        "original": (payload.get("lrc") or {}).get("lyric", ""),
        "translated": (payload.get("tlyric") or {}).get("lyric", ""),
    }

