"""微桥 ASGI 连接结束时，外层取消不能再次打断收发任务的释放。"""

from __future__ import annotations

import asyncio

import anyio
from fastapi import WebSocketDisconnect

from app.services.wechat_webridge import ws


def test_asgi_cancellation_during_disconnect_finishes_child_cleanup(monkeypatch):
    """精确在 gather 释放阶段取消外层请求，复现 TestClient 快速断开时的竞争。"""

    original_gather = asyncio.gather
    cleanup_finished = []
    scope_holder = []

    class Socket:
        received_hello = False

        async def accept(self):
            pass

        async def send_json(self, _frame):
            pass

        async def receive_json(self):
            if not self.received_hello:
                self.received_hello = True
                return {"type": "hello"}
            raise WebSocketDisconnect(code=1000)

    async def cancelling_gather(*tasks, **kwargs):
        # 两个局部收发任务是 serve 的清理阶段；不改变 asyncio.run 自身的关闭流程。
        if len(tasks) == 2 and scope_holder:
            scope_holder[0].cancel()
            await asyncio.sleep(0)
            result = await original_gather(*tasks, **kwargs)
            cleanup_finished.append(True)
            return result
        return await original_gather(*tasks, **kwargs)

    monkeypatch.setattr(ws, "_process", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(ws.asyncio, "gather", cancelling_gather)

    async def scenario():
        with anyio.CancelScope() as scope:
            scope_holder.append(scope)
            await ws.serve(Socket(), channel_id=987654, tenant_id=1, connection_token="test-token")

    asyncio.run(scenario())
    assert cleanup_finished == [True]
    assert 987654 not in ws._connections
