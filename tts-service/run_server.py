import argparse
import faulthandler
import logging
import os
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description='Resident local TTS service')
    parser.add_argument('--config', type=Path, default=Path(__file__).with_name('config.json'))
    parser.add_argument('--background-log', action='store_true',
                        help='Write logs to the configuration runtime directory')
    args = parser.parse_args()
    config = args.config.resolve()
    runtime_dir = config.parent / 'runtime'
    if config.name != 'config.json':
        runtime_dir = runtime_dir / config.stem
    runtime_dir.mkdir(parents=True, exist_ok=True)
    if args.background_log:
        sys.stdout = (runtime_dir / 'server.log').open('a', encoding='utf-8', buffering=1)
        sys.stderr = (runtime_dir / 'server-error.log').open('a', encoding='utf-8', buffering=1)
    faulthandler.enable()
    from tts_service.app import create_app
    from tts_service.config import read_settings
    import uvicorn
    settings = read_settings(config)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    # One process is intentional: each worker would otherwise load another GPU copy.
    server_config = uvicorn.Config(create_app(config), host=settings.host, port=settings.port,
                                   workers=1, reload=False, timeout_graceful_shutdown=300)
    # Fail on an occupied/reserved port before loading several GB of weights.
    sock = server_config.bind_socket()
    pid_path = runtime_dir / 'server.pid'
    pid_path.write_text(str(os.getpid()), encoding='ascii')
    try:
        uvicorn.Server(server_config).run(sockets=[sock])
    finally:
        sock.close()
        if pid_path.exists() and pid_path.read_text(encoding='ascii').strip() == str(os.getpid()):
            pid_path.unlink()


if __name__ == '__main__':
    main()
