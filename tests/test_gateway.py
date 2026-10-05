"""translator_app/gateway.py (내부망 접속 중계기) 시험."""
from __future__ import annotations

import asyncio

from translator_app import gateway


async def echo_server() -> asyncio.Server:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
        writer.close()

    return await asyncio.start_server(handle, "127.0.0.1", 0)


def port_of(srv: asyncio.Server) -> int:
    return srv.sockets[0].getsockname()[1]


async def roundtrip(port: int, payload: bytes) -> bytes:
    """보낸 만큼 돌려받는다. 중계기가 연결을 끊으면 받은 데까지 (보통 b"")."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(payload)
        await writer.drain()
        return await asyncio.wait_for(reader.readexactly(len(payload)), timeout=5)
    except asyncio.IncompleteReadError as e:
        return e.partial
    except ConnectionError:
        return b""
    finally:
        writer.close()


def test_relays_bytes_both_ways():
    async def run() -> None:
        target = await echo_server()
        relay = await gateway.serve("127.0.0.1:0", f"127.0.0.1:{port_of(target)}")
        async with target, relay:
            payload = b"GET / HTTP/1.1\r\n\r\n" + bytes(range(256)) * 1000
            assert await roundtrip(port_of(relay), payload) == payload

    asyncio.run(run())


def test_allow_list_blocks_outside_peer():
    async def run() -> None:
        target = await echo_server()
        relay = await gateway.serve("127.0.0.1:0", f"127.0.0.1:{port_of(target)}", "10.0.0.0/8")
        async with target, relay:
            assert await roundtrip(port_of(relay), b"hello") == b""

    asyncio.run(run())


def test_allow_list_admits_matching_peer():
    async def run() -> None:
        target = await echo_server()
        relay = await gateway.serve("127.0.0.1:0", f"127.0.0.1:{port_of(target)}", "10.0.0.0/8, 127.0.0.0/8")
        async with target, relay:
            assert await roundtrip(port_of(relay), b"hello") == b"hello"

    asyncio.run(run())


def test_target_down_closes_client():
    async def run() -> None:
        probe = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        dead_port = port_of(probe)
        probe.close()
        await probe.wait_closed()
        relay = await gateway.serve("127.0.0.1:0", f"127.0.0.1:{dead_port}")
        async with relay:
            assert await roundtrip(port_of(relay), b"hello") == b""

    asyncio.run(run())


def test_parse_allow():
    nets = gateway.parse_allow("10.1.20.0/24 10.1.21.15, 192.0.2.7/32")
    assert [str(n) for n in nets] == ["10.1.20.0/24", "10.1.21.15/32", "192.0.2.7/32"]
    assert gateway.parse_allow("") == []
