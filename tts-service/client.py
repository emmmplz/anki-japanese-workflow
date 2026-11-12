"""A dependency-free client: python client.py 'こんにちは。' --output hello.wav"""
import argparse
import json
from pathlib import Path
import urllib.error
import urllib.request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('text')
    parser.add_argument('--url', default='http://127.0.0.1:17862')
    parser.add_argument('--voice', default='default')
    parser.add_argument('--language', default='ja')
    parser.add_argument('--output', type=Path, default=Path('speech.wav'))
    args = parser.parse_args()
    data = json.dumps(dict(text=args.text, voice=args.voice, language=args.language)).encode('utf-8')
    request = urllib.request.Request(args.url.rstrip('/') + '/tts', data,
                                     headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            audio = response.read()
            print('Generation seconds:', response.headers.get('X-Synthesis-Seconds'))
    except urllib.error.HTTPError as error:
        raise SystemExit(error.read().decode('utf-8'))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(audio)
    print(args.output.resolve())


if __name__ == '__main__':
    main()
