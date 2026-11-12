"""Conservative source-tree check before publishing; also review manually."""
from pathlib import Path
import re
import sys

root = Path(__file__).resolve().parents[1]
blocked_suffixes = {'.wav', '.ogg', '.mp3', '.pth', '.pt', '.ckpt', '.safetensors', '.onnx', '.apkg', '.xlsx', '.pyc'}
blocked_dirs = {'.venv', '__pycache__', 'external', 'local-models', 'work', 'outputs'}
patterns = [
    ('Windows user path', re.compile(r'(?i)[a-z]:[\\/]+users[\\/]+')),
    ('private project path', re.compile(r'(?i)codex_project[\\/]+anki')),
    ('credential-like token', re.compile(r'(?i)(?:sk-[a-z0-9]{24,}|ghp_[a-z0-9]{24,})')),
    ('local voice identifier', re.compile(r'(?i)0101adv08_ema007|ema-e15|ema_e8_s2232')),
]
errors = []
files = 0
for path in root.rglob('*'):
    if not path.is_file():
        continue
    rel = path.relative_to(root)
    if '.git' in rel.parts:
        continue  # Git history needs its own review and is not part of the source archive.
    if any(part in blocked_dirs for part in rel.parts) or rel.parts[:2] == ('tts-service', 'runtime'):
        errors.append(f'Excluded directory contains file: {rel}')
        continue
    if path.suffix.lower() in blocked_suffixes or path.name.endswith('.local.json'):
        errors.append(f'Excluded file: {rel}')
        continue
    files += 1
    if path.stat().st_size > 1024 * 1024:
        errors.append(f'Unexpected large file: {rel}')
        continue
    try:
        content = path.read_text(encoding='utf-8-sig')
    except UnicodeDecodeError:
        errors.append(f'Unexpected binary file: {rel}')
        continue
    if rel.as_posix() == 'tools/check_release.py':
        continue
    for label, pattern in patterns:
        if pattern.search(content):
            errors.append(f'{label}: {rel}')
if errors:
    print('\n'.join(errors), file=sys.stderr)
    sys.exit(1)
print(f'Checked {files} source files; no known excluded assets or local identifiers found. Manual review is still required.')
