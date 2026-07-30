from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import tempfile
import uuid
from types import SimpleNamespace
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

from app.services.plugin_runtime.contracts import PluginExecutionRequest, PluginExecutionResult
from app.services.wechat_ipad.client import WechatIpadApiClient


DEFAULT_TIMEOUT_SECONDS = 40
AI_COMMANDS = {"ai", "聊天"}
DEFAULT_USER_AGENT = "multi-bot-admin/1.0"


def matches_event(context) -> bool | dict[str, object]:
    command_name = str(getattr(context, "command_name", "") or "").strip().lower()
    mentioned_self = bool(getattr(context, "mentioned_self", False))
    chat_type = str(getattr(context, "chat_type", "") or "").strip().lower()

    if command_name:
        matched = command_name in AI_COMMANDS
        return {
            "matched": matched,
            "reason": "custom_match_hit" if matched else "other_command_reserved",
            "debug": {
                "command_name": command_name,
                "chat_type": chat_type,
                "mentioned_self": mentioned_self,
            },
        }

    if mentioned_self:
        return {
            "matched": True,
            "reason": "mention_hit",
            "debug": {
                "command_name": command_name,
                "chat_type": chat_type,
                "mentioned_self": mentioned_self,
            },
        }

    if chat_type == "direct":
        return {
            "matched": True,
            "reason": "direct_chat_no_command",
            "debug": {
                "command_name": command_name,
                "chat_type": chat_type,
                "mentioned_self": mentioned_self,
            },
        }

    return {
        "matched": False,
        "reason": "ai_no_trigger",
        "debug": {
            "command_name": command_name,
            "chat_type": chat_type,
            "mentioned_self": mentioned_self,
        },
    }


def _text_reply(content: str) -> list[dict[str, str]]:
    normalized = str(content).strip()
    return [{"type": "text", "content": normalized}] if normalized else []


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    api_base = str(request.config.get("api_base") or "").strip().rstrip("/")
    api_key = str(request.config.get("api_key") or "").strip()
    app_mode = _resolve_app_mode(request.config)
    workflow_input_key = str(request.config.get("workflow_input_key") or "query").strip() or "query"
    workflow_session_key = str(request.config.get("workflow_session_key") or "conversation_id").strip()
    request_timeout_seconds = _resolve_timeout_seconds(request.config)
    query, files, quote_debug = _resolve_query_and_files(request, api_base=api_base, api_key=api_key, timeout_seconds=request_timeout_seconds)

    if request.action == "reload":
        if not api_base:
            return PluginExecutionResult(ok=False, status="error", message="Dify 插件缺少 api_base 配置。")
        return PluginExecutionResult(
            ok=True,
            status="running",
            message="Dify 插件已就绪，可执行对话或工作流调用。",
            output={
                "api_base": api_base,
                "api_key_configured": bool(api_key),
                "mode": app_mode,
                "workflow_input_key": workflow_input_key,
                "workflow_session_key": workflow_session_key,
                "request_timeout_seconds": request_timeout_seconds,
                "quote_support": True,
            },
        )

    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")
    if not api_base or not api_key:
        return PluginExecutionResult(ok=False, status="error", message="Dify 插件缺少 api_base 或 api_key 配置。")

    if not query:
        return PluginExecutionResult(ok=False, status="error", message="Dify 执行缺少 query/content。")

    try:
        response = _post_json(
            _build_target_url(api_base, app_mode),
            _build_body(request, query, app_mode, workflow_input_key, workflow_session_key, files),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout_seconds=request_timeout_seconds,
        )
    except Exception as exc:
        return PluginExecutionResult(ok=False, status="error", message=f"Dify 请求失败: {exc}")

    http_status = int(response.get("http_status") or 200)
    if http_status >= 400:
        return PluginExecutionResult(
            ok=False,
            status="error",
            message=str(response.get("message") or response.get("error") or "Dify 返回异常"),
            output={"provider_response": response},
        )

    answer = _extract_text(response)
    replies = _build_replies(
        response,
        answer=answer,
        channel_type=str(request.payload.get("channel_type") or "").strip().lower(),
        timeout_seconds=request_timeout_seconds,
    )
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message="Dify 执行完成。",
        output={
            "mode": app_mode,
            "answer": answer,
            "files": files,
            "quote_debug": quote_debug,
            "provider_response": response,
            "replies": replies,
        },
    )


def _resolve_app_mode(config: dict) -> str:
    explicit = str(config.get("app_mode") or "").strip().lower()
    if explicit in {"chat", "workflow"}:
        return explicit
    if str(config.get("workflow_id") or "").strip():
        return "workflow"
    return "chat"


