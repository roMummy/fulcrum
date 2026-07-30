# -*- coding: utf8 -*-
"""
提供HTTP请求的接口，方便不同的语言对接

1. 加载DLL文件，注入到打开的企微进程中
   - 如果已经打开了企微，直接注入当前企微进程
   - 如果没有，则使用DLL打开企微并注入

2. 开启HTTP服务器，即可向8989端口发送HTTP请求，执行对应操作
   - 请求路径为 /api，请求方法为 POST
   - 接收HTTP请求，并根据请求类型调用DLL文件的函数

3. 对于企微接收到的任何消息，如需监听聊天消息
   - GUI程序会在9000端口启动HTTP回调服务器
   - http_py.py会将消息推送到 http://127.0.0.1:9000/msg
   - GUI程序接收并显示这些回调消息
"""

import logging
import json
import sys
import os
import os.path
import inspect
import copy
import ctypes
import signal
import subprocess
import threading
from collections import deque
from threading import Event, Lock
import time
from datetime import datetime
from flask import Flask, request, jsonify
from functools import wraps
from ctypes import WinDLL, create_string_buffer, WINFUNCTYPE
from process_match import is_workwechat_window_candidate, workwechat_window_priority
from retry_guard import injection_retry_delay_remaining, parse_injection_retry_cooldown
# 进入当前目录
os.chdir(os.path.dirname(os.path.abspath(__file__)))


