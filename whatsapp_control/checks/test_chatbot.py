import asyncio
from base64 import b64decode
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

from whatsapp_control.chatbot import Chatbot
from whatsapp_control.settings import policy_patch


_PNG = b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aJ+0AAAAASUVORK5CYII="
)
_OPUS = b64decode(
    "T2dnUwACAAAAAAAAAACE1o34AAAAAP4Cp1gBE09wdXNIZWFkAQE4AYC7AAAAAABPZ2dTAAAAAAAAAAAAAITWjfgBAAAABMu8rQE8"
    "T3B1c1RhZ3MMAAAATGF2ZjYzLjEuMTAyAQAAABwAAABlbmNvZGVyPUxhdmM2My4xLjEwMiBsaWJvcHVzT2dnUwAEeAwAAAAA"
    "AACE1o34AgAAAGAIrhsEAwMDA/j//vj//vj//vj//g=="
)


class AudioRejected(RuntimeError):
    definitive_rejection = True


class FakeLlm:
    def __init__(self):
        self.calls = []
        self.reply = "Got it."
        self.action = "reply"
        self.error = None
        self.started = None
        self.finish = None

    async def acomplete_structured(self, **kwargs):
        self.calls.append(kwargs)
        if self.started is not None:
            self.started.set()
            await self.finish.wait()
        if self.error is not None:
            raise self.error
        return SimpleNamespace(parsed={"action": self.action, "text": self.reply})


class ChatbotTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.media_root = self.directory / "media"
        self.media_root.mkdir()
        self.now = 1000.0
        self.active = True
        self.downloaded = []
        self.sent = []
        self.voice_send_error = None
        self.native_tts_calls = []
        self.native_tts_result = "success"
        self.model = FakeLlm()
        self.settings = {
            "shop": {"dm_mode": "allowlist", "dm_allowlist": ["62811"], "history_limit": 2},
            "personal": {"dm_mode": "all", "history_limit": 2},
        }
        self.bot = self.create_bot()
        self.addCleanup(lambda: self.bot.close())

    def create_bot(self):
        return Chatbot(self.directory / "chatbot.sqlite", self.model, self.transport, self.settings.get,
                       lambda: self.active, media_root=self.media_root, tts_dispatch=self.native_tts,
                       now=lambda: self.now)

    async def native_tts(self, tool_name, args):
        self.native_tts_calls.append((tool_name, args))
        self.assertEqual(tool_name, "text_to_speech")
        self.assertEqual(set(args), {"text", "output_path"})
        output = Path(args["output_path"])
        self.assertEqual(output.parent.parent, self.media_root.resolve())
        if self.native_tts_result == "failure":
            return '{"success":false,"error":"unavailable fixture"}'
        if self.native_tts_result == "outside":
            output = self.directory / "outside.ogg"
        else:
            output = output.with_suffix(".ogg")
        output.write_bytes(_OPUS)
        return json.dumps({"success": True, "file_path": str(output), "file_paths": [str(output)],
                           "media_tag": "MEDIA:/unused"})

    async def transport(self, op, account, args):
        if op == "download_media":
            kind = "image" if args["id"].startswith("image") else "voice"
            path = self.media_root / args["id"]
            path.write_bytes(_PNG if kind == "image" else _OPUS)
            self.downloaded.append(path)
            return {"path": str(path), "type": kind}
        self.assertIn(op, {"send_text", "send_media"})
        if op == "send_media" and self.voice_send_error is not None:
            raise self.voice_send_error
        content = Path(args["path"]).read_bytes() if op == "send_media" else args["text"]
        self.sent.append((op, account, args, content))
        return {"message_id": args["message_id"]}

    def event(self, identifier, **changes):
        return {
            "event": "message", "account": "shop", "chat": "99411@lid", "chat_type": "dm",
            "id": identifier, "sender": "99411@lid", "sender_alt": "62811@s.whatsapp.net",
            "self_ids": ["62822@s.whatsapp.net", "99322@lid"], "text": "Halo",
            "live": True, "from_me": False, "timestamp": self.now, **changes,
        }

    def input_text(self, index=-1):
        return json.loads(self.model.calls[index]["input"][0]["text"])

    async def test_filters_replay_unlisted_sender_and_outbound_echo_on_another_account(self):
        incoming = self.event("initial")
        await asyncio.gather(self.bot.handle_event(incoming), self.bot.handle_event(incoming))
        outgoing = self.sent[0][2]["message_id"]
        for suppressed in (
            self.event(outgoing, account="personal"), self.event("sync", live=False),
            self.event("self", from_me=True), self.event("unlisted", sender_alt="62899@s.whatsapp.net"),
        ):
            await self.bot.handle_event(suppressed)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.model.calls), 1)

    async def test_known_lid_only_sender_matches_phone_policy_for_its_account(self):
        await self.bot.handle_event(self.event("learn"))
        await self.bot.handle_event(self.event("allowlisted-lid", sender_alt=None))
        self.assertEqual(len(self.sent), 2)

        self.settings["shop"].update(dm_mode="all", exclude_chats=["62811"])
        await self.bot.handle_event(self.event("excluded-lid", sender_alt=None))
        await self.bot.handle_event(self.event("eligible-other-account", sender_alt=None, account="personal"))
        self.assertEqual([account for _, account, _, _ in self.sent], ["shop", "shop", "personal"])

    async def test_excluded_phone_mapping_is_recorded_before_lid_only_delivery(self):
        self.settings["shop"].update(dm_mode="all", exclude_chats=["62811"])
        await self.bot.handle_event(self.event("excluded-anchor"))
        await self.bot.handle_event(self.event("excluded-lid", sender_alt=None))
        self.assertFalse(self.model.calls)

    async def test_linking_verified_alias_preserves_known_request_and_recent_history(self):
        self.settings["shop"]["dm_mode"] = "all"
        await self.bot.handle_event(self.event("id", text="First message", sender_alt=None))
        await self.bot.handle_event(self.event("id", text="First message"))
        self.assertEqual(len(self.sent), 1)

        await self.bot.handle_event(self.event("other", text="Next message"))
        self.assertEqual(self.input_text()["prior_turns"], [
            {"role": "user", "text": "First message"}, {"role": "assistant", "text": "Got it."},
        ])

    async def test_group_uses_actual_mentions_and_supports_per_group_override(self):
        self.settings["shop"].update(group_mode="allowlist", group_allowlist=["101@g.us", "202@g.us"],
                                     require_mention={"202@g.us": False})
        for identifier, chat, mentions in (
            ("text-only", "101@g.us", []), ("actual", "101@g.us", ["99322:9@lid"]),
            ("unrestricted", "202@g.us", []), ("unlisted", "303@g.us", ["99322@lid"]),
        ):
            await self.bot.handle_event(self.event(identifier, chat_type="group", chat=chat,
                                                   mentions=mentions, text="@62822 Halo"))
        self.assertEqual([data["chat"] for _, _, data, _ in self.sent], ["101@g.us", "202@g.us"])

    async def test_completion_has_no_tools_and_uses_only_scoped_bounded_history(self):
        self.settings["shop"].update(history_limit=1, instructions="Reply in Indonesian.")
        await self.bot.handle_event(self.event("first", text="First DM"))
        await self.bot.handle_event(self.event("second", text="Second DM"))
        await self.bot.handle_event(self.event("other", account="personal"))
        self.assertEqual(self.input_text(0), {"prior_turns": [], "current_message": "First DM"})
        self.assertEqual(self.input_text(1)["prior_turns"], [{"role": "assistant", "text": "Got it."}])
        self.assertEqual(self.input_text(2)["prior_turns"], [])
        self.assertEqual(self.model.calls[0]["system_prompt"], "Reply in Indonesian.")
        self.assertNotIn("tools", self.model.calls[0])
        self.assertNotIn("messages", self.model.calls[0])
        self.bot.trim_history("shop", 0)
        await self.bot.handle_event(self.event("trimmed"))
        self.assertEqual(self.input_text()["prior_turns"], [])

    async def test_semantic_handoff_persists_and_discards_queued_replies(self):
        self.settings["shop"]["handoff_seconds"] = 60
        self.model.action, self.model.reply = "handoff", ""
        self.model.started, self.model.finish = asyncio.Event(), asyncio.Event()
        first = asyncio.create_task(self.bot.handle_event(self.event("request", text="Bisa sama admin aslinya?")))
        await self.model.started.wait()
        queued = asyncio.create_task(self.bot.handle_event(self.event("during-model")))
        await asyncio.sleep(0)
        self.model.finish.set()
        await asyncio.gather(first, queued)
        self.assertEqual(self.bot.paused_until("shop", "+62811"), 1060)
        self.assertEqual(len(self.model.calls), 1)
        self.assertFalse(self.sent)

        self.bot.close()
        self.bot = self.create_bot()
        self.model.action, self.model.reply = "reply", "Halo lagi."
        await self.bot.handle_event(self.event("after-restart"))
        self.now = 1070
        await self.bot.handle_event(self.event("backlog", timestamp=1059))
        self.assertFalse(self.sent)
        await self.bot.handle_event(self.event("fresh"))
        self.assertEqual([content for _, _, _, content in self.sent], ["Halo lagi."])

    async def test_owner_pause_or_disable_during_model_cancels_reply(self):
        self.model.started, self.model.finish = asyncio.Event(), asyncio.Event()
        pending = asyncio.create_task(self.bot.handle_event(self.event("pause")))
        await self.model.started.wait()
        self.bot.pause("shop", "99411@lid", 20)
        self.bot.resume("shop", "99411@lid")
        self.model.finish.set()
        await pending
        self.model.started.clear()
        self.model.finish.clear()
        pending = asyncio.create_task(self.bot.handle_event(self.event("disable")))
        await self.model.started.wait()
        self.active = False
        self.model.finish.set()
        await pending
        self.assertFalse(self.sent)

    async def test_model_outage_notifies_each_eligible_chat_once_until_recovery(self):
        notice = "The chatbot is temporarily unavailable. Please try again later."
        self.settings["shop"].update(dm_mode="all", group_mode="allowlist", group_allowlist=["101@g.us"],
                                     require_mention=True)
        self.model.error = TimeoutError("private provider detail")
        await self.bot.handle_event(self.event("first", sender_alt=None))
        await self.bot.handle_event(self.event("same-after-alias"))
        await self.bot.handle_event(self.event("no-mention", chat_type="group", chat="101@g.us"))
        await self.bot.handle_event(self.event("mention", chat_type="group", chat="101@g.us",
                                               mentions=["99322@lid"]))
        await self.bot.handle_event(self.event("other-account", account="personal"))
        self.assertEqual([(account, args["chat"], content) for _, account, args, content in self.sent], [
            ("shop", "99411@lid", notice), ("shop", "101@g.us", notice),
            ("personal", "99411@lid", notice),
        ])
        self.assertEqual(self.bot.model_failure("shop"), "timeout")

        self.model.error = None
        await self.bot.handle_event(self.event("first", sender_alt=None))
        await self.bot.handle_event(self.event("recovered"))
        self.assertEqual(self.sent[-1][3], "Got it.")
        self.assertEqual(self.input_text()["prior_turns"], [])
        self.assertIsNone(self.bot.model_failure("shop"))
        self.assertEqual(self.bot.model_failure("personal"), "timeout")

        self.model.error = TimeoutError()
        await self.bot.handle_event(self.event("next-outage"))
        self.assertEqual(self.sent[-1][3], notice)
        self.assertEqual(len(self.sent), 5)

    async def test_model_outage_notice_respects_handoff_started_during_inference(self):
        self.model.error = TimeoutError()
        self.model.started, self.model.finish = asyncio.Event(), asyncio.Event()
        pending = asyncio.create_task(self.bot.handle_event(self.event("started-before-pause")))
        await self.model.started.wait()
        self.bot.pause("shop", "99411@lid", 60)
        self.model.finish.set()
        await pending
        await self.bot.handle_event(self.event("while-paused"))
        self.assertFalse(self.sent)
        self.assertEqual(self.bot.model_failure("shop"), "timeout")

        self.now += 61
        await self.bot.handle_event(self.event("after-pause"))
        self.assertEqual(len(self.sent), 1)

    async def test_concurrent_old_failure_cannot_reopen_resolved_outage(self):
        started, finish = asyncio.Event(), asyncio.Event()
        self.settings["shop"]["dm_mode"] = "all"

        async def complete(**kwargs):
            message = json.loads(kwargs["input"][0]["text"])["current_message"]
            if message == "old request":
                started.set()
                await finish.wait()
                raise TimeoutError()
            return SimpleNamespace(parsed={"action": "reply", "text": "Recovered"})

        self.model.acomplete_structured = complete
        pending = asyncio.create_task(self.bot.handle_event(self.event("old", text="old request")))
        await started.wait()
        try:
            await self.bot.handle_event(self.event("new", chat="99412@lid", sender="99412@lid",
                                                   sender_alt="62812@s.whatsapp.net"))
        finally:
            finish.set()
            await pending
        self.assertIsNone(self.bot.model_failure("shop"))
        self.assertEqual([content for _, _, _, content in self.sent], ["Recovered"])

    async def test_successful_image_to_text_fallback_keeps_chatbot_healthy(self):
        self.settings["shop"]["image_enabled"] = True

        async def complete(**kwargs):
            if len(kwargs["input"]) == 2:
                raise RuntimeError("Vision unavailable")
            return SimpleNamespace(parsed={"action": "reply", "text": "Describe the image."})

        self.model.acomplete_structured = complete
        await self.bot.handle_event(self.event("image", media={"type": "image"}))
        self.assertEqual([content for _, _, _, content in self.sent], ["Describe the image."])
        self.assertIsNone(self.bot.model_failure("shop"))

    async def test_handoff_and_manual_resume_resolve_both_dm_addresses(self):
        self.bot.pause("shop", "99411@lid", 60)
        await self.bot.handle_event(self.event("first-address-match"))
        self.assertEqual(self.bot.paused_until("shop", "+62811"), 1060)
        self.assertFalse(self.sent)
        self.bot.resume("shop", "+62811")
        self.assertEqual(self.bot.paused_until("shop", "99411@lid"), 0)
        self.now += 1
        await self.bot.handle_event(self.event("after-resume"))
        self.assertEqual(len(self.sent), 1)

    async def test_active_pause_lookup_and_extension_use_resolved_chat_and_existing_expiry(self):
        self.bot.pause("shop", "99411@lid", 60)
        await self.bot.handle_event(self.event("register-alias"))
        self.bot.pause("personal", "99510@lid", 120)
        self.now += 20
        self.assertEqual(self.bot.extend_pause("shop", "99411@lid", 15), 1075)
        self.assertEqual(self.bot.active_pauses("shop"), {"62811@s.whatsapp.net": 1075})
        self.assertEqual(self.bot.active_pauses("personal"), {"99510@lid": 1120})
        self.now = 1075
        self.assertEqual(self.bot.active_pauses("shop"), {})

    async def test_image_and_view_once_handling(self):
        self.settings["shop"]["image_enabled"] = True
        await self.bot.handle_event(self.event("view-once", media={"type": "image", "view_once": True}))
        self.assertFalse(self.downloaded)
        self.assertFalse(self.model.calls)
        await self.bot.handle_event(self.event("image", text="", media={"type": "image"}))
        self.assertEqual(self.model.calls[-1]["input"][1], {"type": "image", "data": _PNG, "mime_type": "image/png"})
        self.assertFalse(self.downloaded[-1].exists())
        self.assertEqual(len(self.sent), 1)

    async def test_quoted_image_passes_current_question_or_reports_unavailable(self):
        self.settings["shop"]["image_enabled"] = True
        await self.bot.handle_event(self.event("image-quote", text="Berapa barang di foto?",
                                               media={"type": "image", "quoted": True}))
        self.assertEqual(self.input_text()["current_message"], "Berapa barang di foto?")
        self.assertEqual(self.model.calls[-1]["input"][1]["data"], _PNG)
        self.assertFalse(self.downloaded[-1].exists())
        await self.bot.handle_event(self.event("image-missing", text="Ini apa?",
                                               media={"type": "image", "quoted": True, "unavailable": True}))
        self.assertEqual(len(self.downloaded), 1)
        self.assertEqual(len(self.model.calls[-1]["input"]), 1)
        self.assertIn("Do not infer", self.model.calls[-1]["instructions"])

    def configure_voice(self):
        self.settings["shop"].update(
            reply_mode="match",
            stt_command=[sys.executable, "-c", "print('Tolong jawab pesan suara ini.')", "{input_path}"],
            tts_command=[sys.executable, "-c",
                         f"from pathlib import Path; import sys; Path(sys.argv[2]).write_bytes({_OPUS!r})",
                         "{input_path}", "{output_path}"],
        )

    async def test_voice_transcription_synthesis_and_text_fallback(self):
        self.configure_voice()
        await self.bot.handle_event(self.event("voice", text="", media={"type": "voice"}))
        self.assertEqual(self.input_text()["current_message"], "Tolong jawab pesan suara ini.")
        op, _, data, content = self.sent[-1]
        self.assertEqual((op, content, data["type"]), ("send_media", _OPUS, "voice"))
        self.assertEqual(list(self.media_root.iterdir()), [])
        self.settings["shop"]["tts_command"] = [str(self.directory / "missing-executable")]
        await self.bot.handle_event(self.event("voice-failure", text="", media={"type": "voice"}))
        self.assertEqual(self.sent[-1][0], "send_text")
        self.assertEqual(list(self.media_root.iterdir()), [])
        self.assertEqual(self.native_tts_calls, [])

    async def test_voice_uses_native_hermes_speech_without_command_override(self):
        self.settings["shop"].update(reply_mode="voice")
        await self.bot.handle_event(self.event("native-voice"))
        self.assertEqual(len(self.native_tts_calls), 1)
        _, args = self.native_tts_calls[0]
        self.assertEqual(args["text"], self.model.reply)
        self.assertNotIn("tools", self.model.calls[-1])
        op, _, payload, content = self.sent[-1]
        self.assertEqual((op, payload["type"], content), ("send_media", "voice", _OPUS))
        self.assertEqual(list(self.media_root.iterdir()), [])

    async def test_native_speech_failure_or_untrusted_output_path_falls_back(self):
        self.settings["shop"].update(reply_mode="voice")
        for mode in ("failure", "outside"):
            self.native_tts_result = mode
            await self.bot.handle_event(self.event(mode))
        self.assertEqual([send[0] for send in self.sent], ["send_text", "send_text"])
        self.assertEqual(list(self.media_root.iterdir()), [])
        self.assertEqual((self.directory / "outside.ogg").read_bytes(), _OPUS)

    async def test_pause_during_native_tts_discards_the_completed_audio(self):
        started, finish = asyncio.Event(), asyncio.Event()

        async def delayed_speech(tool_name, args):
            started.set()
            await finish.wait()
            return await self.native_tts(tool_name, args)

        self.settings["shop"].update(reply_mode="voice")
        self.bot._tts_dispatch = delayed_speech
        task = asyncio.create_task(self.bot.handle_event(self.event("native-paused")))
        try:
            await asyncio.wait_for(started.wait(), 3)
            self.bot.pause("shop", "62811", 60)
        finally:
            finish.set()
            await task
        self.assertEqual(self.sent, [])
        self.assertEqual(list(self.media_root.iterdir()), [])

    async def test_cancel_native_tts_waits_for_dispatch_cleanup_before_removing_audio(self):
        started, stopped = asyncio.Event(), asyncio.Event()

        async def delayed_speech(tool_name, args):
            await self.native_tts(tool_name, args)
            started.set()
            try:
                await asyncio.Future()
            finally:
                await asyncio.sleep(0)
                self.assertTrue(Path(args["output_path"]).with_suffix(".ogg").is_file())
                stopped.set()

        self.settings["shop"].update(reply_mode="voice")
        self.bot._tts_dispatch = delayed_speech
        task = asyncio.create_task(self.bot.handle_event(self.event("native-canceled")))
        try:
            await asyncio.wait_for(started.wait(), 3)
            self.active = False
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(stopped.is_set())
        self.assertEqual(self.sent, [])
        self.assertEqual(list(self.media_root.iterdir()), [])

    async def test_voice_enable_can_change_without_editing_local_commands(self):
        self.configure_voice()
        self.settings["shop"].update(policy_patch({"voice_enabled": False}))
        await self.bot.handle_event(self.event("voice-disabled", text="", media={"type": "voice"}))
        self.assertFalse(self.downloaded)
        self.assertFalse(self.model.calls)
        self.settings["shop"].update(policy_patch({"voice_enabled": True}))
        await self.bot.handle_event(self.event("voice-enabled", text="", media={"type": "voice"}))
        self.assertEqual(self.input_text()["current_message"], "Tolong jawab pesan suara ini.")
        self.assertEqual(self.sent[-1][0], "send_media")

    async def test_failed_voice_send_falls_back_only_when_definitively_rejected(self):
        self.configure_voice()
        self.voice_send_error = AudioRejected("Synthetic rejection")
        await self.bot.handle_event(self.event("voice-rejected", text="", media={"type": "voice"}))
        self.assertEqual([send[0] for send in self.sent], ["send_text"])
        self.assertEqual(list(self.media_root.iterdir()), [])
        self.voice_send_error = RuntimeError("Synthetic uncertain result")
        with self.assertRaisesRegex(RuntimeError, "uncertain"):
            await self.bot.handle_event(self.event("voice-timeout", text="", media={"type": "voice"}))
        self.assertEqual([send[0] for send in self.sent], ["send_text"])
        self.assertEqual(list(self.media_root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
