"""Fail closed if the controller, GPU instance or post-load inference is unverified."""
import json
import time
from pathlib import Path

from app.errors import GatewayError


class RuntimeSafety:
    def __init__(self, path, **kwargs):
        self.path = Path(path)

    def status(self, entry):
        if 'GB10' not in str(entry.metadata.get('device', '')):
            return None
        try:
            value = json.loads(self.path.read_text())
            boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            if value.get('verified') is True and value.get('boot_id') == boot and -5 <= time.time()-value['heartbeat_epoch'] <= 20:
                if entry.modes and value.get('mode') not in entry.modes:
                    raise ValueError()
                return None
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return {'status': 'error', 'reason': 'gpu_unverified', 'message': 'GPU 模型正在加载、验证或不可用，请查看切换任务。'}

    def require(self, entry):
        value = self.status(entry)
        if value:
            raise GatewayError(503, value['message'], value['reason'], 'service_unavailable_error', 'model')
