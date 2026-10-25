import os
import socket
import select
from urllib.parse import unquote


CONTENT_TYPES = {
    ".html": "text/html",
    ".css": "text/css",
    ".js": "application/javascript",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".swf": "application/x-shockwave-flash",
}

CRLF = b"\r\n"
SP = b" "
COLON = b":"


def get_content_type(path: str) -> str:
    _, ext = os.path.splitext(path)
    return CONTENT_TYPES.get(ext.lower(), "application/octet-stream")


def safe_join(root: str, path: str) -> str | None:
    base = os.path.abspath(root)
    joined = os.path.normpath(os.path.join(base, path.lstrip("/")))
    if not joined.startswith(base):
        return None
    return joined


def decode_path(path: bytes) -> str:
    decoded = unquote(path.decode("utf-8", errors="ignore"))
    return decoded


class HttpRequest:
    def __init__(self):
        self.method = ""
        self.path = ""
        self.version = ""
        self.headers = {}
        self.body_ready = False

    @staticmethod
    def parse(data: bytes):
        req = HttpRequest()
        lines = data.split(CRLF)
        if len(lines) < 2:
            return None, b""

        first_line = lines[0]
        parts = first_line.split(SP)
        if len(parts) != 3:
            return None, b""
        req.method = parts[0].decode("ascii", errors="ignore")
        raw_path = parts[1]
        req.version = parts[2].decode("ascii", errors="ignore")

        req.path = decode_path(raw_path)

        i = 1
        while i < len(lines):
            line = lines[i]
            if line == b"":
                break
            if COLON not in line:
                i += 1
                continue
            key, value = line.split(COLON, 1)
            k = key.decode("ascii", errors="ignore").strip()
            v = value.decode("latin1", errors="ignore").strip()
            req.headers[k.lower()] = v
            i += 1

        body_start = i + 1
        remaining = CRLF.join(lines[body_start:])
        return req, remaining


class HttpResponse:
    @staticmethod
    def make_status(code: int, phrase: str = "") -> bytes:
        phrases = {
            200: "OK",
            403: "Forbidden",
            404: "Not Found",
            405: "Method Not Allowed",
        }
        if not phrase:
            phrase = phrases.get(code, "Unknown")
        return f"HTTP/1.1 {code} {phrase}".encode("ascii")

    @staticmethod
    def make_headers(headers: dict) -> bytes:
        lines = []
        for k, v in headers.items():
            lines.append(f"{k}: {v}")
        return CRLF.join(line.encode("ascii") for line in lines) + CRLF

    @staticmethod
    def build(status_code: int, content: bytes | None, extra_headers: dict | None = None) -> bytes:
        headers = extra_headers or {}
        if content is not None:
            headers["Content-Length"] = str(len(content))
        else:
            content = b""
            headers["Content-Length"] = "0"

        status_line = HttpResponse.make_status(status_code)
        header_block = HttpResponse.make_headers(headers)
        return status_line + CRLF + header_block + CRLF + content


class Server:
    def __init__(self, host: str, port: int, doc_root: str):
        self.host = host
        self.port = port
        self.doc_root = os.path.abspath(doc_root)
        self.sock = None
        self.epoll = None
        self.fd_to_conn = {}
        self.state_read_request = 0
        self.state_send_response = 1

    def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.host, self.port))
        self.sock.listen(128)
        self.sock.setblocking(False)

        self.epoll = select.epoll()
        self.epoll.register(self.sock.fileno(), select.EPOLLIN)

        while True:
            events = self.epoll.poll(-1)
            for fd, event in events:
                if fd == self.sock.fileno():
                    self._accept_new_connection()
                else:
                    self._handle_client(fd, event)

    def _accept_new_connection(self):
        conn, addr = self.sock.accept()
        conn.setblocking(False)
        fd = conn.fileno()
        self.fd_to_conn[fd] = (conn, bytearray(), bytearray(), self.state_read_request)
        self.epoll.register(fd, select.EPOLLIN)

    def _handle_client(self, fd, event):
        conn, buf_in, buf_out, state = self.fd_to_conn[fd]

        if event & select.EPOLLIN:
            try:
                data = conn.recv(4096)
            except BlockingIOError:
                return
            if not data:
                self._close_client(fd)
                return
            buf_in.extend(data)

            if state == self.state_read_request:
                req, _ = HttpRequest.parse(bytes(buf_in))
                if req:
                    response_bytes = self._handle_request(req)
                    buf_out.extend(response_bytes)
                    self.fd_to_conn[fd] = (conn, buf_in, buf_out, self.state_send_response)
                    self.epoll.modify(fd, select.EPOLLOUT)

        if event & select.EPOLLOUT:
            _, _, buf_out, _ = self.fd_to_conn[fd]
            try:
                sent = conn.send(bytes(buf_out))
            except BlockingIOError:
                return
            if sent:
                del buf_out[:sent]
            if not buf_out:
                self._close_client(fd)

    def _close_client(self, fd):
        if fd in self.fd_to_conn:
            conn, _, _, _ = self.fd_to_conn.pop(fd)
            try:
                conn.close()
            except Exception:
                pass
        if self.epoll:
            try:
                self.epoll.unregister(fd)
            except Exception:
                pass

    def _handle_request(self, req: HttpRequest) -> bytes:
        method = req.method.upper()
        path = req.path

        if method not in ("GET", "HEAD"):
            return HttpResponse.build(405, None)

        if path.endswith("/"):
            path = os.path.join(path, "index.html")

        full_path = safe_join(self.doc_root, path)
        if full_path is None:
            return HttpResponse.build(403, None)

        if not os.path.isfile(full_path):
            return HttpResponse.build(404, None)

        try:
            with open(full_path, "rb") as f:
                content = f.read()
        except OSError:
            return HttpResponse.build(403, None)

        content_type = get_content_type(full_path)
        headers = {
            "Date": self._http_date(),
            "Server": "SimpleHttpServer/0.1",
            "Content-Type": content_type,
            "Connection": "close",
        }

        if method == "HEAD":
            return HttpResponse.build(200, None, headers)

        return HttpResponse.build(200, content, headers)

    @staticmethod
    def _http_date() -> str:
        from email.utils import formatdate
        return formatdate(timeval=None, localtime=False, usegmt=True)


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("-r", "--root", required=True, help="DOCUMENT_ROOT")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    server = Server(args.host, args.port, args.root)
    print(f"Starting server on {args.host}:{args.port}, DOCUMENT_ROOT={args.root}")
    server.start()


if __name__ == "__main__":
    main()
