import asyncio
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from bot.handlers import register, router
from bot.service_registry import ServiceRegistry


class ConfirmationHandlerTest(unittest.TestCase):
    def test_admin_test_command_sends_only_to_group_and_admin(self):
        async def run():
            context = SimpleNamespace(
                interaction_logger=MagicMock(),
                debug_logger=MagicMock(),
                admin_id=67890,
                group_chat_id=-100,
                bot=SimpleNamespace(send_message=AsyncMock()),
                prepare_test_previews=AsyncMock(
                    return_value={
                        "sunday_date": date(2026, 10, 11),
                        "group_text": "Группа: @{alex}",
                        "personal_messages": [
                            ("Ответственный", "Алексей", "Алексей, будь готов")
                        ],
                        "issues": [],
                    }
                ),
            )
            dispatcher = SimpleNamespace(include_router=MagicMock())
            register(dispatcher, context)
            handler = router.message.handlers[-1].callback
            message = SimpleNamespace(
                from_user=SimpleNamespace(id=67890),
                chat=SimpleNamespace(type="private"),
                answer=AsyncMock(),
            )

            await handler(message)

            self.assertEqual(context.bot.send_message.await_count, 4)
            self.assertEqual(
                context.bot.send_message.await_args_list[0].args[0], -100
            )
            group_text = context.bot.send_message.await_args_list[0].args[1]
            self.assertIn("ТЕСТ бота", group_text)
            self.assertIn("не запускалась", group_text)
            self.assertEqual(
                context.bot.send_message.await_args_list[1].args[0], 67890
            )
            report = context.bot.send_message.await_args_list[1].args[1]
            self.assertIn("Тестовое сообщение в группу: ✅ отправлено", report)
            self.assertIn("Личные сообщения участникам не отправлялись", report)
            group_preview = context.bot.send_message.await_args_list[2]
            self.assertEqual(group_preview.args[0], 67890)
            self.assertIn("Группа: @{alex}", group_preview.args[1])
            self.assertEqual(
                group_preview.kwargs["parse_mode"], "HTML"
            )
            personal_preview = context.bot.send_message.await_args_list[3]
            self.assertEqual(personal_preview.args[0], 67890)
            self.assertIn("Ответственный, Алексей", personal_preview.args[1])
            self.assertIn("Алексей, будь готов", personal_preview.args[1])
            context.prepare_test_previews.assert_awaited_once()
            message.answer.assert_not_awaited()

        asyncio.run(run())

    def test_test_command_rejects_non_admin_and_non_private_chat(self):
        async def run():
            context = SimpleNamespace(
                interaction_logger=MagicMock(),
                debug_logger=MagicMock(),
                admin_id=67890,
                group_chat_id=-100,
                bot=SimpleNamespace(send_message=AsyncMock()),
                prepare_test_previews=AsyncMock(),
            )
            dispatcher = SimpleNamespace(include_router=MagicMock())
            register(dispatcher, context)
            handler = router.message.handlers[-1].callback

            unauthorized = SimpleNamespace(
                from_user=SimpleNamespace(id=12345),
                chat=SimpleNamespace(type="private"),
                answer=AsyncMock(),
            )
            await handler(unauthorized)
            unauthorized.answer.assert_not_awaited()

            group_chat = SimpleNamespace(
                from_user=SimpleNamespace(id=67890),
                chat=SimpleNamespace(type="group"),
                answer=AsyncMock(),
            )
            await handler(group_chat)
            group_chat.answer.assert_not_awaited()
            context.bot.send_message.assert_not_awaited()

        asyncio.run(run())

    def test_group_test_failure_is_reported_to_admin(self):
        async def run():
            from aiogram.exceptions import TelegramAPIError

            context = SimpleNamespace(
                interaction_logger=MagicMock(),
                debug_logger=MagicMock(),
                admin_id=67890,
                group_chat_id=-100,
                bot=SimpleNamespace(
                    send_message=AsyncMock(
                        side_effect=[
                            TelegramAPIError(
                                method=MagicMock(), message="group unavailable"
                            ),
                            None,
                            None,
                        ]
                    )
                ),
                prepare_test_previews=AsyncMock(
                    return_value={
                        "sunday_date": date(2026, 10, 11),
                        "group_text": "Полный текст группы",
                        "personal_messages": [],
                        "issues": [],
                    }
                ),
            )
            dispatcher = SimpleNamespace(include_router=MagicMock())
            register(dispatcher, context)
            handler = router.message.handlers[-1].callback
            message = SimpleNamespace(
                from_user=SimpleNamespace(id=67890),
                chat=SimpleNamespace(type="private"),
                answer=AsyncMock(),
            )

            await handler(message)

            self.assertEqual(context.bot.send_message.await_count, 3)
            self.assertEqual(
                context.bot.send_message.await_args_list[1].args[0], 67890
            )
            self.assertIn(
                "ошибка (TelegramAPIError)",
                context.bot.send_message.await_args_list[1].args[1],
            )
            self.assertIn(
                "Полный текст группы",
                context.bot.send_message.await_args_list[2].args[1],
            )
            context.debug_logger.exception.assert_called_once()

        asyncio.run(run())

    def test_failed_group_update_can_be_retried_on_duplicate_callback(self):
        async def run():
            participant = {
                "name": "<Алексей>",
                "tg_id": 12345,
                "confirmed": False,
                "group_update_sent": False,
            }
            context = SimpleNamespace(
                interaction_logger=MagicMock(),
                debug_logger=MagicMock(),
                state_cache=SimpleNamespace(
                    state={
                        "sunday_date": "2026-10-11",
                        "participants": {"Ответственный": participant},
                    },
                    save=MagicMock(),
                ),
                bot=SimpleNamespace(
                    send_message=AsyncMock(
                        side_effect=[RuntimeError("temporary"), object()]
                    )
                ),
                group_chat_id=-100,
            )
            dispatcher = SimpleNamespace(include_router=MagicMock())
            register(dispatcher, context)
            handler = router.callback_query.handlers[-1].callback
            callback = SimpleNamespace(
                data="cfm:2026-10-11:Ответственный",
                from_user=SimpleNamespace(id=12345, username="alex"),
                message=None,
                answer=AsyncMock(),
            )

            await handler(callback)
            self.assertTrue(participant["confirmed"])
            self.assertFalse(participant["group_update_sent"])

            await handler(callback)

            self.assertTrue(participant["group_update_sent"])
            self.assertEqual(context.bot.send_message.await_count, 2)
            self.assertEqual(
                context.bot.send_message.await_args.kwargs,
                {"parse_mode": None},
            )
            self.assertIn(
                "Нажмите кнопку повторно",
                callback.answer.await_args_list[0].args[0],
            )

        asyncio.run(run())

    def test_confirmation_uses_chat_associated_with_original_reminder(self):
        async def run():
            participant = {
                "name": "Алексей",
                "tg_id": 12345,
                "confirmed": False,
                "group_update_sent": False,
                "group_chat_id": -1001,
            }
            context = SimpleNamespace(
                interaction_logger=MagicMock(),
                debug_logger=MagicMock(),
                state_cache=SimpleNamespace(
                    state={
                        "sunday_date": "2026-10-11",
                        "group_chat_id": -1002,
                        "participants": {"Ответственный": participant},
                    },
                    save=MagicMock(),
                ),
                service_registry=SimpleNamespace(
                    get_service=MagicMock(return_value={"enabled": True})
                ),
                bot=SimpleNamespace(send_message=AsyncMock()),
                group_chat_id=-1002,
            )
            dispatcher = SimpleNamespace(include_router=MagicMock())
            register(dispatcher, context)
            callback = SimpleNamespace(
                data="cfm:2026-10-11:Ответственный",
                from_user=SimpleNamespace(id=12345, username="alex"),
                message=None,
                answer=AsyncMock(),
            )

            await router.callback_query.handlers[-1].callback(callback)

            self.assertEqual(
                context.bot.send_message.await_args.args[0], -1001
            )

        asyncio.run(run())

    def test_disabled_assembly_saves_confirmation_without_group_notice(self):
        async def run():
            participant = {
                "name": "Алексей",
                "tg_id": 12345,
                "confirmed": False,
                "group_update_sent": False,
                "group_chat_id": -1001,
            }
            context = SimpleNamespace(
                interaction_logger=MagicMock(),
                debug_logger=MagicMock(),
                state_cache=SimpleNamespace(
                    state={
                        "sunday_date": "2026-10-11",
                        "group_chat_id": -1001,
                        "participants": {"Ответственный": participant},
                    },
                    save=MagicMock(),
                ),
                service_registry=SimpleNamespace(
                    get_service=MagicMock(return_value={"enabled": False})
                ),
                bot=SimpleNamespace(send_message=AsyncMock()),
                group_chat_id=-1001,
            )
            dispatcher = SimpleNamespace(include_router=MagicMock())
            register(dispatcher, context)
            callback = SimpleNamespace(
                data="cfm:2026-10-11:Ответственный",
                from_user=SimpleNamespace(id=12345, username="alex"),
                message=None,
                answer=AsyncMock(),
            )

            await router.callback_query.handlers[-1].callback(callback)

            self.assertTrue(participant["confirmed"])
            self.assertFalse(participant["group_update_sent"])
            context.bot.send_message.assert_not_awaited()
            self.assertIn("служение отключено", callback.answer.await_args.args[0])

        asyncio.run(run())

    def test_group_update_save_failure_does_not_repeat_post_on_retry(self):
        async def run():
            participant = {
                "name": "Алексей",
                "tg_id": 12345,
                "confirmed": False,
                "group_update_sent": False,
            }
            state_cache = SimpleNamespace(
                state={
                    "sunday_date": "2026-10-11",
                    "participants": {"Ответственный": participant},
                },
                save=MagicMock(side_effect=[None, OSError("disk full"), None]),
            )
            context = SimpleNamespace(
                interaction_logger=MagicMock(),
                debug_logger=MagicMock(),
                admin_id=67890,
                state_cache=state_cache,
                bot=SimpleNamespace(send_message=AsyncMock(return_value=object())),
                group_chat_id=-100,
            )
            dispatcher = SimpleNamespace(include_router=MagicMock())
            register(dispatcher, context)
            handler = router.callback_query.handlers[-1].callback
            callback = SimpleNamespace(
                data="cfm:2026-10-11:Ответственный",
                from_user=SimpleNamespace(id=12345, username="alex"),
                message=None,
                answer=AsyncMock(),
            )

            with patch("bot.handlers._notify_admin", new=AsyncMock()) as notify_admin:
                await handler(callback)

            self.assertTrue(participant["group_update_sent"])
            self.assertFalse(participant["group_update_persisted"])
            self.assertIn("не удалось сохранить", callback.answer.await_args.args[0])
            context.bot.send_message.assert_awaited_once()
            notify_admin.assert_awaited_once()

            with patch("bot.handlers._notify_admin", new=AsyncMock()):
                await handler(callback)

            self.assertTrue(participant["group_update_persisted"])
            self.assertEqual(context.bot.send_message.await_count, 1)
            self.assertEqual(state_cache.save.call_count, 3)

        asyncio.run(run())

    def test_concurrent_callbacks_send_only_one_group_update(self):
        async def run():
            entered = asyncio.Event()
            release = asyncio.Event()

            async def send_message(*_args, **_kwargs):
                entered.set()
                await release.wait()

            participant = {
                "name": "Алексей",
                "tg_id": 12345,
                "confirmed": False,
                "group_update_sent": False,
            }
            context = SimpleNamespace(
                interaction_logger=MagicMock(),
                debug_logger=MagicMock(),
                confirmation_locks={},
                state_cache=SimpleNamespace(
                    state={
                        "sunday_date": "2026-10-11",
                        "participants": {"Ответственный": participant},
                    },
                    save=MagicMock(),
                ),
                bot=SimpleNamespace(send_message=AsyncMock(side_effect=send_message)),
                group_chat_id=-100,
            )
            dispatcher = SimpleNamespace(include_router=MagicMock())
            register(dispatcher, context)
            handler = router.callback_query.handlers[-1].callback

            def callback():
                return SimpleNamespace(
                    data="cfm:2026-10-11:Ответственный",
                    from_user=SimpleNamespace(id=12345, username="alex"),
                    message=None,
                    answer=AsyncMock(),
                )

            first = callback()
            second = callback()
            first_task = asyncio.create_task(handler(first))
            await entered.wait()
            second_task = asyncio.create_task(handler(second))
            await asyncio.sleep(0)
            release.set()
            await asyncio.gather(first_task, second_task)

            self.assertEqual(context.bot.send_message.await_count, 1)
            self.assertTrue(participant["group_update_sent"])
            self.assertEqual(second.answer.await_args.args[0], "Вы уже подтвердили участие!")

        asyncio.run(run())


class ServiceManagementHandlerTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.registry = ServiceRegistry(
            Path(self.temp_dir.name) / "registry.sqlite3"
        )
        self.registry.initialize(67890, -100)
        self.context = SimpleNamespace(
            interaction_logger=MagicMock(),
            debug_logger=MagicMock(),
            admin_id=67890,
            group_chat_id=-100,
            service_registry=self.registry,
            bot=SimpleNamespace(send_message=AsyncMock()),
        )
        self.dispatcher = SimpleNamespace(include_router=MagicMock())
        register(self.dispatcher, self.context)
        self.admin_router = self.dispatcher.include_router.call_args_list[0].args[0]

    @staticmethod
    def message(actor_id, text, chat_type="private"):
        return SimpleNamespace(
            from_user=SimpleNamespace(id=actor_id),
            chat=SimpleNamespace(type=chat_type),
            text=text,
            users_shared=None,
            chat_shared=None,
            answer=AsyncMock(),
        )

    def test_super_admin_can_configure_chat_admin_and_contact(self):
        async def run():
            handlers = self.admin_router.message.handlers
            await handlers[2].callback(
                self.message(67890, "/service_admin_add sound 12345")
            )
            await handlers[4].callback(
                self.message(67890, "/service_chat sound -10022 on")
            )
            await handlers[5].callback(
                self.message(
                    67890,
                    "/service_contact sound Микшер | Алексей Иванов | "
                    "12345 | alex",
                )
            )
            await handlers[4].callback(
                self.message(67890, "/service_chat assembly -10077 off")
            )

            self.assertEqual(self.registry.list_service_admins("sound"), [12345])
            self.assertEqual(self.context.group_chat_id, -10077)
            service = next(
                service for service in self.registry.list_services()
                if service["id"] == "sound"
            )
            self.assertEqual(service["group_chat_id"], -10022)
            self.assertTrue(service["enabled"])
            assembly = self.registry.get_service("assembly")
            self.assertEqual(assembly["group_chat_id"], -10077)
            self.assertFalse(assembly["enabled"])
            self.assertEqual(
                self.registry.get_responsible_contact(
                    "sound", "Микшер", "Алексей Иванов"
                ),
                {"telegram_id": 12345, "username": "alex"},
            )

        asyncio.run(run())

    def test_service_admin_can_only_change_own_notification_settings(self):
        async def run():
            self.registry.add_service_admin(67890, "sound", 12345)
            handler = self.admin_router.message.handlers[6].callback

            forbidden = self.message(
                12345,
                '/service_notify slides 2 17:30 | Текст группы | {}',
            )
            await handler(forbidden)
            self.assertIn(
                "администратору служения",
                forbidden.answer.await_args.args[0],
            )

            allowed = self.message(
                12345,
                '/service_notify sound 2 17:30 | Звук: {name} | '
                '{"Микшер": "{name}, проверьте звук"}',
            )
            await handler(allowed)
            allowed.answer.assert_awaited_once_with(
                "Настройки уведомлений сохранены.",
                parse_mode=None,
                reply_markup=None,
            )
            service = next(
                service for service in self.registry.list_services()
                if service["id"] == "sound"
            )
            self.assertEqual(service["reminder_days"], 2)
            self.assertEqual(service["reminder_time"], "17:30")
            self.assertEqual(service["group_template"], "Звук: {name}")
            self.assertEqual(
                service["personal_templates"],
                {"Микшер": "{name}, проверьте звук"},
            )

        asyncio.run(run())

    def test_service_admin_can_use_guided_notification_setup(self):
        async def run():
            self.registry.add_service_admin(67890, "sound", 12345)
            message = self.message(12345, "/service_notify")

            await self.admin_router.message.handlers[6].callback(message)

            keyboard = message.answer.await_args.kwargs["reply_markup"]
            buttons = [
                button
                for row in keyboard.inline_keyboard
                for button in row
            ]
            self.assertEqual(
                [(button.text, button.callback_data) for button in buttons],
                [("ЗВУК", "service:service_notify:sound")],
            )

            callback = SimpleNamespace(
                data="service:service_notify:sound",
                from_user=SimpleNamespace(id=12345),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.admin_router.callback_query.handlers[0].callback(callback)
            self.assertIn(
                "число дней",
                callback.message.answer.await_args.args[0],
            )

            staged_input = next(
                handler.callback
                for handler in self.admin_router.message.handlers
                if handler.callback.__name__ == "on_service_parameter"
            )
            parameters = self.message(
                12345, '3 17:00 | Звук: {name} | {"Микшер":"{name}"}'
            )
            await staged_input(parameters)
            self.assertEqual(
                self.registry.get_service("sound")["reminder_days"], 3
            )

        asyncio.run(run())

    def test_management_commands_are_silent_outside_private_chat(self):
        async def run():
            message = self.message(
                67890, "/service_admin_add sound 12345", chat_type="group"
            )
            await self.admin_router.message.handlers[2].callback(message)

            message.answer.assert_not_awaited()
            self.assertEqual(self.registry.list_service_admins("sound"), [])

        asyncio.run(run())

    def test_management_commands_report_unavailable_registry(self):
        async def run():
            context = SimpleNamespace(
                interaction_logger=MagicMock(),
                debug_logger=MagicMock(),
                service_registry=None,
            )
            dispatcher = SimpleNamespace(include_router=MagicMock())
            register(dispatcher, context)
            admin_router = dispatcher.include_router.call_args_list[0].args[0]

            for handler_index, command in (
                (0, "/services"),
                (2, "/service_admin_add sound 12345"),
            ):
                message = self.message(67890, command)
                await admin_router.message.handlers[handler_index].callback(
                    message
                )
                self.assertIn(
                    "реестр настроек служений недоступен",
                    message.answer.await_args.args[0].lower(),
                )

        asyncio.run(run())

    def test_service_settings_response_is_split_to_telegram_safe_chunks(self):
        async def run():
            for index in range(60):
                self.registry.set_responsible_contact(
                    67890,
                    "sound",
                    "Микшер",
                    f"Ответственный {index} " + "И" * 90,
                    10000 + index,
                )
            message = self.message(67890, "/service_settings sound")

            await self.admin_router.message.handlers[1].callback(message)

            self.assertGreater(message.answer.await_count, 1)
            chunks = [
                call.args[0] for call in message.answer.await_args_list
            ]
            self.assertTrue(
                all(len(chunk.encode("utf-16-le")) // 2 <= 4000 for chunk in chunks)
            )
            self.assertIn("Ответственный 59", "".join(chunks))

        asyncio.run(run())

    def test_service_listing_uses_only_readable_russian_labels(self):
        async def run():
            message = self.message(67890, "/services")
            await self.admin_router.message.handlers[0].callback(message)

            result = message.answer.await_args.args[0]
            self.assertIn("СОБРАНИЕ", result)
            self.assertIn("ТРАНСЛЯЦИЯ", result)
            self.assertNotIn("assembly", result)
            self.assertNotIn("broadcast", result)

        asyncio.run(run())

    def test_settings_command_without_arguments_offers_service_buttons(self):
        async def run():
            self.registry.add_service_admin(67890, "sound", 12345)
            message = self.message(12345, "/service_settings")

            await self.admin_router.message.handlers[1].callback(message)

            message.answer.assert_awaited_once()
            text = message.answer.await_args.args[0]
            keyboard = message.answer.await_args.kwargs["reply_markup"]
            buttons = [
                button
                for row in keyboard.inline_keyboard
                for button in row
            ]
            self.assertIn("Выберите служение", text)
            self.assertEqual(
                [(button.text, button.callback_data) for button in buttons],
                [("ЗВУК", "service:service_settings:sound")],
            )

            callback = SimpleNamespace(
                data=buttons[0].callback_data,
                from_user=SimpleNamespace(id=12345),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.admin_router.callback_query.handlers[0].callback(callback)
            self.assertIn(
                "ЗВУК",
                callback.message.answer.await_args.args[0],
            )
            callback.answer.assert_awaited_once()

        asyncio.run(run())

    def test_admin_add_command_without_parameters_guides_user_through_flow(self):
        async def run():
            message = self.message(67890, "/service_admin_add")
            await self.admin_router.message.handlers[2].callback(message)

            keyboard = message.answer.await_args.kwargs["reply_markup"]
            button = keyboard.inline_keyboard[0][0]
            self.assertEqual(button.text, "СОБРАНИЕ")
            callback = SimpleNamespace(
                data=button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.admin_router.callback_query.handlers[0].callback(callback)
            self.assertIn("выберите пользователя", callback.message.answer.await_args.args[0])
            keyboard = callback.message.answer.await_args.kwargs["reply_markup"]
            request = keyboard.keyboard[0][0].request_users
            self.assertEqual(request.max_quantity, 1)
            self.assertFalse(request.user_is_bot)

            self.assertEqual(self.registry.list_service_admins("assembly"), [])

        asyncio.run(run())

    def test_old_chat_picker_response_is_rejected_after_handler_restart(self):
        async def run():
            first_command = self.message(67890, "/service_chat sound")
            await self.admin_router.message.handlers[4].callback(first_command)
            old_request_id = (
                first_command.answer.await_args.kwargs["reply_markup"]
                .keyboard[0][0].request_chat.request_id
            )

            new_dispatcher = SimpleNamespace(include_router=MagicMock())
            register(new_dispatcher, self.context)
            new_admin_router = (
                new_dispatcher.include_router.call_args_list[0].args[0]
            )
            new_command = self.message(67890, "/service_chat coffee")
            await new_admin_router.message.handlers[4].callback(new_command)
            new_request_id = (
                new_command.answer.await_args.kwargs["reply_markup"]
                .keyboard[0][0].request_chat.request_id
            )
            self.assertNotEqual(old_request_id, new_request_id)

            stale_response = self.message(67890, None)
            stale_response.chat_shared = SimpleNamespace(
                request_id=old_request_id,
                chat_id=-1009911,
                title="Старый выбор",
                username=None,
            )
            await new_admin_router.message.handlers[10].callback(stale_response)

            self.assertIn("не соответствует активному запросу", stale_response.answer.await_args.args[0])
            self.assertIsNone(self.registry.get_service("sound")["group_chat_id"])
            self.assertIsNone(self.registry.get_service("coffee")["group_chat_id"])

        asyncio.run(run())

    def test_staged_input_filter_does_not_consume_commands(self):
        async def run():
            handler = next(
                handler
                for handler in self.admin_router.message.handlers
                if handler.callback.__name__ == "on_service_parameter"
            )
            filter_object = handler.filters[0]

            command_matches = await filter_object.call(
                value=self.message(67890, "/test_notify")
            )
            text_matches = await filter_object.call(
                value=self.message(67890, "some staged input")
            )

            self.assertFalse(command_matches)
            self.assertTrue(text_matches)

        asyncio.run(run())

    def test_switching_management_commands_clears_old_staged_input(self):
        async def run():
            self.registry.add_service_admin(67890, "sound", 24680)
            remove = self.message(67890, "/service_admin_remove sound")
            await self.admin_router.message.handlers[3].callback(remove)
            settings = self.message(67890, "/service_settings sound")
            await self.admin_router.message.handlers[1].callback(settings)
            staged_input = next(
                handler.callback
                for handler in self.admin_router.message.handlers
                if handler.callback.__name__ == "on_service_parameter"
            )
            numeric_text = self.message(67890, "24680")

            await staged_input(numeric_text)

            self.assertEqual(self.registry.list_service_admins("sound"), [24680])
            numeric_text.answer.assert_not_awaited()

        asyncio.run(run())

    def test_back_to_services_reports_registry_read_errors(self):
        async def run():
            self.context.service_registry = SimpleNamespace(
                is_super_admin=MagicMock(
                    side_effect=sqlite3.OperationalError("database unavailable")
                )
            )
            callback = SimpleNamespace(
                data="service:back_to_services:list",
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )

            await self.admin_router.callback_query.handlers[0].callback(callback)

            self.assertEqual(
                callback.message.answer.await_args.args[0],
                "Не удалось прочитать реестр служений.",
            )
            callback.answer.assert_awaited_once()
            self.context.debug_logger.exception.assert_called()

        asyncio.run(run())

    def test_shared_user_is_confirmed_before_becoming_service_admin(self):
        async def run():
            command = self.message(67890, "/service_admin_add")
            await self.admin_router.message.handlers[2].callback(command)
            service_keyboard = command.answer.await_args.kwargs["reply_markup"]
            assembly_button = service_keyboard.inline_keyboard[0][0]
            selected_service_message = self.message(
                67890, "service selection", chat_type="private"
            )
            service_callback = SimpleNamespace(
                data=assembly_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=selected_service_message,
                answer=AsyncMock(),
            )
            await self.admin_router.callback_query.handlers[0].callback(
                service_callback
            )

            user_keyboard = selected_service_message.answer.await_args.kwargs[
                "reply_markup"
            ]
            request_id = user_keyboard.keyboard[0][0].request_users.request_id
            share_message = self.message(67890, None)
            share_message.users_shared = SimpleNamespace(
                request_id=request_id,
                users=[
                    SimpleNamespace(
                        user_id=24680,
                        first_name="Анна",
                        username="candidate",
                    )
                ],
            )
            await self.admin_router.message.handlers[9].callback(
                share_message
            )
            self.assertEqual(self.registry.list_service_admins("assembly"), [])
            confirmation = share_message.answer.await_args.kwargs["reply_markup"]
            confirm_button = confirmation.inline_keyboard[0][0]
            self.assertIn("Анна", share_message.answer.await_args.args[0])
            self.assertIn("@candidate", share_message.answer.await_args.args[0])

            admin_callback = SimpleNamespace(
                data=confirm_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.admin_router.callback_query.handlers[1].callback(
                admin_callback
            )
            self.assertEqual(
                self.registry.list_service_admins("assembly"), [24680]
            )
            self.assertIn(
                "24680",
                admin_callback.message.answer.await_args.args[0],
            )

        asyncio.run(run())

    def test_shared_group_chat_is_saved_and_service_can_be_disabled(self):
        async def run():
            command = self.message(67890, "/service_chat sound")
            await self.admin_router.message.handlers[4].callback(command)
            keyboard = command.answer.await_args.kwargs["reply_markup"]
            request = keyboard.keyboard[0][0].request_chat
            self.assertFalse(request.chat_is_channel)
            self.assertTrue(request.bot_is_member)

            shared_chat = self.message(67890, None)
            shared_chat.chat_shared = SimpleNamespace(
                request_id=request.request_id,
                chat_id=-1002233,
                title="Звук",
                username=None,
            )
            await self.admin_router.message.handlers[10].callback(shared_chat)
            sound = self.registry.get_service("sound")
            self.assertEqual(sound["group_chat_id"], -1002233)
            self.assertTrue(sound["enabled"])
            self.assertIn("-1002233", shared_chat.answer.await_args.args[0])

            disable = self.message(67890, "/service_chat sound off")
            await self.admin_router.message.handlers[4].callback(disable)
            sound = self.registry.get_service("sound")
            self.assertEqual(sound["group_chat_id"], -1002233)
            self.assertFalse(sound["enabled"])

        asyncio.run(run())

    def test_settings_message_has_next_action_button(self):
        async def run():
            self.registry.add_service_admin(67890, "sound", 24680)
            selection_message = self.message(
                67890, "/service_settings sound"
            )
            await self.admin_router.message.handlers[1].callback(
                selection_message
            )
            keyboard = selection_message.answer.await_args.kwargs["reply_markup"]
            edit_button = keyboard.inline_keyboard[0][0]
            self.assertEqual(
                edit_button.callback_data, "service:edit_notify:sound"
            )
            self.assertIn(
                "изменить дни",
                selection_message.answer.await_args.args[0].lower(),
            )

            callback = SimpleNamespace(
                data=edit_button.callback_data,
                from_user=SimpleNamespace(id=24680),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.admin_router.callback_query.handlers[0].callback(callback)
            self.assertIn(
                "число дней",
                callback.message.answer.await_args.args[0],
            )

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
