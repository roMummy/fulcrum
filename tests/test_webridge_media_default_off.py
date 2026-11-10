"""微桥入站媒体默认不下载：只有群开启“自动下载附件”才会向手机下发 upload_message_media。"""
from app.services import channel_events


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_media_not_queued_when_group_switch_off(monkeypatch):
    called = []
    monkeypatch.setattr(channel_events, "_chatroom_auto_download_media_enabled", lambda *a, **k: False)
    monkeypatch.setattr(channel_events, "create_wechat_media_attachment", lambda *a, **k: called.append(1))
    channel = _Obj(channel_type="wechat_webridge", id=2)
    message = _Obj(message_type="video", id=1)
    channel_events._queue_inbound_media_fetch_if_needed(
        None, channel=channel, conversation=_Obj(), raw_event=_Obj(id=1),
        normalized={"details": {"media": {"x": 1}}}, message=message,
    )
    assert called == []
