from __future__ import annotations


class ProviderError(RuntimeError):
    pass


class WechatWindowNotFoundError(ProviderError):
    pass


class SessionNotFoundError(ProviderError):
    pass


class WatcherAlreadyRunningError(ProviderError):
    pass
