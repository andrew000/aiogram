from __future__ import annotations

import datetime
import importlib
import importlib.machinery
import importlib.util
import sys
from collections.abc import AsyncGenerator
from http import HTTPStatus
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, cast

from aiohttp.hdrs import USER_AGENT
from aiohttp.http import SERVER_SOFTWARE
from pydantic import ValidationError

from aiogram.__meta__ import __version__
from aiogram.exceptions import (
    ClientDecodeError,
    RestartingTelegram,
    TelegramAPIError,
    TelegramBadRequest,
    TelegramConflictError,
    TelegramEntityTooLarge,
    TelegramForbiddenError,
    TelegramMigrateToChat,
    TelegramNetworkError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramServerError,
    TelegramUnauthorizedError,
)
from aiogram.methods import Response
from aiogram.methods.base import TelegramType

from .base import BaseSession

if TYPE_CHECKING:
    from aiogram.client.bot import Bot
    from aiogram.methods import TelegramMethod
    from aiogram.types import InputFile


_MISSING_NATIVE_MESSAGE = (
    "RustSession requires the optional Rust transport. "
    "Install aiogram from a wheel that bundles the Rust extension, "
    "or build aiogram from source with a Rust toolchain available."
)
_UNSUPPORTED_FAST_VALUE = object()


def _load_native() -> ModuleType:
    try:
        return importlib.import_module("aiogram.client.session._rust")
    except ImportError as exc:
        native = _load_native_from_installed_extension()
        if native is not None:
            return native
        raise RuntimeError(_MISSING_NATIVE_MESSAGE) from exc


def _load_native_from_installed_extension() -> ModuleType | None:
    module_name = "aiogram.client.session._rust"
    relative_path = Path("aiogram", "client", "session")
    for entry in sys.path:
        if not entry:
            continue
        package_path = Path(entry, relative_path)
        for suffix in importlib.machinery.EXTENSION_SUFFIXES:
            extension_path = package_path / f"_rust{suffix}"
            if not extension_path.exists():
                continue
            spec = importlib.util.spec_from_file_location(module_name, extension_path)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            return module
    return None


