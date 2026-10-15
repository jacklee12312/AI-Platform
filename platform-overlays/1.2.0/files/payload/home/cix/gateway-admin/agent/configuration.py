from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RuntimeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rate_limit_per_minute: int = Field(default=120, ge=10, le=1000)
    max_body_mb: int = Field(default=25, ge=1, le=100)
    chat_timeout_seconds: int = Field(default=300, ge=30, le=900)
    cv_timeout_seconds: int = Field(default=180, ge=10, le=600)
    audio_timeout_seconds: int = Field(default=600, ge=60, le=3600)
    audio_max_body_mb: int = Field(default=100, ge=1, le=512)
    generation_defaults_enabled: bool = False
    default_max_tokens: int = Field(default=1024, ge=1, le=8192)
    default_temperature: float = Field(default=0.2, ge=0, le=2)
    default_top_p: float = Field(default=0.9, ge=0, le=1)
    cv_default_confidence: float = Field(default=0.25, ge=0, le=1)
    cv_default_iou: float = Field(default=0.45, ge=0, le=1)
    asr_num_threads: int = Field(default=4, ge=1, le=8)
    asr_max_concurrency: int = Field(default=2, ge=1, le=4)
    asr_max_file_mb: int = Field(default=100, ge=1, le=512)
    asr_max_audio_seconds: int = Field(default=3600, ge=60, le=7200)
    asr_stream_max_seconds: int = Field(default=3600, ge=60, le=7200)

    @model_validator(mode="after")
    def validate_resource_relationships(self) -> "RuntimeSettings":
        if self.asr_max_concurrency > self.asr_num_threads:
            raise ValueError("ASR 并发数不能大于 ASR CPU 线程数")
        return self


@dataclass(frozen=True)
class Binding:
    setting: str
    env_key: str
    service: Literal["gateway", "sensevoice"]
    encode: Callable[[Any], str] = str
    decode: Callable[[str], Any] = str


def _bool_encode(value: Any) -> str:
    return "true" if bool(value) else "false"


def _bool_decode(value: str) -> bool:
    return value.lower() in {"1", "true", "yes", "on"}


def _mb_encode(value: Any) -> str:
    return str(int(value) * 1024 * 1024)


def _mb_decode(value: str) -> int:
    return int(value) // (1024 * 1024)


def _float_encode(value: Any) -> str:
    return format(float(value), "g")


GATEWAY_BINDINGS = (
    Binding("rate_limit_per_minute", "RATE_LIMIT_PER_MINUTE", "gateway", str, int),
    Binding("max_body_mb", "MAX_BODY_BYTES", "gateway", _mb_encode, _mb_decode),
    Binding("chat_timeout_seconds", "CHAT_TIMEOUT_SECONDS", "gateway", str, int),
    Binding("cv_timeout_seconds", "CV_TIMEOUT_SECONDS", "gateway", str, int),
    Binding("audio_timeout_seconds", "AUDIO_TIMEOUT_SECONDS", "gateway", str, int),
    Binding("audio_max_body_mb", "AUDIO_MAX_BODY_BYTES", "gateway", _mb_encode, _mb_decode),
    Binding("generation_defaults_enabled", "GENERATION_DEFAULTS_ENABLED", "gateway", _bool_encode, _bool_decode),
    Binding("default_max_tokens", "DEFAULT_MAX_TOKENS", "gateway", str, int),
    Binding("default_temperature", "DEFAULT_TEMPERATURE", "gateway", _float_encode, float),
    Binding("default_top_p", "DEFAULT_TOP_P", "gateway", _float_encode, float),
    Binding("cv_default_confidence", "CV_DEFAULT_CONFIDENCE", "gateway", _float_encode, float),
    Binding("cv_default_iou", "CV_DEFAULT_IOU", "gateway", _float_encode, float),
)

GUARD_BINDINGS = (
)

