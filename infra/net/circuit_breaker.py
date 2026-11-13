"""服务容错与连续失败熔断器。"""

from __future__ import annotations

import threading

_CIRCUIT_BREAKERS: list[ProviderCircuitBreaker] = []


class ProviderCircuitBreaker:
    """线程安全的连续失败熔断器，避免不可达外部服务拖慢批量查询。"""

    def __init__(self, threshold: int = 5):
        self.threshold = threshold
        self._lock = threading.Lock()
        self._consec_fails = 0
        self._open = False
        _CIRCUIT_BREAKERS.append(self)

    def is_open(self) -> bool:
        with self._lock:
            return self._open

    def record_success(self) -> None:
        with self._lock:
            self._consec_fails = 0

    def record_failure(self) -> bool:
        with self._lock:
            self._consec_fails += 1
            if self._consec_fails >= self.threshold:
                self._open = True
            return self._open

    def reset(self) -> None:
        with self._lock:
            self._consec_fails = 0
            self._open = False


def reset_all_circuit_breakers() -> None:
    """重置所有注册的熔断器状态。"""
    for cb in _CIRCUIT_BREAKERS:
        cb.reset()
