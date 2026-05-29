from __future__ import annotations

from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from aiogram.client.session import rust
from aiogram.client.session.rust import RustSession
from aiogram.exceptions import ClientDecodeError, TelegramBadRequest, TelegramNetworkError
from aiogram.methods import TelegramMethod
from aiogram.types import BufferedInputFile
from tests.mocked_bot import MockedBot


class FakeRustHttpClient:
    instances: list[FakeRustHttpClient] = []

    def __init__(
        self,
        limit: int = 100,
        user_agent: str | None = None,
        proxy: str | None = None,
    ) -> None:
        self.limit = limit
        self.user_agent = user_agent
        self.proxy = proxy
        self.closed = False
        self.post = AsyncMock(return_value=(200, '{"ok": true, "result": 42}'))
        self.stream_content = AsyncMock(return_value=FakeRustByteStream())
        self.instances.append(self)

    def close(self) -> None:
        self.closed = True


class FakeRustByteStream:
    def __init__(self) -> None:
        self.next_chunks = AsyncMock(side_effect=[([b"ab", b"cd"], False), ([b"ef"], True)])


class FailingRustHttpClient(FakeRustHttpClient):
    def __init__(
        self,
        limit: int = 100,
        user_agent: str | None = None,
        proxy: str | None = None,
    ) -> None:
        super().__init__(limit=limit, user_agent=user_agent, proxy=proxy)
        self.post = AsyncMock(side_effect=RuntimeError("transport failed"))


class BareInputFile(BufferedInputFile):
    async def read(self, bot: Any) -> AsyncGenerator[bytes, None]:
        yield b"foo"
        yield b"bar"


@pytest.fixture(autouse=True)
def reset_fake_clients() -> None:
    FakeRustHttpClient.instances = []


@pytest.fixture()
def native_module() -> SimpleNamespace:
    return SimpleNamespace(
        RustHttpClient=FakeRustHttpClient,
        RustTransportError=RuntimeError,
        __version__="3.28.2",
    )


def test_import_without_native_extension() -> None:
    assert rust.RustSession is RustSession


def test_init_without_native_extension_raises() -> None:
    with (
        patch("importlib.import_module", side_effect=ImportError("missing")),
        patch.object(rust, "_load_native_from_installed_extension", return_value=None),
        pytest.raises(RuntimeError, match="wheel that bundles the Rust extension"),
    ):
        RustSession()


async def test_create_and_close_session(native_module: SimpleNamespace) -> None:
    with patch("aiogram.client.session.rust._load_native", return_value=native_module):
        session = RustSession(proxy="http://proxy.example:8080", limit=10)

    client = await session.create_session()
    assert client.limit == 10
    assert client.proxy == "http://proxy.example:8080"
    assert "aiogram/" in client.user_agent

    await session.close()
    assert client.closed is True
    assert session._session is None


async def test_build_request_data(native_module: SimpleNamespace, bot: MockedBot) -> None:
    class TestMethod(TelegramMethod[bool]):
        __api_method__ = "test"
        __returning__ = bool

        str_: str
        int_: int
        bool_: bool
        none_: None
        list_: list[str]
        file_: BufferedInputFile

    with patch("aiogram.client.session.rust._load_native", return_value=native_module):
        session = RustSession()

    fields, files = await session.build_form_data(
        bot,
        TestMethod(
            str_="value",
            int_=42,
            bool_=True,
            none_=None,
            list_=["foo"],
            file_=BareInputFile(b"ignored", filename="file.txt"),
        ),
    )

    field_names = [name for name, _ in fields]
    assert "none_" not in field_names
    assert ("str_", "value") in fields
    assert ("int_", "42") in fields
    assert ("bool_", "true") in fields
    assert ("list_", '["foo"]') in fields

    assert len(files) == 1
    file_key, filename, data = files[0]
    assert filename == "file.txt"
    assert data == b"foobar"
    assert ("file_", f"attach://{file_key}") in fields


