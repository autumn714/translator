"""translator_app/gateway.py (내부망 접속 중계기) 시험."""
from __future__ import annotations

import asyncio

import pytest

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


@pytest.mark.parametrize("bad", ["10.1.20.0/33", "10.1.20.*", "10.1.20.0/24 hello"])
def test_parse_allow_names_the_bad_item(bad):
    with pytest.raises(ValueError, match="UI_ALLOW") as e:
        gateway.parse_allow(bad)
    assert bad.split()[-1] in str(e.value)


def test_check_mode_validates_allow_without_serving(capsys):
    assert gateway.main(["--check", "--allow", "10.1.20.0/24, 10.1.21.15"]) == 0
    assert gateway.main(["--check", "--allow", ""]) == 0
    capsys.readouterr()
    assert gateway.main(["--check", "--allow", "10.1.20.0/33"]) == 2
    err = capsys.readouterr().err
    assert "10.1.20.0/33" in err and "Traceback" not in err


def test_serve_mode_with_bad_allow_exits_cleanly(capsys):
    # 예전에는 ValueError 가 그대로 터져 중계기가 알아보기 힘든 오류로 죽었다
    assert gateway.main(["--listen", "127.0.0.1:0", "--target", "127.0.0.1:1", "--allow", "10.1.20.*"]) == 2
    assert "UI_ALLOW" in capsys.readouterr().err
