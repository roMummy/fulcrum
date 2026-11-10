"""秀人图片插件:从 MinIO 预读取一批图片再发送。

包含 S3 客户端(签名、列对象、取对象)与插件服务层(预读取、取用即删、配置)的测试;
S3 部分用本机假服务模拟 MinIO,不依赖真实服务。
"""

from __future__ import annotations

import base64
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from plugins._shared import s3
from plugins._shared.s3 import S3Client, S3Config, S3Error
from plugins.xiuren_image import service as xs
from plugins.xiuren_image.service import load_config

# 1x1 的有效 PNG,能通过插件的图片类型校验。
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC")


def _config(**overrides) -> dict:
    base = {"minio_endpoint": "192.168.2.32:9000", "minio_bucket": "pics", "minio_access_key": "ak", "minio_secret_key": "sk"}
    base.update(overrides)
    return base


# ---------- S3 签名 ----------

def test_sigv4_matches_the_official_aws_test_vector() -> None:
    """AWS 官方 SigV4 测试向量 get-vanilla:验证签名算法本身正确。"""

    headers = {"host": "example.amazonaws.com", "x-amz-date": "20150830T123600Z"}
    canonical, signed = s3.canonical_request("GET", "/", {}, headers, s3.EMPTY_PAYLOAD_SHA256)
    assert signed == "host;x-amz-date"
    result = s3.signature(
        "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY", canonical, amz_date="20150830T123600Z", region="us-east-1", service="service"
    )
    assert result == "5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31"


def test_unsafe_endpoints_are_rejected_but_lan_is_allowed() -> None:
    for endpoint in ("127.0.0.1:9000", "localhost:9000", "169.254.169.254", "0.0.0.0:9000"):
        with pytest.raises(S3Error):
            s3.reject_unsafe_endpoint(endpoint)
    with pytest.raises(S3Error):
        s3.reject_unsafe_endpoint("evil.com/path")
    s3.reject_unsafe_endpoint("192.168.2.32:9000")  # 局域网 MinIO 允许


# ---------- 假 MinIO ----------

class _FakeS3(BaseHTTPRequestHandler):
    objects: dict[str, bytes] = {}
    seen_auth: list[str] = []
    page_size = 1000

    def log_message(self, *args) -> None:  # 静默
        pass

    def do_GET(self) -> None:  # noqa: N802
        parts = urlsplit(self.path)
        type(self).seen_auth.append(self.headers.get("Authorization", ""))
        segments = unquote(parts.path).lstrip("/").split("/", 1)
        if segments[0] != "pics":
            self.send_error(404)
            return
        if len(segments) == 1 or not segments[1]:
            query = parse_qs(parts.query)
            prefix = query.get("prefix", [""])[0]
            token = int(query.get("continuation-token", ["0"])[0] or 0)
            keys = sorted(key for key in self.objects if key.startswith(prefix))
            page = keys[token : token + self.page_size]
            more = token + self.page_size < len(keys)
            items = "".join(f"<Contents><Key>{k}</Key><Size>{len(self.objects[k])}</Size></Contents>" for k in page)
            nxt = f"<NextContinuationToken>{token + self.page_size}</NextContinuationToken>" if more else ""
            body = f'<?xml version="1.0"?><ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><IsTruncated>{str(more).lower()}</IsTruncated>{items}{nxt}</ListBucketResult>'.encode()
        else:
            data = self.objects.get(segments[1])
            if data is None:
                self.send_error(404)
                return
            body = data
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def fake_minio():
    _FakeS3.objects = {}
    _FakeS3.seen_auth = []
    _FakeS3.page_size = 1000
    server = HTTPServer(("127.0.0.1", 0), _FakeS3)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, S3Client(S3Config(endpoint=f"127.0.0.1:{server.server_port}", bucket="pics", access_key="ak", secret_key="sk"))
    finally:
        server.shutdown()


def test_client_lists_with_prefix_and_pagination_and_gets_objects(fake_minio) -> None:
    _, client = fake_minio
    _FakeS3.objects = {f"album/{i:03d}.png": PNG for i in range(5)} | {"other/x.png": PNG, "album/sub/": b""}
    _FakeS3.page_size = 2  # 强制分页
    listed = client.list_objects("album/")
    assert [key for key, _ in listed] == [f"album/{i:03d}.png" for i in range(5)]
    assert client.get_object("album/002.png", max_bytes=1024) == PNG
    # 请求带了 SigV4 授权头
    assert all(auth.startswith("AWS4-HMAC-SHA256 Credential=ak/") and "Signature=" in auth for auth in _FakeS3.seen_auth)


def test_client_handles_non_ascii_keys_and_size_limits(fake_minio) -> None:
    _, client = fake_minio
    _FakeS3.objects = {"相册 一/图 1.png": PNG}
    assert client.get_object("相册 一/图 1.png", max_bytes=1024) == PNG
    with pytest.raises(S3Error):
        client.get_object("相册 一/图 1.png", max_bytes=10)  # 超过大小限制
    with pytest.raises(S3Error):
        client.get_object("不存在.png", max_bytes=1024)  # 404


