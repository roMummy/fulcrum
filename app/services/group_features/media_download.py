"""自动下载附件:自动下载本群收到的图片、语音、视频。

原先是群记录 extra_json 里的 auto_download_media 开关,现在收归群功能。
这是一个“开关型”能力:不响应命令也不处理事件,入站媒体处理(channel_events)在收到
图片/语音/视频时读取本群是否开启。手机端下载媒体很耗资源,所以默认关闭。
"""

from __future__ import annotations

from app.services.group_features.registry import GroupFeature, register

# iPad、微桥和咕嘎助手都实现了入站媒体下载（手机侧解密后回传）。
SUPPORTED_CHANNEL_TYPES = ("wechat_ipad", "wechat_webridge", "wechat_gugatool")

FEATURE = register(
    GroupFeature(
        key="media_download",
        name="自动下载附件",
        description="自动下载本群收到的图片、语音、视频。会占用手机资源,默认关闭。",
        channel_types=SUPPORTED_CHANNEL_TYPES,
    )
)
