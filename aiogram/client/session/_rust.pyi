from __future__ import annotations

from typing import Any

__version__: str

class RustTransportError(Exception): ...

class RustHttpClient:
    def __init__(
        self,
        limit: int = 100,
        user_agent: str | None = None,
        proxy: str | None = None,
    ) -> None: ...
    def close(self) -> None: ...
    async def post(
        self,
        url: str,
        timeout: float,
        fields: list[tuple[str, str]],
        files: list[tuple[str, str, bytes]],
    ) -> tuple[int, str]: ...
    async def stream_content(
        self,
        url: str,
        headers: list[tuple[str, Any]],
        timeout: float,
        raise_for_status: bool = True,
    ) -> RustByteStream: ...

class RustByteStream:
    async def next_chunks(
        self, chunk_size: int = 65536, max_chunks: int = 16
    ) -> tuple[list[bytes], bool]: ...
