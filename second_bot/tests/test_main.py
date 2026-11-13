"""Unit tests for the standalone Discord bot; no Discord connection is made."""
from __future__ import annotations

import os
import json
import tempfile
import time
import unittest
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import discord
from discord.ext import commands

_BOT_SPEC = spec_from_file_location("second_bot_main", Path(__file__).resolve().parents[1] / "main.py")
assert _BOT_SPEC is not None and _BOT_SPEC.loader is not None
main = module_from_spec(_BOT_SPEC)
_BOT_SPEC.loader.exec_module(main)


ROLE_ID = 123456789012345678


def make_context(*, role_ids: tuple[int, ...] = (ROLE_ID,), permissions: discord.Permissions | None = None, guild_id: int = 777):
    if permissions is None:
        permissions = discord.Permissions(
            view_channel=True,
            send_messages=True,
            manage_messages=True,
            send_messages_in_threads=True,
        )

    author = MagicMock(spec=discord.Member)
    author.id = 987654321
    author.roles = [SimpleNamespace(id=role_id) for role_id in role_ids]

    bot_member = MagicMock(spec=discord.Member)
    channel = SimpleNamespace(
        id=555,
        permissions_for=Mock(return_value=permissions),
    )
    message = SimpleNamespace(id=444, delete=AsyncMock())
    return SimpleNamespace(
        guild=SimpleNamespace(id=guild_id, me=bot_member),
        author=author,
        channel=channel,
        message=message,
        send=AsyncMock(),
        command=None,
    )


class BotConfigurationTests(unittest.TestCase):
    def test_load_config_returns_token_and_positive_role_id(self):
        with patch.dict(os.environ, {
            "SECOND_BOT_TOKEN": " test-token ",
            "SAY_ALLOWED_ROLE_ID": str(ROLE_ID),
        }):
            self.assertEqual(main.load_config(), ("test-token", ROLE_ID))

    def test_dedicated_token_takes_precedence_and_bot_prefix_is_removed(self):
        with patch.dict(os.environ, {
            "SECOND_BOT_TOKEN": " Bot second-bot-token ",
            "DISCORD_BOT_TOKEN": "cosmo-coins-token",
            "SAY_ALLOWED_ROLE_ID": str(ROLE_ID),
        }):
            self.assertEqual(main.load_config(), ("second-bot-token", ROLE_ID))

    def test_load_config_rejects_missing_token(self):
        with patch.dict(os.environ, {
            "SECOND_BOT_TOKEN": "",
            "DISCORD_BOT_TOKEN": "primary-bot-token",
            "SAY_ALLOWED_ROLE_ID": str(ROLE_ID),
        }):
            with self.assertRaisesRegex(ValueError, "SECOND_BOT_TOKEN"):
                main.load_config()

    def test_load_config_rejects_invalid_role_id(self):
        with patch.dict(os.environ, {"SECOND_BOT_TOKEN": "token", "SAY_ALLOWED_ROLE_ID": "not-a-role"}):
            with self.assertRaisesRegex(ValueError, "SAY_ALLOWED_ROLE_ID"):
                main.load_config()

    def test_create_bot_uses_account_mode_without_intents(self):
        with patch.object(main.commands, "Bot", return_value="fake-bot") as bot_factory:
            self.assertEqual(main.create_bot(), "fake-bot")

        options = bot_factory.call_args.kwargs
        self.assertNotIn("intents", options)
        self.assertTrue(options["user_bot"])
        self.assertNotIn("self_bot", options)
        self.assertEqual(options["command_prefix"], "$")


class SharedGuildCommandCheckTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_path = Path(self.temp_dir.name) / "shared_guilds.json"
        self.path_patch = patch.object(main, "SHARED_GUILD_STATE_PATH", self.state_path)
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)
        self.addCleanup(self.temp_dir.cleanup)

    def write_state(self, guild_ids, updated_at=None):
        self.state_path.write_text(json.dumps({
            "updated_at": time.time() if updated_at is None else updated_at,
            "guild_ids": guild_ids,
        }), encoding="utf-8")

    async def test_shared_guild_is_allowed(self):
        self.write_state([777, 888])
        self.assertTrue(await main.check_shared_guild(make_context()))

    async def test_non_shared_guild_is_rejected(self):
        self.write_state([888])
        with self.assertRaises(main.NotInSharedGuild):
            await main.check_shared_guild(make_context())

    async def test_missing_or_stale_state_fails_closed(self):
        self.assertEqual(main.load_shared_guild_ids(), set())
        self.write_state([777], updated_at=time.time() - main.SHARED_GUILD_STATE_MAX_AGE_SECONDS - 1)
        self.assertEqual(main.load_shared_guild_ids(), set())
        with self.assertRaises(main.NotInSharedGuild):
            await main.check_shared_guild(make_context())

    async def test_direct_messages_are_rejected(self):
        ctx = make_context()
        ctx.guild = None
        with self.assertRaises(main.NotInSharedGuild):
            await main.check_shared_guild(ctx)

    async def test_rejected_guild_command_is_silent(self):
        ctx = make_context()
        await main.on_command_error(ctx, main.NotInSharedGuild("not shared"))
        ctx.send.assert_not_awaited()


class UserCommandCooldownTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        main.user_command_cooldowns.clear()

    def tearDown(self):
        main.user_command_cooldowns.clear()

    async def test_cooldown_is_personal_and_reports_remaining_time(self):
        first_user = make_context()
        second_user = make_context()
        second_user.author.id = 987654322

        with patch.object(main.time, "monotonic", side_effect=(100.0, 101.0, 101.0)):
            self.assertTrue(await main.check_user_command_cooldown(first_user))
            with self.assertRaises(main.UserCommandCooldown) as raised:
                await main.check_user_command_cooldown(first_user)
            self.assertAlmostEqual(raised.exception.retry_after, 29.0)
            self.assertTrue(await main.check_user_command_cooldown(second_user))

        await main.on_command_error(first_user, raised.exception)
        self.assertIn("29", first_user.send.await_args.args[0])

    async def test_same_user_can_use_command_in_another_guild(self):
        first_guild = make_context(guild_id=777)
        second_guild = make_context(guild_id=888)

        with patch.object(main.time, "monotonic", return_value=100.0):
            self.assertTrue(await main.check_user_command_cooldown(first_guild))
            self.assertTrue(await main.check_user_command_cooldown(second_guild))


class SayCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        main.SAY_ALLOWED_ROLE_ID = ROLE_ID
        main.bot.is_owner = AsyncMock(return_value=False)

    async def test_authorized_role_deletes_command_and_sends_text_without_mentions(self):
        ctx = make_context()

        await main.say.callback(ctx, text="hello")

        ctx.message.delete.assert_awaited_once_with()
        ctx.send.assert_awaited_once()
        args, kwargs = ctx.send.await_args
        self.assertEqual(args, ("hello",))
        self.assertEqual(
            kwargs["allowed_mentions"].to_dict(),
            discord.AllowedMentions.none().to_dict(),
        )
        main.bot.is_owner.assert_not_awaited()

    async def test_bot_owner_is_allowed_without_configured_role(self):
        ctx = make_context(role_ids=())
        main.bot.is_owner = AsyncMock(return_value=True)

        await main.say.callback(ctx, text="owner message")

        ctx.message.delete.assert_awaited_once_with()
        self.assertEqual(ctx.send.await_args.args, ("owner message",))
        main.bot.is_owner.assert_awaited_once_with(ctx.author)

    async def test_empty_text_is_rejected(self):
        ctx = make_context()

        await main.say.callback(ctx, text="   \t  ")

        ctx.message.delete.assert_not_awaited()
        self.assertIn("فارغ", ctx.send.await_args.args[0])

    async def test_text_over_2000_characters_is_rejected(self):
        ctx = make_context()

        await main.say.callback(ctx, text="x" * 2001)

        ctx.message.delete.assert_not_awaited()
        self.assertIn("2000", ctx.send.await_args.args[0])

    async def test_text_of_exactly_2000_characters_is_accepted(self):
        ctx = make_context()

        await main.say.callback(ctx, text="x" * 2000)

        ctx.message.delete.assert_awaited_once_with()
        self.assertEqual(ctx.send.await_args.args[0], "x" * 2000)

    async def test_unauthorized_user_is_rejected(self):
        ctx = make_context(role_ids=())

        await main.say.callback(ctx, text="not allowed")

        ctx.message.delete.assert_not_awaited()
        self.assertIn("ليس لديك صلاحية", ctx.send.await_args.args[0])

    async def test_missing_manage_messages_permission_is_reported(self):
        ctx = make_context(
            permissions=discord.Permissions(send_messages=True, manage_messages=False)
        )

        await main.say.callback(ctx, text="hello")

        ctx.message.delete.assert_not_awaited()
        self.assertIn("Manage Messages", ctx.send.await_args.args[0])

    async def test_forbidden_delete_is_reported_without_stopping_handler(self):
        ctx = make_context()
        forbidden = discord.Forbidden(
            response=SimpleNamespace(status=403, reason="Forbidden"),
            message="missing permission",
        )
        ctx.message.delete.side_effect = forbidden

        await main.say.callback(ctx, text="hello")

        ctx.message.delete.assert_awaited_once_with()
        self.assertIn("تعذر حذف", ctx.send.await_args.args[0])

    async def test_missing_say_argument_has_clear_error(self):
        ctx = make_context()
        ctx.command = main.say
        error = commands.MissingRequiredArgument(main.say.clean_params["text"])

        await main.on_command_error(ctx, error)

        self.assertIn("النص فارغ", ctx.send.await_args.args[0])

    async def test_unexpected_command_error_is_reported(self):
        ctx = make_context()

        await main.on_command_error(ctx, commands.CommandError("simulated failure"))

        self.assertIn("خطأ غير متوقع", ctx.send.await_args.args[0])

    async def test_unknown_command_is_ignored(self):
        ctx = make_context()

        await main.on_command_error(ctx, commands.CommandNotFound("!unknown"))

        ctx.send.assert_not_awaited()


class PingCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_ping_reports_online_status_latency_and_disables_mentions(self):
        ctx = make_context()
        fake_bot = SimpleNamespace(latency=0.1234)

        with patch.object(main, "bot", fake_bot):
            await main.ping.callback(ctx)

        response = ctx.send.await_args.args[0]
        self.assertIn("حالة البوت: متصل", response)
        self.assertIn("123 ms", response)
        self.assertEqual(
            ctx.send.await_args.kwargs["allowed_mentions"].to_dict(),
            discord.AllowedMentions.none().to_dict(),
        )