def test_client_reports_connection_failure() -> None:
    client = S3Client(S3Config(endpoint="127.0.0.1:1", bucket="pics", access_key="ak", secret_key="sk", timeout_seconds=2))
    with pytest.raises(S3Error):
        client.list_objects()


# ---------- 插件服务层 ----------

def test_config_defaults_limits_and_endpoint_normalization() -> None:
    config = load_config(_config(minio_endpoint="http://192.168.2.32:9000/", prefetch_count=999, resource_dir="/abs"))
    assert config.minio_endpoint == "192.168.2.32:9000"  # 去掉协议和多余斜杠
    assert config.prefetch_count == 50 and config.resource_dir == "images"
    assert load_config({}).prefetch_count == 20
    assert load_config(_config()).source_configured() and not load_config({}).source_configured()


class _FakeClient:
    """不走网络的客户端:列出固定对象,取对象返回 PNG,个别 key 失败。"""

    def __init__(self, keys, bad=()):
        self.keys, self.bad = keys, set(bad)

    def list_objects(self, prefix="", *, max_keys=5000):
        return [(k, len(PNG)) for k in self.keys if k.startswith(prefix)] + [("a/readme.txt", 5), ("a/empty.png", 0)]

    def get_object(self, key, *, max_bytes):
        if key in self.bad:
            raise S3Error("boom")
        return PNG


