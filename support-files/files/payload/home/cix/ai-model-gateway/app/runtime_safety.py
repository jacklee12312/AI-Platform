"""Read-only admission checks. Missing/stale safety evidence never implies health."""
import json
import math
import time
from pathlib import Path
from urllib.parse import urlsplit

from .errors import GatewayError

PORTS = {17701:'houmo-copaw',17702:'houmo-bge-m3',17703:'houmo-reranker',
         17705:'houmo-qwen3-vl-8b',17709:'houmo-glm-ocr-0.9b',17707:'houmo-qwen38-27b',17712:'houmo-qwen36-35b-a3b',
         17711:'houmo-spark-x1',8091:'houmo-cosyvoice3'}


class RuntimeSafety:
    def __init__(self, path, *, required=False, max_age=45, boot_path='/proc/sys/kernel/random/boot_id'):
        self.path, self.required, self.max_age, self.boot_path = Path(path), required, max_age, Path(boot_path)

    def status(self, entry):
        if not self.required or 'LQ50' not in str(entry.metadata.get('device','')):
            return None
        try:
            with self.path.open(encoding='utf-8') as stream:
                raw=stream.read(131073)
            if len(raw)>131072: raise ValueError()
            state=json.loads(raw)
            heartbeat=state['heartbeat_epoch']
            if state['schema_version'] != 1 or not isinstance(heartbeat,(int,float)) or not math.isfinite(heartbeat):
                raise ValueError()
            if not -5 <= time.time()-heartbeat <= self.max_age or state['boot_id'] != self.boot_path.read_text().strip():
                raise ValueError()
            incident=state.get('incident')
            if incident:
                return {'status':'error','reason':'npu_quarantined','message':'NPU 故障隔离，已停止接收新请求',
                        'incident':{k:incident.get(k) for k in ('id','at','source','kind','detail')}}
            if state.get('maintenance'):
                return {'status':'error','reason':'npu_recovering','message':'设备恢复与推理验证中，暂不接收请求'}
            if state.get('monitor_error'): raise ValueError()
            name=PORTS.get(urlsplit(entry.base_url or '').port)
            row=state['services'].get(name,{})
            if not row.get('verified') or not row.get('running') or row.get('health') != 'healthy':
                return {'status':'error','reason':'npu_unverified','message':'当前实例尚未通过实际推理验证，请执行设备故障恢复'}
            return None
        except (OSError,ValueError,TypeError,KeyError,AttributeError):
            return {'status':'error','reason':'npu_monitor_unavailable','message':'NPU 监控状态缺失或过期，暂不接收新请求'}

    def require(self, entry):
        status=self.status(entry)
        if status:
            raise GatewayError(503,status['message'],status['reason'],'service_unavailable_error','model',
                               details={k:v for k,v in status.items() if k not in {'message','status'}})
