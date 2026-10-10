"""Reserve the port before startup, reuse a running app, and open its browser page."""
import argparse
import json
import socket
import sys
import threading
import urllib.error
import urllib.request
import webbrowser


def existing_app(url):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url + '/api/health', timeout=1) as response:
            data = json.loads(response.read(4096))
            return isinstance(data, dict) and data.get('app') == 'photo-curator'
    except (OSError, urllib.error.URLError, ValueError):
        pass
    # Compatibility with an instance started before the health endpoint was added.
    try:
        with opener.open(url, timeout=1) as response:
            page = response.read(65536).decode('utf-8')
        if '<title>轻相册 · 本地照片整理</title>' not in page:
            return False
        with opener.open(url + '/api/jobs', timeout=1) as response:
            data = json.loads(response.read(1_000_000))
        return isinstance(data, dict) and isinstance(data.get('jobs'), list) and 'progress' in data
    except (OSError, urllib.error.URLError, ValueError):
        return False


def open_when_ready(url, stopped):
    for _ in range(80):
        if stopped.is_set():
            return
        if existing_app(url):
            webbrowser.open(url)
            return
        stopped.wait(0.25)


def main(argv=None):
    parser = argparse.ArgumentParser(description='启动轻相册，已运行时直接打开页面')
    parser.add_argument('--port', type=int, default=8618)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args(argv)
    host = '127.0.0.1'
    url = f'http://{host}:{args.port}'
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            # Binding first prevents a duplicate process from running task recovery/migration.
            listener.bind((host, args.port))
        except OSError as error:
            for _ in range(12):
                if existing_app(url):
                    print(f'轻相册已经运行，打开已有工作台：{url}', flush=True)
                    if not args.no_browser:
                        webbrowser.open(url)
                    return 0
                threading.Event().wait(0.15)
            print(f'无法使用端口 {args.port}：{error}\n请检查是否有其他程序占用此端口。', file=sys.stderr)
            return 1
        try:
            import uvicorn
        except ImportError:
            print('缺少启动依赖，请先运行 setup.bat。', file=sys.stderr)
            return 1
        print(f'轻相册工作台：{url}\n保留此窗口，按 Ctrl+C 可以停止服务。', flush=True)
        stopped = threading.Event()
        if not args.no_browser:
            threading.Thread(target=open_when_ready, args=(url, stopped), daemon=True).start()
        try:
            server = uvicorn.Server(uvicorn.Config('webapp:app', host=host, port=args.port))
            server.run(sockets=[listener])
            return 0 if server.started else 1
        except Exception as error:
            print(f'启动失败：{error}', file=sys.stderr)
            return 1
        finally:
            stopped.set()


if __name__ == '__main__':
    raise SystemExit(main())
