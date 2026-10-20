"""Keyboard event dispatch for the dashboard browser stream."""

from __future__ import annotations

from typing import Any

from octop.api.routers.browser.stream import _handle_client_event


class FakeCDPClient:
    def __init__(self) -> None:
        self.commands: list[tuple[str, dict[str, Any]]] = []

    async def send(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.commands.append((method, params))
        return {}


class FakeSession:
    def __init__(self) -> None:
        self.client = FakeCDPClient()
        self._internal = type("Internal", (), {"client": self.client})()

    async def type(self, _text: str) -> None:
        raise AssertionError("special keys should not use type()")


async def test_keydown_dispatches_key_down_and_key_up() -> None:
    session = FakeSession()

    await _handle_client_event(
        session,
        {
            "type": "keydown",
            "key": "Backspace",
            "code": "Backspace",
            "modifiers": 2,
        },
    )

    assert session.client.commands == [
        (
            "Input.dispatchKeyEvent",
            {
                "type": "keyDown",
                "key": "Backspace",
                "code": "Backspace",
                "modifiers": 2,
                "windowsVirtualKeyCode": 8,
                "nativeVirtualKeyCode": 8,
            },
        ),
        (
            "Input.dispatchKeyEvent",
            {
                "type": "keyUp",
                "key": "Backspace",
                "code": "Backspace",
                "modifiers": 2,
                "windowsVirtualKeyCode": 8,
                "nativeVirtualKeyCode": 8,
            },
        ),
    ]


async def test_keydown_maps_enter_and_function_keys() -> None:
    session = FakeSession()

    await _handle_client_event(
        session,
        {"type": "keydown", "key": "Enter", "code": "Enter"},
    )
    await _handle_client_event(
        session,
        {"type": "keydown", "key": "F5", "code": "F5"},
    )

    enter_down = session.client.commands[0][1]
    f5_down = session.client.commands[2][1]
    assert enter_down["text"] == "\r"
    assert enter_down["windowsVirtualKeyCode"] == 13
    assert f5_down["windowsVirtualKeyCode"] == 116


async def test_ctrl_shortcut_maps_letter_virtual_key() -> None:
    session = FakeSession()

    await _handle_client_event(
        session,
        {
            "type": "keydown",
            "key": "a",
            "code": "KeyA",
            "modifiers": 2,
        },
    )

    key_down = session.client.commands[0][1]
    assert key_down["modifiers"] == 2
    assert key_down["windowsVirtualKeyCode"] == 65
