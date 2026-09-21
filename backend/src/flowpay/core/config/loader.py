"""配置读取流程：读取文件、应用环境覆盖、解析路径、构造并校验。"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from pydantic import TypeAdapter

from flowpay.core.json import JsonObject, parse_json, require_object, require_text
from flowpay.core.llm.embedding import EmbeddingSettings

from .settings import ModelSettings, Settings


def load_settings(path: Path | None = None) -> Settings:
    """显式路径优先于 FLOWPAY_CONFIG；环境变量覆盖 TOML，路径相对配置文件。"""
    config_path = (path or Path(os.getenv("FLOWPAY_CONFIG", "configs/local.toml"))).resolve()
    with config_path.open("rb") as config_file:
        raw_config = tomllib.load(config_file)
    effective_config = apply_environment_overrides(raw_config)
    resolved_config = resolve_paths(effective_config, config_path.parent)
    return build_settings(resolved_config)


def env_override(name: str) -> str | None:
    """读取一个覆盖项；空字符串当作没设置。

    compose 用 ${VAR:-} 传可选变量时，未填的那些会以空串进到容器里。
    不做这一步，一个没填的 FLOWPAY_MODEL_BASE_URL 会把配置里的地址清空。
    """
    value = os.getenv(name)
    return value or None


def apply_environment_overrides(raw_config: JsonObject) -> JsonObject:
    """产生新字典，不修改读取结果；request_options 按完整 JSON 对象覆盖。"""
    allowed = set(Settings.__dataclass_fields__)
    if unknown := set(raw_config) - allowed:
        raise ValueError(f"Unknown configuration fields: {sorted(unknown)}")
    config = dict(raw_config)
    for field in allowed - {"model", "embedding"}:
        override = env_override(f"FLOWPAY_{field.upper()}")
        if override is not None:
            config[field] = int(override) if field in {"port", "mock_delay_ms"} else override
    config["model"] = _model_overrides(require_object(raw_config.get("model", {})))
    config["embedding"] = _embedding_overrides(raw_config.get("embedding"))
    return config


def _model_overrides(raw_model: JsonObject) -> JsonObject:
    model = dict(raw_model)
    for field in ModelSettings.__dataclass_fields__:
        override = env_override(f"FLOWPAY_MODEL_{field.upper()}")
        if override is None:
            continue
        if field == "request_options":
            model[field] = parse_json(override)
        elif field == "timeout_seconds":
            model[field] = float(override)
        else:
            model[field] = override
    return model


def _embedding_overrides(raw_embedding: object) -> JsonObject | None:
    overrides = {
        field: env_override(f"FLOWPAY_EMBEDDING_{field.upper()}")
        for field in EmbeddingSettings.__dataclass_fields__
    }
    if raw_embedding is None and all(value is None for value in overrides.values()):
        return None
    embedding = dict(require_object(raw_embedding or {}))
    for field, override in overrides.items():
        if override is None:
            continue
        if field in {"dimensions", "batch_size"}:
            embedding[field] = int(override)
        elif field == "timeout_seconds":
            embedding[field] = float(override)
        elif field == "request_options":
            embedding[field] = parse_json(override)
        else:
            embedding[field] = override
    return embedding


def resolve_paths(config: JsonObject, directory: Path) -> JsonObject:
    resolved = dict(config)
    for field in ("data_dir", "dataset_path"):
        if field not in resolved:
            raise ValueError(f"Missing configuration field: {field}")
        resolved[field] = (directory / require_text(resolved[field])).resolve()
    if resolved.get("sources_config"):
        resolved["sources_config"] = (
            directory / require_text(resolved["sources_config"])
        ).resolve()
    return resolved


def build_settings(config: JsonObject) -> Settings:
    values = dict(config)
    for name, settings_type in (("model", ModelSettings), ("embedding", EmbeddingSettings)):
        raw = values.get(name)
        if raw is None:
            continue
        fields = require_object(raw)
        if unknown := set(fields) - set(settings_type.__dataclass_fields__):
            raise ValueError(f"Unknown {name} configuration fields: {sorted(unknown)}")
        try:
            require_object(fields.get("request_options", {}))
        except ValueError as error:
            raise ValueError(f"{name} request_options must be an object") from error
    # TOML 与环境变量是动态输入，在这里统一验证嵌套 dataclass，之后只传 Settings。
    settings = TypeAdapter(Settings).validate_python(values)
    if not 1 <= settings.port <= 65535 or not 0 <= settings.mock_delay_ms <= 5000:
        raise ValueError("Invalid port or mock_delay_ms")
    if settings.model.timeout_seconds <= 0:
        raise ValueError("Model timeout must be positive")
    return settings