class RustSession(BaseSession):
    def __init__(self, proxy: str | None = None, limit: int = 100, **kwargs: Any) -> None:
        """
        Client session based on the optional Rust transport.

        :param proxy: Proxy URL to be used for requests. Proxy chains are not supported.
        :param limit: The total number of simultaneous connections. Default is 100.
        :param kwargs: Additional keyword arguments passed to :class:`BaseSession`.
        """
        super().__init__(**kwargs)

        native = _load_native()
        self._client_type = native.RustHttpClient
        self._session: Any | None = None
        self._limit = limit
        self._proxy = proxy
        self._headers = {
            USER_AGENT: f"{SERVER_SOFTWARE} aiogram/{__version__}",
        }

    @property
    def proxy(self) -> str | None:
        return self._proxy

    async def create_session(self) -> Any:
        if self._session is None:
            self._session = self._client_type(
                limit=self._limit,
                user_agent=self._headers[USER_AGENT],
                proxy=self._proxy,
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None

    async def build_form_data(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
    ) -> tuple[list[tuple[str, str]], list[tuple[str, str, bytes]]]:
        fields: list[tuple[str, str]] = []
        files: dict[str, InputFile] = {}
        for key, value in method.model_dump(warnings=False).items():
            prepared = self._prepare_value_fast(value, dumps_json=True)
            value = (
                self.prepare_value(value, bot=bot, files=files)
                if prepared is _UNSUPPORTED_FAST_VALUE
                else prepared
            )
            if not value:
                continue
            fields.append((key, value))

        file_parts: list[tuple[str, str, bytes]] = []
        for key, value in files.items():
            data = bytearray()
            async for chunk in value.read(bot):
                data.extend(chunk)
            file_parts.append((key, value.filename or key, bytes(data)))

        return fields, file_parts

    def _prepare_value_fast(self, value: Any, dumps_json: bool) -> Any:
        if value is None:
            return None
        if isinstance(value, str):
            return value
        if isinstance(value, bool):
            return self.json_dumps(value) if dumps_json else value
        if isinstance(value, int | float):
            return self.json_dumps(value) if dumps_json else value
        if isinstance(value, dict):
            dict_result: dict[Any, Any] = {}
            for key, item in value.items():
                prepared = self._prepare_value_fast(item, dumps_json=False)
                if prepared is _UNSUPPORTED_FAST_VALUE:
                    return _UNSUPPORTED_FAST_VALUE
                if prepared is not None:
                    dict_result[key] = prepared
            return self.json_dumps(dict_result) if dumps_json else dict_result
        if isinstance(value, list):
            list_result: list[Any] = []
            for item in value:
                prepared = self._prepare_value_fast(item, dumps_json=False)
                if prepared is _UNSUPPORTED_FAST_VALUE:
                    return _UNSUPPORTED_FAST_VALUE
                if prepared is not None:
                    list_result.append(prepared)
            return self.json_dumps(list_result) if dumps_json else list_result
        if isinstance(value, datetime.timedelta):
            now = datetime.datetime.now()  # noqa: DTZ005
            return str(round((now + value).timestamp()))
        if isinstance(value, datetime.datetime):
            return str(round(value.timestamp()))
        return _UNSUPPORTED_FAST_VALUE

    def check_response(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        status_code: int,
        content: str,
    ) -> Response[TelegramType]:
        try:
            response_type = Response[method.__returning__]  # type: ignore
            response = response_type.model_validate_json(content, context={"bot": bot})
        except ValidationError as e:
            msg = "Failed to deserialize object"
            if any(error.get("type") == "json_invalid" for error in e.errors()):
                msg = "Failed to decode object"
            raise ClientDecodeError(msg, e, content) from e

        if HTTPStatus.OK <= status_code <= HTTPStatus.IM_USED and response.ok:
            return response

        description = cast(str, response.description)

        if parameters := response.parameters:
            if parameters.retry_after:
                raise TelegramRetryAfter(
                    method=method,
                    message=description,
                    retry_after=parameters.retry_after,
                )
            if parameters.migrate_to_chat_id:
                raise TelegramMigrateToChat(
                    method=method,
                    message=description,
                    migrate_to_chat_id=parameters.migrate_to_chat_id,
                )
        if status_code == HTTPStatus.BAD_REQUEST:
            raise TelegramBadRequest(method=method, message=description)
        if status_code == HTTPStatus.NOT_FOUND:
            raise TelegramNotFound(method=method, message=description)
        if status_code == HTTPStatus.CONFLICT:
            raise TelegramConflictError(method=method, message=description)
        if status_code == HTTPStatus.UNAUTHORIZED:
            raise TelegramUnauthorizedError(method=method, message=description)
        if status_code == HTTPStatus.FORBIDDEN:
            raise TelegramForbiddenError(method=method, message=description)
        if status_code == HTTPStatus.REQUEST_ENTITY_TOO_LARGE:
            raise TelegramEntityTooLarge(method=method, message=description)
        if status_code >= HTTPStatus.INTERNAL_SERVER_ERROR:
            if "restart" in description:
                raise RestartingTelegram(method=method, message=description)
            raise TelegramServerError(method=method, message=description)

        raise TelegramAPIError(
            method=method,
            message=description,
        )

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: int | None = None,
    ) -> TelegramType:
        session = await self.create_session()

        url = self.api.api_url(token=bot.token, method=method.__api_method__)
        fields, files = await self.build_form_data(bot=bot, method=method)

        try:
            status_code, raw_result = await session.post(
                url=url,
                timeout=self.timeout if timeout is None else timeout,
                fields=fields,
                files=files,
            )
        except TimeoutError as e:
            raise TelegramNetworkError(method=method, message="Request timeout error") from e
        except Exception as e:  # noqa: BLE001
            raise TelegramNetworkError(method=method, message=f"{type(e).__name__}: {e}") from e
        response = self.check_response(
            bot=bot,
            method=method,
            status_code=status_code,
            content=raw_result,
        )
        return cast(TelegramType, response.result)

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        if headers is None:
            headers = {}

        session = await self.create_session()
        stream = await session.stream_content(
            url=url,
            headers=list(headers.items()),
            timeout=timeout,
            raise_for_status=raise_for_status,
        )

        while True:
            chunks, done = await stream.next_chunks(chunk_size, 16)
            for chunk in chunks:
                yield chunk
            if done:
                break

    async def __aenter__(self) -> RustSession:
        await self.create_session()
        return self
