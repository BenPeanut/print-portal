import argparse
import socket
import threading
import time
import webbrowser

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


def _wait_for_server(host, port, timeout_seconds=20):
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def _open_browser(url):
    webbrowser.open(url, new=1)


def main():
    parser = argparse.ArgumentParser(description='Launch the local printing business desktop app.')
    parser.add_argument('--host', default='127.0.0.1', help='Host interface to bind the local server to.')
    parser.add_argument('--port', type=int, default=5000, help='Preferred local port for the server.')
    parser.add_argument(
        '--open-path',
        default='/user_login?next=/model-capture-app/',
        help='Relative path to open in the browser after the server starts.',
    )
    parser.add_argument('--no-browser', action='store_true', help='Start the server without opening the browser.')
    args = parser.parse_args()

    port = _pick_port(args.host, args.port)
    base_url = f'http://{args.host}:{port}'
    start_url = base_url + (args.open_path if str(args.open_path).startswith('/') else '/' + str(args.open_path))

    print('Starting Printing Business desktop app...')
    print(f'User sign-in: {start_url}')
    print(f'Admin sign-in: {base_url}/login?next=/model-capture-app/')
    print('Close this window to stop the local server.')

    if not args.no_browser:
        browser_thread = threading.Thread(target=lambda: (_wait_for_server(args.host, port) and _open_browser(start_url)), daemon=True)
        browser_thread.start()

    app.run(host=args.host, port=port, debug=False, use_reloader=False)


if __name__ == '__main__':
    main()