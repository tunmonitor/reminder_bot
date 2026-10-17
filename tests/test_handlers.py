import asyncio
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from bot.handlers import register, router
from bot.service_registry import ServiceRegistry


def _confirmation_handler():
    return next(
        item.callback
        for item in reversed(router.callback_query.handlers)
        if item.callback.__name__ == "on_confirm"
    )


class ConfirmationHandlerTest(unittest.TestCase):
    def test_test_notify_offers_selected_service_preview(self):
        async def run():
            context = SimpleNamespace(
                interaction_logger=MagicMock(),
                debug_logger=MagicMock(),
                admin_id=67890,
                group_chat_id=-100,
                service_registry=SimpleNamespace(
                    list_services=MagicMock(
                        return_value=[
                            {
                                "id": "sound",
                                "name": "Звук",
                                "group_chat_id": -200,
                                "enabled": True,
                            }
                        ]
                    ),
                    get_service=MagicMock(
                        return_value={
                            "id": "sound",
                            "name": "Звук",
                            "group_chat_id": -200,
                            "enabled": True,
                        }
                    ),
                ),
                bot=SimpleNamespace(
                    send_message=AsyncMock(), delete_message=AsyncMock()
                ),
                prepare_service_test_previews=AsyncMock(
                    return_value={
                        "sunday_date": date(2026, 10, 11),
                        "group_text": "Звук: {task_assignments}",
                        "personal_messages": [
                            {
                                "task": "Микшер",
                                "name": "Алексей",
                                "telegram_id": "123",
                                "text": "Алексей, проверьте звук",
                            }
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

            selection = message.answer.await_args.kwargs["reply_markup"]
            button = selection.inline_keyboard[0][0]
            self.assertEqual(button.callback_data, "service:test_notify:sound")
            admin_router = dispatcher.include_router.call_args_list[0].args[0]
            service_handler = next(
                item.callback for item in admin_router.callback_query.handlers
                if item.callback.__name__ == "on_service_selected"
            )
            callback = SimpleNamespace(
                data=button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await service_handler(callback)

            self.assertEqual(context.bot.send_message.await_count, 0)
            action_markup = callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            preview_button = next(
                button
                for row in action_markup.inline_keyboard
                for button in row
                if button.callback_data == "test_action:preview:sound"
            )
            test_action = next(
                item.callback
                for item in reversed(router.callback_query.handlers)
                if item.callback.__name__ == "on_test_notify_action"
            )
            preview_callback = SimpleNamespace(
                data=preview_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await test_action(preview_callback)

            context.prepare_service_test_previews.assert_awaited_once_with("sound")
            context.bot.send_message.assert_awaited_once()
            self.assertEqual(
                context.bot.send_message.await_args.args[0], 67890
            )
            self.assertIn("Звук", context.bot.send_message.await_args.args[1])
            self.assertIn("Микшер", context.bot.send_message.await_args.args[1])
            self.assertIn(
                "проверьте звук", context.bot.send_message.await_args.args[1]
            )

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

    def test_group_test_sends_only_a_marked_message_to_selected_group(self):
        async def run():
            context = SimpleNamespace(
                interaction_logger=MagicMock(),
                debug_logger=MagicMock(),
                admin_id=67890,
                service_registry=SimpleNamespace(
                    list_services=MagicMock(
                        return_value=[
                            {
                                "id": "sound",
                                "name": "Звук",
                                "group_chat_id": -200,
                                "enabled": True,
                            }
                        ]
                    ),
                    get_service=MagicMock(
                        return_value={
                            "id": "sound",
                            "name": "Звук",
                            "group_chat_id": -200,
                            "enabled": True,
                        }
                    ),
                ),
                bot=SimpleNamespace(send_message=AsyncMock()),
                prepare_service_test_previews=AsyncMock(),
            )
            dispatcher = SimpleNamespace(include_router=MagicMock())
            register(dispatcher, context)
            handler = next(
                item.callback
                for item in reversed(router.callback_query.handlers)
                if item.callback.__name__ == "on_test_notify_action"
            )
            callback = SimpleNamespace(
                data="test_action:group:sound",
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await handler(callback)

            context.bot.send_message.assert_awaited_once()
            self.assertEqual(
                context.bot.send_message.await_args.args[0], -200
            )
            self.assertIn(
                "ТЕСТ службы «Звук»",
                context.bot.send_message.await_args.args[1],
            )
            self.assertIn(
                "не запускалась",
                callback.message.answer.await_args.args[0],
            )
            context.prepare_service_test_previews.assert_not_awaited()

        asyncio.run(run())

    def test_confirmation_uses_current_registry_group_after_chat_change(self):
        async def run():
            participant = {
                "name": "Алексей",
                "tg_id": 12345,
                "confirmed": True,
                "group_update_sent": False,
                "group_chat_id": -1001,
            }
            context = SimpleNamespace(
                interaction_logger=MagicMock(),
                debug_logger=MagicMock(),
                admin_id=67890,
                state_cache=SimpleNamespace(
                    state={
                        "sunday_date": "2026-10-11",
                        "group_chat_id": -1002,
                        "participants": {"Ответственный": participant},
                    },
                    save=MagicMock(),
                ),
                service_registry=SimpleNamespace(
                    get_service=MagicMock(
                        return_value={
                            "enabled": True,
                            "group_chat_id": -1002,
                        }
                    )
                ),
                bot=SimpleNamespace(send_message=AsyncMock()),
                group_chat_id=-1001,
            )
            dispatcher = SimpleNamespace(include_router=MagicMock())
            register(dispatcher, context)
            handler = _confirmation_handler()
            callback = SimpleNamespace(
                data="cfm:2026-10-11:Ответственный",
                from_user=SimpleNamespace(id=12345, username="alex"),
                message=None,
                answer=AsyncMock(),
            )

            await handler(callback)

            context.bot.send_message.assert_awaited_once()
            self.assertEqual(
                context.bot.send_message.await_args.args[0], -1002
            )
            self.assertEqual(participant["group_chat_id"], -1002)
            self.assertTrue(participant["group_update_sent"])
            from bot.app import _merge_week_state

            merged = _merge_week_state(
                context.state_cache.state,
                {
                    "sunday_date": "2026-10-11",
                    "group_chat_id": -1002,
                    "participants": {
                        "Ответственный": {
                            "name": "Алексей",
                            "tg_id": 12345,
                        }
                    },
                },
                -1002,
            )
            self.assertTrue(
                merged["participants"]["Ответственный"]["group_update_sent"]
            )
            callback.answer.assert_awaited_once_with(
                "Вы уже подтвердили участие"
            )

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
            handler = _confirmation_handler()
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

    def test_confirmation_uses_currently_configured_group(self):
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
                    get_service=MagicMock(
                        return_value={
                            "enabled": True,
                            "group_chat_id": -1002,
                        }
                    )
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

            await _confirmation_handler()(callback)

            self.assertEqual(
                context.bot.send_message.await_args.args[0], -1002
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

            await _confirmation_handler()(callback)

            self.assertTrue(participant["confirmed"])
            self.assertFalse(participant["group_update_sent"])
            context.bot.send_message.assert_not_awaited()
            self.assertIn("служение отключено", callback.answer.await_args.args[0])

        asyncio.run(run())

    def test_failed_confirmation_save_is_reported_and_retried_when_disabled(self):
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
                admin_id=67890,
                state_cache=SimpleNamespace(
                    state={
                        "sunday_date": "2026-10-11",
                        "group_chat_id": -1001,
                        "participants": {"Ответственный": participant},
                    },
                    save=MagicMock(
                        side_effect=[OSError("disk full"), None]
                    ),
                ),
                service_registry=SimpleNamespace(
                    get_service=MagicMock(return_value={"enabled": False})
                ),
                bot=SimpleNamespace(send_message=AsyncMock()),
                group_chat_id=-1001,
            )
            dispatcher = SimpleNamespace(include_router=MagicMock())
            register(dispatcher, context)
            handler = _confirmation_handler()

            def make_callback():
                return SimpleNamespace(
                    data="cfm:2026-10-11:Ответственный",
                    from_user=SimpleNamespace(id=12345, username="alex"),
                    message=None,
                    answer=AsyncMock(),
                )

            with patch("bot.handlers._notify_admin", new=AsyncMock()):
                first = make_callback()
                await handler(first)
                self.assertIn("Не удалось сохранить", first.answer.await_args.args[0])
                self.assertTrue(participant["confirmed"])
                self.assertFalse(participant["confirmation_persisted"])

                second = make_callback()
                await handler(second)

            self.assertIn("служение отключено", second.answer.await_args.args[0])
            self.assertNotIn("confirmation_persisted", participant)
            self.assertFalse(participant["group_update_sent"])
            context.bot.send_message.assert_not_awaited()
            self.assertEqual(context.state_cache.save.call_count, 2)

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
            handler = _confirmation_handler()
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
            handler = _confirmation_handler()

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
            self.assertEqual(
                second.answer.await_args.args[0],
                "Вы уже подтвердили участие",
            )

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
            bot=SimpleNamespace(
                send_message=AsyncMock(), delete_message=AsyncMock()
            ),
            load_service_task_assignments=AsyncMock(
                return_value={"Микшер": ["Имя из расписания"]}
            ),
        )
        self.dispatcher = SimpleNamespace(include_router=MagicMock())
        register(self.dispatcher, self.context)
        self.admin_router = self.dispatcher.include_router.call_args_list[0].args[0]

    @staticmethod
    def message(actor_id, text, chat_type="private"):
        return SimpleNamespace(
            from_user=SimpleNamespace(id=actor_id),
            chat=SimpleNamespace(type=chat_type, id=actor_id),
            text=text,
            users_shared=None,
            chat_shared=None,
            answer=AsyncMock(),
        )

    def callback_handler(self, name):
        return next(
            handler.callback
            for handler in self.admin_router.callback_query.handlers
            if handler.callback.__name__ == name
        )

    async def dialog_button(
        self, message, predicate, actor_id, router_instance=None
    ):
        target_router = router_instance or self.admin_router
        for _ in range(20):
            markup = message.answer.await_args.kwargs.get("reply_markup")
            buttons = [
                button
                for row in markup.inline_keyboard
                for button in row
            ] if markup else []
            button = next((item for item in buttons if predicate(item)), None)
            if button is not None:
                return button
            next_page = next(
                (
                    item for item in buttons
                    if item.callback_data
                    and item.callback_data.startswith("ui_page:")
                    and "Далее" in item.text
                ),
                None,
            )
            if next_page is None:
                break
            callback = SimpleNamespace(
                data=next_page.callback_data,
                from_user=SimpleNamespace(id=actor_id),
                message=message,
                answer=AsyncMock(),
            )
            page_handler = next(
                handler.callback
                for handler in target_router.callback_query.handlers
                if handler.callback.__name__ == "on_dialog_page"
            )
            await page_handler(callback)
        self.fail("Не удалось найти кнопку на страницах диалога")

    def test_personal_notifications_wizard_toggles_and_manages_schedules(self):
        async def run():
            actor_id = 4004
            self.registry.set_responsible_contact(
                67890, "sound", "Микшер", "Алексей", actor_id
            )
            self.assertFalse(
                self.registry.get_user_notifications_enabled(actor_id, "sound")
            )
            next_message_id = 10

            def message(text=""):
                nonlocal next_message_id
                next_message_id += 1
                return SimpleNamespace(
                    from_user=SimpleNamespace(id=actor_id),
                    chat=SimpleNamespace(type="private", id=actor_id),
                    text=text,
                    answer=AsyncMock(
                        return_value=SimpleNamespace(
                            message_id=next_message_id
                        )
                    ),
                )

            def callback(data):
                return SimpleNamespace(
                    data=data,
                    from_user=SimpleNamespace(id=actor_id),
                    message=message(),
                    answer=AsyncMock(),
                )

            async def press(source, predicate):
                button = next(
                    button
                    for row in source.answer.await_args.kwargs[
                        "reply_markup"
                    ].inline_keyboard
                    for button in row
                    if predicate(button)
                )
                result = callback(button.callback_data)
                await personal_handler(result)
                return result

            personal_handler = self.callback_handler(
                "on_personal_notifications_callback"
            )
            command = message("/my_notifications")
            command_handler = next(
                item.callback
                for item in self.admin_router.message.handlers
                if item.callback.__name__ == "on_my_notifications"
            )
            await command_handler(command)
            service_button = next(
                button
                for row in command.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row
                if "ЗВУК" in button.text
            )

            service_callback = callback(service_button.callback_data)
            await personal_handler(service_callback)
            service_markup = service_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            stale_texts_button = next(
                button
                for row in service_markup.inline_keyboard
                for button in row
                if button.text == "📝 Мои тексты"
            )
            toggle_button = next(
                button
                for row in service_markup.inline_keyboard
                for button in row
                if button.text == "✅ Включить"
            )
            toggle_callback = callback(toggle_button.callback_data)
            await personal_handler(toggle_callback)
            self.assertTrue(
                self.registry.get_user_notifications_enabled(actor_id, "sound")
            )
            stale_callback = callback(stale_texts_button.callback_data)
            await personal_handler(stale_callback)
            self.assertIn("устарело", stale_callback.answer.await_args.args[0])

            texts_button = next(
                button
                for row in toggle_callback.message.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row
                if button.text == "📝 Мои тексты"
            )
            texts_callback = callback(texts_button.callback_data)
            await personal_handler(texts_callback)
            task_button = next(
                button
                for row in texts_callback.message.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row
                if "Микшер" in button.text
            )
            task_callback = callback(task_button.callback_data)
            await personal_handler(task_callback)
            custom_body = "Особое напоминание для {name}: {task}"
            self.registry.upsert_user_text(
                actor_id, "sound", "Микшер", custom_body
            )
            schedules_button = next(
                button
                for row in task_callback.message.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row
                if button.text == "⏰ Расписание"
            )
            schedules_callback = callback(schedules_button.callback_data)
            await personal_handler(schedules_callback)
            add_button = next(
                button
                for row in schedules_callback.message.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row
                if button.text == "➕ Добавить правило"
            )
            add_callback = callback(add_button.callback_data)
            await personal_handler(add_callback)
            input_message = message("2 18:30")
            text_handler = next(
                item.callback
                for item in self.admin_router.message.handlers
                if item.callback.__name__ == "on_service_parameter"
            )
            await text_handler(input_message)

            entry = self.registry.get_user_text(actor_id, "sound", "Микшер")
            schedules = self.registry.list_schedules(entry["id"])
            self.assertEqual(len(schedules), 1)
            self.assertEqual(schedules[0]["reminder_days"], 2)
            self.assertEqual(schedules[0]["reminder_time"], "18:30")
            self.assertEqual(
                self.registry.get_user_text(actor_id, "sound", "Микшер")[
                    "body"
                ],
                custom_body,
            )
            disabled_schedule = await press(
                input_message,
                lambda button: button.text.startswith("🟢 за 2 дн."),
            )
            self.assertFalse(
                self.registry.list_schedules(entry["id"])[0]["enabled"]
            )
            delete_schedule_callback = await press(
                disabled_schedule.message,
                lambda button: button.text.startswith("🗑 Удалить ·"),
            )
            self.assertEqual(self.registry.list_schedules(entry["id"]), [])
            task_detail = await press(
                delete_schedule_callback.message,
                lambda button: button.text == "⬅️ Назад",
            )
            await press(
                task_detail.message,
                lambda button: button.text == "✏️ Задать / изменить текст",
            )
            text_message = message("Моё напоминание для {name}: {task}")
            await text_handler(text_message)
            self.assertEqual(
                self.registry.get_user_text(actor_id, "sound", "Микшер")[
                    "body"
                ],
                "Моё напоминание для {name}: {task}",
            )
            delete_confirmation = await press(
                text_message,
                lambda button: button.text == "🗑 Удалить текст",
            )
            await press(
                delete_confirmation.message,
                lambda button: button.text == "🗑 Да, удалить",
            )
            self.assertIsNone(
                self.registry.get_user_text(actor_id, "sound", "Микшер")
            )
            self.assertEqual(
                self.registry.list_schedules(entry["id"]), []
            )
            self.assertTrue(
                self.registry.get_user_notifications_enabled(actor_id, "sound")
            )
            self.assertEqual(
                self.registry.get_service("sound")["group_chat_id"], None
            )
            self.context.bot.delete_message.assert_awaited()
            self.context.load_service_task_assignments.assert_awaited_once_with(
                "sound"
            )

        asyncio.run(run())

    def test_personal_input_refresh_failures_are_retryable(self):
        async def run():
            actor_id = 4405
            assignments = {"Микшер": ["Алексей"]}
            self.registry.set_responsible_contact(
                67890, "sound", "Микшер", "Алексей", actor_id
            )
            self.context.load_service_task_assignments = AsyncMock(
                side_effect=[
                    assignments,
                    RuntimeError("temporary workbook failure"),
                    assignments,
                    OSError("temporary filesystem failure"),
                    assignments,
                ]
            )

            def callback(button):
                return SimpleNamespace(
                    data=button.callback_data,
                    from_user=SimpleNamespace(id=actor_id),
                    message=self.message(actor_id, ""),
                    answer=AsyncMock(),
                )

            async def press(source, predicate):
                button = next(
                    button
                    for row in source.answer.await_args.kwargs[
                        "reply_markup"
                    ].inline_keyboard
                    for button in row
                    if predicate(button)
                )
                result = callback(button)
                await personal_handler(result)
                return result

            personal_handler = self.callback_handler(
                "on_personal_notifications_callback"
            )
            command = self.message(actor_id, "/my_notifications")
            command_handler = next(
                item.callback
                for item in self.admin_router.message.handlers
                if item.callback.__name__ == "on_my_notifications"
            )
            await command_handler(command)
            service = await press(
                command, lambda button: "ЗВУК" in button.text
            )
            texts = await press(
                service.message,
                lambda button: button.text == "📝 Мои тексты",
            )
            task = await press(
                texts.message, lambda button: "Микшер" in button.text
            )
            await press(
                task.message,
                lambda button: button.text == "✏️ Задать / изменить текст",
            )
            text_handler = next(
                item.callback
                for item in self.admin_router.message.handlers
                if item.callback.__name__ == "on_service_parameter"
            )
            failed_text = self.message(actor_id, "Моё сообщение для {name}")
            with patch(
                "bot.handlers.PERSONAL_TASK_CACHE_TTL_SECONDS", -1
            ):
                await text_handler(failed_text)
            self.assertIn(
                "Не удалось сверить задачи с актуальным графиком",
                failed_text.answer.await_args.args[0],
            )
            retry_text = self.message(actor_id, "Моё сообщение для {name}")
            with patch(
                "bot.handlers.PERSONAL_TASK_CACHE_TTL_SECONDS", -1
            ):
                await text_handler(retry_text)
            self.assertEqual(
                self.registry.get_user_text(actor_id, "sound", "Микшер")[
                    "body"
                ],
                "Моё сообщение для {name}",
            )

            schedules = await press(
                retry_text, lambda button: button.text == "⏰ Расписание"
            )
            await press(
                schedules.message,
                lambda button: button.text == "➕ Добавить правило",
            )
            failed_schedule = self.message(actor_id, "2 18:30")
            with patch(
                "bot.handlers.PERSONAL_TASK_CACHE_TTL_SECONDS", -1
            ):
                await text_handler(failed_schedule)
            self.assertIn(
                "Не удалось сверить задачи с актуальным графиком",
                failed_schedule.answer.await_args.args[0],
            )
            retry_schedule = self.message(actor_id, "2 18:30")
            with patch(
                "bot.handlers.PERSONAL_TASK_CACHE_TTL_SECONDS", -1
            ):
                await text_handler(retry_schedule)

            text_entry = self.registry.get_user_text(
                actor_id, "sound", "Микшер"
            )
            self.assertEqual(
                [
                    (item["reminder_days"], item["reminder_time"])
                    for item in self.registry.list_schedules(text_entry["id"])
                ],
                [(2, "18:30")],
            )
            self.assertEqual(
                self.context.load_service_task_assignments.await_count, 5
            )
            self.context.debug_logger.exception.assert_any_call(
                "Не удалось сверить задачи с актуальным графиком"
            )

        asyncio.run(run())

    def test_personal_notification_screens_exclude_informational_tasks(self):
        async def run():
            actor_id = 4404
            self.registry.set_responsible_contact(
                67890, "sound", "Микшер", "Алексей", actor_id
            )
            self.registry.set_responsible_contact(
                67890, "sound", "Праздник", "Дежурный", actor_id
            )
            self.context.load_service_task_assignments = AsyncMock(
                return_value={
                    "Микшер": {
                        "names": ["Алексей"],
                        "is_informational": False,
                    },
                    "Праздник": {
                        "names": ["Дежурный"],
                        "is_informational": True,
                    },
                }
            )
            message_id = 200

            def message(text=""):
                nonlocal message_id
                message_id += 1
                return SimpleNamespace(
                    from_user=SimpleNamespace(id=actor_id),
                    chat=SimpleNamespace(type="private", id=actor_id),
                    text=text,
                    answer=AsyncMock(
                        return_value=SimpleNamespace(message_id=message_id)
                    ),
                )

            def callback(data):
                return SimpleNamespace(
                    data=data,
                    from_user=SimpleNamespace(id=actor_id),
                    message=message(),
                    answer=AsyncMock(),
                )

            handler = self.callback_handler(
                "on_personal_notifications_callback"
            )
            command = message("/my_notifications")
            await next(
                item.callback
                for item in self.admin_router.message.handlers
                if item.callback.__name__ == "on_my_notifications"
            )(command)
            service_button = next(
                button
                for row in command.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row
                if "ЗВУК" in button.text
            )
            service_callback = callback(service_button.callback_data)
            await handler(service_callback)
            service_text = service_callback.message.answer.await_args.args[0]
            self.assertIn("Доступно задач: 1", service_text)

            texts_button = next(
                button
                for row in service_callback.message.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row
                if button.text == "📝 Мои тексты"
            )
            texts_callback = callback(texts_button.callback_data)
            await handler(texts_callback)
            texts_markup = texts_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            task_labels = [
                button.text
                for row in texts_markup.inline_keyboard
                for button in row
            ]
            self.assertTrue(any("Микшер" in label for label in task_labels))
            self.assertFalse(any("Праздник" in label for label in task_labels))
            self.assertIsNone(
                self.registry.get_user_text(actor_id, "sound", "Праздник")
            )

        asyncio.run(run())

    def test_informational_task_text_can_be_reset_and_deleted_without_refresh(self):
        async def run():
            actor_id = 4406
            self.registry.set_responsible_contact(
                67890, "sound", "Праздник", "Дежурный", actor_id
            )
            self.registry.upsert_user_text(
                actor_id, "sound", "Праздник", "Личное {name}: {task}"
            )
            self.context.load_service_task_assignments = AsyncMock(
                return_value={
                    "Праздник": {
                        "names": [],
                        "is_informational": True,
                    }
                }
            )

            def callback(button):
                return SimpleNamespace(
                    data=button.callback_data,
                    from_user=SimpleNamespace(id=actor_id),
                    message=self.message(actor_id, ""),
                    answer=AsyncMock(),
                )

            async def press(source, predicate):
                button = next(
                    button
                    for row in source.answer.await_args.kwargs[
                        "reply_markup"
                    ].inline_keyboard
                    for button in row
                    if predicate(button)
                )
                result = callback(button)
                await handler(result)
                return result

            handler = self.callback_handler(
                "on_personal_notifications_callback"
            )
            command = self.message(actor_id, "/my_notifications")
            command_handler = next(
                item.callback
                for item in self.admin_router.message.handlers
                if item.callback.__name__ == "on_my_notifications"
            )
            await command_handler(command)
            service = await press(
                command, lambda button: "ЗВУК" in button.text
            )
            texts = await press(
                service.message,
                lambda button: button.text == "📝 Мои тексты",
            )
            stale_task = await press(
                texts.message,
                lambda button: "Праздник" in button.text,
            )
            task = await press(
                stale_task.message,
                lambda button: button.text == "🔄 Вернуть стандартный",
            )
            self.assertIsNone(
                self.registry.get_user_text(
                    actor_id, "sound", "Праздник"
                )["body"]
            )
            delete = await press(
                task.message,
                lambda button: button.text == "🗑 Удалить текст",
            )
            await press(
                delete.message,
                lambda button: button.text == "🗑 Да, удалить",
            )

            self.assertIsNone(
                self.registry.get_user_text(
                    actor_id, "sound", "Праздник"
                )
            )
            self.context.load_service_task_assignments.assert_awaited_once_with(
                "sound"
            )

        asyncio.run(run())

    def test_personal_notifications_bulk_toggle_preserves_rules_and_conflicts(self):
        async def run():
            actor_id = 4004
            self.registry.set_responsible_contact(
                67890, "sound", "Микшер", "Алексей", actor_id
            )
            self.registry.set_responsible_contact(
                67890, "slides", "Слайды", "Алексей", actor_id
            )
            text_id = self.registry.upsert_user_text(
                actor_id, "sound", "Микшер", None
            )
            schedule = self.registry.add_schedule(text_id, 1, "09:00")
            next_message_id = 20

            def message(text=""):
                nonlocal next_message_id
                next_message_id += 1
                return SimpleNamespace(
                    from_user=SimpleNamespace(id=actor_id),
                    chat=SimpleNamespace(type="private", id=actor_id),
                    text=text,
                    answer=AsyncMock(
                        return_value=SimpleNamespace(
                            message_id=next_message_id
                        )
                    ),
                )

            def callback(data):
                return SimpleNamespace(
                    data=data,
                    from_user=SimpleNamespace(id=actor_id),
                    message=message(),
                    answer=AsyncMock(),
                )

            command_handler = next(
                item.callback
                for item in self.admin_router.message.handlers
                if item.callback.__name__ == "on_my_notifications"
            )
            personal_handler = self.callback_handler(
                "on_personal_notifications_callback"
            )
            command = message("/my_notifications")
            await command_handler(command)
            markup = command.answer.await_args.kwargs["reply_markup"]
            enable_all = next(
                button
                for row in markup.inline_keyboard
                for button in row
                if button.text == "✅ Включить все"
            )
            stale_service = next(
                button
                for row in markup.inline_keyboard
                for button in row
                if "ЗВУК" in button.text
            )
            bulk_callback = callback(enable_all.callback_data)
            await personal_handler(bulk_callback)
            self.assertTrue(
                self.registry.get_user_notifications_enabled(actor_id, "sound")
            )
            self.assertTrue(
                self.registry.get_user_notifications_enabled(actor_id, "slides")
            )
            self.assertEqual(
                self.registry.list_schedules(text_id), [schedule]
            )

            service_callback = callback(stale_service.callback_data)
            await personal_handler(service_callback)
            self.assertIn("устарело", service_callback.answer.await_args.args[0])

            service_button = next(
                button
                for row in bulk_callback.message.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row
                if "ЗВУК" in button.text
            )
            active_service_callback = callback(service_button.callback_data)
            await personal_handler(active_service_callback)
            stale_toggle = next(
                button
                for row in active_service_callback.message.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row
                if button.text == "🔕 Выключить"
            )
            legacy_command = message("/service_notify")
            service_notify = next(
                item.callback
                for item in self.admin_router.message.handlers
                if item.callback.__name__ == "on_service_notify"
            )
            await service_notify(legacy_command)
            self.context.bot.delete_message.assert_awaited()
            stale_after_conflict = callback(stale_toggle.callback_data)
            await personal_handler(stale_after_conflict)
            self.assertIn(
                "устарело",
                stale_after_conflict.answer.await_args.args[0],
            )

        asyncio.run(run())

    def test_service_list_shows_all_eight_services_in_requested_order(self):
        async def run():
            handler = next(
                item.callback
                for item in self.admin_router.message.handlers
                if item.callback.__name__ == "on_services"
            )
            message = self.message(67890, "/services")
            await handler(message)

            markup = message.answer.await_args.kwargs["reply_markup"]
            self.assertEqual(
                [
                    [button.callback_data.rsplit(":", 1)[-1] for button in row]
                    for row in markup.inline_keyboard
                ],
                [
                    ["assembly", "broadcast", "slides"],
                    ["order", "kitchen", "coffee"],
                    ["sound", "rehearsal"],
                ],
            )
            self.assertEqual(sum(map(len, markup.inline_keyboard)), 8)
            self.assertFalse(
                any(
                    button.callback_data.startswith("ui_page:")
                    for row in markup.inline_keyboard
                    for button in row
                )
            )

        asyncio.run(run())

    def test_super_admin_can_configure_chat_admin_and_contact(self):
        async def run():
            handlers = self.admin_router.message.handlers
            direct_add = self.message(
                67890, "/service_admin_add sound 12345"
            )
            await handlers[2].callback(
                direct_add
            )
            self.assertEqual(self.registry.list_service_admins("sound"), [])
            confirmation_button = direct_add.answer.await_args.kwargs[
                "reply_markup"
            ].inline_keyboard[0][0]
            confirmation = SimpleNamespace(
                data=confirmation_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_shared_admin_confirmation")(
                confirmation
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
                None,
            )

        asyncio.run(run())

    def test_notification_settings_require_guided_flow(self):
        async def run():
            self.registry.add_service_admin(67890, "sound", 12345)
            handler = self.admin_router.message.handlers[6].callback

            manual = self.message(
                12345,
                '/service_notify slides 2 17:30 | Текст группы | {}',
            )
            await handler(manual)
            self.assertIn(
                "пошагово",
                manual.answer.await_args.args[0],
            )
            service = next(
                service for service in self.registry.list_services()
                if service["id"] == "sound"
            )
            self.assertEqual(service["reminder_days"], 1)
            self.assertIsNone(service["group_template"])

        asyncio.run(run())

    def test_notification_step_back_keeps_session_and_root_close_has_navigation(self):
        async def run():
            self.registry.add_service_admin(67890, "sound", 12345)
            command = self.message(12345, "/service_notify")
            await self.admin_router.message.handlers[6].callback(command)
            service_callback = SimpleNamespace(
                data="service:service_notify:sound",
                from_user=SimpleNamespace(id=12345),
                message=self.message(12345, ""),
                answer=AsyncMock(),
            )
            service_callback.message.answer.return_value = SimpleNamespace(
                message_id=501
            )
            await self.callback_handler("on_service_selected")(service_callback)
            root_markup = service_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            group_button = next(
                button
                for row in root_markup.inline_keyboard
                for button in row
                if button.callback_data.startswith(
                    "group_reminder:default:sound:"
                )
            )
            edit_callback = SimpleNamespace(
                data=group_button.callback_data,
                from_user=SimpleNamespace(id=12345),
                message=self.message(12345, ""),
                answer=AsyncMock(),
            )
            edit_callback.message.answer.return_value = SimpleNamespace(
                message_id=502
            )
            await self.callback_handler("on_group_reminder_action")(
                edit_callback
            )
            edit_default_button = next(
                button
                for row in edit_callback.message.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row
                if button.callback_data.startswith(
                    "group_reminder:edit_default:"
                )
            )
            edit_default_callback = SimpleNamespace(
                data=edit_default_button.callback_data,
                from_user=SimpleNamespace(id=12345),
                message=self.message(12345, ""),
                answer=AsyncMock(),
            )
            edit_default_callback.message.answer.return_value = SimpleNamespace(
                message_id=502
            )
            await self.callback_handler("on_group_reminder_action")(
                edit_default_callback
            )
            prompt_markup = edit_default_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            back_button = next(
                button
                for row in prompt_markup.inline_keyboard
                for button in row
                if button.callback_data.startswith("notify_back:")
            )
            back_callback = SimpleNamespace(
                data=back_button.callback_data,
                from_user=SimpleNamespace(id=12345),
                message=edit_default_callback.message,
                answer=AsyncMock(),
            )
            back_callback.message.answer.return_value = SimpleNamespace(
                message_id=503
            )
            await self.callback_handler("on_notification_edit_back")(
                back_callback
            )
            returned_markup = back_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            root_back = next(
                button
                for row in returned_markup.inline_keyboard
                for button in row
                if button.callback_data.startswith(
                    "group_reminder:root_back:"
                )
            )
            root_callback = SimpleNamespace(
                data=root_back.callback_data,
                from_user=SimpleNamespace(id=12345),
                message=back_callback.message,
                answer=AsyncMock(),
            )
            await self.callback_handler("on_group_reminder_action")(
                root_callback
            )
            returned_markup = root_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            self.assertTrue(
                any(
                    button.callback_data.startswith("notify_finish:")
                    for row in returned_markup.inline_keyboard
                    for button in row
                )
            )
            self.assertEqual(
                self.registry.get_service("sound")["group_template"], None
            )

            close_button = next(
                button
                for row in returned_markup.inline_keyboard
                for button in row
                if button.text == "✖️ Закрыть настройку"
            )
            close_callback = SimpleNamespace(
                data=close_button.callback_data,
                from_user=SimpleNamespace(id=12345),
                message=self.message(12345, ""),
                answer=AsyncMock(),
            )
            close_callback.message.answer.return_value = SimpleNamespace(
                message_id=504
            )
            await self.callback_handler("on_notification_setting_cancel")(
                close_callback
            )
            result_markup = close_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            self.assertEqual(
                len(result_markup.inline_keyboard),
                2,
            )
            self.assertEqual(
                [
                    call.kwargs["message_id"]
                    for call in self.context.bot.delete_message.await_args_list
                ],
                [501, 502, 502, 503, 503],
            )

        asyncio.run(run())

    def test_chat_picker_prompt_is_deleted_after_shared_chat_arrives(self):
        async def run():
            command = self.message(67890, "/service_chat")
            command_handler = next(
                item.callback
                for item in self.admin_router.message.handlers
                if item.callback.__name__ == "on_service_chat"
            )
            await command_handler(command)
            service_button = next(
                button
                for row in command.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row
                if button.callback_data.startswith("service:service_chat:")
            )
            selection = SimpleNamespace(
                data=service_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=self.message(67890, ""),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_service_selected")(selection)
            action_selection = SimpleNamespace(
                data=f"service:service_chat_set:{service_button.callback_data.rsplit(':', 1)[1]}",
                from_user=SimpleNamespace(id=67890),
                message=self.message(67890, ""),
                answer=AsyncMock(),
            )
            action_selection.message.answer.return_value = SimpleNamespace(
                message_id=601
            )
            await self.callback_handler("on_service_selected")(action_selection)
            keyboard = action_selection.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            request_id = keyboard.keyboard[0][0].request_chat.request_id
            shared_message = SimpleNamespace(
                from_user=SimpleNamespace(id=67890),
                chat=SimpleNamespace(type="private", id=67890),
                chat_shared=SimpleNamespace(
                    request_id=request_id,
                    chat_id=-100601,
                    title="Test chat",
                ),
                answer=AsyncMock(),
            )
            chat_shared_handler = next(
                item.callback
                for item in self.admin_router.message.handlers
                if item.callback.__name__ == "on_chat_shared"
            )
            await chat_shared_handler(shared_message)

            self.context.bot.delete_message.assert_awaited_once_with(
                chat_id=67890, message_id=601
            )

        asyncio.run(run())

    def test_service_admin_can_use_guided_notification_setup(self):
        async def run():
            self.registry.add_service_admin(67890, "sound", 12345)
            self.registry.set_notification_settings(
                67890,
                "sound",
                reminder_days=2,
                reminder_time="17:00",
                group_template="Текст группы",
                personal_templates={
                    "Микшер": "Старый текст",
                    "Задача отсутствующая в этом графике": "Не удалять",
                },
            )
            message = self.message(12345, "/service_notify")
            await self.admin_router.message.handlers[6].callback(message)
            service_button = message.answer.await_args.kwargs[
                "reply_markup"
            ].inline_keyboard[0][0]
            service_callback = SimpleNamespace(
                data=service_button.callback_data,
                from_user=SimpleNamespace(id=12345),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_service_selected")(service_callback)
            menu = service_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            reminders_button = next(
                button
                for row in menu.inline_keyboard
                for button in row
                if button.callback_data.startswith(
                    "group_reminder:list:sound:"
                )
            )
            edit_callback = SimpleNamespace(
                data=reminders_button.callback_data,
                from_user=SimpleNamespace(id=12345),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler(
                "on_group_reminder_action"
            )(edit_callback)
            rules_menu = edit_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            rule_button = next(
                button
                for row in rules_menu.inline_keyboard
                for button in row
                if button.callback_data.startswith("group_reminder:open:sound:")
            )
            rule_callback = SimpleNamespace(
                data=rule_button.callback_data,
                from_user=SimpleNamespace(id=12345),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_group_reminder_action")(
                rule_callback
            )
            edit_rule_menu = rule_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            days_button = next(
                button
                for row in edit_rule_menu.inline_keyboard
                for button in row
                if button.text == "📅 Срок"
            )
            days_callback = SimpleNamespace(
                data=days_button.callback_data,
                from_user=SimpleNamespace(id=12345),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_group_reminder_action")(
                days_callback
            )
            self.assertIn("0 до 6", days_callback.message.answer.await_args.args[0])
            staged_input = next(
                item.callback
                for item in self.admin_router.message.handlers
                if item.callback.__name__ == "on_service_parameter"
            )
            await staged_input(self.message(12345, "7"))
            updated_days = self.message(12345, "3")
            await staged_input(updated_days)
            rules_markup = updated_days.answer.await_args.kwargs["reply_markup"]
            back_to_list = next(
                button
                for row in rules_markup.inline_keyboard
                for button in row
                if button.callback_data.startswith("group_reminder:back:")
            )
            list_callback = SimpleNamespace(
                data=back_to_list.callback_data,
                from_user=SimpleNamespace(id=12345),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_group_reminder_action")(
                list_callback
            )
            rules_menu = list_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            root_back = next(
                button
                for row in rules_menu.inline_keyboard
                for button in row
                if button.callback_data.startswith(
                    "group_reminder:root_back:"
                )
            )
            root_callback = SimpleNamespace(
                data=root_back.callback_data,
                from_user=SimpleNamespace(id=12345),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_group_reminder_action")(
                root_callback
            )
            root_menu = root_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            finish_button = next(
                button
                for row in root_menu.inline_keyboard
                for button in row
                if button.text == "✅ Завершить и сохранить"
            )
            finish_callback = SimpleNamespace(
                data=finish_button.callback_data,
                from_user=SimpleNamespace(id=12345),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler(
                "on_notification_setting_finish"
            )(finish_callback)

            service = self.registry.get_service("sound")
            self.assertEqual(
                self.registry.list_group_reminders("sound")[0][
                    "reminder_days"
                ],
                3,
            )
            self.assertEqual(service["group_template"], "Текст группы")
            self.assertEqual(
                service["personal_templates"],
                {
                    "Микшер": "Старый текст",
                    "Задача отсутствующая в этом графике": "Не удалять",
                },
            )
            self.context.load_service_task_assignments.assert_not_awaited()
            result_markup = finish_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            self.assertEqual(
                {
                    button.text
                    for row in result_markup.inline_keyboard
                    for button in row
                },
                {"⚙️ К настройкам служения", "📋 К списку служений"},
            )

            retry_command = self.message(12345, "/service_notify")
            await self.admin_router.message.handlers[6].callback(retry_command)
            retry_service_callback = SimpleNamespace(
                data="service:service_notify:sound",
                from_user=SimpleNamespace(id=12345),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_service_selected")(
                retry_service_callback
            )
            retry_menu = retry_service_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            cancel_button = next(
                button
                for row in retry_menu.inline_keyboard
                for button in row
                if button.text == "✖️ Закрыть настройку"
            )
            cancel_callback = SimpleNamespace(
                data=cancel_button.callback_data,
                from_user=SimpleNamespace(id=12345),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_notification_setting_cancel")(
                cancel_callback
            )
            cancelled_service = self.registry.get_service("sound")
            self.assertEqual(
                self.registry.list_group_reminders("sound")[0][
                    "reminder_days"
                ],
                3,
            )

        asyncio.run(run())

    def test_group_reminder_preview_is_private_and_returns_to_card(self):
        async def run():
            self.context.get_group_reminder_preview = AsyncMock(
                return_value={
                    "date": "25.10.2026",
                    "time": "18:00",
                    "text": "Отрендеренное сообщение группы " + "🙂" * 5000,
                    "has_data": True,
                    "error": None,
                }
            )
            command = self.message(67890, "/service_notify")
            await self.admin_router.message.handlers[6].callback(command)
            service_callback = SimpleNamespace(
                data="service:service_notify:sound",
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private", id=67890),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_service_selected")(service_callback)
            settings = service_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            reminders_button = next(
                button
                for row in settings.inline_keyboard
                for button in row
                if button.callback_data.startswith("group_reminder:list:sound:")
            )
            reminders_callback = SimpleNamespace(
                data=reminders_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private", id=67890),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_group_reminder_action")(
                reminders_callback
            )
            reminder_button = next(
                button
                for row in reminders_callback.message.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row
                if button.callback_data.startswith(
                    "group_reminder:open:sound:"
                )
            )
            reminder_callback = SimpleNamespace(
                data=reminder_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private", id=67890),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_group_reminder_action")(
                reminder_callback
            )
            card_markup = reminder_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            preview_button = next(
                button
                for row in card_markup.inline_keyboard
                for button in row
                if button.text == "👁 Предпросмотр"
            )
            reminder_id = self.registry.list_group_reminders("sound")[0]["id"]
            self.assertEqual(
                preview_button.callback_data,
                f"group_reminder:preview:sound:{reminder_id}",
            )
            preview_callback = SimpleNamespace(
                data=preview_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private", id=67890),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_group_reminder_action")(
                preview_callback
            )

            self.context.get_group_reminder_preview.assert_awaited_once_with(
                reminder_id
            )
            preview_text = preview_callback.message.answer.await_args.args[0]
            self.assertIn("👁 Предпросмотр · ЗВУК", preview_text)
            self.assertIn("📅 25.10.2026 · 🕒 18:00", preview_text)
            self.assertIn("Отрендеренное сообщение группы", preview_text)
            self.assertIn("... (текст обрезан)", preview_text)
            back_button = preview_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ].inline_keyboard[0][0]
            self.assertEqual(back_button.text, "⬅️ К напоминанию")
            self.assertTrue(
                back_button.callback_data.startswith(
                    "group_reminder:back:sound:"
                )
            )
            self.context.bot.send_message.assert_not_awaited()

            back_callback = SimpleNamespace(
                data=back_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private", id=67890),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_group_reminder_action")(
                back_callback
            )
            card_markup = back_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            preview_button = next(
                button
                for row in card_markup.inline_keyboard
                for button in row
                if button.text == "👁 Предпросмотр"
            )
            self.context.get_group_reminder_preview.side_effect = (
                sqlite3.OperationalError("database unavailable")
            )
            db_error_callback = SimpleNamespace(
                data=preview_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private", id=67890),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_group_reminder_action")(
                db_error_callback
            )
            self.assertIn(
                "Не удалось прочитать настройки напоминания.",
                db_error_callback.message.answer.await_args.args[0],
            )
            self.assertEqual(
                self.context.get_group_reminder_preview.await_count, 2
            )
            self.context.bot.send_message.assert_not_awaited()

        asyncio.run(run())

    def test_guided_notification_setup_adds_independent_reminder(self):
        async def run():
            self.registry.add_service_admin(67890, "sound", 12345)
            command = self.message(12345, "/service_notify")
            await self.admin_router.message.handlers[6].callback(command)
            selection = SimpleNamespace(
                data="service:service_notify:sound",
                from_user=SimpleNamespace(id=12345),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_service_selected")(selection)
            main_menu = selection.message.answer.await_args.kwargs["reply_markup"]
            reminder_button = next(
                button
                for row in main_menu.inline_keyboard
                for button in row
                if button.text.startswith("⏰ Напоминания")
            )
            reminder_menu_callback = SimpleNamespace(
                data=reminder_button.callback_data,
                from_user=SimpleNamespace(id=12345),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_group_reminder_action")(
                reminder_menu_callback
            )
            rules_menu = reminder_menu_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            add_button = next(
                button
                for row in rules_menu.inline_keyboard
                for button in row
                if button.text == "➕ Добавить напоминание"
            )
            add_callback = SimpleNamespace(
                data=add_button.callback_data,
                from_user=SimpleNamespace(id=12345),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_group_reminder_action")(
                add_callback
            )
            confirm_add_button = next(
                button
                for row in add_callback.message.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row
                if button.callback_data.startswith(
                    "group_reminder:confirm_add:"
                )
            )
            confirm_add_callback = SimpleNamespace(
                data=confirm_add_button.callback_data,
                from_user=SimpleNamespace(id=12345),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_group_reminder_action")(
                confirm_add_callback
            )
            parameter_handler = next(
                item.callback
                for item in self.admin_router.message.handlers
                if item.callback.__name__ == "on_service_parameter"
            )
            card_markup = confirm_add_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            edit_days = next(
                button
                for row in card_markup.inline_keyboard
                for button in row
                if button.text == "📅 Срок"
            )
            edit_days_callback = SimpleNamespace(
                data=edit_days.callback_data,
                from_user=SimpleNamespace(id=12345),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_group_reminder_action")(
                edit_days_callback
            )
            updated_days = self.message(12345, "3")
            await parameter_handler(updated_days)
            card_markup = updated_days.answer.await_args.kwargs[
                "reply_markup"
            ]
            edit_time = next(
                button
                for row in card_markup.inline_keyboard
                for button in row
                if button.text == "🕒 Время"
            )
            edit_time_callback = SimpleNamespace(
                data=edit_time.callback_data,
                from_user=SimpleNamespace(id=12345),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_group_reminder_action")(
                edit_time_callback
            )
            saved_time_input = self.message(12345, "10:30")
            await parameter_handler(saved_time_input)

            reminders = self.registry.list_group_reminders("sound")
            self.assertEqual(
                sorted(
                    (item["reminder_days"], item["reminder_time"])
                    for item in reminders
                ),
                [(1, "18:00"), (3, "10:30")],
            )

        asyncio.run(run())

    def test_old_notification_buttons_cannot_change_new_session(self):
        async def run():
            async def begin_session():
                command = self.message(67890, "/service_notify")
                await self.admin_router.message.handlers[6].callback(command)
                service_callback = SimpleNamespace(
                    data="service:service_notify:sound",
                    from_user=SimpleNamespace(id=67890),
                    message=SimpleNamespace(
                        chat=SimpleNamespace(type="private"),
                        answer=AsyncMock(),
                    ),
                    answer=AsyncMock(),
                )
                await self.callback_handler("on_service_selected")(
                    service_callback
                )
                return service_callback.message.answer.await_args.kwargs[
                    "reply_markup"
                ]

            first_menu = await begin_session()
            first_buttons = [
                button
                for row in first_menu.inline_keyboard
                for button in row
            ]
            old_buttons = {
                "reminders": next(
                    button.callback_data for button in first_buttons
                    if button.callback_data.startswith(
                        "group_reminder:list:"
                    )
                ),
                "finish": next(
                    button.callback_data for button in first_buttons
                    if button.callback_data.startswith("notify_finish:")
                ),
                "cancel": next(
                    button.callback_data for button in first_buttons
                    if button.callback_data.startswith("notify_cancel:")
                ),
            }
            second_menu = await begin_session()
            current_finish = next(
                button.callback_data
                for row in second_menu.inline_keyboard
                for button in row
                if button.callback_data.startswith("notify_finish:")
            )
            self.assertNotEqual(
                old_buttons["finish"], current_finish
            )

            for callback_name, old_data in (
                (
                    "on_group_reminder_action",
                    old_buttons["reminders"],
                ),
                (
                    "on_notification_setting_finish",
                    old_buttons["finish"],
                ),
                (
                    "on_notification_setting_cancel",
                    old_buttons["cancel"],
                ),
            ):
                callback = SimpleNamespace(
                    data=old_data,
                    from_user=SimpleNamespace(id=67890),
                    message=SimpleNamespace(
                        chat=SimpleNamespace(type="private"),
                        answer=AsyncMock(),
                    ),
                    answer=AsyncMock(),
                )
                await self.callback_handler(callback_name)(callback)
                self.assertIn(
                    "устарела",
                    callback.answer.await_args.args[0].lower(),
                )

            finish_callback = SimpleNamespace(
                data=current_finish,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler(
                "on_notification_setting_finish"
            )(finish_callback)
            self.assertEqual(
                self.registry.get_service("sound")["reminder_days"], 1
            )

        asyncio.run(run())

    def test_notification_callbacks_answer_when_access_check_hits_database_error(self):
        async def run():
            edit = SimpleNamespace(
                data="group_reminder:default:sound:session",
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            finish = SimpleNamespace(
                data="notify_finish:sound:session",
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            with patch.object(
                self.registry,
                "is_service_admin",
                side_effect=sqlite3.OperationalError("database is locked"),
            ), patch.object(
                self.registry, "is_super_admin", return_value=False
            ):
                await self.callback_handler("on_group_reminder_action")(
                    edit
                )
                await self.callback_handler("on_notification_setting_finish")(
                    finish
                )

            self.assertIn("устарела", edit.answer.await_args.args[0])
            self.assertEqual(finish.answer.await_args.args[0], "Не удалось проверить права.")
            self.context.debug_logger.exception.assert_any_call(
                "Не удалось проверить права на сохранение настроек служения"
            )

        asyncio.run(run())

    def test_personal_template_prompt_displays_single_braced_names(self):
        async def run():
            self.context.load_service_task_assignments = AsyncMock(
                return_value={
                    "Микшер": {
                        "names": ["Алексей"],
                        "is_informational": False,
                    },
                    "Причастие": {
                        "names": [],
                        "is_informational": True,
                    },
                }
            )
            command = self.message(67890, "/service_notify")
            await self.admin_router.message.handlers[6].callback(command)
            service_callback = SimpleNamespace(
                data="service:service_notify:sound",
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_service_selected")(service_callback)
            menu = service_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            personal_button = next(
                button
                for row in menu.inline_keyboard
                for button in row
                if button.text.startswith("📝 Личные шаблоны")
            )
            personal_callback = SimpleNamespace(
                data=personal_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_notification_setting_edit")(
                personal_callback
            )
            task_keyboard = personal_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            task_button = next(
                button
                for row in task_keyboard.inline_keyboard
                for button in row
                if button.text == "ℹ️ Причастие · инфо"
            )
            self.assertTrue(
                any(
                    button.text.endswith("Микшер")
                    for row in task_keyboard.inline_keyboard
                    for button in row
                )
            )
            task_callback = SimpleNamespace(
                data=task_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_notify_task_choice")(
                task_callback
            )
            prompt = task_callback.message.answer.await_args.args[0]
            self.assertIn("ℹ️ Причастие · инфо", prompt)
            for placeholder in (
                "{name}",
                "{task}",
                "{service_name}",
                "{sunday_date}",
                "{telegram_id}",
                "{username}",
            ):
                self.assertIn(placeholder, prompt)
                self.assertNotIn("{" + placeholder + "}", prompt)

        asyncio.run(run())

    def test_template_edit_prompts_show_saved_text_and_upcoming_preview(self):
        async def run():
            self.registry.set_notification_settings(
                67890,
                "sound",
                reminder_days=1,
                reminder_time="18:00",
                group_template="Сохранённая группа: {service_name}",
                personal_templates={"Микшер": "Сохранённый текст {name}"},
            )
            with closing(sqlite3.connect(self.registry.path)) as connection:
                connection.execute(
                    "UPDATE services SET group_template = ? WHERE id = ?",
                    ("Напоминание: {tg_responsible}", "sound"),
                )
                connection.commit()
            self.context.prepare_service_test_previews = AsyncMock(
                return_value={
                    "sunday_date": date(2026, 10, 11),
                    "group_text": "Пример группы на воскресенье",
                    "example_group_template": (
                        "Напоминание о служении «{service_name}» в воскресенье "
                        "{sunday_date}:\n{task_assignments}"
                    ),
                    "example_group_text": "Пример с задачами и ответственными",
                    "personal_messages": [
                        {
                            "task": "Микшер",
                            "name": "Алексей",
                            "telegram_id": "12345",
                            "text": "Пример личного сообщения",
                        }
                    ],
                    "issues": [],
                }
            )
            command = self.message(67890, "/service_notify")
            await self.admin_router.message.handlers[6].callback(command)
            service_callback = SimpleNamespace(
                data="service:service_notify:sound",
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_service_selected")(
                service_callback
            )
            menu = service_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            group_button = next(
                button
                for row in menu.inline_keyboard
                for button in row
                if button.text == "📝 Шаблон по умолчанию"
            )
            group_callback = SimpleNamespace(
                data=group_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_group_reminder_action")(
                group_callback
            )
            group_prompt = group_callback.message.answer.await_args.args[0]
            self.assertIn("Напоминание: {tg_responsible}", group_prompt)
            edit_default_button = next(
                button
                for row in group_callback.message.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row
                if button.callback_data.startswith(
                    "group_reminder:edit_default:"
                )
            )
            edit_default_callback = SimpleNamespace(
                data=edit_default_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_group_reminder_action")(
                edit_default_callback
            )
            self.assertIn(
                "{service_name}",
                edit_default_callback.message.answer.await_args.args[0],
            )
            staged_input = next(
                item.callback
                for item in self.admin_router.message.handlers
                if item.callback.__name__ == "on_service_parameter"
            )
            invalid_group = self.message(67890, "{unknown}")
            await staged_input(invalid_group)
            self.assertIn(
                "не поддерживается",
                invalid_group.answer.await_args.args[0],
            )
            self.assertEqual(
                self.registry.get_service("sound")["group_template"],
                "Напоминание: {tg_responsible}",
            )
            valid_group = self.message(67890, "Новое {service_name}")
            await staged_input(valid_group)
            self.assertEqual(
                self.registry.get_service("sound")["group_template"],
                "Напоминание: {tg_responsible}",
            )
            root_back = next(
                button
                for row in valid_group.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row
                if button.callback_data.startswith(
                    "group_reminder:root_back:"
                )
            )
            root_callback = SimpleNamespace(
                data=root_back.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_group_reminder_action")(
                root_callback
            )
            menu = root_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]

            personal_button = next(
                button
                for row in menu.inline_keyboard
                for button in row
                if button.text.startswith("📝 Личные шаблоны")
            )
            personal_callback = SimpleNamespace(
                data=personal_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_notification_setting_edit")(
                personal_callback
            )
            task_button = next(
                button
                for row in personal_callback.message.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row
                if button.text.endswith("Микшер")
            )
            task_callback = SimpleNamespace(
                data=task_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_notify_task_choice")(
                task_callback
            )
            personal_prompt = task_callback.message.answer.await_args.args[0]
            self.assertIn("Сохранённый текст {name}", personal_prompt)
            self.assertIn("Пример личного сообщения", personal_prompt)
            self.assertEqual(
                self.context.prepare_service_test_previews.await_args.kwargs[
                    "personal_templates"
                ],
                {"Микшер": "Сохранённый текст {name}"},
            )
            invalid_personal = self.message(67890, "{name.attribute}")
            await staged_input(invalid_personal)
            self.assertIn(
                "не поддерживается",
                invalid_personal.answer.await_args.args[0],
            )
            valid_personal = self.message(67890, "Новое личное {name}")
            await staged_input(valid_personal)
            finish_button = next(
                button
                for row in valid_personal.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row
                if button.callback_data.startswith("notify_finish:")
            )
            finish_callback = SimpleNamespace(
                data=finish_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_notification_setting_finish")(
                finish_callback
            )
            self.assertEqual(
                self.registry.get_service("sound")["group_template"],
                "Новое {service_name}",
            )
            self.assertEqual(
                self.registry.get_service("sound")["personal_templates"][
                    "Микшер"
                ],
                "Новое личное {name}",
            )

        asyncio.run(run())

    def test_service_contact_uses_task_choice_and_native_user_picker(self):
        async def run():
            self.registry.set_responsible_contact(
                67890, "sound", "Микшер", "Имя из расписания", 11111
            )
            command = self.message(67890, "/service_contact sound")
            await self.admin_router.message.handlers[5].callback(command)
            task_button = command.answer.await_args.kwargs[
                "reply_markup"
            ].inline_keyboard[0][0]
            self.assertEqual(task_button.text, "🎚️ Микшер")

            task_callback = SimpleNamespace(
                data=task_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            task_handler = next(
                handler.callback
                for handler in self.admin_router.callback_query.handlers
                if handler.callback.__name__ == "on_task_choice"
            )
            await task_handler(task_callback)
            name_markup = task_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            name_button = name_markup.inline_keyboard[0][0]
            self.assertIn(
                "Привязано 1 из 1",
                task_callback.message.answer.await_args.args[0],
            )
            self.assertTrue(name_button.text.startswith("✅ "))
            self.assertNotIn(
                "11111",
                name_button.text,
            )
            name_callback = SimpleNamespace(
                data=name_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            name_handler = next(
                handler.callback
                for handler in self.admin_router.callback_query.handlers
                if handler.callback.__name__ == "on_contact_person_choice"
            )
            await name_handler(name_callback)
            user_button = name_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ].keyboard[0][0]
            self.assertIsNotNone(user_button.request_users)

            share_message = self.message(67890, None)
            share_message.users_shared = SimpleNamespace(
                request_id=user_button.request_users.request_id,
                users=[
                    SimpleNamespace(
                        user_id=22222,
                        first_name="Новый",
                        last_name="Пользователь",
                        username="new_user",
                    )
                ],
            )
            user_handler = next(
                handler.callback
                for handler in self.admin_router.message.handlers
                if handler.callback.__name__ == "on_users_shared"
            )
            await user_handler(share_message)
            confirmation = share_message.answer.await_args.kwargs[
                "reply_markup"
            ].inline_keyboard[0][0]
            self.assertIn("Микшер", share_message.answer.await_args.args[0])
            self.assertIn(
                "Имя из расписания",
                share_message.answer.await_args.args[0],
            )

            confirm_callback = SimpleNamespace(
                data=confirmation.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            confirm_handler = next(
                handler.callback
                for handler in self.admin_router.callback_query.handlers
                if handler.callback.__name__ == "on_shared_admin_confirmation"
            )
            await confirm_handler(confirm_callback)
            self.assertEqual(
                self.registry.get_responsible_contact(
                    "sound", "Микшер", "Имя из расписания"
                ),
                {"telegram_id": 22222, "username": "new_user"},
            )

        asyncio.run(run())

    def test_service_sync_refreshes_schedule_and_shows_existing_links(self):
        async def run():
            self.context.load_service_task_assignments = AsyncMock(
                return_value={
                    "Микшер": {
                        "names": ["Имя из расписания"],
                        "is_informational": False,
                    },
                    "Праздник": {
                        "names": ["Пометка"],
                        "is_informational": True,
                    },
                }
            )
            self.registry.set_responsible_contact(
                67890, "sound", "Микшер", "Имя из расписания", 11111
            )
            command = self.message(67890, "/service_sync")
            sync_handler = next(
                item.callback
                for item in self.admin_router.message.handlers
                if item.callback.__name__ == "on_service_sync"
            )
            await sync_handler(command)

            service_button = await self.dialog_button(
                command,
                lambda button: button.callback_data
                == "service:service_sync:sound",
                67890,
            )
            callback = SimpleNamespace(
                data=service_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_service_selected")(callback)

            self.context.load_service_task_assignments.assert_awaited_once_with(
                "sound"
            )
            output = "\n".join(
                call.args[0]
                for call in callback.message.answer.await_args_list
                if call.args
            )
            task_button = next(
                button
                for call in callback.message.answer.await_args_list
                for row in (call.kwargs.get("reply_markup").inline_keyboard
                    if call.kwargs.get("reply_markup")
                    else [])
                for button in row
                if button.text.endswith("Микшер")
            )
            task_callback = SimpleNamespace(
                data=task_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_task_choice")(task_callback)
            contact_screen = task_callback.message.answer.await_args
            self.assertIn("Привязано 1 из 1", contact_screen.args[0])
            self.assertNotIn("Праздник", output)
            self.assertNotIn("Праздник", contact_screen.args[0])
            self.assertTrue(
                contact_screen.kwargs["reply_markup"]
                .inline_keyboard[0][0].text.startswith("✅ ")
            )
            self.assertIn(
                "Имя из расписания",
                contact_screen.kwargs["reply_markup"]
                .inline_keyboard[0][0].text,
            )
            self.assertNotIn("ID 11111", output)
            self.assertNotIn("11111", output)
            self.assertNotIn("11111", contact_screen.args[0])

        asyncio.run(run())

    def test_service_sync_does_not_claim_success_when_schedule_load_fails(self):
        async def run():
            self.context.load_service_task_assignments = AsyncMock(
                side_effect=RuntimeError("schedule unavailable")
            )
            command = self.message(67890, "/service_sync")
            await next(
                item.callback
                for item in self.admin_router.message.handlers
                if item.callback.__name__ == "on_service_sync"
            )(command)
            service_callback = SimpleNamespace(
                data="service:service_sync:sound",
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )

            await self.callback_handler("on_service_selected")(
                service_callback
            )

            replies = [
                call.args[0]
                for call in service_callback.message.answer.await_args_list
            ]
            self.assertTrue(any("загружаю" in text for text in replies))
            self.assertTrue(any("Не удалось прочитать файл графика" in text for text in replies))
            self.assertFalse(any("График загружен" in text for text in replies))

        asyncio.run(run())

    def test_cancel_during_schedule_load_does_not_crash_or_show_task_list(self):
        async def run():
            async def cancel_while_loading(_service_id):
                cancel_message = self.message(67890, "/cancel")
                await next(
                    item.callback
                    for item in self.admin_router.message.handlers
                    if item.callback.__name__ == "on_cancel_service_command"
                )(cancel_message)
                return {"Микшер": ["Алексей"]}

            self.context.load_service_task_assignments = AsyncMock(
                side_effect=cancel_while_loading
            )
            command = self.message(67890, "/service_contact sound")
            await self.admin_router.message.handlers[5].callback(command)
            service_callback = SimpleNamespace(
                data="service:service_contact:sound",
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )

            await self.callback_handler("on_service_selected")(
                service_callback
            )

            output = "\n".join(
                call.args[0]
                for call in service_callback.message.answer.await_args_list
                if call.args
            )
            self.assertIn("отменён во время загрузки", output)
            self.assertFalse(
                any(
                    call.kwargs.get("reply_markup")
                    for call in service_callback.message.answer.await_args_list
                )
            )

        asyncio.run(run())

    def test_service_sync_requires_super_admin_before_showing_services(self):
        async def run():
            command = self.message(12345, "/service_sync")
            handler = next(
                item.callback
                for item in self.admin_router.message.handlers
                if item.callback.__name__ == "on_service_sync"
            )

            await handler(command)

            self.assertIn(
                "только супер-администратору",
                command.answer.await_args.args[0].lower(),
            )
            self.assertIsNone(
                command.answer.await_args.kwargs.get("reply_markup")
            )

        asyncio.run(run())

    def test_service_contact_links_same_first_name_users_to_schedule_names(self):
        async def run():
            self.context.load_service_task_assignments = AsyncMock(
                return_value={
                    "Микшер": ["Алексей Один", "Алексей Два"],
                }
            )
            task_handler = self.callback_handler("on_task_choice")
            person_handler = self.callback_handler("on_contact_person_choice")
            user_handler = next(
                handler.callback
                for handler in self.admin_router.message.handlers
                if handler.callback.__name__ == "on_users_shared"
            )
            confirm_handler = self.callback_handler(
                "on_shared_admin_confirmation"
            )

            for person_name, user_id, last_name in (
                ("Алексей Один", 30101, "One"),
                ("Алексей Два", 30202, "Two"),
            ):
                command = self.message(67890, "/service_contact sound")
                await self.admin_router.message.handlers[5].callback(command)
                task_button = command.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard[0][0]
                task_callback = SimpleNamespace(
                    data=task_button.callback_data,
                    from_user=SimpleNamespace(id=67890),
                    message=SimpleNamespace(
                        chat=SimpleNamespace(type="private"),
                        answer=AsyncMock(),
                    ),
                    answer=AsyncMock(),
                )
                await task_handler(task_callback)
                person_button = next(
                    button
                    for row in task_callback.message.answer.await_args.kwargs[
                        "reply_markup"
                    ].inline_keyboard
                    for button in row
                    if person_name in button.text
                )
                person_callback = SimpleNamespace(
                    data=person_button.callback_data,
                    from_user=SimpleNamespace(id=67890),
                    message=SimpleNamespace(
                        chat=SimpleNamespace(type="private"),
                        answer=AsyncMock(),
                    ),
                    answer=AsyncMock(),
                )
                await person_handler(person_callback)
                request_id = (
                    person_callback.message.answer.await_args.kwargs[
                        "reply_markup"
                    ].keyboard[0][0].request_users.request_id
                )
                shared = self.message(67890, None)
                shared.users_shared = SimpleNamespace(
                    request_id=request_id,
                    users=[
                        SimpleNamespace(
                            user_id=user_id,
                            first_name="Алексей",
                            last_name=last_name,
                            username=None,
                        )
                    ],
                )
                await user_handler(shared)
                confirm_button = shared.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard[0][0]
                confirmation = SimpleNamespace(
                    data=confirm_button.callback_data,
                    from_user=SimpleNamespace(id=67890),
                    message=SimpleNamespace(
                        chat=SimpleNamespace(type="private"),
                        answer=AsyncMock(),
                    ),
                    answer=AsyncMock(),
                )
                await confirm_handler(confirmation)

            contacts = [
                item
                for item in self.registry.list_responsible_contacts(
                    67890, "sound"
                )
                if item["task"] == "Микшер"
            ]
            self.assertEqual(
                {item["name"]: item["telegram_id"] for item in contacts},
                {"Алексей Один": 30101, "Алексей Два": 30202},
            )

        asyncio.run(run())

    def test_large_schedule_task_lists_are_paginated(self):
        async def run():
            assignments = {
                f"Задача {index:02}": [f"Ответственный {index:02}"]
                for index in range(25)
            }
            self.context.load_service_task_assignments = AsyncMock(
                return_value=assignments
            )
            contact_command = self.message(
                67890, "/service_contact sound"
            )
            await self.admin_router.message.handlers[5].callback(
                contact_command
            )
            contact_keyboard = contact_command.answer.await_args_list[-1].kwargs[
                "reply_markup"
            ]
            self.assertLessEqual(
                sum(len(row) for row in contact_keyboard.inline_keyboard), 6
            )
            next_page = next(
                button
                for row in contact_keyboard.inline_keyboard
                for button in row
                if button.callback_data.startswith("task_page:")
            )
            page_callback = SimpleNamespace(
                data=next_page.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_task_page")(page_callback)
            self.assertLessEqual(
                sum(
                    len(row)
                    for row in page_callback.message.answer.await_args.kwargs[
                        "reply_markup"
                    ].inline_keyboard
                ),
                6,
            )

            notify_command = self.message(67890, "/service_notify")
            await self.admin_router.message.handlers[6].callback(notify_command)
            service_callback = SimpleNamespace(
                data="service:service_notify:sound",
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_service_selected")(service_callback)
            menu = service_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            personal_button = next(
                button
                for row in menu.inline_keyboard
                for button in row
                if button.callback_data.startswith(
                    "notify_edit:personal:sound:"
                )
            )
            personal_callback = SimpleNamespace(
                data=personal_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler(
                "on_notification_setting_edit"
            )(personal_callback)
            self.assertEqual(
                self.context.load_service_task_assignments.await_args.args,
                ("sound",),
            )
            notify_keyboard = personal_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            self.assertLessEqual(
                sum(len(row) for row in notify_keyboard.inline_keyboard),
                6,
            )

        asyncio.run(run())

    def test_old_task_page_button_is_rejected_after_task_selection(self):
        async def run():
            assignments = {
                f"Задача {index:02}": [f"Ответственный {index:02}"]
                for index in range(11)
            }
            self.context.load_service_task_assignments = AsyncMock(
                return_value=assignments
            )
            command = self.message(67890, "/service_contact sound")
            await self.admin_router.message.handlers[5].callback(command)
            keyboard = command.answer.await_args_list[-1].kwargs["reply_markup"]
            task_button = next(
                button
                for row in keyboard.inline_keyboard
                for button in row
                if button.text.endswith("Задача 00")
            )
            old_page_button = next(
                button
                for row in keyboard.inline_keyboard
                for button in row
                if button.callback_data.startswith("task_page:")
            )

            task_callback = SimpleNamespace(
                data=task_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_task_choice")(task_callback)

            page_callback = SimpleNamespace(
                data=old_page_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_task_page")(page_callback)

            page_callback.message.answer.assert_not_awaited()
            self.assertIn("устарел", page_callback.answer.await_args.args[0])

        asyncio.run(run())

    def test_notification_setup_recovers_if_first_schedule_load_fails(self):
        async def run():
            self.context.load_service_task_assignments = AsyncMock(
                side_effect=[RuntimeError("temporary download failure"), {}]
            )
            handler = self.admin_router.message.handlers[6].callback
            command = self.message(67890, "/service_notify")
            await handler(command)
            service_callback = SimpleNamespace(
                data="service:service_notify:sound",
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_service_selected")(service_callback)
            menu = service_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            personal_button = next(
                button
                for row in menu.inline_keyboard
                for button in row
                if button.callback_data.startswith(
                    "notify_edit:personal:sound:"
                )
            )
            def make_personal_callback():
                return SimpleNamespace(
                    data=personal_button.callback_data,
                    from_user=SimpleNamespace(id=67890),
                    message=SimpleNamespace(
                        chat=SimpleNamespace(type="private"),
                        answer=AsyncMock(),
                    ),
                    answer=AsyncMock(),
                )

            failed_load = make_personal_callback()
            await self.callback_handler(
                "on_notification_setting_edit"
            )(failed_load)
            self.assertIn(
                "Не удалось прочитать файл графика",
                failed_load.message.answer.await_args.args[0],
            )

            retry = make_personal_callback()
            await self.callback_handler(
                "on_notification_setting_edit"
            )(retry)
            return_button = next(
                button
                for row in retry.message.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row
                if button.text == "⬅️ К настройкам"
            )
            return_callback = SimpleNamespace(
                data=return_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_notify_task_choice")(
                return_callback
            )
            finish_button = next(
                button
                for row in return_callback.message.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row
                if button.text == "✅ Завершить и сохранить"
            )
            finish_callback = SimpleNamespace(
                data=finish_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler(
                "on_notification_setting_finish"
            )(finish_callback)

            service = self.registry.get_service("sound")
            self.assertIsNone(service["group_template"])
            self.assertEqual(
                self.context.load_service_task_assignments.await_count, 2
            )

        asyncio.run(run())

    def test_service_chat_offers_disable_button_instead_of_command_syntax(self):
        async def run():
            self.registry.set_group_chat(
                67890, "sound", -100222, enabled=True
            )
            command = self.message(67890, "/service_chat")
            await self.admin_router.message.handlers[4].callback(command)
            service_button = await self.dialog_button(
                command,
                lambda button: button.callback_data
                == "service:service_chat:sound",
                67890,
            )
            service_callback = SimpleNamespace(
                data=service_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            service_handler = next(
                handler.callback
                for handler in self.admin_router.callback_query.handlers
                if handler.callback.__name__ == "on_service_selected"
            )
            await service_handler(service_callback)
            action_keyboard = service_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            disable_button = next(
                button
                for row in action_keyboard.inline_keyboard
                for button in row
                if button.text == "🔴 Отключить служение"
            )
            disable_callback = SimpleNamespace(
                data=disable_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await service_handler(disable_callback)
            sound = self.registry.get_service("sound")
            self.assertFalse(sound["enabled"])
            self.assertEqual(sound["group_chat_id"], -100222)

        asyncio.run(run())

    def test_test_notify_selects_service_before_group_delivery(self):
        async def run():
            self.registry.set_group_chat(
                67890, "sound", -100333, enabled=True
            )
            context = SimpleNamespace(
                interaction_logger=MagicMock(),
                debug_logger=MagicMock(),
                admin_id=67890,
                group_chat_id=-100,
                service_registry=self.registry,
                bot=SimpleNamespace(send_message=AsyncMock()),
                prepare_service_test_previews=AsyncMock(),
            )
            dispatcher = SimpleNamespace(include_router=MagicMock())
            register(dispatcher, context)
            admin_router = dispatcher.include_router.call_args_list[0].args[0]

            test_handler = router.message.handlers[-1].callback
            command = self.message(67890, "/test_notify")
            await test_handler(command)
            selection = command.answer.await_args.kwargs["reply_markup"]
            service_button = await self.dialog_button(
                command,
                lambda button: button.callback_data
                == "service:test_notify:sound",
                67890,
                admin_router,
            )

            callback = SimpleNamespace(
                data=service_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            service_handler = next(
                handler.callback
                for handler in admin_router.callback_query.handlers
                if handler.callback.__name__ == "on_service_selected"
            )
            await service_handler(callback)

            context.bot.send_message.assert_not_awaited()
            action_keyboard = callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            group_button = next(
                button
                for row in action_keyboard.inline_keyboard
                for button in row
                if button.callback_data == "test_action:group:sound"
            )
            action_handler = next(
                handler.callback
                for handler in reversed(router.callback_query.handlers)
                if handler.callback.__name__ == "on_test_notify_action"
            )
            action_callback = SimpleNamespace(
                data=group_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await action_handler(action_callback)

            self.assertEqual(
                context.bot.send_message.await_args.args[0], -100333
            )
            self.assertIn(
                "ТЕСТ службы «Звук»",
                context.bot.send_message.await_args.args[1],
            )
            context.prepare_service_test_previews.assert_not_awaited()

        asyncio.run(run())

    def test_test_preview_formatting_error_is_reported_and_callback_answered(self):
        async def run():
            sound = self.registry.get_service("sound")
            context = SimpleNamespace(
                interaction_logger=MagicMock(),
                debug_logger=MagicMock(),
                admin_id=67890,
                service_registry=SimpleNamespace(
                    list_services=MagicMock(return_value=[sound]),
                    get_service=MagicMock(return_value=sound),
                ),
                bot=SimpleNamespace(send_message=AsyncMock()),
                prepare_service_test_previews=AsyncMock(
                    side_effect=ValueError(
                        "Некорректный групповой шаблон служения «Звук»"
                    )
                ),
            )
            dispatcher = SimpleNamespace(include_router=MagicMock())
            register(dispatcher, context)
            command = self.message(67890, "/test_notify")
            await router.message.handlers[-1].callback(command)
            service_button = command.answer.await_args.kwargs[
                "reply_markup"
            ].inline_keyboard[0][0]
            service_callback = SimpleNamespace(
                data=service_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await next(
                item.callback
                for item in dispatcher.include_router.call_args_list[0]
                .args[0].callback_query.handlers
                if item.callback.__name__ == "on_service_selected"
            )(service_callback)
            preview_callback = SimpleNamespace(
                data="test_action:preview:sound",
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await next(
                item.callback
                for item in reversed(router.callback_query.handlers)
                if item.callback.__name__ == "on_test_notify_action"
            )(preview_callback)

            self.assertIn(
                "ValueError",
                preview_callback.message.answer.await_args.args[0],
            )
            preview_callback.answer.assert_awaited_once()
            context.bot.send_message.assert_not_awaited()

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

    def test_service_settings_screen_stays_compact_with_many_contacts(self):
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

            self.assertEqual(message.answer.await_count, 1)
            chunks = [call.args[0] for call in message.answer.await_args_list]
            self.assertTrue(
                all(len(chunk.encode("utf-16-le")) // 2 <= 500 for chunk in chunks)
            )
            self.assertIn("Настройки", "".join(chunks))
            self.assertNotIn("Ответственный 59", "".join(chunks))

        asyncio.run(run())

    def test_settings_group_contacts_by_task(self):
        async def run():
            self.registry.set_responsible_contact(
                67890, "sound", "Ведущий", "Аркадий", 718343838, "arkady"
            )
            self.registry.set_responsible_contact(
                67890, "sound", "Ведущий", "Евгений", 306139797, "evgen"
            )
            self.registry.set_responsible_contact(
                67890, "sound", "Проповедник", "Денис", 801625873, "denis"
            )
            self.context.load_service_task_assignments = AsyncMock(
                return_value={
                    "Ведущий": ["Аркадий", "Евгений"],
                    "Проповедник": ["Денис"],
                }
            )
            message = self.message(67890, "/service_contact sound")

            await self.admin_router.message.handlers[5].callback(message)
            task_button = next(
                button
                for row in message.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row
                if button.callback_data.startswith("task:service_contact:")
                and button.text.endswith("Ведущий")
            )
            callback = SimpleNamespace(
                data=task_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=self.message(67890, "task selection"),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_task_choice")(callback)
            text = callback.message.answer.await_args.args[0]
            self.assertIn("Ведущий", text)
            self.assertIn("Привязано 2 из 2", text)
            self.assertTrue(
                any(
                    button.text.endswith("Аркадий · @arkady")
                    for row in callback.message.answer.await_args.kwargs[
                        "reply_markup"
                    ].inline_keyboard
                    for button in row
                )
            )
            for user_id in ("718343838", "306139797"):
                self.assertNotIn(user_id, text)

        asyncio.run(run())

    def test_service_listing_uses_only_readable_russian_labels(self):
        async def run():
            message = self.message(67890, "/services")
            await self.admin_router.message.handlers[0].callback(message)

            assembly = await self.dialog_button(
                message,
                lambda button: button.callback_data
                == "service:service_settings:assembly",
                67890,
            )
            broadcast = await self.dialog_button(
                message,
                lambda button: button.callback_data
                == "service:service_settings:broadcast",
                67890,
            )
            self.assertTrue(assembly.text.endswith("СОБРАНИЕ"))
            self.assertTrue(broadcast.text.endswith("ТРАНСЛЯЦИЯ"))

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
                [button.callback_data for button in buttons],
                ["service:service_settings:sound"],
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
            await self.callback_handler("on_service_selected")(callback)
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
            self.assertEqual(button.text, "🟢 СОБРАНИЕ")
            callback = SimpleNamespace(
                data=button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_service_selected")(callback)
            self.assertIn("выберите пользователя", callback.message.answer.await_args.args[0])
            keyboard = callback.message.answer.await_args.kwargs["reply_markup"]
            request = keyboard.keyboard[0][0].request_users
            self.assertEqual(request.max_quantity, 1)
            self.assertFalse(request.user_is_bot)

            self.assertEqual(self.registry.list_service_admins("assembly"), [])

        asyncio.run(run())

    def test_admin_can_add_self_by_entering_id_and_username(self):
        async def run():
            command = self.message(67890, "/service_admin_add")
            await self.admin_router.message.handlers[2].callback(command)
            service_button = await self.dialog_button(
                command,
                lambda button: button.callback_data
                == "service:service_admin_add:assembly",
                67890,
            )
            service_callback = SimpleNamespace(
                data=service_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_service_selected")(
                service_callback
            )
            staged_input = next(
                item.callback
                for item in self.admin_router.message.handlers
                if item.callback.__name__ == "on_service_parameter"
            )
            manual_input = self.message(67890, "67890 @self_admin")
            await staged_input(manual_input)
            confirmation = manual_input.answer.await_args.kwargs[
                "reply_markup"
            ].inline_keyboard[0][0]
            callback = SimpleNamespace(
                data=confirmation.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )

            await self.callback_handler("on_shared_admin_confirmation")(
                callback
            )

            self.assertEqual(
                self.registry.list_service_admin_details("assembly"),
                [{"telegram_id": 67890, "username": "self_admin"}],
            )

        asyncio.run(run())

    def test_direct_admin_add_requires_explicit_confirmation(self):
        async def run():
            message = self.message(
                67890, "/service_admin_add sound 12345 @target_user"
            )

            await self.admin_router.message.handlers[2].callback(message)

            self.assertEqual(self.registry.list_service_admins("sound"), [])
            self.assertIn(
                "target_user",
                message.answer.await_args.args[0],
            )
            button = message.answer.await_args.kwargs[
                "reply_markup"
            ].inline_keyboard[0][0]
            callback = SimpleNamespace(
                data=button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )

            await self.callback_handler("on_shared_admin_confirmation")(
                callback
            )

            self.assertEqual(
                self.registry.list_service_admin_details("sound"),
                [{"telegram_id": 12345, "username": "target_user"}],
            )

        asyncio.run(run())

    def test_services_listing_separates_services_and_shows_admin_usernames(self):
        async def run():
            self.registry.add_service_admin(67890, "sound", 12345, "sound_admin")
            self.registry.add_service_admin(67890, "slides", 12345, "sound_admin")
            message = self.message(67890, "/services")

            await self.admin_router.message.handlers[0].callback(message)

            result = "\n".join(
                call.args[0] for call in message.answer.await_args_list
            )
            self.assertNotIn("12345", result)
            sound_button = await self.dialog_button(
                message,
                lambda button: button.callback_data
                == "service:service_settings:sound",
                67890,
            )
            slides_button = await self.dialog_button(
                message,
                lambda button: button.callback_data
                == "service:service_settings:slides",
                67890,
            )
            self.assertTrue(sound_button.text.endswith("ЗВУК"))
            self.assertTrue(slides_button.text.endswith("СЛАЙДЫ"))
            self.assertTrue(self.registry.is_service_admin(12345, "sound"))
            self.assertTrue(self.registry.is_service_admin(12345, "slides"))

        asyncio.run(run())

    def test_contact_manual_input_cancel_and_confirm_return_to_contact_list(self):
        async def run():
            self.context.load_service_task_assignments = AsyncMock(
                return_value={"Микшер": ["Алексей", "Иван"]}
            )
            command = self.message(67890, "/service_contact sound")
            await self.admin_router.message.handlers[5].callback(command)
            task_button = command.answer.await_args.kwargs[
                "reply_markup"
            ].inline_keyboard[0][0]
            task_callback = SimpleNamespace(
                data=task_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_task_choice")(task_callback)
            person_button = task_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ].inline_keyboard[0][0]
            person_callback = SimpleNamespace(
                data=person_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_contact_person_choice")(
                person_callback
            )
            staged_input = next(
                item.callback
                for item in self.admin_router.message.handlers
                if item.callback.__name__ == "on_service_parameter"
            )

            async def enter_user_and_finish(action):
                manual_message = self.message(
                    67890, "33333 @manual_user"
                )
                await staged_input(manual_message)
                button = manual_message.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard[0][1 if action == "cancel" else 0]
                callback = SimpleNamespace(
                    data=button.callback_data,
                    from_user=SimpleNamespace(id=67890),
                    message=SimpleNamespace(
                        chat=SimpleNamespace(type="private"),
                        answer=AsyncMock(),
                    ),
                    answer=AsyncMock(),
                )
                await self.callback_handler(
                    "on_shared_admin_confirmation"
                )(callback)
                return callback, manual_message

            cancelled, _ = await enter_user_and_finish("cancel")
            self.assertIn(
                "Назначение отменено",
                cancelled.message.answer.await_args_list[0].args[0],
            )
            list_markup = cancelled.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            self.assertTrue(
                any(
                    button.text == "⬅️ К задачам"
                    for row in list_markup.inline_keyboard
                    for button in row
                )
            )

            person_button = list_markup.inline_keyboard[1][0]
            person_callback = SimpleNamespace(
                data=person_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_contact_person_choice")(
                person_callback
            )
            confirmed, _ = await enter_user_and_finish("confirm")
            self.assertEqual(
                self.registry.get_responsible_contact(
                    "sound", "Микшер", "Иван"
                ),
                {"telegram_id": 33333, "username": "manual_user"},
            )
            self.assertIn(
                "Ответственный «Иван»",
                confirmed.message.answer.await_args.args[0],
            )
            list_markup = confirmed.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            self.assertEqual(
                [
                    button.text
                    for row in list_markup.inline_keyboard
                    for button in row
                ],
                [
                    "⚪ Алексей · не привязан",
                    "✅ Иван · @manual_user",
                    "⬅️ К задачам",
                ],
            )
            next_person = list_markup.inline_keyboard[0][0]
            next_person_callback = SimpleNamespace(
                data=next_person.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_contact_person_choice")(
                next_person_callback
            )
            self.assertIn(
                "для имени «Алексей»",
                next_person_callback.message.answer.await_args.args[0],
            )

        asyncio.run(run())

    def test_contact_navigation_returns_to_tasks_then_service_picker(self):
        async def run():
            self.context.load_service_task_assignments = AsyncMock(
                return_value={"Микшер": ["Алексей"]}
            )
            command = self.message(67890, "/service_contact sound")
            await self.admin_router.message.handlers[5].callback(command)
            task_button = command.answer.await_args.kwargs[
                "reply_markup"
            ].inline_keyboard[0][0]
            task_callback = SimpleNamespace(
                data=task_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_task_choice")(task_callback)

            return_to_tasks = task_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ].inline_keyboard[-1][0]
            tasks_callback = SimpleNamespace(
                data=return_to_tasks.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_contact_navigation")(
                tasks_callback
            )
            task_markup = tasks_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            service_picker_button = task_markup.inline_keyboard[-1][0]
            services_callback = SimpleNamespace(
                data=service_picker_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    from_user=SimpleNamespace(id=99999),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_contact_navigation")(
                services_callback
            )
            self.assertIn(
                "Выберите служение",
                services_callback.message.answer.await_args.args[0],
            )
            service_picker = services_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            self.assertTrue(
                any(
                    button.callback_data.startswith(
                        "service:service_contact:"
                    )
                    for row in service_picker.inline_keyboard
                    for button in row
                )
            )

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

            await self.callback_handler("on_service_selected")(callback)

            self.assertEqual(
                callback.message.answer.await_args.args[0],
                "<b>Не удалось прочитать реестр служений.</b>",
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
            await self.callback_handler("on_service_selected")(service_callback)

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
            await self.callback_handler(
                "on_shared_admin_confirmation"
            )(admin_callback)
            self.assertEqual(
                self.registry.list_service_admins("assembly"), [24680]
            )
            self.assertNotIn(
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
            self.assertIn(
                "-1002233",
                shared_chat.answer.await_args_list[0].args[0],
            )

            disable = self.message(67890, "/service_chat sound off")
            await self.admin_router.message.handlers[4].callback(disable)
            sound = self.registry.get_service("sound")
            self.assertEqual(sound["group_chat_id"], -1002233)
            self.assertFalse(sound["enabled"])

        asyncio.run(run())

    def test_settings_message_has_next_action_button(self):
        async def run():
            self.registry.add_service_admin(67890, "sound", 24680)
            self.registry.set_notification_settings(
                24680,
                "sound",
                reminder_days=1,
                reminder_time="16:00",
                group_template="Сохранённый групповой текст",
                personal_templates={"Микшер": "Сохранённый личный текст"},
            )
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
                "выберите параметр для изменения",
                selection_message.answer.await_args.args[0].lower(),
            )
            self.assertIn(
                "📡 Настройки",
                selection_message.answer.await_args.args[0],
            )
            self.assertIn(
                "⚙️ ЗВУК",
                selection_message.answer.await_args.args[0],
            )
            settings_response = selection_message.answer.await_args.args[0]
            self.assertIn("Сохранённый групповой текст", settings_response)
            self.assertIn("Сохранённый личный текст", settings_response)
            self.assertFalse(settings_response.startswith("✅"))

            callback = SimpleNamespace(
                data=edit_button.callback_data,
                from_user=SimpleNamespace(id=24680),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_service_selected")(callback)
            menu = callback.message.answer.await_args.kwargs["reply_markup"]
            self.assertIn(
                "ℹ️ выберите, что изменить",
                callback.message.answer.await_args.args[0].lower(),
            )
            self.assertIn(
                next(
                    button.callback_data
                    for row in menu.inline_keyboard
                    for button in row
                    if button.text.startswith("⏰ Напоминания")
                ),
                [
                    button.callback_data
                    for row in menu.inline_keyboard
                    for button in row
                ],
            )

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