ASR_BINDINGS = (
    Binding("asr_num_threads", "SENSEVOICE_NUM_THREADS", "sensevoice", str, int),
    Binding("asr_max_concurrency", "SENSEVOICE_MAX_CONCURRENCY", "sensevoice", str, int),
    Binding("asr_max_file_mb", "SENSEVOICE_MAX_FILE_BYTES", "sensevoice", _mb_encode, _mb_decode),
    Binding("asr_max_audio_seconds", "SENSEVOICE_MAX_AUDIO_SECONDS", "sensevoice", str, int),
    Binding("asr_stream_max_seconds", "SENSEVOICE_STREAM_MAX_SECONDS", "sensevoice", str, int),
)

ALL_BINDINGS = GATEWAY_BINDINGS + GUARD_BINDINGS + ASR_BINDINGS


def _pattern(env_key: str) -> re.Pattern[str]:
    return re.compile(
        rf"^(?P<prefix>\s*{re.escape(env_key)}:\s*)[\"']?(?P<value>[A-Za-z0-9.+-]+)[\"']?(?P<suffix>\s*(?:#.*)?)$",
        re.MULTILINE,
    )


def read_env(text: str, env_key: str) -> str:
    matches = list(_pattern(env_key).finditer(text))
    if len(matches) != 1:
        raise ValueError(f"compose.yaml 必须且只能包含一个 {env_key} 配置")
    return matches[0].group("value")


def replace_env(text: str, env_key: str, value: str) -> str:
    pattern = _pattern(env_key)
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        raise ValueError(f"compose.yaml 必须且只能包含一个 {env_key} 配置")
    return pattern.sub(
        lambda match: f'{match.group("prefix")}"{value}"{match.group("suffix")}',
        text,
        count=1,
    )


def read_runtime_settings(gateway_text: str, houmo_text: str, sensevoice_text: str) -> RuntimeSettings:
    texts = {"gateway": gateway_text, "houmo": houmo_text, "sensevoice": sensevoice_text}
    values = {
        binding.setting: binding.decode(read_env(texts[binding.service], binding.env_key))
        for binding in ALL_BINDINGS
    }
    return RuntimeSettings.model_validate(values)


def prepare_runtime_texts(
    settings: RuntimeSettings,
    gateway_text: str,
    houmo_text: str,
    sensevoice_text: str,
) -> tuple[dict[str, str], list[dict[str, Any]], set[str]]:
    texts = {"gateway": gateway_text, "houmo": houmo_text, "sensevoice": sensevoice_text}
    current = read_runtime_settings(gateway_text, houmo_text, sensevoice_text)
    before = current.model_dump()
    after = settings.model_dump()
    changes: list[dict[str, Any]] = []
    affected: set[str] = set()
    for binding in ALL_BINDINGS:
        if before[binding.setting] == after[binding.setting]:
            continue
        texts[binding.service] = replace_env(
            texts[binding.service], binding.env_key, binding.encode(after[binding.setting])
        )
        affected.add(binding.service)
        changes.append({
            "id": binding.setting,
            "group": binding.service,
            "before": before[binding.setting],
            "after": after[binding.setting],
            "restart": [binding.service],
        })
    return texts, changes, affected


