import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Voice(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(min_length=1)
    name: str
    language: str = 'ja'
    options: dict[str, Any] = Field(default_factory=dict)


class BackendSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    factory: str
    options: dict[str, Any]


class Settings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    host: str = '127.0.0.1'
    port: int = Field(default=17862, ge=1, le=65535)
    max_queue_size: int = Field(default=8, ge=1, le=100)
    max_text_length: int = Field(default=1000, ge=1, le=10000)
    languages: list[str] = Field(default_factory=lambda: ['ja'])
    default_language: str = 'ja'
    default_voice: str = 'default'
    warmup_text: str = 'こんにちは。'
    backend: BackendSettings
    voices: list[Voice]

    @field_validator('voices')
    @classmethod
    def unique_voices(cls, value):
        if not value or len({v.id for v in value}) != len(value):
            raise ValueError('Voice IDs must be nonempty and unique')
        return value


def read_settings(path: Path) -> Settings:
    settings = Settings.model_validate(json.loads(path.read_text(encoding='utf-8-sig')))
    if settings.default_voice not in {v.id for v in settings.voices}:
        raise ValueError('default_voice does not exist')
    if settings.default_language not in settings.languages:
        raise ValueError('default_language is not supported')
    return settings
