from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LauncherStatus:
    workwechat_state: str = "unknown"
    provider_state: str = "stopped"
    hook_state: str = "stopped"
    callback_state: str = "unknown"
    current_version: str = ""
    required_version: str = ""
    error: str = ""

    @property
    def summary(self) -> str:
        # 摘要只放窗口最醒目的状态，方便客服快速判断当前能不能继续工作。
        if self.workwechat_state == "missing":
            return "请先打开企业微信"
        if self.workwechat_state == "version_mismatch":
            return "企业微信版本不匹配"
        if self.error:
            return "启动异常"
        if self.provider_state == "running" and self.hook_state == "running":
            return "运行正常"
        if self.provider_state == "starting" or self.hook_state == "starting":
            return "正在启动"
        return "等待启动"

    @property
    def detail(self) -> str:
        # 详情面向客服，不暴露 DLL、进程参数等技术信息；技术细节由日志记录。
        if self.workwechat_state == "missing":
            return "请先打开企业微信并登录，然后点击“启动”。"
        if self.workwechat_state == "version_mismatch":
            return f"当前企业微信版本是 {self.current_version or '未知'}，需要 {self.required_version or '指定版本'}。"
        if self.error:
            return self.error
        if self.provider_state == "running" and self.hook_state == "running":
            return "Provider 和 Hook 已运行，可以最小化到托盘。"
        return "请打开并登录企业微信，然后点击“启动”。"
