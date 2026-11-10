"""长轮询唤醒:新命令入队(或到了该下发的时间)时,立刻叫醒挂起等待中的手机轮询请求。

手机用长轮询取命令,服务端挂起期间每隔 POLL_CHECK_INTERVAL_SECONDS(1 秒)查一次队列,
所以一条命令最多会晚一秒才被取走。对大多数命令无所谓,但"延迟 N 毫秒抢红包"这类对时间敏感的命令不行。
这里提供一个进程内的唤醒通道:入队方在命令提交之后调用 notify(channel_id, delay_seconds),
挂起中的 poll 请求立刻重新查询队列;原来的 1 秒检查仍然保留作为兜底(进程重启、通知丢失时不会卡住命令)。

线程模型:poll 在事件循环里等待;入队方可能在后台 worker 线程里,所以必须经 call_soon_threadsafe 唤醒,
延迟通知用守护 Timer,不阻塞调用方。
"""

from __future__ import annotations

import asyncio
import threading

_lock = threading.Lock()
# 渠道 -> 正在挂起等待的 (事件循环, 事件)。同一渠道可能同时有多个 poll 请求。
_waiters: dict[int, set[tuple[asyncio.AbstractEventLoop, asyncio.Event]]] = {}


def register(channel_id: int, loop: asyncio.AbstractEventLoop, event: asyncio.Event) -> None:
    with _lock:
        _waiters.setdefault(int(channel_id), set()).add((loop, event))


def unregister(channel_id: int, loop: asyncio.AbstractEventLoop, event: asyncio.Event) -> None:
    with _lock:
        waiters = _waiters.get(int(channel_id))
        if waiters is None:
            return
        waiters.discard((loop, event))
        if not waiters:
            del _waiters[int(channel_id)]


def _fire(channel_id: int) -> None:
    with _lock:
        targets = list(_waiters.get(int(channel_id), ()))
    for loop, event in targets:
        try:
            loop.call_soon_threadsafe(event.set)
        except RuntimeError:
            # 事件循环已经关闭(请求结束或进程在退出),这个等待者不会再有用。
            pass


def notify(channel_id: int, delay_seconds: float = 0.0) -> None:
    """叫醒该渠道挂起中的 poll;delay_seconds > 0 时在那么久之后再叫醒(命令的 available_at 到点时)。

    必须在命令所在的数据库事务提交之后调用,否则被叫醒的 poll 查不到这条命令。
    """

    if delay_seconds > 0:
        timer = threading.Timer(delay_seconds, _fire, args=(channel_id,))
        timer.daemon = True
        timer.start()
    else:
        _fire(channel_id)
