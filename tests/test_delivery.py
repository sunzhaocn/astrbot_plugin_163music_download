import unittest
from unittest.mock import AsyncMock, patch

from astrbot.api.message_components import Nodes, Plain, Record
from astrbot.api.platform import MessageType
from data.plugins.astrbot_plugin_163music_download import main, netease_music


class FakeEvent:
    def __init__(self, text: str, message_type: MessageType):
        self.text = text
        self.message_type = message_type
        self.sent = []
        self.stopped = False
        self.reject_voice = False
        self.unified_msg_origin = "test:FriendMessage:1"

    def get_message_str(self) -> str:
        return self.text

    def get_message_type(self) -> MessageType:
        return self.message_type

    def get_self_id(self) -> str:
        return "123"

    def get_sender_id(self) -> str:
        return "456"

    def get_messages(self) -> list:
        return []

    async def send(self, message) -> None:
        if self.reject_voice and isinstance(message.chain[0], Record):
            raise RuntimeError("voice rejected")
        self.sent.append(message)

    def stop_event(self) -> None:
        self.stopped = True


class MusicDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.plugin = main.MusicPlugin.__new__(main.MusicPlugin)
        self.plugin.music_cookie = ""
        self.plugin._pending_selections = {}

    async def test_merged_message_precedes_voice(self) -> None:
        event = FakeEvent("", MessageType.FRIEND_MESSAGE)
        with (
            patch.object(
                main, "get_playable_song_url", new_callable=AsyncMock
            ) as get_url,
            patch.object(main, "get_lyrics", return_value={"original": "歌词"}),
        ):
            get_url.return_value = "https://example.com/song.mp3"
            await self.plugin._send_song_results(event, 123)

        self.assertEqual(len(event.sent), 2)
        self.assertIsInstance(event.sent[0].chain[0], Nodes)
        self.assertIsInstance(event.sent[1].chain[0], Record)
        self.assertEqual(event.sent[1].chain[0].file, "https://example.com/song.mp3")

    async def test_no_voice_when_song_has_no_playable_url(self) -> None:
        event = FakeEvent("", MessageType.FRIEND_MESSAGE)
        with (
            patch.object(
                main,
                "get_playable_song_url",
                new_callable=AsyncMock,
                side_effect=main.MusicAPIError("无直链"),
            ),
            patch.object(main, "get_lyrics", return_value={"original": "歌词"}),
        ):
            await self.plugin._send_song_results(event, 123)

        self.assertEqual(len(event.sent), 1)
        self.assertIsInstance(event.sent[0].chain[0], Nodes)

    async def test_voice_failure_keeps_merged_message_and_reports_error(self) -> None:
        event = FakeEvent("", MessageType.FRIEND_MESSAGE)
        event.reject_voice = True
        with (
            patch.object(
                main, "get_playable_song_url", new_callable=AsyncMock
            ) as get_url,
            patch.object(main, "get_lyrics", return_value={"original": "歌词"}),
            patch.object(main.logger, "exception"),
        ):
            get_url.return_value = "https://example.com/song.mp3"
            await self.plugin._send_song_results(event, 123)

        self.assertEqual(len(event.sent), 2)
        self.assertIsInstance(event.sent[0].chain[0], Nodes)
        self.assertIsInstance(event.sent[1].chain[0], Plain)
        self.assertIn("语音发送失败", event.sent[1].chain[0].text)

    async def test_private_link_does_not_require_mention(self) -> None:
        event = FakeEvent(
            "https://music.163.com/song?id=123", MessageType.FRIEND_MESSAGE
        )
        self.plugin._send_song_results = AsyncMock()
        async for _ in self.plugin.auto_link(event):
            pass

        self.plugin._send_song_results.assert_awaited_once_with(event, 123)
        self.assertTrue(event.stopped)

    async def test_group_link_requires_mention(self) -> None:
        event = FakeEvent(
            "https://music.163.com/song?id=123", MessageType.GROUP_MESSAGE
        )
        self.plugin._send_song_results = AsyncMock()
        async for _ in self.plugin.auto_link(event):
            pass
        self.plugin._send_song_results.assert_not_awaited()

        self.plugin._is_at_bot = lambda _: True
        async for _ in self.plugin.auto_link(event):
            pass
        self.plugin._send_song_results.assert_awaited_once_with(event, 123)
        self.assertTrue(event.stopped)


class MusicUrlTests(unittest.IsolatedAsyncioTestCase):
    async def test_403_with_cookie_uses_anonymous_url(self) -> None:
        signed = "http://m704.music.126.net/signed.mp3"
        anonymous = "http://m701.music.126.net/anonymous.mp3"
        with (
            patch.object(netease_music, "get_song_url", side_effect=[signed, anonymous]) as get_url,
            patch.object(
                netease_music, "check_song_url_status", new_callable=AsyncMock
            ) as check,
        ):
            check.side_effect = [403, 200]
            result = await netease_music.get_playable_song_url(123, "MUSIC_U=value")

        self.assertEqual(result, anonymous)
        self.assertEqual([call.kwargs["cookie"] for call in get_url.call_args_list], ["MUSIC_U=value", ""])

    async def test_403_uses_accessible_cdn_host(self) -> None:
        signed = "http://m804.music.126.net/signed.mp3?token=example"
        with (
            patch.object(netease_music, "get_song_url", return_value=signed),
            patch.object(
                netease_music, "check_song_url_status", new_callable=AsyncMock
            ) as check,
        ):
            check.side_effect = [403, 200]
            result = await netease_music.get_playable_song_url(123)

        self.assertEqual(result, signed.replace("m804", "m701"))

    async def test_all_cdn_candidates_403(self) -> None:
        signed = "http://m804.music.126.net/signed.mp3"
        with (
            patch.object(netease_music, "get_song_url", return_value=signed),
            patch.object(
                netease_music, "check_song_url_status", new_callable=AsyncMock
            ) as check,
        ):
            check.return_value = 403
            with self.assertRaisesRegex(netease_music.MusicAPIError, "403"):
                await netease_music.get_playable_song_url(123)

if __name__ == "__main__":
    unittest.main()
