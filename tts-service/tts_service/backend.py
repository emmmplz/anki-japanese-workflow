"""Engine-neutral contract. This module deliberately does not import torch."""
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence


@dataclass(frozen=True)
class SynthesisInput:
    text: str
    language: str
    voice: str
    speed: float = 1.0
    seed: int = 20261102


@dataclass(frozen=True)
class AudioOutput:
    data: bytes
    sample_rate: int
    duration_seconds: float
    media_type: str = 'audio/wav'


class TTSBackend(Protocol):
    """All methods run on one dedicated inference thread, including load/close.

    load() loads model weights once. synthesize() reuses the loaded instance.
    Return a complete WAV in AudioOutput, or raise; never return fake silence.
    info() returns JSON-compatible metadata, without importing model details
    into the HTTP layer. close() is only called when the service shuts down.
    """

    def load(self, options: Mapping[str, Any], base_directory: Path) -> None: ...
    def synthesize(self, request: SynthesisInput) -> AudioOutput: ...
    def info(self) -> dict[str, Any]: ...
    def close(self) -> None: ...


class BatchTTSBackend(TTSBackend, Protocol):
    """Optional extension; existing single-request backends remain valid.

    One call batches 1-4 compatible requests on the already-loaded model.
    Results must be complete WAV files in the exact input order and count.
    The runtime does not emulate this by calling synthesize in a loop.
    """

    def synthesize_batch(self, requests: Sequence[SynthesisInput]) -> list[AudioOutput]: ...
