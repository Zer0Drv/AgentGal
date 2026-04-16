import asyncio

import pytest

from storage.vector_store import VectorStore


class FakeConnection:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_close_resets_connection_state() -> None:
    store = VectorStore()
    fake_connection = FakeConnection()
    loop = asyncio.get_running_loop()
    store._db = fake_connection  # type: ignore[assignment]
    store._tables_initialized = True
    store._tables_initialized_loop = loop
    store._write_lock = asyncio.Lock()
    store._write_lock_loop = loop

    await store.close()

    assert fake_connection.closed is True
    assert store._db is None
    assert store._tables_initialized is False
    assert store._tables_initialized_loop is None
    assert store._write_lock is None
    assert store._write_lock_loop is None


@pytest.mark.asyncio
async def test_close_without_connection_is_noop() -> None:
    store = VectorStore()

    await store.close()

    assert store._db is None