async def test_build_request_data_with_custom_json_dumps(
    native_module: SimpleNamespace,
    bot: MockedBot,
) -> None:
    class TestMethod(TelegramMethod[bool]):
        __api_method__ = "test"
        __returning__ = bool

        values: list[str]

    def json_dumps(value: Any) -> str:
        return f"custom:{value!r}"

    with patch("aiogram.client.session.rust._load_native", return_value=native_module):
        session = RustSession(json_dumps=json_dumps)

    fields, files = await session.build_form_data(
        bot,
        TestMethod(values=["foo"]),
    )

    assert files == []
    assert fields == [("values", "custom:['foo']")]


def test_check_response_with_custom_json_loads(
    native_module: SimpleNamespace,
    bot: MockedBot,
) -> None:
    class TestMethod(TelegramMethod[int]):
        __api_method__ = "test"
        __returning__ = int

    calls: list[str] = []

    def json_loads(value: str) -> Any:
        calls.append(value)
        return {"ok": True, "result": 42}

    with patch("aiogram.client.session.rust._load_native", return_value=native_module):
        session = RustSession(json_loads=json_loads)

    response = session.check_response(
        bot=bot,
        method=TestMethod(),
        status_code=200,
        content='{"ignored": true}',
    )

    assert calls == ['{"ignored": true}']
    assert response.result == 42


def test_check_response_fast_path(native_module: SimpleNamespace, bot: MockedBot) -> None:
    class TestMethod(TelegramMethod[int]):
        __api_method__ = "test"
        __returning__ = int

    with patch("aiogram.client.session.rust._load_native", return_value=native_module):
        session = RustSession()

    response = session.check_response(
        bot=bot,
        method=TestMethod(),
        status_code=200,
        content='{"ok": true, "result": 42}',
    )

    assert response.result == 42


def test_check_response_fast_path_error(native_module: SimpleNamespace, bot: MockedBot) -> None:
    class TestMethod(TelegramMethod[int]):
        __api_method__ = "test"
        __returning__ = int

    with patch("aiogram.client.session.rust._load_native", return_value=native_module):
        session = RustSession()

    with pytest.raises(TelegramBadRequest):
        session.check_response(
            bot=bot,
            method=TestMethod(),
            status_code=400,
            content='{"ok": false, "description": "test"}',
        )


def test_check_response_fast_path_decode_error(
    native_module: SimpleNamespace,
    bot: MockedBot,
) -> None:
    class TestMethod(TelegramMethod[int]):
        __api_method__ = "test"
        __returning__ = int

    with patch("aiogram.client.session.rust._load_native", return_value=native_module):
        session = RustSession()

    with pytest.raises(ClientDecodeError, match="Failed to decode object"):
        session.check_response(
            bot=bot,
            method=TestMethod(),
            status_code=200,
            content="is not a JSON object",
        )


async def test_make_request(native_module: SimpleNamespace, bot: MockedBot) -> None:
    class TestMethod(TelegramMethod[int]):
        __returning__ = int
        __api_method__ = "method"

    with patch("aiogram.client.session.rust._load_native", return_value=native_module):
        session = RustSession()

    result = await session.make_request(bot, TestMethod())

    assert result == 42
    client = FakeRustHttpClient.instances[0]
    client.post.assert_awaited_once()


async def test_make_request_network_error(bot: MockedBot) -> None:
    native_module = SimpleNamespace(
        RustHttpClient=FailingRustHttpClient,
        RustTransportError=RuntimeError,
        __version__="3.28.2",
    )

    class TestMethod(TelegramMethod[int]):
        __returning__ = int
        __api_method__ = "method"

    with patch("aiogram.client.session.rust._load_native", return_value=native_module):
        session = RustSession()

    with pytest.raises(TelegramNetworkError):
        await session.make_request(bot, TestMethod())


async def test_stream_content(native_module: SimpleNamespace) -> None:
    with patch("aiogram.client.session.rust._load_native", return_value=native_module):
        session = RustSession()

    chunks = [
        chunk
        async for chunk in session.stream_content(
            "https://example.com/file",
            headers={"X-Test": "value"},
            timeout=5,
            chunk_size=2,
        )
    ]

    assert chunks == [b"ab", b"cd", b"ef"]
    client = FakeRustHttpClient.instances[0]
    client.stream_content.assert_awaited_once_with(
        url="https://example.com/file",
        headers=[("X-Test", "value")],
        timeout=5,
        raise_for_status=True,
    )
    stream = client.stream_content.return_value
    assert stream.next_chunks.await_args_list[0].args == (2, 16)