def _resolve_timeout_seconds(config: dict) -> int:
    raw = config.get("request_timeout_seconds")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SECONDS
    return max(5, value)


def _build_target_url(api_base: str, app_mode: str) -> str:
    if app_mode == "workflow":
        return f"{api_base}/workflows/run"
    return f"{api_base}/chat-messages"


def _build_body(
    request: PluginExecutionRequest,
    query: str,
    app_mode: str,
    workflow_input_key: str,
    workflow_session_key: str,
    files: list[dict[str, str]],
) -> dict:
    user = str(request.payload.get("user") or "multi-bot-admin")
    inputs = request.payload.get("inputs")
    if not isinstance(inputs, dict):
        inputs = {}
    if app_mode == "workflow":
        workflow_inputs = dict(inputs)
        workflow_inputs.setdefault(workflow_input_key, query)
        session_value = (
            request.payload.get("conversation_external_id")
            or request.payload.get("conversation_id")
            or request.payload.get("chat_id")
            or request.payload.get("room_id")
            or ""
        )
        if workflow_session_key and session_value:
            workflow_inputs.setdefault(workflow_session_key, session_value)
        return {
            "files": files,
            "inputs": workflow_inputs,
            "response_mode": str(request.payload.get("response_mode") or "blocking"),
            "user": user,
        }
    return {
        "query": query,
        "files": files,
        "inputs": inputs,
        "response_mode": str(request.payload.get("response_mode") or "blocking"),
        "conversation_id": request.payload.get("conversation_id") or "",
        "user": user,
    }


def _extract_text(response: dict) -> str:
    for key in ("answer", "text", "message"):
        value = response.get(key)
        if value not in (None, ""):
            return str(value)
    data = response.get("data")
    if isinstance(data, dict):
        for key in ("answer", "text", "message"):
            value = data.get(key)
            if value not in (None, ""):
                return str(value)
        for key in ("outputs", "output", "answer", "text"):
            value = data.get(key)
            if isinstance(value, dict):
                for nested_key in ("answer", "text", "result", "content"):
                    nested_value = value.get(nested_key)
                    if nested_value not in (None, ""):
                        return str(nested_value)
                if len(value) == 1:
                    only_value = next(iter(value.values()))
                    if only_value not in (None, ""):
                        return str(only_value)
                return json.dumps(value, ensure_ascii=False)
            if isinstance(value, list):
                collected: list[str] = []
                for item in value:
                    if isinstance(item, str) and item.strip():
                        collected.append(item.strip())
                    elif isinstance(item, dict):
                        for nested_key in ("answer", "text", "result", "content"):
                            nested_value = item.get(nested_key)
                            if nested_value not in (None, ""):
                                collected.append(str(nested_value))
                                break
                if collected:
                    return "\n".join(collected)
            if value not in (None, ""):
                return str(value)
    return ""


def _build_replies(
    response: dict,
    *,
    answer: str,
    channel_type: str,
    timeout_seconds: int,
) -> list[dict[str, str]]:
    replies: list[dict[str, str]] = []
    fallback_lines: list[str] = []
    media_items = _extract_media_items(response)
    for item in media_items:
        media_type = str(item.get("type") or "").strip().lower()
        media_url = str(item.get("url") or "").strip()
        if not media_url:
            continue
        if media_type == "image":
            image_reply = _build_image_reply(media_url, timeout_seconds=timeout_seconds)
            if image_reply:
                replies.append(image_reply)
                continue
            fallback_lines.append(f"图片地址：{media_url}")
            continue
        if media_type == "video":
            video_reply = _build_video_reply(media_url, channel_type=channel_type, timeout_seconds=timeout_seconds)
            if video_reply:
                replies.append(video_reply)
                continue
            fallback_lines.append(f"视频地址：{media_url}")

    text_parts: list[str] = []
    normalized_answer = str(answer or "").strip()
    if normalized_answer:
        text_parts.append(normalized_answer)
    if fallback_lines:
        text_parts.append("\n".join(fallback_lines))
    if text_parts:
        replies.extend(_text_reply("\n\n".join(part for part in text_parts if part)))
    return replies


def _extract_media_items(response: dict) -> list[dict[str, str]]:
    media_items: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    candidates: list[object] = [response]
    data = response.get("data")
    if isinstance(data, dict):
        candidates.append(data)
        for key in ("outputs", "output", "files", "artifacts"):
            value = data.get(key)
            if value not in (None, ""):
                candidates.append(value)

    for candidate in candidates:
        _collect_media_items(candidate, media_items, seen)
    return media_items


