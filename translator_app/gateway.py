"""내부망 접속 중계기 (TCP 그대로 전달, 표준 라이브러리만 사용).

번역기 화면 컨테이너는 인터넷으로 나가는 길이 없는 내부 전용 네트워크에만 붙어 있다.
도커는 그런 네트워크에서 포트를 열어 주지 못하므로, 이 중계기만 일반 네트워크(포트 공개)와
내부 전용 네트워크에 함께 붙어 브라우저 연결을 번역기 화면으로 넘겨준다. 내용을 저장하거나
다른 곳으로 보내지 않는다. 목적지는 시작할 때 정한 한 곳으로 고정되어 있어 일반 프록시로 쓸 수 없다.

python gateway.py --listen 0.0.0.0:7860 --target translator-app:7860 [--allow "10.1.20.0/24 10.1.21.15/32"]
python gateway.py --check --allow "..."   허용 범위만 확인 (run.sh 가 시작 전에 쓴다)
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import ipaddress
import sys


def now() -> str:
    return dt.datetime.now().strftime("%H:%M:%S")


def parse_allow(text: str) -> list:
    """띄어쓰기·쉼표로 구분한 주소 범위(CIDR) 목록. 비면 모두 허용. 틀린 항목이 있으면 ValueError."""
    nets = []
    for item in text.replace(",", " ").split():
        try:
            nets.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            raise ValueError(f"접속 허용 범위(UI_ALLOW)에 올바르지 않은 값이 있습니다: {item}") from None
    return nets


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


async def handle(cr: asyncio.StreamReader, cw: asyncio.StreamWriter, host: str, port: int, nets: list) -> None:
    peer = cw.get_extra_info("peername")
    ip = peer[0] if peer else "?"
    if nets:
        try:
            allowed = any(ipaddress.ip_address(ip) in n for n in nets)
        except ValueError:
            allowed = False
        if not allowed:
            print(f"{now()} 허용 범위 밖 접속 차단: {ip}", flush=True)
            cw.close()
            return
    try:
        tr, tw = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=10)
    except Exception as e:  # noqa: BLE001
        print(f"{now()} 화면 연결 실패 ({ip}): {e}", flush=True)
        cw.close()
        return
    await asyncio.gather(pipe(cr, tw), pipe(tr, cw))


async def serve(listen: str, target: str, allow: str = "") -> asyncio.Server:
    """중계 서버를 열어 돌려준다 (시험에서도 쓴다)."""
    nets = parse_allow(allow)
    lh, lp = listen.rsplit(":", 1)
    th, tp = target.rsplit(":", 1)
    return await asyncio.start_server(lambda r, w: handle(r, w, th, int(tp), nets), lh, int(lp))


async def run(listen: str, target: str, allow: str) -> None:
    nets = parse_allow(allow)
    srv = await serve(listen, target, allow)
    print(f"{now()} 중계 시작: {listen} -> {target}" + (f" (허용: {', '.join(map(str, nets))})" if nets else ""), flush=True)
    async with srv:
        await srv.serve_forever()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", default="0.0.0.0:7860")
    ap.add_argument("--target", default="translator-app:7860")
    ap.add_argument("--allow", default="", help="접속을 허용할 주소 범위(CIDR)를 띄어쓰기로 구분. 비우면 모두 허용")
    ap.add_argument("--check", action="store_true", help="허용 범위만 확인하고 끝낸다")
    a = ap.parse_args(argv)
    try:
        nets = parse_allow(a.allow)
    except ValueError as e:
        print(str(e) if a.check else f"{now()} {e}", file=sys.stderr, flush=True)
        return 2
    if a.check:
        print(f"허용 범위 {len(nets)}개 확인", flush=True)
        return 0
    asyncio.run(run(a.listen, a.target, a.allow))
    return 0


if __name__ == "__main__":
    sys.exit(main())
