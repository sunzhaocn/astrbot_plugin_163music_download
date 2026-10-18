import asyncio
import time

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, Node, Nodes, Plain, Record
from astrbot.api.platform import MessageType
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.command import GreedyStr

from .netease_music import (
    MusicAPIError,
    get_lyrics,
    get_playable_song_url,
    match_music_link,
    parse_song_id,
    search_songs,
)


class MusicPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | dict | None = None):
        super().__init__(context, config)
        self.music_cookie = str((config or {}).get("music_cookie", "")).strip()
        self._pending_selections: dict[tuple[str, str], tuple[float, list[dict]]] = {}

    @staticmethod
    def _selection_key(event: AstrMessageEvent) -> tuple[str, str]:
        return (str(event.unified_msg_origin), str(event.get_sender_id()))

    @staticmethod
    def _is_at_bot(event: AstrMessageEvent) -> bool:
        bot_id = str(event.get_self_id())
        return bool(bot_id) and any(
            isinstance(message, At) and str(message.qq) == bot_id
            for message in event.get_messages()
        )

    @staticmethod
    def _lyric_chunks(song_id: int, lyrics: dict[str, str]):
        for label, content in (
            ("歌词", lyrics.get("original", "")),
            ("翻译歌词", lyrics.get("translated", "")),
        ):
            if not content:
                if label == "歌词":
                    yield f"歌曲 ID：{song_id}\n暂无歌词"
                continue
            prefix = f"歌曲 ID：{song_id}\n{label}：\n"
            chunk = prefix
            for line in content.splitlines(keepends=True):
                if len(chunk) + len(line) > 1800 and chunk != prefix:
                    yield chunk.rstrip()
                    chunk = prefix
                while len(line) > 1800 - len(chunk):
                    size = 1800 - len(chunk)
                    chunk += line[:size]
                    yield chunk.rstrip()
                    chunk = prefix
                    line = line[size:]
                chunk += line
            if chunk != prefix:
                yield chunk.rstrip()

    async def _send_song_results(
        self, event: AstrMessageEvent, song_id: int, selected_song: dict | None = None
    ) -> None:
        """Send the merged song details, followed by the playable voice message."""
        parts = []
        audio_url = None
        if selected_song:
            parts.append(
                f"已选择：{selected_song['name']} - {selected_song['artists']}"
            )
        try:
            lyrics = await asyncio.to_thread(
                get_lyrics, song_id, cookie=self.music_cookie
            )
            lyric_parts = list(self._lyric_chunks(song_id, lyrics))
        except (ValueError, MusicAPIError) as exc:
            lyric_parts = [f"歌曲 ID：{song_id}\n获取歌词失败：{exc}"]
        try:
            audio_url = await get_playable_song_url(song_id, self.music_cookie)
            parts.append(f"歌曲 ID：{song_id}\n播放直链：{audio_url}")
        except (ValueError, MusicAPIError) as exc:
            parts.append(f"歌曲 ID：{song_id}\n获取直链失败：{exc}")
        parts.extend(lyric_parts)
        bot_id = str(event.get_self_id() or "0")
        nodes = [
            Node(name="网易云音乐", uin=bot_id, content=[Plain(text=part)])
            for part in parts
        ]
        await event.send(MessageChain([Nodes(nodes)]))
        if audio_url:
            try:
                await event.send(MessageChain([Record.fromURL(audio_url)]))
            except Exception:
                logger.exception("Failed to send NetEase music voice message")
                await event.send(
                    MessageChain([Plain("语音发送失败，播放直链已在合并消息中。")])
                )

    @filter.command("网易云帮助")
    async def help(self, event: AstrMessageEvent):
        yield event.plain_result(
            "网易云音乐命令：\n"
            "/网易云搜索 歌名或歌手（随后回复 1-4 选歌）\n"
            "/网易云链接 歌曲ID或含链接的文字\n"
            "群聊艾特机器人并附上歌曲链接，私聊直接发送歌曲链接，均可获取直链、歌词和语音。"
        )

    @filter.command("网易云搜索")
    async def search(self, event: AstrMessageEvent, query: GreedyStr):
        if not query.strip():
            yield event.plain_result("用法：/网易云搜索 歌名或歌手")
            return
        now = time.monotonic()
        self._pending_selections = {
            key: pending
            for key, pending in self._pending_selections.items()
            if pending[0] > now
        }
        self._pending_selections.pop(self._selection_key(event), None)
        try:
            songs = await asyncio.to_thread(
                search_songs, query.strip(), cookie=self.music_cookie, limit=4
            )
        except (ValueError, MusicAPIError) as exc:
            yield event.plain_result(f"搜索失败：{exc}")
            return
        if not songs:
            yield event.plain_result("没有找到歌曲。")
            return
        self._pending_selections[self._selection_key(event)] = (
            time.monotonic() + 300,
            songs,
        )
        lines = ["搜索结果："]
        for index, song in enumerate(songs, 1):
            lines.append(
                f"{index}. {song['name']} - {song['artists']}（ID：{song['id']}）"
            )
        lines.append(f"请在 5 分钟内回复 1-{len(songs)} 选择歌曲。")
        yield event.plain_result("\n".join(lines))

    @filter.command("网易云链接", alias={"歌曲直链", "网易云音乐直链"})
    async def direct_link(self, event: AstrMessageEvent, song: GreedyStr):
        try:
            song_id = await asyncio.to_thread(parse_song_id, song)
        except (ValueError, MusicAPIError) as exc:
            yield event.plain_result(f"解析歌曲失败：{exc}")
            return
        await self._send_song_results(event, song_id)
        event.stop_event()

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def auto_link(self, event: AstrMessageEvent):
        """Handle song selections and links in private or mentioned group messages."""
        text = event.get_message_str().strip()
        if text in {"1", "2", "3", "4"}:
            key = self._selection_key(event)
            pending = self._pending_selections.get(key)
            if pending:
                expires_at, songs = pending
                if time.monotonic() > expires_at:
                    self._pending_selections.pop(key, None)
                    yield event.plain_result("选歌已过期，请重新搜索。")
                    return
                index = int(text) - 1
                if index >= len(songs):
                    yield event.plain_result(f"请选择 1-{len(songs)}。")
                    return
                self._pending_selections.pop(key, None)
                song = songs[index]
                await self._send_song_results(
                    event, int(song["id"]), selected_song=song
                )
                event.stop_event()
            return
        if not match_music_link(text):
            return
        message_type = event.get_message_type()
        if message_type == MessageType.GROUP_MESSAGE and not self._is_at_bot(event):
            return
        if message_type not in {MessageType.GROUP_MESSAGE, MessageType.FRIEND_MESSAGE}:
            return
        commands = (
            "网易云搜索",
            "网易云链接",
            "网易云帮助",
            "歌曲直链",
            "网易云音乐直链",
        )
        command_text = text.lstrip("/")
        if any(
            command_text == command or command_text.startswith(command + " ")
            for command in commands
        ):
            return
        try:
            song_id = await asyncio.to_thread(parse_song_id, text)
        except (ValueError, MusicAPIError) as exc:
            yield event.plain_result(f"解析网易云歌曲链接失败：{exc}")
            return
        await self._send_song_results(event, song_id)
        event.stop_event()
