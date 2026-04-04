import argparse
import socket
import threading
import time

import webview

from app import app


def _is_port_available(host, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def _pick_port(host, preferred_port):
    if _is_port_available(host, preferred_port):
        return preferred_port

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return sock.getsockname()[1]


def _wait_for_server(host, port, timeout_seconds=30):
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def _start_flask_server(host, port):
    app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)


def main():
    parser = argparse.ArgumentParser(description='Launch the Printing Business desktop app window.')
    parser.add_argument('--host', default='127.0.0.1', help='Host interface for the internal local server.')
    parser.add_argument('--port', type=int, default=5000, help='Preferred local port for the internal server.')
    parser.add_argument(
        '--open-path',
        default='/desktop-capture',
        help='Relative path shown first in the app window.',
    )
    parser.add_argument('--no-gui', action='store_true', help='Run the internal server only (debug/testing mode).')
    args = parser.parse_args()

    port = _pick_port(args.host, args.port)
    open_path = args.open_path if str(args.open_path).startswith('/') else '/' + str(args.open_path)
    start_url = f'http://{args.host}:{port}{open_path}'

    server_thread = threading.Thread(target=_start_flask_server, args=(args.host, port), daemon=True)
    server_thread.start()

    if not _wait_for_server(args.host, port):
        raise RuntimeError(f'Internal server failed to start at http://{args.host}:{port}')

    if args.no_gui:
        while True:
            time.sleep(1)

    window = webview.create_window(
        title='Printing Business App',
        url=start_url,
        width=1280,
        height=860,
        min_size=(980, 700),
    )
    webview.start(debug=False)


if __name__ == '__main__':
    main()