CONFIG_SCHEMA: dict[str, Any] = {'version': 2, 'groups': [{'id': 'gateway', 'label': '网关与接口', 'description': '请求限流、体积和各类接口超时', 'fields': [{'id': 'rate_limit_per_minute', 'label': '每分钟请求上限', 'type': 'integer', 'min': 10, 'max': 1000, 'unit': '次/分钟', 'help': '按客户端地址限制，防止单个调用方占满盒子。'}, {'id': 'max_body_mb', 'label': '普通请求最大体积', 'type': 'integer', 'min': 1, 'max': 100, 'unit': 'MiB', 'help': 'JSON 和图片请求的最大总大小。'}, {'id': 'chat_timeout_seconds', 'label': '对话请求超时', 'type': 'integer', 'min': 30, 'max': 900, 'unit': '秒', 'help': '长文本生成可适当增加。'}, {'id': 'cv_timeout_seconds', 'label': 'CV 请求超时', 'type': 'integer', 'min': 10, 'max': 600, 'unit': '秒', 'help': '分类、检测、人脸和分割接口共用。'}, {'id': 'audio_timeout_seconds', 'label': '音频请求超时', 'type': 'integer', 'min': 60, 'max': 3600, 'unit': '秒', 'help': '较长录音需要更长等待时间。'}, {'id': 'audio_max_body_mb', 'label': '音频请求最大体积', 'type': 'integer', 'min': 1, 'max': 512, 'unit': 'MiB', 'help': '上传录音和 multipart 请求的总大小。'}]}, {'id': 'generation', 'label': '模型默认值', 'description': '仅在业务请求省略对应参数时补入', 'fields': [{'id': 'generation_defaults_enabled', 'label': '启用缺省生成参数', 'type': 'boolean', 'help': '关闭时保持 OpenAI 兼容透传；开启后只补充调用方未传入的字段。'}, {'id': 'default_max_tokens', 'label': '最大输出 Token', 'type': 'integer', 'min': 1, 'max': 8192, 'unit': 'Token', 'depends_on': 'generation_defaults_enabled', 'help': '不是模型上下文长度。'}, {'id': 'default_temperature', 'label': 'Temperature', 'type': 'number', 'min': 0, 'max': 2, 'step': 0.1, 'depends_on': 'generation_defaults_enabled', 'help': '越低越稳定，越高越有随机性。'}, {'id': 'default_top_p', 'label': 'Top P', 'type': 'number', 'min': 0, 'max': 1, 'step': 0.05, 'depends_on': 'generation_defaults_enabled', 'help': '通常保持 0.9。'}]}, {'id': 'multimedia', 'label': 'ASR 与 CV', 'description': '语音识别资源限制和视觉接口默认阈值', 'fields': [{'id': 'asr_num_threads', 'label': 'ASR CPU 线程', 'type': 'integer', 'min': 1, 'max': 8, 'unit': '线程', 'restart': 'sensevoice'}, {'id': 'asr_max_concurrency', 'label': 'ASR 最大并发', 'type': 'integer', 'min': 1, 'max': 4, 'unit': '路', 'restart': 'sensevoice'}, {'id': 'asr_max_file_mb', 'label': 'ASR 最大文件', 'type': 'integer', 'min': 1, 'max': 512, 'unit': 'MiB', 'restart': 'sensevoice'}, {'id': 'asr_max_audio_seconds', 'label': 'ASR 单次最长音频', 'type': 'integer', 'min': 60, 'max': 7200, 'unit': '秒', 'restart': 'sensevoice'}, {'id': 'asr_stream_max_seconds', 'label': 'ASR 流式最长时长', 'type': 'integer', 'min': 60, 'max': 7200, 'unit': '秒', 'restart': 'sensevoice'}, {'id': 'cv_default_confidence', 'label': 'CV 默认置信度', 'type': 'number', 'min': 0, 'max': 1, 'step': 0.05, 'help': '调用方未传 confidence 时使用。'}, {'id': 'cv_default_iou', 'label': 'CV 默认 IoU', 'type': 'number', 'min': 0, 'max': 1, 'step': 0.05, 'help': '调用方未传 iou 时使用。'}]}], 'profiles': [{'id': 'stable', 'label': '稳定优先（推荐）', 'values': {'rate_limit_per_minute': 120, 'chat_timeout_seconds': 300, 'generation_defaults_enabled': False, 'asr_max_concurrency': 2}}, {'id': 'latency', 'label': '响应优先', 'values': {'rate_limit_per_minute': 120, 'chat_timeout_seconds': 180, 'generation_defaults_enabled': True, 'default_max_tokens': 512, 'default_temperature': 0.2}}, {'id': 'concurrency', 'label': '并发优先', 'values': {'rate_limit_per_minute': 240, 'generation_defaults_enabled': True, 'default_max_tokens': 512, 'asr_max_concurrency': 4}}]}
