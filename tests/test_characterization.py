"""Characterize reviewed helpers without importing or starting the payment bot.

Only selected function definitions are compiled from the real source. This
legacy-code seam deliberately excludes module-level database/network setup.
These unit tests do NOT cover payment transactions, startup, or integration.
"""

import ast
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock


SOURCE = Path(__file__).resolve().parents[1] / "bot.py"
HELPERS = {
    "_parse_admin_ids",
    "all_admin_ids",
    "tracked_reply",
    "tracked_send_message",
    "tracked_send_photo",
    "tracked_send_document",
    "tracked_edit",
    "safe_query_answer",
}


def load_helpers() -> dict[str, Any]:
    """Load real helper bodies, never the application's top-level statements."""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    nodes = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in HELPERS
    ]
    found = {node.name for node in nodes}
    if found != HELPERS or len(nodes) != len(HELPERS):
        raise AssertionError("Reviewed helper definitions are missing or duplicated")
    namespace: dict[str, Any] = {
        "ADMIN_IDS": {101},
        "PROMOTED_ADMIN_IDS": {202},
        "LATEST_MESSAGE_IDS": {},
        "back_button": Mock(return_value="back-menu"),
        "logger": Mock(),
    }
    module = ast.Module(body=nodes, type_ignores=[])
    exec(compile(module, str(SOURCE), "exec"), namespace)
    return namespace


class AdminHelpersTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ns = load_helpers()

    def test_empty_admin_configuration(self) -> None:
        for value in ("", " ", ",,,", " , , "):
            with self.subTest(value=value):
                self.assertEqual(self.ns["_parse_admin_ids"](value), set())

    def test_ids_are_trimmed_deduplicated_and_converted(self) -> None:
        self.assertEqual(self.ns["_parse_admin_ids"]("101, 202,101,"), {101, 202})

    def test_invalid_ids_raise_configuration_error(self) -> None:
        for value in ("abc", "101,nope", "1.5"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(RuntimeError, "ADMIN_IDS"):
                    self.ns["_parse_admin_ids"](value)

    def test_admin_union_includes_promoted_admins(self) -> None:
        self.assertEqual(self.ns["all_admin_ids"](), {101, 202})

    def test_admin_union_does_not_expose_mutable_configuration(self) -> None:
        result = self.ns["all_admin_ids"]()
        result.add(303)
        self.assertEqual(self.ns["ADMIN_IDS"], {101})
        self.assertEqual(self.ns["PROMOTED_ADMIN_IDS"], {202})


class MessageHelpersTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.ns = load_helpers()
        self.message = SimpleNamespace(message_id=42)
        self.bot = SimpleNamespace(
            send_message=AsyncMock(return_value=self.message),
            send_photo=AsyncMock(return_value=self.message),
            send_document=AsyncMock(return_value=self.message),
        )

    async def test_reply_adds_back_menu_and_tracks_message(self) -> None:
        reply = AsyncMock(return_value=self.message)
        update = SimpleNamespace(
            message=SimpleNamespace(reply_text=reply),
            effective_user=SimpleNamespace(id=101),
        )
        result = await self.ns["tracked_reply"](update, None, "hello")
        reply.assert_awaited_once_with("hello", reply_markup="back-menu")
        self.assertIs(result, self.message)
        self.assertEqual(self.ns["LATEST_MESSAGE_IDS"], {101: 42})

    async def test_reply_preserves_explicit_keyboard(self) -> None:
        reply = AsyncMock(return_value=self.message)
        update = SimpleNamespace(
            message=SimpleNamespace(reply_text=reply),
            effective_user=SimpleNamespace(id=101),
        )
        await self.ns["tracked_reply"](update, None, "hello", reply_markup="custom")
        reply.assert_awaited_once_with("hello", reply_markup="custom")

    async def test_reply_without_user_does_not_track(self) -> None:
        update = SimpleNamespace(
            message=SimpleNamespace(reply_text=AsyncMock(return_value=self.message)),
            effective_user=None,
        )
        await self.ns["tracked_reply"](update, None, "hello")
        self.assertEqual(self.ns["LATEST_MESSAGE_IDS"], {})

    async def test_text_send_tracks_even_without_keyboard(self) -> None:
        # Characterizes current behavior; not a claim that this is ideal UX.
        result = await self.ns["tracked_send_message"](self.bot, 101, "hello")
        self.bot.send_message.assert_awaited_once_with(101, "hello")
        self.assertIs(result, self.message)
        self.assertEqual(self.ns["LATEST_MESSAGE_IDS"], {101: 42})

    async def test_media_without_keyboard_preserves_previous_message(self) -> None:
        for kind in ("photo", "document"):
            for kwargs in ({}, {"reply_markup": None}):
                with self.subTest(kind=kind, kwargs=kwargs):
                    self.ns["LATEST_MESSAGE_IDS"] = {101: 7}
                    result = await self.ns[f"tracked_send_{kind}"](
                        self.bot, 101, "file-id", **kwargs
                    )
                    self.assertIs(result, self.message)
                    self.assertEqual(self.ns["LATEST_MESSAGE_IDS"], {101: 7})

    async def test_media_with_keyboard_updates_tracking(self) -> None:
        for kind in ("photo", "document"):
            with self.subTest(kind=kind):
                self.ns["LATEST_MESSAGE_IDS"] = {101: 7}
                await self.ns[f"tracked_send_{kind}"](
                    self.bot, 101, "file-id", reply_markup="custom"
                )
                getattr(self.bot, f"send_{kind}").assert_awaited_once_with(
                    101, "file-id", reply_markup="custom"
                )
                self.assertEqual(self.ns["LATEST_MESSAGE_IDS"], {101: 42})

    async def test_failed_sends_do_not_update_tracking(self) -> None:
        for kind in ("message", "photo", "document"):
            with self.subTest(kind=kind):
                self.ns["LATEST_MESSAGE_IDS"] = {101: 7}
                getattr(self.bot, f"send_{kind}").side_effect = RuntimeError("offline")
                with self.assertRaisesRegex(RuntimeError, "offline"):
                    await self.ns[f"tracked_send_{kind}"](
                        self.bot, 101, "payload", reply_markup="custom"
                    )
                self.assertEqual(self.ns["LATEST_MESSAGE_IDS"], {101: 7})

    async def test_edit_tracks_original_message(self) -> None:
        query = SimpleNamespace(
            from_user=SimpleNamespace(id=101),
            message=SimpleNamespace(message_id=7),
            edit_message_text=AsyncMock(return_value=self.message),
        )
        result = await self.ns["tracked_edit"](query, None, "edited")
        query.edit_message_text.assert_awaited_once_with("edited", reply_markup="back-menu")
        self.assertIs(result, self.message)
        self.assertEqual(self.ns["LATEST_MESSAGE_IDS"], {101: 7})

    async def test_edit_uses_returned_message_when_original_is_missing(self) -> None:
        query = SimpleNamespace(
            from_user=SimpleNamespace(id=101),
            message=None,
            edit_message_text=AsyncMock(return_value=self.message),
        )
        await self.ns["tracked_edit"](query, None, "edited", reply_markup="custom")
        query.edit_message_text.assert_awaited_once_with("edited", reply_markup="custom")
        self.assertEqual(self.ns["LATEST_MESSAGE_IDS"], {101: 42})

    async def test_empty_callback_answer(self) -> None:
        query = SimpleNamespace(answer=AsyncMock())
        await self.ns["safe_query_answer"](query)
        query.answer.assert_awaited_once_with()

    async def test_callback_answer_forwards_alert(self) -> None:
        query = SimpleNamespace(answer=AsyncMock())
        await self.ns["safe_query_answer"](query, "notice", show_alert=True)
        query.answer.assert_awaited_once_with("notice", show_alert=True)

    async def test_callback_failure_is_logged_without_propagating(self) -> None:
        query = SimpleNamespace(answer=AsyncMock(side_effect=RuntimeError("expired")))
        await self.ns["safe_query_answer"](query)
        self.ns["logger"].debug.assert_called_once_with(
            "Callback query answer failed", exc_info=True
        )


if __name__ == "__main__":
    unittest.main()