def _collect_media_items(candidate: object, media_items: list[dict[str, str]], seen: set[tuple[str, str]]) -> None:
    if isinstance(candidate, list):
        for item in candidate:
            _collect_media_items(item, media_items, seen)
        return

    if not isinstance(candidate, dict):
        return

    candidate_type = _resolve_media_type(candidate)
    if candidate_type:
        for key in ("result", "url", "download_url", "image", "image_url", "video", "video_url"):
            value = candidate.get(key)
            if isinstance(value, str):
                _append_media_item(media_items, seen, candidate_type, value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, str):
                        _append_media_item(media_items, seen, candidate_type, item)
                    elif isinstance(item, dict):
                        nested_url = _pick_url(item)
                        if nested_url:
                            nested_type = _resolve_media_type(item) or candidate_type
                            _append_media_item(media_items, seen, nested_type, nested_url)

    for key, value in candidate.items():
        if isinstance(value, dict):
            _collect_media_items(value, media_items, seen)
            continue
        if isinstance(value, list):
            _collect_media_items(value, media_items, seen)
            continue
        if not isinstance(value, str):
            continue
        inferred_type = _infer_media_type_from_key(key) or _infer_media_type_from_url(value)
        if inferred_type:
            _append_media_item(media_items, seen, inferred_type, value)


def _append_media_item(
    media_items: list[dict[str, str]],
    seen: set[tuple[str, str]],
    media_type: str,
    media_url: str,
) -> None:
    normalized_url = str(media_url or "").strip()
    normalized_type = str(media_type or "").strip().lower()
    if not normalized_url or not normalized_type:
        return
    if not normalized_url.startswith(("http://", "https://")):
        return
    key = (normalized_type, normalized_url)
    if key in seen:
        return
    seen.add(key)
    media_items.append({"type": normalized_type, "url": normalized_url})


def _resolve_media_type(candidate: dict) -> str | None:
    explicit = str(candidate.get("type") or candidate.get("mime_type") or candidate.get("mime") or "").strip().lower()
    if explicit.startswith("image"):
        return "image"
    if explicit.startswith("video"):
        return "video"
    if explicit in {"image", "img", "photo"}:
        return "image"
    if explicit in {"video", "movie"}:
        return "video"
    for key in candidate.keys():
        inferred = _infer_media_type_from_key(str(key))
        if inferred:
            return inferred
    return None


def _infer_media_type_from_key(key: str) -> str | None:
    normalized = str(key or "").strip().lower()
    if not normalized:
        return None
    if "image" in normalized or "img" in normalized or "photo" in normalized:
        return "image"
    if "video" in normalized or "movie" in normalized:
        return "video"
    return None


def _infer_media_type_from_url(value: str) -> str | None:
    normalized = str(value or "").strip().lower()
    if not normalized.startswith(("http://", "https://")):
        return None
    if any(token in normalized for token in (".png", ".jpg", ".jpeg", ".gif", ".webp")):
        return "image"
    if any(token in normalized for token in (".mp4", ".mov", ".m4v", ".webm", ".avi")):
        return "video"
    return None


def _pick_url(candidate: dict) -> str | None:
    for key in ("url", "download_url", "image", "image_url", "video", "video_url", "result"):
        value = candidate.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _build_image_reply(media_url: str, *, timeout_seconds: int) -> dict[str, str] | None:
    downloaded = _download_binary(media_url, timeout_seconds=timeout_seconds)
    if not downloaded["content"]:
        return None
    return {
        "type": "image",
        "base64": base64.b64encode(downloaded["content"]).decode("ascii"),
    }


def _build_video_reply(media_url: str, *, channel_type: str, timeout_seconds: int) -> dict[str, str] | None:
    if channel_type != "wechat_ipad":
        return None
    downloaded = _download_binary(media_url, timeout_seconds=timeout_seconds)
    video_bytes = downloaded["content"]
    if not video_bytes:
        return None
    cover_bytes = _extract_video_cover(video_bytes)
    if not cover_bytes:
        return None
    return {
        "type": "video",
        "video_base64": base64.b64encode(video_bytes).decode("ascii"),
        "image_base64": base64.b64encode(cover_bytes).decode("ascii"),
    }


def _download_binary(url: str, *, timeout_seconds: int) -> dict[str, object]:
    req = urlrequest.Request(
        url,
        method="GET",
        headers={"User-Agent": DEFAULT_USER_AGENT},
    )
    try:
        with urlrequest.urlopen(req, timeout=timeout_seconds) as response:
            return {
                "content": response.read(),
                "content_type": str(response.headers.get("Content-Type") or "").strip(),
            }
    except Exception:
        return {"content": b"", "content_type": ""}