def test_prefetch_reads_a_batch_of_images_and_skips_failures(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(xs, "tenant_media_root", lambda tenant_id, code: tmp_path / "tenant")
    config = load_config(_config(prefetch_count=3, minio_prefix="a/"))
    keys = [f"a/{i}.png" for i in range(10)]
    state = xs.prefetch_images(_FakeClient(keys, bad={"a/0.png"}), 7, config, {}, sampler=lambda objs, n: objs[:n])
    # 非图片、空文件被过滤;抽到 3 张里有 1 张失败 => 成功 2 张;同内容重复没有关系(按哈希合并)
    assert state["listed"] == 10 and state["downloaded"] == 2 and state["failed"] == 1
    assert len(state["files"]) == 1  # 两张内容相同,按哈希合成一条
    assert (tmp_path / "tenant" / "images" / f"{state['files'][0]['digest']}.img").is_file()


def test_pool_is_consumed_one_by_one_and_capped(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(xs, "tenant_media_root", lambda tenant_id, code: tmp_path / "tenant")
    config = load_config(_config(prefetch_count=2))
    root = tmp_path / "tenant" / "images"
    root.mkdir(parents=True)
    files = []
    for name in ("a", "b", "c"):
        (root / f"{name}.img").write_bytes(PNG)
        files.append({"path": f"{name}.img", "digest": name, "source": name})
    state = {"files": files, "queued_at": "x"}

    taken = xs.take_cached_image(7, config, state, chooser=lambda items: items[0])
    assert taken is not None
    data, info = taken
    assert data == PNG and info["taken"]["digest"] == "a"
    # 取用不改动磁盘,扣费成功后才由调用方删除;新状态已去掉这一张,且保留其他键
    assert (root / "a.img").exists()
    assert [f["digest"] for f in info["state"]["files"]] == ["b", "c"] and info["state"]["queued_at"] == "x"
    xs.discard_cached_file(7, config, info["taken"])
    assert not (root / "a.img").exists()

    # 池子上限是预读张数的 2 倍:再预读一批后旧文件被淘汰并删除
    big = xs.prefetch_images(_FakeClient([f"k{i}.png" for i in range(4)]), 7, config, info["state"], sampler=lambda objs, n: objs[:n])
    assert len(big["files"]) <= config.prefetch_count * 2


def test_broken_cached_image_is_dropped_and_next_one_is_used(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(xs, "tenant_media_root", lambda tenant_id, code: tmp_path / "tenant")
    config = load_config(_config())
    root = tmp_path / "tenant" / "images"
    root.mkdir(parents=True)
    (root / "bad.img").write_bytes(b"not an image")
    (root / "good.img").write_bytes(PNG)
    state = {"files": [{"path": "bad.img", "digest": "bad", "source": ""}, {"path": "good.img", "digest": "good", "source": ""}]}
    taken = xs.take_cached_image(7, config, state, chooser=lambda items: items[0])
    assert taken is not None and taken[1]["taken"]["digest"] == "good"
    assert not (root / "bad.img").exists()
    # 池空返回 None
    assert xs.take_cached_image(7, config, {"files": []}) is None


# ---------- 插件入口完整流程(用替身代替限流、状态存储、积分和入队) ----------

class _Env:
    """把插件入口依赖的外部协作者替换成内存替身,并记录调用。"""

    def __init__(self, monkeypatch, tmp_path: Path, *, points_ok: bool = True):
        from plugins.xiuren_image import plugin as xp

        self.xp, self.spent, self.enqueued = xp, [], []
        self.state: dict = {}
        outer = self
        monkeypatch.setattr(xs, "tenant_media_root", lambda tenant_id, code: tmp_path / "tenant")
        monkeypatch.setattr(xp, "require_tenant_id", lambda: 7)

        class _Decision:
            allowed = True
            retry_after_seconds = 0

        class _Limiter:
            def __init__(self, *a): pass
            def consume(self, *a, **k): return _Decision()

        class _Store:
            def __init__(self, *a): pass
            def get(self, ns, key): return outer.state or None
            def put(self, ns, key, value): outer.state = value

        class _Points:
            def __init__(self, db): pass
            def spend(self, ctx, cost, **kw):
                outer.spent.append(cost)
                return type("R", (), {"ok": points_ok, "message": "积分不足"})()

        monkeypatch.setattr(xp, "PluginRateLimiter", _Limiter)
        monkeypatch.setattr(xp, "PluginStateStore", _Store)
        monkeypatch.setattr(xp, "PointsService", _Points)
        monkeypatch.setattr(xp, "enqueue_plugin_scheduled_action", lambda *a, **k: outer.enqueued.append(k.get("action_key")))
        self.db = type("DB", (), {"get": staticmethod(lambda model, pk: object())})()
        self.root = tmp_path / "tenant" / "images"

    def seed(self, count: int) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        files = []
        for i in range(count):
            (self.root / f"{i}.img").write_bytes(PNG)
            files.append({"path": f"{i}.img", "digest": str(i), "source": ""})
        self.state = {"files": files}

    def run(self, config: dict):
        from app.services.plugin_runtime.contracts import PluginExecutionContext, PluginExecutionRequest

        context = PluginExecutionContext(
            tenant_id=7, binding_id=1, bot_id=1, channel_id=1, conversation_id=3, sender_id="u1", message_id=9,
            channel_type="wechat_webridge", chat_type="group", message_type="text",
        )
        return self.xp.execute_message_action(self.db, PluginExecutionRequest(action="execute", config=config, context=context))


def test_flow_requires_minio_configuration(monkeypatch, tmp_path: Path) -> None:
    env = _Env(monkeypatch, tmp_path)
    result = env.run({})
    assert result.output["provider_status"] == "source_not_configured" and env.spent == []


def test_flow_empty_pool_queues_a_prefetch_once(monkeypatch, tmp_path: Path) -> None:
    env = _Env(monkeypatch, tmp_path)
    assert env.run(_config()).output["provider_status"] == "refresh_queued"
    assert env.enqueued == ["refresh_resources"] and env.spent == []
    # 冷却期内再次触发不重复入队
    env.run(_config())
    assert env.enqueued == ["refresh_resources"]


def test_flow_sends_one_image_charges_points_and_consumes_it(monkeypatch, tmp_path: Path) -> None:
    env = _Env(monkeypatch, tmp_path)
    env.seed(10)
    result = env.run(_config())
    assert result.ok and result.output["provider_status"] == "completed"
    assert result.output["replies"][0]["type"] == "image"
    assert env.spent == [5] and len(env.state["files"]) == 9 and env.enqueued == []
    assert len(list(env.root.glob("*.img"))) == 9  # 已发送的文件被删除


def test_flow_does_not_lose_the_image_when_points_are_insufficient(monkeypatch, tmp_path: Path) -> None:
    env = _Env(monkeypatch, tmp_path, points_ok=False)
    env.seed(3)
    result = env.run(_config())
    assert not result.ok and result.output["provider_status"] == "insufficient_points"
    assert len(env.state["files"]) == 3 and len(list(env.root.glob("*.img"))) == 3


def test_flow_schedules_next_batch_when_pool_runs_low(monkeypatch, tmp_path: Path) -> None:
    env = _Env(monkeypatch, tmp_path)
    env.seed(xs.LOW_WATER + 1)
    env.run(_config())  # 取走一张后剩 LOW_WATER 张 => 触发后台补图
    assert env.enqueued == ["refresh_resources"] and env.state.get("queued_at")
    env.run(_config())  # 再取一张:仍在冷却期,不重复入队
    assert env.enqueued == ["refresh_resources"]


def test_scheduled_refresh_prefetches_and_reports_readable_errors(monkeypatch, tmp_path: Path) -> None:
    from app.services.plugin_runtime.contracts import PluginExecutionContext, PluginExecutionRequest

    env = _Env(monkeypatch, tmp_path)
    context = PluginExecutionContext(tenant_id=7, binding_id=1, bot_id=1)

    def run(config):
        return env.xp.execute_scheduled_action(env.db, PluginExecutionRequest(action="refresh_resources", config=config, context=context))

    assert run({}).output["provider_status"] == "source_not_configured"
    monkeypatch.setattr(env.xp, "build_client", lambda config: _FakeClient([f"a/{i}.png" for i in range(5)]))
    ok = run(_config(prefetch_count=2))
    assert ok.ok and ok.output["downloaded"] == 2 and env.state["files"]

    class _Down:
        def list_objects(self, *a, **k):
            raise S3Error("无法连接 MinIO")

    monkeypatch.setattr(env.xp, "build_client", lambda config: _Down())
    failed = run(_config())
    # 错误原因是可读的中文,且不带密钥
    assert not failed.ok and "无法连接 MinIO" in failed.message and "sk" not in failed.message.replace("MinIO", "")