class NumberExtractionTests(unittest.IsolatedAsyncioTestCase):
    def test_parse_numbers_accepts_json_code_block(self):
        self.assertEqual(
            main.parse_numbers_response('```json\n{"numbers":["101","22.5"]}\n```'),
            ["101", "22.5"],
        )

    def test_parse_ocr_space_response_requires_five_ascii_digits(self):
        response = {"ParsedResults": [{"ParsedText": "12 880\n"}]}
        self.assertEqual(main.parse_ocr_space_response(response), "12880")
        with self.assertRaisesRegex(RuntimeError, "five-digit"):
            main.parse_ocr_space_response({"ParsedResults": [{"ParsedText": "1288"}]})

    def test_first_five_digit_result_normalizes_provider_values(self):
        self.assertEqual(main.first_five_digit_result(["not useful", "12 880"]), "12880")
        self.assertIsNone(main.first_five_digit_result(["1288", "abcdef"]))

    def test_prepare_ocr_space_variants_converts_to_png(self):
        image_bytes = (Path(__file__).resolve().parents[2] / "captcha_samples" / "type1_01.webp").read_bytes()
        original_png, enhanced_png, otsu_png, adaptive_png = main.prepare_ocr_space_variants(image_bytes)
        self.assertTrue(original_png.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertTrue(enhanced_png.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertTrue(otsu_png.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertTrue(adaptive_png.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertNotEqual(original_png, enhanced_png)
        self.assertNotEqual(original_png, otsu_png)
        self.assertNotEqual(original_png, adaptive_png)

    def test_otsu_threshold_separates_two_grayscale_modes(self):
        image = main.Image.new("L", (100, 10), 240)
        for x in range(40):
            for y in range(10):
                image.putpixel((x, y), 20)

        threshold = main.otsu_threshold(image)

        self.assertGreaterEqual(threshold, 20)
        self.assertLess(threshold, 240)

    async def test_ocr_space_variants_checks_only_engine_three_for_each_variant(self):
        responses = AsyncMock(side_effect=lambda payload, mime_type, *, api_key, engine: [f"{payload.decode()}-{engine}"])
        with patch.object(
            main,
            "prepare_ocr_space_variants",
            return_value=(b"original", b"enhanced", b"otsu", b"adaptive"),
        ), patch.object(main, "extract_numbers_with_ocr_space", new=responses):
            result = await main.extract_numbers_with_ocr_space_variants(
                b"source-image", api_key="ocr-test-key"
            )

        self.assertEqual(responses.await_count, 4)
        self.assertEqual(
            [call.kwargs["engine"] for call in responses.await_args_list],
            ["3", "3", "3", "3"],
        )
        self.assertEqual(result[("before contrast", "3")], "original-3")
        self.assertEqual(result[("upscaled otsu", "3")], "otsu-3")
        self.assertEqual(result[("adaptive threshold", "3")], "adaptive-3")

    async def test_ocr_space_rate_limit_stops_remaining_variants(self):
        response = AsyncMock(side_effect=main.OCRSpaceRateLimitError("OCR.Space API error 429: HTTP 429"))
        with patch.object(
            main,
            "prepare_ocr_space_variants",
            return_value=(b"original", b"enhanced", b"otsu", b"adaptive"),
        ), patch.object(main, "extract_numbers_with_ocr_space", new=response):
            with self.assertRaisesRegex(RuntimeError, "429"):
                await main.extract_numbers_with_ocr_space_variants(
                    b"source-image", api_key="ocr-test-key"
                )
        self.assertEqual(response.await_count, 1)

    async def test_ocr_space_local_limiter_persists_cooldown_and_daily_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(main, "BASE_DIR", Path(tmp) / "second_bot"), \
                    patch.object(main, "OCR_SPACE_MIN_INTERVAL_SECONDS", 0), \
                    patch.object(main, "OCR_SPACE_DAILY_REQUEST_BUDGET", 1):
                await main.reserve_ocr_space_request_slot()
                await main.register_ocr_space_rate_limit(120)
                state = main.load_ocr_space_rate_state()
                self.assertEqual(state["requests"], 1)
                self.assertGreater(float(state["cooldown_until"]), time.time())
                with self.assertRaises(main.OCRSpaceCooldownError):
                    await main.reserve_ocr_space_request_slot()

                state["cooldown_until"] = 0
                main.write_runtime_json(
                    main.runtime_state_path("ocr_space_rate_state.json"), state
                )
                with self.assertRaises(main.OCRSpaceDailyBudgetError):
                    await main.reserve_ocr_space_request_slot()

    async def test_ocr_space_http_429_saves_retry_after_without_another_call(self):
        class FakeResponse:
            status = 429
            headers = {"Retry-After": "120"}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def json(self, content_type=None):
                return {"ErrorMessage": "HTTP 429"}

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            def post(self, *args, **kwargs):
                return FakeResponse()

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(main, "BASE_DIR", Path(tmp) / "second_bot"), \
                    patch.object(main, "OCR_SPACE_MIN_INTERVAL_SECONDS", 0), \
                    patch.object(main.aiohttp, "ClientSession", return_value=FakeSession()):
                with self.assertRaises(main.OCRSpaceRateLimitError) as caught:
                    await main.extract_numbers_with_ocr_space(
                        b"fake-image", "image/png", api_key="test-key", engine="3"
                    )
                self.assertEqual(caught.exception.retry_after_seconds, 120.0)
                with self.assertRaises(main.OCRSpaceCooldownError):
                    await main.reserve_ocr_space_request_slot()
                state = main.load_ocr_space_rate_state()
                self.assertEqual(state["requests"], 1)
                self.assertGreaterEqual(float(state["cooldown_until"]), time.time() + 110)

    def test_retry_after_parses_seconds(self):
        self.assertEqual(main.parse_retry_after_seconds("45"), 45.0)

    def test_old_gemini_fallback_model_is_migrated(self):
        self.assertEqual(
            main.resolve_gemini_fallback_model("gemini-2.5-flash-lite"),
            "gemini-3.5-flash-lite",
        )
        self.assertEqual(main.resolve_gemini_fallback_model("custom-vision-model"), "custom-vision-model")

    def test_openrouter_plain_text_accepts_only_one_unambiguous_five_digit_value(self):
        self.assertEqual(main.parse_openrouter_numbers_response("The number appears to be 12345."), ["12345"])
        with self.assertRaisesRegex(RuntimeError, "single unambiguous"):
            main.parse_openrouter_numbers_response("Maybe 12345 or 54321")

    def test_type1_dataset_reader_loads_only_labeled_type1_images(self):
        sample_dir = Path(__file__).resolve().parents[2] / "captcha_samples"
        samples = main.load_type1_test_samples(sample_dir)
        self.assertEqual(len(samples), 100)
        self.assertTrue(all(row["type"] == "type1" for row in samples))

    def test_preprocessing_update_invalidates_old_testall_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint_path = Path(tmp) / "checkpoint.json"
            checkpoint_path.write_text(json.dumps({
                "version": 1,
                "dataset_fingerprint": "same-dataset",
                "records": {"old.webp": {"predictions": {"upscaled binary": "12345"}}},
            }), encoding="utf-8")

            checkpoint, resumed = main.load_testall_checkpoint(checkpoint_path, "same-dataset")

        self.assertFalse(resumed)
        self.assertEqual(checkpoint["version"], main.OCR_PREPROCESSING_VERSION)
        self.assertEqual(checkpoint["records"], {})

    def test_five_digit_comparison_reports_positions_and_confusions(self):
        count, mismatches = main.compare_five_digit_answer("92824", "91482")
        self.assertEqual(count, 4)
        self.assertEqual(
            mismatches,
            [(2, "2", "1"), (3, "8", "4"), (4, "2", "8"), (5, "4", "2")],
        )
        self.assertEqual(main.compare_five_digit_answer("92824", None), (None, []))

    def test_report_summarizes_each_variant_and_exports_mistaken_digits(self):
        records = [
            {
                "filename": "one.webp",
                "expected": "12345",
                "predictions": {
                    "before contrast": "12345",
                    "after contrast": "12344",
                    "upscaled otsu": None,
                    "adaptive threshold": "12345",
                },
            },
            {
                "filename": "two.webp",
                "expected": "54321",
                "predictions": {
                    "before contrast": "54321",
                    "after contrast": None,
                    "upscaled otsu": "54321",
                },
            },
        ]
        summary, report = main.build_type1_test_report(records)
        report_text = report.decode("utf-8-sig")
        self.assertIn("صحيح بالكامل 2/2 من المختبر (100.0%)", summary)
        self.assertIn("خطأ برقم واحد 1", summary)
        self.assertIn("غير مقروء 1", summary)
        self.assertIn("5:5>4", report_text)
        self.assertIn("لم تُختبر", report_text)  # Missing attempts are not counted as unreadable.
        self.assertIn("upscaled_otsu_prediction", report_text)
        self.assertIn("adaptive_threshold_prediction", report_text)

    def test_report_keeps_api_errors_out_of_ocr_unreadable_counts(self):
        records = [{
            "filename": "rate_limited.webp",
            "expected": "12345",
            "predictions": {},
            "provider_errors": {"before contrast": "OCR.Space API error 429: HTTP 429"},
        }]
        summary, report = main.build_type1_test_report(records)
        report_text = report.decode("utf-8-sig")
        self.assertIn("صحيح بالكامل 0/0 من المختبر (0.0%)", summary)
        self.assertIn("غير مقروء 0", summary)
        self.assertIn("فشل API 1", summary)
        self.assertIn("OCR.Space API error 429", report_text)

    async def test_testall_uses_engine_three_and_sends_csv_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sample_dir = root / "captcha_samples"
            sample_dir.mkdir()
            (sample_dir / "one.webp").write_bytes(b"first")
            (sample_dir / "two.webp").write_bytes(b"second")
            (sample_dir / "label.csv").write_text(
                "filename,type,answer\none.webp,type1,12345\ntwo.webp,type1,54321\n",
                encoding="utf-8",
            )
            ctx = make_context()
            results = [[answer] for answer in (
                "12345", "12345", "12345", "12345",
                "54321", "54321", "54321", "54321",
            )]
            ocr_space = AsyncMock(side_effect=results)
            with patch.dict(os.environ, {"OCR_SPACE_API_KEY": "ocr-test-key"}), \
                    patch.object(main, "BASE_DIR", root / "second_bot"), \
                    patch.object(main, "prepare_ocr_space_variants", return_value=(b"a", b"b", b"c", b"d")), \
                    patch.object(main, "extract_numbers_with_ocr_space", new=ocr_space):
                await main.testall.callback(ctx)

        self.assertEqual(ocr_space.await_count, 8)
        self.assertTrue(all(call.kwargs["engine"] == "3" for call in ocr_space.await_args_list))
        final_call = ctx.send.await_args
        self.assertIn("صحيح بالكامل 2/2 من المختبر", final_call.args[0])
        self.assertEqual(final_call.kwargs["file"].filename, "testall_engine3_report.csv")

    async def test_testall_stops_on_429_and_reports_provider_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sample_dir = root / "captcha_samples"
            sample_dir.mkdir()
            (sample_dir / "one.webp").write_bytes(b"first")
            (sample_dir / "label.csv").write_text(
                "filename,type,answer\none.webp,type1,12345\n",
                encoding="utf-8",
            )
            ctx = make_context()
            ocr_space = AsyncMock(
                side_effect=main.OCRSpaceRateLimitError("OCR.Space API error 429: HTTP 429")
            )
            with patch.dict(os.environ, {"OCR_SPACE_API_KEY": "ocr-test-key"}), \
                    patch.object(main, "BASE_DIR", root / "second_bot"), \
                    patch.object(main, "prepare_ocr_space_variants", return_value=(b"a", b"b", b"c", b"d")), \
                    patch.object(main, "extract_numbers_with_ocr_space", new=ocr_space):
                await main.testall.callback(ctx)

        self.assertEqual(ocr_space.await_count, 1)
        final_call = ctx.send.await_args
        self.assertIn("فشل API 1", final_call.args[0])
        self.assertIn("غير مقروء 0", final_call.args[0])

    async def test_testall_resumes_at_failed_variant_and_restarts_after_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sample_dir = root / "captcha_samples"
            sample_dir.mkdir()
            (sample_dir / "one.webp").write_bytes(b"first")
            (sample_dir / "two.webp").write_bytes(b"second")
            (sample_dir / "label.csv").write_text(
                "filename,type,answer\none.webp,type1,12345\ntwo.webp,type1,54321\n",
                encoding="utf-8",
            )
            bot_dir = root / "second_bot"
            checkpoint_path = bot_dir / ".runtime" / "testall_engine3_checkpoint.json"

            first_context = make_context()
            first_run = AsyncMock(side_effect=[
                ["12345"], ["12345"], ["12345"], ["12345"],
                main.OCRSpaceRateLimitError("OCR.Space API error 429: HTTP 429"),
            ])
            with patch.dict(os.environ, {"OCR_SPACE_API_KEY": "ocr-test-key"}), \
                    patch.object(main, "BASE_DIR", bot_dir), \
                    patch.object(main, "prepare_ocr_space_variants", return_value=(b"a", b"b", b"c", b"d")), \
                    patch.object(main, "extract_numbers_with_ocr_space", new=first_run):
                await main.testall.callback(first_context)
                self.assertTrue(checkpoint_path.is_file())
                samples = main.load_type1_test_samples(sample_dir)
                checkpoint, resumed = main.load_testall_checkpoint(
                    checkpoint_path, main.fingerprint_type1_dataset(sample_dir, samples)
                )
                self.assertTrue(resumed)
                self.assertEqual(main.testall_resume_position(samples, checkpoint), ("two.webp", "before contrast"))
                self.assertEqual(
                    set(checkpoint["records"]["one.webp"]["predictions"]),
                    {variant for variant, _ in main.TYPE1_TEST_VARIANTS},
                )
                self.assertEqual(main.testall_remaining_request_count(samples, checkpoint), 4)

            resume_context = make_context()
            resumed_run = AsyncMock(side_effect=[["54321"]] * 4)
            with patch.dict(os.environ, {"OCR_SPACE_API_KEY": "ocr-test-key"}), \
                    patch.object(main, "BASE_DIR", bot_dir), \
                    patch.object(main, "prepare_ocr_space_variants", return_value=(b"a", b"b", b"c", b"d")), \
                    patch.object(main, "extract_numbers_with_ocr_space", new=resumed_run):
                await main.testall.callback(resume_context)
                self.assertFalse(checkpoint_path.exists())

            self.assertEqual(first_run.await_count, 5)
            self.assertEqual(resumed_run.await_count, 4)
            self.assertIn("صحيح بالكامل 2/2 من المختبر", resume_context.send.await_args.args[0])

            fresh_context = make_context()
            fresh_run = AsyncMock(side_effect=[["12345"]] * 4 + [["54321"]] * 4)
            with patch.dict(os.environ, {"OCR_SPACE_API_KEY": "ocr-test-key"}), \
                    patch.object(main, "BASE_DIR", bot_dir), \
                    patch.object(main, "prepare_ocr_space_variants", return_value=(b"a", b"b", b"c", b"d")), \
                    patch.object(main, "extract_numbers_with_ocr_space", new=fresh_run):
                await main.testall.callback(fresh_context)

            self.assertEqual(fresh_run.await_count, 8)

    def test_attachment_image_type_uses_filename_when_content_type_is_missing(self):
        attachment = SimpleNamespace(content_type=None, filename="receipt.JPG")
        self.assertEqual(main.attachment_image_mime_type(attachment), "image/jpeg")

    async def test_extract_numbers_uses_json_response_and_image_data_url(self):
        fake_response = SimpleNamespace(text='{"numbers":["12.50","٣"]}')
        fake_models = SimpleNamespace(generate_content=AsyncMock(return_value=fake_response))
        fake_client = SimpleNamespace(aio=SimpleNamespace(models=fake_models))

        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key", "GEMINI_VISION_MODEL": "vision-test"}):
            result = await main.extract_numbers_from_image(
                b"image-bytes", "image/png", client=fake_client
            )

        self.assertEqual(result, ["12.50", "٣"])
        request = fake_models.generate_content.await_args.kwargs
        self.assertEqual(request["model"], "vision-test")
        image_part = request["contents"][1].inline_data
        self.assertEqual(image_part.mime_type, "image/png")
        self.assertEqual(image_part.data, b"image-bytes")

    async def test_numbers_without_image_reports_usage(self):
        ctx = make_context()
        ctx.message.attachments = []

        await main.numbers.callback(ctx)

        self.assertIn("أرفق صورة", ctx.send.await_args.args[0])

    async def test_numbers_rejects_oversized_image_before_reading(self):
        ctx = make_context()
        attachment = SimpleNamespace(
            content_type="image/png",
            filename="numbers.png",
            size=main.MAX_IMAGE_BYTES + 1,
            read=AsyncMock(),
        )
        ctx.message.attachments = [attachment]

        await main.numbers.callback(ctx)

        attachment.read.assert_not_awaited()
        self.assertIn("8 ميغابايت", ctx.send.await_args.args[0])

    async def test_numbers_explains_ocr_space_429_as_provider_limit(self):
        ctx = make_context()
        attachment = SimpleNamespace(
            content_type="image/png",
            filename="type1.png",
            size=4,
            read=AsyncMock(return_value=b"type1-image"),
        )
        ctx.message.attachments = [attachment]
        with patch.object(main, "detect_image_style", return_value="type1"), \
                patch.object(
                    main,
                    "extract_type1_with_fallback",
                    new=AsyncMock(side_effect=RuntimeError("OCR.Space API error 429: HTTP 429")),
                ):
            await main.numbers.callback(ctx)
        self.assertIn("HTTP 429", ctx.send.await_args.args[0])
        self.assertIn("لا يعني أن الصورة غير مقروءة", ctx.send.await_args.args[0])

    async def test_numbers_uses_local_ocr_for_type2_without_api_key(self):
        ctx = make_context()
        attachment = SimpleNamespace(
            content_type="image/png",
            filename="type2.png",
            size=4,
            read=AsyncMock(return_value=b"type2-image"),
        )
        ctx.message.attachments = [attachment]

        with patch.dict(os.environ, {"GEMINI_API_KEY": "", "OPENROUTER_API_KEY": "", "OCR_SPACE_API_KEY": ""}), \
                patch.object(main, "detect_image_style", return_value="type2"), \
                patch.object(main, "extract_numbers_locally", return_value=["99598"]), \
                patch.object(main, "extract_numbers_from_image", new=AsyncMock()) as remote_ocr:
            await main.numbers.callback(ctx)

        remote_ocr.assert_not_awaited()
        self.assertIn("99598", ctx.send.await_args.args[0])

    async def test_numbers_uses_ocr_space_for_type1_only(self):
        ctx = make_context()
        attachment = SimpleNamespace(
            content_type="image/png",
            filename="type1.png",
            size=4,
            read=AsyncMock(return_value=b"type1-image"),
        )
        ctx.message.attachments = [attachment]

        with patch.dict(os.environ, {"OCR_SPACE_API_KEY": "ocr-test-key"}), \
                patch.object(main, "detect_image_style", return_value="type1"), \
                patch.object(main, "extract_numbers_with_ocr_space_variants", new=AsyncMock(return_value={
                    ("before contrast", "3"): "12880",
                    ("after contrast", "3"): "12880",
                    ("upscaled otsu", "3"): "12880",
                    ("adaptive threshold", "3"): "12880",
                })) as ocr_space, \
                patch.object(main, "extract_numbers_locally", return_value=["wrong"] ) as local_ocr:
            await main.numbers.callback(ctx)

        ocr_space.assert_awaited_once_with(b"type1-image", api_key="ocr-test-key")
        local_ocr.assert_not_called()
        self.assertIn("12880", ctx.send.await_args.args[0])
        output = ctx.send.await_args.args[0]
        self.assertIn("قبل تحسين التباين (المحرك 3)", output)
        self.assertNotIn("المحرك 2", output)
        self.assertIn("بعد تحسين التباين (المحرك 3)", output)
        self.assertIn("بعد التكبير وعتبة Otsu (المحرك 3)", output)
        self.assertIn("بعد العتبة المحلية التكيفية (المحرك 3)", output)

    async def test_type1_uses_gemini_when_ocr_space_fails(self):
        with patch.dict(os.environ, {
            "GEMINI_API_KEY": "gemini-test-key",
            "GEMINI_VISION_MODEL": "vision-test",
            "OPENROUTER_API_KEY": "",
        }), patch.object(
            main,
            "extract_numbers_with_ocr_space_variants",
            new=AsyncMock(side_effect=RuntimeError("no OCR text")),
        ), patch.object(
            main,
            "extract_numbers_from_image",
            new=AsyncMock(return_value=["12 880"]),
        ) as gemini, patch.object(main, "extract_numbers_with_openrouter", new=AsyncMock()) as openrouter:
            result = await main.extract_type1_with_fallback(
                b"type1-image",
                "image/png",
                ocr_space_key="ocr-test-key",
            )

        self.assertEqual(result, ({}, "12880", None))
        gemini.assert_awaited_once_with(b"type1-image", "image/png", model="vision-test")
        openrouter.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