def _extract_video_cover(video_bytes: bytes) -> bytes | None:
    if not video_bytes:
        return None
    source_path = ""
    target_path = ""
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as source_file:
            source_file.write(video_bytes)
            source_path = source_file.name
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as target_file:
            target_path = target_file.name
        command = [
            "ffmpeg",
            "-y",
            "-i",
            source_path,
            "-frames:v",
            "1",
            "-q:v",
            "2",
            target_path,
        ]
        completed = subprocess.run(
            command,
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )
        if completed.returncode != 0 or not os.path.exists(target_path):
            return None
        with open(target_path, "rb") as handle:
            cover_bytes = handle.read()
        return cover_bytes or None
    except Exception:
        return None
    finally:
        for path in (source_path, target_path):
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass


def _post_json(url: str, body: dict, *, headers: dict[str, str], timeout_seconds: int) -> dict:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urlrequest.Request(url, data=data, method="POST")
    for key, value in headers.items():
        req.add_header(key, value)
    try:
        with urlrequest.urlopen(req, timeout=timeout_seconds) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return _parse_json_like(raw, getattr(response, "status", 200))
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        return _parse_json_like(raw, exc.code)
    except URLError:
        raise


def _parse_json_like(raw: str, status: int) -> dict:
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {"http_status": status, "raw": raw}
    if isinstance(parsed, dict):
        parsed.setdefault("http_status", status)
        return parsed
    return {"http_status": status, "data": parsed}


def _resolve_query_and_files(
    request: PluginExecutionRequest,
    *,
    api_base: str,
    api_key: str,
    timeout_seconds: int,
) -> tuple[str, list[dict[str, str]], dict[str, object]]:
    query = _normalize_user_query(str(
        request.payload.get("command_args")
        or request.payload.get("stripped_query")
        or request.payload.get("stripped_content")
        or request.payload.get("query")
        or request.payload.get("content")
        or ""
    ))
    files = request.payload.get("files")
    normalized_files = files if isinstance(files, list) else []
    quote_payload = _extract_quote_payload(request.payload)
    quote_debug: dict[str, object] = {"quote_detected": bool(quote_payload)}
    if not quote_payload:
        return query, normalized_files, quote_debug

    ref_type = int(quote_payload.get("ref_type") or 0)
    ref_content = str(quote_payload.get("ref_content") or "").strip()
    title = str(quote_payload.get("title") or "").strip()
    quote_debug.update({"ref_type": ref_type, "title": title[:120]})
    if ref_type == 1 and ref_content:
        merged_query = _merge_quote_text_query(query=query, ref_content=ref_content)
        quote_debug["merged_text_quote"] = merged_query != query
        return merged_query, normalized_files, quote_debug

    if ref_type == 3:
        upload_id = _upload_quote_image_to_dify(
            request.payload,
            quote_payload,
            api_base=api_base,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
        )
        if upload_id:
            normalized_files = [
                *normalized_files,
                {
                    "type": "image",
                    "transfer_method": "local_file",
                    "upload_file_id": upload_id,
                },
            ]
            quote_debug["upload_file_id"] = upload_id
        title_query = _normalize_user_query(_strip_trigger_prefix(title, request.payload))
        if title_query:
            query = title_query
    return query, normalized_files, quote_debug


def _extract_quote_payload(payload: dict | object) -> dict[str, object] | None:
    if not isinstance(payload, dict):
        return None
    direct = payload.get("quote_payload")
    if isinstance(direct, dict):
        return direct
    normalized_event = payload.get("normalized_event")
    if isinstance(normalized_event, dict):
        details = normalized_event.get("details")
        if isinstance(details, dict):
            quote_payload = details.get("quote_payload")
            if isinstance(quote_payload, dict):
                return quote_payload
    return None


def _strip_trigger_prefix(title: str, payload: dict) -> str:
    text = str(title or "").strip()
    if not text:
        return ""
    command_prefix = str(payload.get("command_prefix") or "/").strip() or "/"
    command_name = str(payload.get("command_name") or "").strip()
    if command_name and text.startswith(f"{command_prefix}{command_name}"):
        return text[len(f"{command_prefix}{command_name}") :].strip()
    return text


def _normalize_user_query(text: str) -> str:
    normalized = str(text or "").strip()
    if not normalized:
        return ""
    normalized = normalized.replace("\u2005", " ")
    normalized = normalized.replace("@ ", "@")
    normalized = re.sub(r"@\S+\s*", "", normalized)
    return normalized.strip()