# 配置日志。打包后的 Windows GUI 进程 stdout 可能仍按 GBK 编码，遇到 emoji 会触发 logging error；
# 这里优先把标准输出切到 UTF-8，失败也不影响文件日志继续写入。
try:
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('wechat_http_service.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger('WeChatHttpService')


def is_64bit():
    return sys.maxsize > 2 ** 32


def is_process_running(process_name=None):
    """检测企业微信进程是否已经存在。

    Hook runtime 运行在独立 32 位进程里，不能依赖 UI 层的进程探测结果；
    这里用 tasklist 做一次本地确认，避免 InjectWeChat 失败后误判为“未启动”
    并继续调用会拉起新客户端的 InjectWeChatMultiOpen。
    """
    target_name = (process_name or os.environ.get('WORKWECHAT_PROCESS_NAME') or 'WXWork.exe').strip() or 'WXWork.exe'
    try:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        completed = subprocess.run(
            ['tasklist', '/FI', f'IMAGENAME eq {target_name}', '/NH'],
            capture_output=True,
            text=True,
            encoding='mbcs',
            errors='replace',
            timeout=5,
            check=False,
            creationflags=creationflags,
        )
        output = f"{completed.stdout or ''}\n{completed.stderr or ''}".lower()
        return target_name.lower() in output
    except Exception as e:
        # 探测失败时采用保守策略：认为进程存在，避免误启动一个新的企业微信。
        logger.warning(f"检测企业微信进程失败，跳过自动拉起新客户端: {e}")
        return True


def find_visible_workwechat_pid():
    """查找带主窗口的企业微信进程 PID。

    企业微信会同时存在 broker 和多个 `--switch-account` 子进程；泛化 InjectWxWork
    可能选中能响应账号查询、但不负责聊天窗口通知的路径。优先注入带主窗口的 PID，
    可以把 hook 绑定到当前客服实际操作的客户端实例。
    """
    if os.name != 'nt':
        return None

    target_name = (os.environ.get('WORKWECHAT_PROCESS_NAME') or 'WXWork.exe').lower()
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    candidates = []

    enum_windows_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    @enum_windows_proc
    def _enum_window(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        title = ""
        if length > 0:
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            title = buffer.value.strip()

        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return True

        try:
            process_handle = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not process_handle:
                return True
            path_buffer = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_ulong(len(path_buffer))
            ok = kernel32.QueryFullProcessImageNameW(process_handle, 0, path_buffer, ctypes.byref(size))
            kernel32.CloseHandle(process_handle)
            process_path = path_buffer.value if ok else ""
        except Exception:
            process_path = ""

        if is_workwechat_window_candidate(process_path, title, target_name):
            candidates.append({
                'pid': int(pid.value),
                'title': title,
                'path': process_path,
                'hwnd': int(hwnd),
            })
        return True

    try:
        user32.EnumWindows(_enum_window, None)
    except Exception as e:
        logger.warning(f"枚举企业微信主窗口失败: {e}")
        return None

    if not candidates:
        return None
    # 多窗口时优先选择标题明确为企业微信的主窗口，避免误选弹窗。
    candidates.sort(key=workwechat_window_priority)
    return candidates[0]


def c_string(data):
    """转换为C字符串（ANSI编码，和易语言版本一致）"""
    # 易语言使用ANSI编码，Python默认使用UTF-8
    # 但Windows API通常使用ANSI（GBK/CP936）或UTF-8
    # 先尝试UTF-8，如果失败再尝试ANSI
    try:
        return ctypes.c_char_p(data.encode('utf-8'))
    except UnicodeEncodeError:
        # 如果UTF-8失败，尝试使用系统默认编码（通常是GBK/CP936）
        return ctypes.c_char_p(data.encode('gbk'))


class MessageType:
    MT_DEBUG_LOG = 11024
    MT_USER_LOGIN = 11026
    MT_USER_LOGOUT = 11027
    MT_SEND_TEXTMSG = 11029
    MT_HEARTBEAT = 108019


class CallbackHandler:
    pass


class WeChatServiceHandler(CallbackHandler):
    """微信服务回调处理器"""

    def __init__(self, service):
        self.service = service
        self.connected_clients = set()

    @property
    def on_connect(self):
        """
        客户端连接回调

        易语言版本行为：
        ============
        易语言版本的 on_connect 回调：
        1. 保存 clientId 到全局变量：集_客户端 ＝ clientId
        2. 记录日志：logdebug ("客户端加入成功" ＋ 到文本 (clientId))
        3. 调试输出：调试输出 ("客户端加入成功", 集_客户端)

        Python版本保持一致：
        ==================
        1. 保存 clientId
        2. 记录日志
        """
        def _on_connect(client_id):
            self.connected_clients.add(client_id)
            # 🔍 关键：和易语言版本一致，保存 clientId 并记录日志
            # 易语言版本：集_客户端 ＝ clientId
            # 易语言版本：logdebug ("客户端加入成功" ＋ 到文本 (clientId))
            logger.info(f"✅ 客户端加入成功，socket client_id: {client_id}")

            # 🔍 关键：更新服务的 client_id（这是真正的socket客户端ID）
            # InjectWeChat 返回的是进程ID（PID），不是socket客户端ID
            # 真正的 client_id 应该从 on_connect 回调中获取
            old_client_id = self.service.client_id
            self.service.mark_client_connected(client_id)
            logger.info(f"   更新 client_id: {old_client_id} -> {client_id}")

            # 连接成功，更新心跳时间（用于统计，不做断连检测）
            self.service.last_heartbeat = time.time()
        return _on_connect

    @property
    def on_receive(self):
        """接收消息回调"""
        def _on_receive(client_id, message_type, data):
            # 更新心跳时间（任何消息都算心跳）
            self.service.last_heartbeat = time.time()

            # 保存进程ID（从消息中获取，和易语言版本一致）
            # 易语言版本会从消息中提取 pid 并保存
            if isinstance(data, dict) and 'pid' in data:
                pid = data.get('pid')
                if pid and pid != self.service.wechat_pid:
                    self.service.wechat_pid = pid
                    logger.debug(f"更新企业微信进程ID: {pid}")

            # 处理不同类型的消息
            if message_type == MessageType.MT_HEARTBEAT:
                logger.debug(f"💓 收到心跳消息（108019）- 保持连接活跃")
            elif message_type == MessageType.MT_USER_LOGIN:
                # 🔍 关键：用户登录成功（11026消息）
                # DLL会自动处理登录恢复，不需要手动保存映射关系
                logger.info(f"用户登录: {data}")
                # 保存当前登录账号的 user_id，用于过滤自己发送的消息
                if isinstance(data, dict) and 'user_id' in data:
                    self.service.current_user_id = data.get('user_id')
                    logger.info(f"保存当前登录账号 user_id: {self.service.current_user_id}")
            elif message_type == MessageType.MT_USER_LOGOUT:
                logger.info(f"用户登出: {data}")
            elif message_type == MessageType.MT_DEBUG_LOG:
                # type=11024 是接口就绪通知
                logger.info(f"✅ 接口就绪通知: {data}")
                # 设置接口就绪标志
                self.service.api_ready = True
                logger.info("接口已就绪，可以发送消息了")

            # 🔍 关键：检查是否有等待此消息类型的请求
            # 如果有，将响应数据存储起来并通知等待的线程
            is_api_response = False
            with self.service.request_lock:
                # 清理超时的请求（超过10秒的请求）
                current_time = time.time()
                expired_requests = []
                for req_id, req_info in list(self.service.pending_requests.items()):
                    try:
                        timeout_threshold = max(10.0, float(req_info.get('timeout_seconds') or 10.0))
                    except (TypeError, ValueError):
                        timeout_threshold = 10.0
                    if current_time - req_info.get('timestamp', 0) > timeout_threshold:
                        expired_requests.append(req_id)

                # 清理超时的请求
                for req_id in expired_requests:
                    logger.debug(f"清理超时的请求: request_id={req_id}")
                    self.service.pending_requests.pop(req_id, None)

                # 对于 CDN 下载响应（11170/11171），尝试使用 save_path 进行精确匹配。
                # 企微文件下载可能并发触发，仅按 type 匹配容易把 A 文件的响应交给 B 请求。
                response_save_path = None
                if message_type in CDN_DOWNLOAD_TYPES and isinstance(data, dict):
                    response_save_path = data.get('save_path')

                # 查找等待此消息类型的请求
                matched_request_id = None
                for request_id, request_info in list(self.service.pending_requests.items()):
                    expected_type = request_info.get('expected_type')
                    if expected_type == message_type:
                        # 如果是 CDN 下载响应，尝试使用 save_path 进行精确匹配。
                        if message_type in CDN_DOWNLOAD_TYPES and response_save_path:
                            request_save_path = request_info.get('save_path')
                            if request_save_path and request_save_path == response_save_path:
                                # 精确匹配成功
                                matched_request_id = request_id
                                logger.debug(f"找到等待的请求 {request_id}，类型 {message_type}，save_path精确匹配: {response_save_path}")
                                break
                            elif not request_save_path:
                                # 请求没有save_path，使用第一个匹配的（向后兼容）
                                if matched_request_id is None:
                                    matched_request_id = request_id
                        else:
                            # 非CDN下载响应，使用第一个匹配的
                            matched_request_id = request_id
                            logger.debug(f"找到等待的请求 {request_id}，类型 {message_type}")
                            break

                # 如果找到匹配的请求，设置响应
                if matched_request_id is not None:
                    # 检查请求是否还存在（可能已经超时被清理）
                    if matched_request_id in self.service.pending_requests:
                        request_info = self.service.pending_requests[matched_request_id]
                        request_info['response'] = {
                            'type': message_type,
                            'data': data
                        }
                        request_info['event'].set()  # 通知等待的线程
                        is_api_response = True  # 标记这是API响应
                    else:
                        # 请求已超时被清理，响应到达太晚，记录日志但不报错
                        logger.debug(f"响应到达时请求已超时被清理: request_id={matched_request_id}, type={message_type}")

            # 🔍 关键：只推送"通知同步事件"下的消息相关通知，不推送API调用的响应
            # 根据API.txt文档，只推送消息相关的通知类型：
            #   11041: 文本消息
            #   11042: 图片消息
            #   11047: 链接消息
            #   11048: GIF消息
            #   11045: 文件消息
            #   11043: 视频消息
            #   11050: 名片消息
            #   11066: 小程序消息
            #   11124: 视频号消息
            #   11049: 红包消息
            #   11044: 语音消息
            #   11046: 位置消息
            #   11068: 图文消息
            #   11123: 撤回消息通知
            #   11195: 视频号直播消息
            #   11211: 聊天记录
            NOTIFICATION_MESSAGE_TYPES = {
                # 消息相关（只推送这些）
                11041,  # 文本消息
                11042,  # 图片消息
                11047,  # 链接消息
                11048,  # GIF消息
                11045,  # 文件消息
                11043,  # 视频消息
                11050,  # 名片消息
                11066,  # 小程序消息
                11124,  # 视频号消息
                11049,  # 红包消息
                11044,  # 语音消息
                11046,  # 位置消息
                11068,  # 图文消息
                11123,  # 撤回消息通知
                11195,  # 视频号直播消息
                11211,  # 聊天记录
            }

            # 如果配置了消息推送URL，且这不是API响应，且是"通知同步事件"类型，则发送HTTP POST请求到外部服务
            # 原始数据包格式：{"data":{...},"type":xxx} - 保持原始顺序
            if is_api_response:
                logger.debug(f"消息类型 {message_type} 是API响应，不推送到GUI")
                return _on_receive
            elif message_type not in NOTIFICATION_MESSAGE_TYPES:
                logger.debug(f"消息类型 {message_type} 不是消息通知类型，不推送到GUI")
                return _on_receive

            # 旧逻辑会直接丢弃当前登录账号自己发出的消息，导致客服在群里手动发言无法同步到云端。
            # 现在默认推送这类消息，但显式打上 is_self_sent 标记，服务端会按出站消息落库并跳过插件派发。
            if self.service.current_user_id and isinstance(data, dict):
                sender = data.get('sender')
                if sender == self.service.current_user_id:
                    if not self.service.push_self_messages:
                        logger.debug(f"消息发送者是当前账号 ({sender})，配置为不推送")
                        return _on_receive
                    data = dict(data)
                    data['is_self_sent'] = True
                    data['self_user_id'] = self.service.current_user_id
                    logger.debug(f"消息发送者是当前账号 ({sender})，按自发消息推送")

            # 如果配置了消息推送URL，则推送消息到GUI
            if self.service.msg_push_url:
                try:
                    import requests
                    from requests.exceptions import ConnectionError as RequestsConnectionError
                    import json as json_module
                    from collections import OrderedDict
                    # 使用OrderedDict确保顺序：data在前，type在后（和原始数据包格式一致）
                    payload_dict = OrderedDict([
                        ('data', data),
                        ('type', message_type)
                    ])
                    # 序列化为JSON字符串，确保顺序正确
                    json_str = json_module.dumps(payload_dict, ensure_ascii=False, separators=(',', ':'))
                    logger.info(f"📤 推送企微新消息到GUI (类型: {message_type}): {self.service.msg_push_url}")
                    logger.debug(f"推送JSON字符串: {json_str[:200]}...")  # 只显示前200字符

                    # 记录推送开始时间
                    push_start = time.time()

                    # 使用Session发送请求（保持连接，避免每次建立新连接）
                    if self.service.http_session:
                        response = self.service.http_session.post(
                            self.service.msg_push_url,
                            data=json_str.encode('utf-8'),
                            headers={'Content-Type': 'application/json; charset=utf-8'},
                            timeout=5
                        )
                    else:
                        # 如果没有Session，回退到普通requests.post
                        response = requests.post(
                            self.service.msg_push_url,
                            data=json_str.encode('utf-8'),
                            headers={'Content-Type': 'application/json; charset=utf-8'},
                            timeout=5
                        )

                    # 计算推送耗时
                    push_time = (time.time() - push_start) * 1000
                    self.service.last_push_result = {
                        'ok': response.status_code == 200,
                        'type': message_type,
                        'status_code': response.status_code,
                        'url': self.service.msg_push_url,
                        'elapsed_ms': round(push_time, 2),
                        'at': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    }
                    logger.info(f"✅ 消息推送成功: {message_type} -> {self.service.msg_push_url}, 响应状态: {response.status_code}, 耗时: {push_time:.2f}ms")
                    if response.status_code != 200:
                        logger.warning(f"GUI返回异常状态码: {response.status_code}, 响应内容: {response.text}")
                except ImportError:
                    logger.warning("requests库未安装，无法推送消息。请安装: pip install requests")
                except (RequestsConnectionError, OSError) as e:
                    # GUI未打开时连接被拒绝，静默跳过，不记录错误
                    # 常见的连接错误：ConnectionRefusedError, NewConnectionError 等
                    error_str = str(e)
                    if '10061' in error_str or 'refused' in error_str.lower() or 'actively refused' in error_str.lower():
                        # GUI未打开，静默跳过（使用debug级别，不显示在INFO日志中）
                        logger.debug(f"GUI未打开，跳过消息推送 (类型: {message_type})")
                    else:
                        # 其他连接错误，记录警告但不打印堆栈
                        logger.warning(f"消息推送连接失败: {e}")
                except Exception as e:
                    # 其他异常（非连接错误），记录警告但不打印堆栈
                    self.service.last_push_result = {
                        'ok': False,
                        'type': message_type,
                        'url': self.service.msg_push_url,
                        'error': str(e),
                        'at': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    }
                    logger.warning(f"消息推送失败: {e}")

        return _on_receive

    @property
    def on_close(self):
        """
        客户端断开回调

        易语言版本行为：
        ============
        易语言版本的 on_close 回调只是记录日志，完全没有重连逻辑！
        代码：logdebug ("客户端关闭" ＋ 到文本 (clientId))

        这说明：
        1. 易语言版本完全依赖DLL的自动重连机制
        2. 或者易语言版本真的不会断连（心跳消息处理得好）
        3. Python版本也应该保持一致，只记录日志，不做重连
        """
        def _on_close(client_id):
            self.connected_clients.discard(client_id)
            logger.info(f"客户端关闭 {client_id}")
            # 连接关闭后必须同步清理服务层状态，否则 HTTP API 会继续拿已经失效的
            # socket client_id 调用 SendWeChatData，表现为反复 500 且无法自恢复。
            self.service.mark_client_closed(client_id)

        return _on_close


# 全局回调列表
_GLOBAL_CONNECT_CALLBACK_LIST = []
_GLOBAL_RECV_CALLBACK_LIST = []
_GLOBAL_CLOSE_CALLBACK_LIST = []


@WINFUNCTYPE(None, ctypes.c_void_p)
def wechat_connect_callback(client_id):
    for func in _GLOBAL_CONNECT_CALLBACK_LIST:
        func(client_id)


@WINFUNCTYPE(None, ctypes.c_long, ctypes.c_char_p, ctypes.c_ulong)
def wechat_recv_callback(client_id, data, length):
    """接收消息回调"""
    try:
        data = copy.deepcopy(data)
        json_data = data.decode('utf-8', errors='ignore')

        try:
            dict_data = json.loads(json_data)
        except json.JSONDecodeError as e:
            logger.warning(f"⚠️  JSON解析失败: {json_data[:100]}... 错误: {e}")
            if '"type":108019' in json_data or '"type": 108019' in json_data or '108019' in json_data:
                dict_data = {'type': 108019, 'data': {}}
                logger.debug("检测到心跳消息（108019），已手动处理")
            else:
                logger.error(f"❌ 无法解析的消息: {json_data[:200]}")
                return

        if service:
            service.record_raw_callback(client_id, dict_data.get('type'), dict_data.get('data', {}))

        # 🔍 打印接收到的原始数据包（方便检查驱动接收到了什么信息）
        # 当前账号自己发出的消息也会被回调到这里；为了避免刷屏，只降低日志噪音，不再影响后续推送。
        should_print = True
        if service and service.current_user_id:
            message_data = dict_data.get('data', {})
            if isinstance(message_data, dict):
                sender = message_data.get('sender')
                if sender == service.current_user_id:
                    should_print = False

        if should_print:
            logger.info(f"📨 原始数据包: {json_data}")

        for func in _GLOBAL_RECV_CALLBACK_LIST:
            try:
                func(client_id, dict_data['type'], dict_data.get('data', {}))
            except Exception as e:
                logger.error(f"回调函数执行失败: {e}")

    except Exception as e:
        logger.error(f"❌ wechat_recv_callback 异常: {e}")


@WINFUNCTYPE(None, ctypes.c_ulong)
def wechat_close_callback(client_id):
    for func in _GLOBAL_CLOSE_CALLBACK_LIST:
        func(client_id)


def add_callback_handler(callbackHandler):
    for dummy, handler in inspect.getmembers(callbackHandler, callable):
        if hasattr(handler, '_wx_connect_handled'):
            _GLOBAL_CONNECT_CALLBACK_LIST.append(handler)
        elif hasattr(handler, '_wx_recv_handled'):
            _GLOBAL_RECV_CALLBACK_LIST.append(handler)
        elif hasattr(handler, '_wx_close_handled'):
            _GLOBAL_CLOSE_CALLBACK_LIST.append(handler)


# 🔍 WeChatDataManager 已删除，DLL会自动处理登录恢复，不需要手动管理数据目录

class NoveLoader:
    """DLL加载器"""
    loader_module_base: int = 0

    # 偏移
    _InitWeChatSocket: int = 0x4B10
    _GetUserWeChatVersion: int = 0x4B70
    _InjectWeChat: int = 0x4BF0
    _SendWeChatData: int = 0x5800
    _DestroyWeChat: int = 0x5310
    _UseUtf8: int = 0x4A60
    _InjectWeChatPid: int = 0x50D0
    _InjectWeChatMultiOpen: int = 0x4E80
    # _SetDataLocationPath: int = 0x4B20  # 已删除，不再设置数据目录

    def __init__(self, loader_path: str):
        loader_path = os.path.realpath(loader_path)
        if not os.path.exists(loader_path):
            logger.error('libs path error or loader not exist')
            return

        loader_module = WinDLL(loader_path)
        self.loader_module_base = loader_module._handle

        # 使用utf8编码
        self.UseUtf8()

        # 初始化接口回调
        self.InitWeChatSocket(wechat_connect_callback, wechat_recv_callback, wechat_close_callback)

    def __get_non_exported_func(self, offset: int, arg_types, return_type):
        func_addr = self.loader_module_base + offset
        if arg_types:
            func_type = ctypes.WINFUNCTYPE(return_type, *arg_types)
        else:
            func_type = ctypes.WINFUNCTYPE(return_type)
        return func_type(func_addr)

    def InitWeChatSocket(self, connect_callback, recv_callback, close_callback):
        func = self.__get_non_exported_func(
            self._InitWeChatSocket,
            [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p],
            ctypes.c_bool
        )
        return func(connect_callback, recv_callback, close_callback)

    def GetUserWeChatVersion(self) -> str:
        func = self.__get_non_exported_func(self._GetUserWeChatVersion, None, ctypes.c_bool)
        out = create_string_buffer(20)
        if func(out):
            return out.value.decode('utf-8')
        else:
            return ''

    def InjectWeChat(self, dll_path: str) -> ctypes.c_uint32:
        func = self.__get_non_exported_func(self._InjectWeChat, [ctypes.c_char_p], ctypes.c_uint32)
        return func(c_string(dll_path))

    def SendWeChatData(self, client_id: int, message: str) -> ctypes.c_bool:
        """
        发送消息到企微

        参数:
        - client_id: 客户端ID（从注入返回或连接回调中获取）
        - message: JSON格式的消息字符串

        返回:
        - True: 发送成功
        - False: 发送失败

        注意: 根据回调函数的签名，client_id 应该是 ctypes.c_long 或 ctypes.c_ulong
        """
        # 尝试两种类型：c_ulong 和 c_uint32
        # 根据回调函数，client_id 在回调中使用的是 c_long 或 c_ulong
        func_ulong = self.__get_non_exported_func(
            self._SendWeChatData,
            [ctypes.c_ulong, ctypes.c_char_p],
            ctypes.c_bool
        )

        func_uint32 = self.__get_non_exported_func(
            self._SendWeChatData,
            [ctypes.c_uint32, ctypes.c_char_p],
            ctypes.c_bool
        )

        # 转换消息字符串
        message_bytes = c_string(message)

        # 先尝试使用 c_ulong（和回调函数一致）
        try:
            client_id_ulong = ctypes.c_ulong(client_id).value
            logger.debug(f"尝试使用 c_ulong: client_id={client_id} -> {client_id_ulong}")
            result = func_ulong(client_id_ulong, message_bytes)
            if result:
                logger.debug("✅ 使用 c_ulong 发送成功")
                return result
            else:
                logger.debug("⚠️  使用 c_ulong 返回 False，尝试 c_uint32...")
        except Exception as e:
            logger.debug(f"使用 c_ulong 异常: {e}，尝试 c_uint32...")

        # 如果 c_ulong 失败，尝试 c_uint32
        try:
            client_id_uint32 = ctypes.c_uint32(client_id).value
            logger.debug(f"尝试使用 c_uint32: client_id={client_id} -> {client_id_uint32}")
            result = func_uint32(client_id_uint32, message_bytes)
            return result
        except Exception as e:
            logger.error(f"❌ SendWeChatData 调用异常: {e}")
            logger.error(f"  - client_id: {client_id}")
            logger.error(f"  - message: {message[:200]}...")
            logger.error(f"  - message_bytes type: {type(message_bytes)}")
            raise

    def DestroyWeChat(self) -> ctypes.c_bool:
        func = self.__get_non_exported_func(self._DestroyWeChat, None, ctypes.c_bool)
        return func()

    def UseUtf8(self):
        func = self.__get_non_exported_func(self._UseUtf8, None, ctypes.c_bool)
        return func()

    def InjectWeChatPid(self, pid: int, dll_path: str) -> ctypes.c_uint32:
        func = self.__get_non_exported_func(
            self._InjectWeChatPid,
            [ctypes.c_uint32, ctypes.c_char_p],
            ctypes.c_uint32
        )
        return func(pid, c_string(dll_path))

    def InjectWeChatMultiOpen(self, dll_path: str, exe_path: str = "") -> ctypes.c_uint32:
        """
        多开一个新的企业微信并注入（和易语言版本一致）

        参数：
        - dll_path: WeChat_5.0.0.6008.dll 路径
        - exe_path: WXWork.exe 路径，如果为空则自动读取企业微信安装目录
        """
        func = self.__get_non_exported_func(
            self._InjectWeChatMultiOpen,
            [ctypes.c_char_p, ctypes.c_char_p],
            ctypes.c_uint32
        )
        return func(c_string(dll_path), c_string(exe_path) if exe_path else c_string(""))

    # 🔍 SetDataLocationPath 已删除，直接使用工程目录（根目录）


class WeChatService:
    """微信服务管理器"""

    def __init__(self, loader_path: str, dll_path: str):
        self.loader_path = loader_path
        self.dll_path = dll_path
        self.loader = None
        self.handler = None
        self.is_running = False
        self.should_stop = False
        self.client_id = None
        self.wechat_pid = None
        self.last_heartbeat = time.time()
        # 记录当前 DLL socket 连接是否有效；InjectWeChat 返回的是 PID，真正可发送
        # 消息的 client_id 必须以后续 on_connect 回调为准。
        self.connected_client_ids = set()
        # 保护注入/重连过程，避免多个 HTTP 请求同时触发 DLL 注入。
        self.connection_lock = Lock()
        # 首次注入到 socket 连接建立之间存在几秒窗口；健康检查不能在这个窗口里触发第二次注入。
        self.injection_in_progress = False
        self.last_injected_at = 0.0
        self.last_injected_pid = None
        self.last_injection_method = ""
        self.last_pid_candidate = None
        self.last_injection_error = ""
        # 注入失败后进入短冷却，避免 /health 每 3 秒触发一次 DLL 注入和 tasklist 探测。
        self.injection_retry_cooldown_seconds = parse_injection_retry_cooldown(
            os.environ.get('WORKWECHAT_INJECTION_RETRY_COOLDOWN_SECONDS')
        )
        self.last_injection_attempt_at = 0.0
        self.last_injection_failed_at = 0.0
        self.last_injection_cooldown_log_at = 0.0
        self.msg_push_url = None  # 消息推送URL（9000端口）
        self.api_ready = False  # 接口就绪标志（收到type=11024后设置为True）
        # 🔍 请求-响应机制：存储等待响应的请求
        self.pending_requests = {}  # {request_id: {'event': Event, 'response': dict, 'timestamp': float}}
        self.request_lock = Lock()  # 保护 pending_requests 的锁
        self.request_counter = 0  # 请求计数器
        self.current_user_id = None  # 当前登录账号的 user_id
        # 最近原始回调用于现场排障：只存摘要，避免大文件/图片消息把状态接口撑爆。
        self.raw_callback_counts = {}
        self.recent_raw_callbacks = deque(maxlen=30)
        self.last_raw_callback_at = 0.0
        self.last_notification_at = 0.0
        self.last_notification_type = None
        self.last_push_result = {}
        # 默认同步客服手动在群里发的消息；如需保持旧行为，可用环境变量 WORKWECHAT_PUSH_SELF_MESSAGES=0 关闭。
        self.push_self_messages = os.environ.get('WORKWECHAT_PUSH_SELF_MESSAGES', '1').strip().lower() not in {'0', 'false', 'no'}
        # 使用Session保持HTTP连接，避免每次请求都建立新连接
        try:
            import requests
            self.http_session = requests.Session()
            # 设置连接池大小和超时
            # 增加连接池大小以支持并发请求（多个消息同时推送）
            adapter = requests.adapters.HTTPAdapter(pool_connections=1, pool_maxsize=10)
            self.http_session.mount('http://', adapter)
        except ImportError:
            self.http_session = None

    def initialize(self):
        """初始化服务"""
        try:
            logger.info("正在初始化微信服务...")

            if is_64bit():
                logger.error("检测到64位Python，但DLL是32位的。请使用32位Python运行此程序。")
                return False

            if not os.path.exists(self.loader_path):
                logger.error(f"Loader DLL 文件不存在: {self.loader_path}")
                return False

            if not os.path.exists(self.dll_path):
                logger.error(f"Helper DLL 文件不存在: {self.dll_path}")
                return False

            self.loader = NoveLoader(self.loader_path)
            if not self.loader:
                logger.error("创建 NoveLoader 失败")
                return False

            self.handler = WeChatServiceHandler(self)
            _GLOBAL_CONNECT_CALLBACK_LIST.append(self.handler.on_connect)
            _GLOBAL_RECV_CALLBACK_LIST.append(self.handler.on_receive)
            _GLOBAL_CLOSE_CALLBACK_LIST.append(self.handler.on_close)

            # 🔍 关键：不再设置数据目录，直接使用工程目录（根目录）
            # 企微会自动在工程目录下创建数据目录
            logger.info("微信服务初始化成功（使用工程目录作为数据目录）")
            return True

        except Exception as e:
            logger.error(f"初始化微信服务失败: {e}")
            return False

    def start(self):
        """
        启动服务

        DLL会自动处理登录恢复（因为数据目录已经指定了），不需要手动指定user_id
        """
        if not self.initialize():
            return False

        self.is_running = True
        self.should_stop = False

        try:
            # 🔍 关键：注入逻辑（不支持多开）
            # 1. 如果企微已打开，直接注入当前企微进程
            # 2. 如果没有打开，则DLL打开并注入
            #
            # 易语言版本使用 InjectWeChatMultiOpen，它会启动企微并注入
            # 先尝试 InjectWeChat（如果企微已打开）
            # 如果失败，再使用 InjectWeChatMultiOpen（启动企微并注入）

            injected_pid = self.inject_wechat()
            if injected_pid:
                logger.info(f"注入成功，等待企微初始化...")
                time.sleep(2)  # 等待2秒让企微完全启动

                logger.info(f"✅ 注入成功，进程ID（PID）: {injected_pid}")
                logger.info(f"   等待 socket 连接回调获取真正的 client_id...")

                # 启动主服务循环
                self.run_service()
                return True
            else:
                logger.error(f"注入微信失败，client_id = {self.client_id}")
                logger.error("可能的原因：")
                logger.error("   1. DLL文件路径不正确")
                logger.error("   2. 企微版本不匹配")
                logger.error("   3. 权限不足（需要管理员权限）")
                logger.error("   4. 企微进程异常")
                return False

        except Exception as e:
            logger.error(f"启动微信服务失败: {e}", exc_info=True)
            return False

    def inject_wechat(self):
        """执行一次 DLL 注入，并返回企微进程 PID。

        Loader 的 InjectWeChat/InjectWeChatMultiOpen 返回值是进程 PID，不是后续
        SendWeChatData 需要使用的 socket client_id；真正的 client_id 会由
        on_connect 回调写入。
        """
        if not self.loader:
            logger.error("Loader 未初始化，无法注入企业微信")
            return None

        with self.connection_lock:
            self.injection_in_progress = True
            self.last_injection_attempt_at = time.time()
            self.last_injection_error = ""
            logger.info("正在尝试注入微信...")
            logger.info(f"   - DLL路径: {self.dll_path}")

            # 新一轮注入前清理旧连接状态，避免 PID 被误当作 socket client_id 使用。
            self.client_id = None
            self.connected_client_ids.clear()

            try:
                injected_pid = None
                prefer_pid_injection = os.environ.get('WORKWECHAT_PREFER_PID_INJECTION', '1').strip().lower() not in {'0', 'false', 'no'}
                pid_candidate = find_visible_workwechat_pid() if prefer_pid_injection else None
                self.last_pid_candidate = pid_candidate
                if pid_candidate and pid_candidate.get('pid'):
                    logger.info(
                        f"   - 步骤1: 尝试 InjectWeChatPid 注入可见企业微信窗口 "
                        f"PID={pid_candidate.get('pid')}, title={pid_candidate.get('title')}"
                    )
                    time.sleep(1)
                    injected_pid = self.loader.InjectWeChatPid(int(pid_candidate['pid']), self.dll_path)
                    self.last_injection_method = "InjectWeChatPid"
                    logger.info(f"   - InjectWeChatPid 返回值: {injected_pid}")

                if not injected_pid or injected_pid == 0:
                    logger.info("   - 步骤2: 尝试 InjectWeChat（如果企微已打开）...")
                    time.sleep(1)  # 延迟1秒，和易语言版本一致
                    injected_pid = self.loader.InjectWeChat(self.dll_path)
                    self.last_injection_method = "InjectWeChat"
                    logger.info(f"   - InjectWeChat 返回值: {injected_pid}")

                if not injected_pid or injected_pid == 0:
                    if is_process_running():
                        self.record_injection_failure("inject_existing_process_failed")
                        logger.error("   - 已检测到企业微信进程，但 InjectWeChat 返回 0；为避免多开，不再调用 InjectWeChatMultiOpen")
                        logger.error("   - 请检查 provider/hook 是否与企业微信同权限运行，或重启企业微信后再试")
                        return None
                    logger.info("   - 未检测到企业微信进程，尝试 InjectWeChatMultiOpen（启动企微并注入）...")
                    logger.info("   - 步骤2: 使用 InjectWeChatMultiOpen 启动并注入企业微信...")
                    injected_pid = self.loader.InjectWeChatMultiOpen(self.dll_path, "")
                    logger.info(f"   - InjectWeChatMultiOpen 返回值: {injected_pid}")

                if injected_pid and injected_pid != 0:
                    self.wechat_pid = injected_pid
                    self.last_injected_pid = injected_pid
                    self.last_injected_at = time.time()
                    self.last_injection_failed_at = 0.0
                    self.last_injection_error = ""
                    return injected_pid
                self.record_injection_failure("inject_returned_zero")
                return None
            finally:
                self.injection_in_progress = False

    def record_injection_failure(self, error_code):
        """记录注入失败时间，供健康检查节流使用。"""
        self.last_injection_error = error_code
        self.last_injection_failed_at = time.time()

    def injection_retry_remaining_seconds(self):
        """返回当前注入冷却剩余秒数；为 0 表示可以再次尝试注入。"""
        return injection_retry_delay_remaining(
            self.last_injection_failed_at,
            time.time(),
            self.injection_retry_cooldown_seconds,
        )

    def should_skip_injection_retry(self):
        """判断本次健康检查是否应跳过注入重试，并对日志做节流。"""
        remaining = self.injection_retry_remaining_seconds()
        if remaining <= 0:
            return False
        now = time.time()
        if now - self.last_injection_cooldown_log_at >= 5:
            logger.warning(
                f"上次注入失败，{remaining:.1f} 秒后再重试；"
                f"error={self.last_injection_error or 'unknown'}"
            )
            self.last_injection_cooldown_log_at = now
        return True

    def has_active_client(self):
        """判断当前 client_id 是否仍在 DLL 回调报告的已连接集合中。"""
        return bool(self.client_id and self.client_id in self.connected_client_ids)

    def wait_for_active_client(self, timeout_seconds=8):
        """等待首次注入后的 socket 连接回调，避免健康检查抢跑导致重复注入。"""
        deadline = time.time() + max(1, timeout_seconds)
        while time.time() < deadline:
            if self.has_active_client():
                return True
            time.sleep(0.2)
        return self.has_active_client()

    def mark_client_connected(self, client_id):
        """记录 DLL socket 连接成功，并把该 client_id 作为当前发送目标。"""
        self.connected_client_ids.add(client_id)
        self.client_id = client_id
        self.last_heartbeat = time.time()

    def mark_client_closed(self, client_id):
        """记录 DLL socket 断开；如果断开的正是当前连接，则立即清空。"""
        self.connected_client_ids.discard(client_id)
        if self.client_id == client_id:
            logger.warning(f"当前 socket client_id 已断开，清理连接状态: {client_id}")
            self.client_id = None
            self.api_ready = False

    def ensure_connected(self, timeout_seconds=8):
        """确保存在可用 socket client；无连接时尝试重新注入并等待 on_connect。"""
        if self.has_active_client():
            return True

        # 首次注入刚完成时，DLL 还需要异步回调 on_connect；这里先等待，不要立即二次注入。
        if self.injection_in_progress or (self.last_injected_at and time.time() - self.last_injected_at < timeout_seconds):
            logger.warning("注入刚完成但 socket 尚未连接，先等待连接回调，避免重复注入")
            return self.wait_for_active_client(timeout_seconds)

        if self.should_skip_injection_retry():
            return False

        logger.warning("当前没有有效 socket client，准备重新注入企业微信")
        injected_pid = self.inject_wechat()
        if not injected_pid:
            return False

        if self.wait_for_active_client(timeout_seconds):
            return True

        self.record_injection_failure("socket_connect_timeout")
        logger.error("重新注入后仍未收到 socket 连接回调")
        return False

    def record_raw_callback(self, client_id, message_type, data):
        """记录 DLL 回调摘要，帮助判断真实聊天消息是否到达本地 hook。"""
        now = time.time()
        self.last_raw_callback_at = now
        type_key = str(message_type)
        self.raw_callback_counts[type_key] = int(self.raw_callback_counts.get(type_key, 0)) + 1
        is_notification = message_type in {
            11041, 11042, 11047, 11048, 11045, 11043, 11050, 11066,
            11124, 11049, 11044, 11046, 11068, 11123, 11195, 11211,
            11063, 11076, 11077,
        }
        if is_notification:
            self.last_notification_at = now
            self.last_notification_type = message_type
        data_summary = {}
        if isinstance(data, dict):
            for key in ("conversation_id", "sender", "sender_name", "receiver", "send_time", "local_id", "server_id", "pid"):
                if key in data:
                    data_summary[key] = data.get(key)
            content = str(data.get("content") or "")
            if content:
                data_summary["content_preview"] = content[:120]
        self.recent_raw_callbacks.appendleft({
            "at": datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S"),
            "client_id": client_id,
            "type": message_type,
            "is_notification": is_notification,
            "data": data_summary,
        })

    def run_service(self):
        """
        运行服务主循环

        易语言版本行为：
        ============
        易语言版本没有主循环检测断开的逻辑！
        易语言版本完全依赖DLL的回调机制：
        1. 客户端加入回调：记录日志，保存 clientId
        2. 客户端关闭回调：只记录日志，不做任何操作
        3. 客户端发送消息回调：处理消息

        Python版本保持一致：
        ==================
        1. 只运行主循环，保持程序运行
        2. 不做任何断连检测和重连操作
        3. 完全依赖DLL的自动重连机制（如果DLL有的话）
        """
        logger.info("微信服务已启动，正在运行...")
        logger.info("=" * 60)
        logger.info("💡 行为说明（和易语言版本一致）：")
        logger.info("   - 注入成功后，只要close回调没有触发，默认没有断连")
        logger.info("   - 客户端关闭回调只记录日志，不做重连操作")
        logger.info("   - 完全依赖DLL的自动重连机制（如果DLL有的话）")
        logger.info("=" * 60)

        try:
            # 🔍 关键：和易语言版本一致，只运行主循环，不做任何断连检测
            # 易语言版本没有断连检测逻辑，完全依赖DLL的回调机制
            while self.is_running and not self.should_stop:
                time.sleep(1)

        except KeyboardInterrupt:
            logger.info("收到中断信号，正在停止服务...")
            self.should_stop = True
        except Exception as e:
            logger.error(f"服务运行异常: {e}")
        finally:
            self.stop()

    # 🔍 关键：移除所有重连相关的方法（和易语言版本一致）
    # 易语言版本没有重连逻辑，完全依赖DLL的自动重连机制

    def stop(self):
        """停止服务"""
        logger.info("正在停止微信服务...")
        self.should_stop = True
        self.is_running = False

        try:
            if self.loader:
                self.loader.DestroyWeChat()
                logger.info("微信连接已断开")
        except Exception as e:
            logger.error(f"停止服务时发生异常: {e}")

        logger.info("微信服务已停止")

    def send_message(self, message: str):
        """发送消息"""
        if not self.loader:
            logger.error("服务未连接，无法发送消息")
            logger.error(f"  - client_id: {self.client_id}")
            logger.error(f"  - loader: {self.loader is not None}")
            return False

        if not self.has_active_client():
            logger.warning("当前没有有效连接，发送前尝试重新注入")
            if not self.ensure_connected():
                logger.error("重新注入失败，无法发送消息")
                logger.error(f"  - client_id: {self.client_id}")
                logger.error(f"  - connected_client_ids: {sorted(self.connected_client_ids)}")
                return False

        # 检查接口是否就绪（可选，但建议等待）
        if not self.api_ready:
            logger.warning("⚠️  接口可能尚未就绪（未收到type=11024通知）")
            logger.warning("   建议等待接口就绪后再发送消息")
            logger.warning("   但会继续尝试发送...")

        try:
            # 记录发送的详细信息
            logger.info(f"准备发送消息:")
            logger.info(f"  - client_id: {self.client_id} (type: {type(self.client_id)})")
            logger.info(f"  - message长度: {len(message)}")
            logger.info(f"  - message内容: {message}")
            logger.info(f"  - api_ready: {self.api_ready}")

            # 验证 client_id
            if self.client_id == 0 or self.client_id is None:
                logger.error(f"❌ 无效的 client_id: {self.client_id}")
                return False

            result = self.loader.SendWeChatData(self.client_id, message)

            if result:
                logger.info(f"✅ 消息发送成功")
                logger.debug(f"  - client_id: {self.client_id}")
                logger.debug(f"  - message: {message[:100]}...")
                return True
            else:
                logger.error(f"❌ 消息发送失败")
                logger.error(f"  - client_id: {self.client_id}")
                logger.error(f"  - message: {message[:200]}...")
                logger.error(f"  - SendWeChatData返回值: {result}")
                logger.error(f"  - 可能的原因:")
                logger.error(f"    1. client_id 不正确")
                logger.error(f"    2. 消息格式不正确")
                logger.error(f"    3. DLL函数调用失败")
                logger.error(f"    4. 企微进程未就绪")
                return False
        except Exception as e:
            logger.error(f"❌ 发送消息时发生异常: {e}", exc_info=True)
            return False


# --- HTTP API 服务 (8989端口) ---
api_app = Flask(__name__)


@api_app.route('/status', methods=['GET'])
def status_handler():
    """返回 Hook runtime 现场状态，供 provider/UI 排查真实消息链路。"""
    if not service:
        return jsonify({
            'success': False,
            'message': '服务未初始化',
            'env_msg_push_url': os.environ.get('MSG_PUSH_URL', ''),
        }), 503
    return jsonify({
        'success': True,
        'client_id': service.client_id,
        'wechat_pid': service.wechat_pid,
        'connected_client_ids': sorted(service.connected_client_ids),
        'api_ready': service.api_ready,
        'current_user_id': service.current_user_id,
        'msg_push_url': service.msg_push_url,
        'env_msg_push_url': os.environ.get('MSG_PUSH_URL', ''),
        'push_self_messages': service.push_self_messages,
        'last_heartbeat': service.last_heartbeat,
        'injecting': service.injection_in_progress,
        'last_injected_pid': service.last_injected_pid,
        'last_injected_at': service.last_injected_at,
        'last_injection_method': service.last_injection_method,
        'last_pid_candidate': service.last_pid_candidate,
        'last_injection_error': service.last_injection_error,
        'last_injection_attempt_at': service.last_injection_attempt_at,
        'last_injection_failed_at': service.last_injection_failed_at,
        'injection_retry_cooldown_seconds': service.injection_retry_cooldown_seconds,
        'injection_retry_remaining_seconds': service.injection_retry_remaining_seconds(),
        'last_raw_callback_at': service.last_raw_callback_at,
        'last_notification_at': service.last_notification_at,
        'last_notification_type': service.last_notification_type,
        'raw_callback_counts': service.raw_callback_counts,
        'recent_raw_callbacks': list(service.recent_raw_callbacks),
        'last_push_result': service.last_push_result,
        'pending_request_count': len(service.pending_requests),
    }), 200
service = None  # 全局服务实例

# 🔍 定义"发送消息"类指令中没有返回的类型（发送后直接返回成功，不等待响应）
# 注意：只有"发送消息"类的指令才需要区分，其他查询/操作类指令都有返回
# 根据API文档，以下发送消息类型发送后没有返回：
SEND_MESSAGE_TYPES_WITHOUT_RESPONSE = {
    11029,  # 发送文本消息
    11069,  # 发送群@消息
    11030,  # 发送图片消息
    11031,  # 发送文件消息
    11070,  # 发送动图消息
    11067,  # 发视频消息
}

# 🔍 定义"发送消息"类指令中有返回的类型（需要等待响应）
# 以下发送消息类型发送后会返回响应数据：
SEND_MESSAGE_TYPES_WITH_RESPONSE = {
    11162,  # 发送小程序
    11172,  # 发送视频号
    11159,  # 发送卡片消息
    11161,  # 发送名片
    11196,  # 发送视频号直播消息
}

# CDN 下载有 WxCDN(11171) 和 C2cCDN(11170) 两套接口。第三方机器人发到群里的文件通常是
# C2cCDN，如果只给 11171 加长等待时间，11170 会走默认 5 秒等待并容易被误判为下载失败。
CDN_DOWNLOAD_TYPES = {11170, 11171}


@api_app.route('/api', methods=['POST'])
def api_handler():
    """
    HTTP API接口：接收JSON格式的请求，调用DLL函数

    请求格式：
    {
        "type": 11029,  // 消息类型（必填）
        "data": {       // 消息数据（必填）
            "conversation_id": "xxxxxxx",
            "content": "你好，世界"
        }
    }

    返回格式：
    {
        "success": true/false,
        "message": "操作结果描述",
        "data": {}  // 返回数据（如果有）
    }
    """
    try:
        if not service:
            return jsonify({
                'success': False,
                'message': '服务未连接，请等待注入完成'
            }), 503
        if not service.has_active_client():
            # HTTP 健康检查和业务请求都会走这里；先给 DLL 一次重新注入机会，
            # 避免 socket close 后必须人工重启 provider。
            if not service.ensure_connected():
                return jsonify({
                    'success': False,
                    'message': '服务未连接，请等待注入完成',
                    'last_injection_error': service.last_injection_error,
                    'retry_after_seconds': service.injection_retry_remaining_seconds(),
                }), 503

        body = request.get_json(silent=True)
        if not body:
            return jsonify({
                'success': False,
                'message': '请求体必须是JSON格式'
            }), 400

        msg_type = body.get('type')
        msg_data = body.get('data')

        if msg_type is None:
            return jsonify({
                'success': False,
                'message': '缺少必需参数: type'
            }), 400

        if msg_data is None:
            return jsonify({
                'success': False,
                'message': '缺少必需参数: data'
            }), 400

        # 构造完整的消息
        payload = {
            'type': msg_type,
            'data': msg_data
        }
        message = json.dumps(payload, ensure_ascii=False)

        # 记录请求信息
        logger.info(f"📤 收到API请求:")
        logger.info(f"  - type: {msg_type}")
        logger.info(f"  - data: {json.dumps(msg_data, ensure_ascii=False)}")
        logger.info(f"  - client_id: {service.client_id}")

        # 🔍 检查此消息类型是否需要等待响应
        # 规则：
        # 1. 如果是"发送消息"类指令且没有返回，直接返回成功
        # 2. 其他所有指令（包括有返回的发送消息、查询类、操作类）都需要等待响应
        is_send_message_without_response = msg_type in SEND_MESSAGE_TYPES_WITHOUT_RESPONSE
        has_response = not is_send_message_without_response  # 除了没有返回的发送消息，其他都有返回

        if has_response:
            # 有返回的消息类型：需要等待响应
            # 生成请求ID
            with service.request_lock:
                service.request_counter += 1
                request_id = service.request_counter

            # 创建等待事件和响应存储
            response_event = Event()
            # 对于 CDN 下载请求（11170/11171），保存 save_path 用于精确匹配，并使用文件下载超时时间。
            save_path = None
            wait_timeout = 5.0
            if msg_type in CDN_DOWNLOAD_TYPES and isinstance(msg_data, dict):
                save_path = msg_data.get('save_path')
                try:
                    wait_timeout = max(5.0, min(float(msg_data.get('timeout_seconds') or 45.0), 300.0))
                except (TypeError, ValueError):
                    wait_timeout = 45.0
            request_info = {
                'event': response_event,
                'response': None,
                'timestamp': time.time(),
                'expected_type': msg_type,  # 期望的响应类型
                'save_path': save_path,  # 保存路径用于精确匹配（仅用于 CDN 下载）
                'timeout_seconds': wait_timeout,
            }

            with service.request_lock:
                service.pending_requests[request_id] = request_info

        try:
            # 发送消息
            result = service.send_message(message)

            if not result:
                # 发送失败
                if has_response:
                    # 清理请求
                    with service.request_lock:
                        service.pending_requests.pop(request_id, None)
                logger.error(f"❌ API请求失败: type={msg_type}")
                return jsonify({
                    'success': False,
                    'message': f'操作失败，请检查日志。client_id={service.client_id}',
                    'client_id': service.client_id,
                    'request_type': msg_type
                }), 500

            if has_response:
                # 有返回的消息类型：等待响应；CDN 文件下载可能明显慢于普通 API。
                logger.debug(f"等待响应，request_id={request_id}, type={msg_type}")
                response_received = response_event.wait(timeout=wait_timeout)

                # 获取响应数据
                with service.request_lock:
                    response_data = service.pending_requests.pop(request_id, {}).get('response')

                if response_received and response_data:
                    logger.info(f"✅ API请求成功，收到响应: type={msg_type}")
                    # 🔍 返回格式和API文档一致：{"data": {...}, "type": xxx}
                    return jsonify({
                        'data': response_data.get('data', {}),
                        'type': response_data.get('type')
                    }), 200
                else:
                    # 超时或没有响应
                    logger.warning(f"⚠️  API请求超时或未收到响应: type={msg_type}")
                    # 清理超时的请求（如果还在pending_requests中）
                    with service.request_lock:
                        if request_id in service.pending_requests:
                            service.pending_requests.pop(request_id, None)
                    # 超时时返回错误信息
                    return jsonify({
                        'success': False,
                        'message': '请求已发送，但未收到响应（可能超时）',
                        'error': 'timeout',
                        'request': payload
                    }), 504  # 504 Gateway Timeout
            else:
                # 没有返回的消息类型：直接返回成功，不等待
                logger.info(f"✅ API请求成功（无返回类型）: type={msg_type}")
                return jsonify({
                    'success': True,
                    'message': '消息已发送',
                    'type': msg_type
                }), 200

        except Exception as e:
            # 清理请求（只有在有响应的情况下才需要清理）
            if has_response:
                with service.request_lock:
                    service.pending_requests.pop(request_id, None)
            logger.error(f"等待响应时发生异常: {e}")
            return jsonify({
                'success': False,
                'message': f'等待响应时发生异常: {str(e)}'
            }), 500

    except Exception as e:
        logger.error(f"/api 接口异常: {e}")
        return jsonify({
            'success': False,
            'message': f'服务器错误: {str(e)}'
        }), 500


# --- 消息推送服务 (9000端口) ---
msg_app = Flask(__name__)


@msg_app.route('/msg', methods=['POST'])
def msg_handler():
    """
    消息推送接口：接收企微消息推送

    外部服务可以向此接口发送消息，用于接收企微消息推送
    请求格式：
    {
        "client_id": 1,
        "type": 11041,
        "data": {},
        "timestamp": 1234567890
    }
    """
    try:
        body = request.get_json(silent=True)
        if not body:
            return jsonify({
                'success': False,
                'message': '请求体必须是JSON格式'
            }), 400

        logger.info(f"收到消息推送: {body}")

        # 这里可以处理接收到的消息推送
        # 例如：保存到数据库、转发到其他服务等

        return jsonify({
            'success': True,
            'message': '消息接收成功'
        }), 200

    except Exception as e:
        logger.error(f"/msg 接口异常: {e}")
        return jsonify({
            'success': False,
            'message': f'服务器错误: {str(e)}'
        }), 500


# --- MAIN EXECUTION LOGIC ---
if __name__ == '__main__':
    # 配置 DLL 路径
    # 检测是否在 PyInstaller 打包环境中
    if getattr(sys, 'frozen', False):
        # 打包成 exe 后，使用 exe 所在目录
        base_dir = os.path.dirname(sys.executable)
    else:
        # 开发环境，使用脚本所在目录
        base_dir = os.path.dirname(os.path.abspath(__file__))

    loader_path = os.path.join(base_dir, "Loader.dll")
    dll_path = os.path.join(base_dir, "WeChat_5.0.0.6008.dll")

    # 转换为绝对路径
    loader_path = os.path.abspath(loader_path)
    dll_path = os.path.abspath(dll_path)

    # 🔍 DLL会自动处理登录恢复（因为数据目录已经指定了），不需要手动配置
    # 创建服务实例
    service = WeChatService(loader_path, dll_path)

    # 配置消息推送URL（可选）
    # 当企微收到消息时，会向此URL发送POST请求
    # 例如：service.msg_push_url = "http://127.0.0.1:9000/msg"
    # 如果不配置，则不会推送消息
    # 默认推送到GUI的回调服务器（GUI使用9000端口）
    # 注意：使用127.0.0.1而不是localhost，避免Windows系统DNS解析延迟（约2秒）
    service.msg_push_url = os.environ.get('MSG_PUSH_URL', "http://127.0.0.1:9000/msg")

    # 安装信号处理器
    def _signal_handler(signum, frame):
        logger.info(f"收到信号 {signum}，准备停止服务...")
        service.should_stop = True
        # 停止服务
        try:
            service.stop()
        except Exception as e:
            logger.error(f"停止服务时发生异常: {e}")
        # 强制退出（因为 Flask 开发服务器不支持优雅关闭）
        logger.info("正在退出程序...")
        os._exit(0)

    signal.signal(signal.SIGINT, _signal_handler)
    if hasattr(signal, 'SIGTERM'):
        try:
            signal.signal(signal.SIGTERM, _signal_handler)
        except Exception:
            pass
    if hasattr(signal, 'SIGBREAK'):
        try:
            signal.signal(signal.SIGBREAK, _signal_handler)
        except Exception:
            pass

    # 启动服务（DLL会自动处理登录恢复）
    service_thread = threading.Thread(target=service.start, daemon=True)
    service_thread.start()

    # 等待首次注入建立 socket 连接后再开放 HTTP API，避免 provider 启动健康检查抢跑，
    # 在 DLL 还没回调 client_id 时触发第二次 InjectWeChat，导致消息通知链路不稳定。
    startup_deadline = time.time() + int(os.environ.get('HOOK_STARTUP_READY_TIMEOUT', '12'))
    while time.time() < startup_deadline:
        if service.has_active_client() and service.api_ready:
            logger.info("Hook socket 已连接且接口就绪，准备开放 HTTP API")
            break
        if not service_thread.is_alive():
            logger.warning("Hook 服务线程已退出，仍继续开放 HTTP API 以便状态排查")
            break
        time.sleep(0.2)
    else:
        logger.warning("等待 Hook socket/API ready 超时，继续开放 HTTP API 以便状态排查")

    # 启动 HTTP API 服务（8989端口）
    api_host = os.environ.get('API_HOST', '0.0.0.0')
    api_port = int(os.environ.get('API_PORT', '8989'))
    logger.info(f"HTTP API 启动于 http://{api_host}:{api_port}/api")

    # 启动消息推送服务（9000端口）
    # 注意：如果GUI程序正在运行，它会使用9000端口，所以这里不启动9000端口的服务器
    # GUI程序有自己的回调服务器在9000端口接收消息
    # 如果需要http_py.py自己的消息推送服务，可以通过环境变量启用
    msg_host = os.environ.get('MSG_HOST', '0.0.0.0')
    msg_port = int(os.environ.get('MSG_PORT', '0'))  # 默认0表示不启动
    if msg_port > 0:
        logger.info(f"消息推送服务启动于 http://{msg_host}:{msg_port}/msg")
        # 在单独的线程中运行消息推送服务

        def run_msg_server():
            try:
                msg_app.run(host=msg_host, port=msg_port, debug=False, use_reloader=False)
            except Exception as e:
                logger.error(f"消息推送服务异常: {e}")

        msg_thread = threading.Thread(target=run_msg_server, daemon=True)
        msg_thread.start()
    else:
        logger.info("消息推送服务未启动（GUI程序会使用9000端口）")

    # 运行 API 服务（主线程）
    try:
        api_app.run(host=api_host, port=api_port, debug=False, use_reloader=False)
    except KeyboardInterrupt:
        logger.info("收到键盘中断信号")
    finally:
        service.should_stop = True
        logger.info("程序退出")