def _merge_quote_text_query(*, query: str, ref_content: str) -> str:
    current_query = _normalize_user_query(query)
    quoted_text = str(ref_content or "").strip()
    if not quoted_text:
        return current_query
    if not current_query:
        return quoted_text
    if current_query == quoted_text:
        return current_query
    if quoted_text in current_query:
        return current_query
    return f"我补充说明一下当前问题：{current_query}\n\n以下是我引用的原文，请结合它来回答：\n{quoted_text}"


def _upload_quote_image_to_dify(
    payload: dict,
    quote_payload: dict[str, object],
    *,
    api_base: str,
    api_key: str,
    timeout_seconds: int,
) -> str | None:
    image_info = quote_payload.get("image_info")
    if not isinstance(image_info, dict):
        return None
    wxid = _resolve_wechat_self_wxid(payload)
    channel_api_base = str(payload.get("channel_api_base") or "").strip()
    if not wxid or not channel_api_base:
        return None

    channel_stub = SimpleNamespace(api_base=channel_api_base)
    client = WechatIpadApiClient()
    download_result = client.download_image(
        channel_stub,
        wxid=wxid,
        file_aes_key=str(image_info.get("aeskey") or ""),
        file_no=str(image_info.get("url") or ""),
    )
    base64_payload = _extract_downloaded_image_base64(download_result)
    if not base64_payload:
        return None
    try:
        image_bytes = base64.b64decode(base64_payload, validate=True)
    except Exception:
        return None
    if not image_bytes:
        return None
    file_name = f"quote-{str(image_info.get('md5') or uuid.uuid4().hex)}.jpg"
    return _upload_file_to_dify(
        api_base=api_base,
        api_key=api_key,
        user=str(payload.get("user") or payload.get("sender_id") or "multi-bot-admin"),
        file_name=file_name,
        content_type="image/jpeg",
        file_bytes=image_bytes,
        timeout_seconds=timeout_seconds,
    )


def _resolve_wechat_self_wxid(payload: dict) -> str:
    normalized_event = payload.get("normalized_event")
    if isinstance(normalized_event, dict):
        details = normalized_event.get("details")
        if isinstance(details, dict):
            wxid = str(details.get("self_wxid") or "").strip()
            if wxid:
                return wxid
    channel_adapter_config = payload.get("channel_adapter_config")
    if isinstance(channel_adapter_config, dict):
        return str(channel_adapter_config.get("wxid") or "").strip()
    return ""


def _extract_downloaded_image_base64(response: dict) -> str:
    if not isinstance(response, dict):
        return ""
    candidate_paths = (
        ("Data", "Base64"),
        ("Data", "base64"),
        ("Base64",),
        ("base64",),
    )
    for path in candidate_paths:
        current = response
        for part in path:
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(part)
        if current not in (None, ""):
            return str(current).strip()
    return ""


def _upload_file_to_dify(
    *,
    api_base: str,
    api_key: str,
    user: str,
    file_name: str,
    content_type: str,
    file_bytes: bytes,
    timeout_seconds: int,
) -> str | None:
    boundary = f"----MultiBotAdmin{uuid.uuid4().hex}"
    body = bytearray()
    body.extend(f"--{boundary}\r\n".encode("utf-8"))
    body.extend(
        f'Content-Disposition: form-data; name="user"\r\n\r\n{user}\r\n'.encode("utf-8")
    )
    body.extend(f"--{boundary}\r\n".encode("utf-8"))
    body.extend(
        (
            f'Content-Disposition: form-data; name="file"; filename="{file_name}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("utf-8")
    )
    body.extend(file_bytes)
    body.extend(f"\r\n--{boundary}--\r\n".encode("utf-8"))

    req = urlrequest.Request(
        f"{api_base.rstrip('/')}/files/upload",
        data=bytes(body),
        method="POST",
    )
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    try:
        with urlrequest.urlopen(req, timeout=timeout_seconds) as response:
            raw = response.read().decode("utf-8", errors="replace")
            parsed = _parse_json_like(raw, getattr(response, "status", 200))
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        parsed = _parse_json_like(raw, exc.code)
    except URLError:
        return None

    if int(parsed.get("http_status") or 200) >= 400:
        return None
    if parsed.get("id"):
        return str(parsed["id"])
    data = parsed.get("data")
    if isinstance(data, dict):
        if data.get("id"):
            return str(data["id"])
        if data.get("file_id"):
            return str(data["file_id"])
    return None
