#!/usr/bin/env python3
"""本地 CONNECT 转发代理：绕过「DNS 解析到不可达 GitHub IP」的问题。

背景（CSST1 实测，2026-10）
---------------------------
`getent hosts github.com` 返回 20.205.243.166，该 IP 的 443 端口被阻断，
于是 `git push` 报：

    fatal: 无法访问 'https://github.com/...'：GnuTLS recv error (-110):
    The TLS connection was non-properly terminated.

但 GitHub 其他官方 IP（140.82.112.3 / 140.82.113.3 / 20.27.177.113 …）的
443 完全正常，且 github.com:22、ssh.github.com:443 也可连。

本脚本只做**纯 TCP 转发**（HTTP CONNECT），TLS 握手仍由 git/curl 完成，
所以 SNI 与证书校验不受影响，也不需要改 /etc/hosts（无需 root）。

用法
----
    python3 scripts/gh_relay.py 7899 &
    git -c http.proxy=http://127.0.0.1:7899 push          # 单次
    # 或对某个仓库长期生效：
    git config http.proxy http://127.0.0.1:7899

长期用建议先确认 IP 可用：`curl -sI --resolve github.com:443:140.82.112.3 https://github.com`

注意：更省事的正解是把 git remote 换成 SSH（`git@github.com:owner/repo.git`），
本脚本只用于必须走 HTTPS 的场合。
"""
import socket
import sys
import threading

LISTEN = ("127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 7899)

# 当 DNS 给出的地址连不上时，按顺序尝试的备用 IP
FALLBACK_IPS = [
    "140.82.112.3",
    "140.82.113.3",
    "140.82.114.3",
    "140.82.116.3",
    "20.27.177.113",
    "20.200.245.247",
]
CONNECT_TIMEOUT = 6.0
_cache = {}
_cache_lock = threading.Lock()


def _try(ip, port, timeout=CONNECT_TIMEOUT):
    try:
        return socket.create_connection((ip, port), timeout=timeout)
    except OSError:
        return None


def connect(host, port):
    """先按 DNS 顺序连，全部失败再试备用 IP；成功结果缓存。"""
    key = (host, port)
    with _cache_lock:
        cached = _cache.get(key)
    if cached:
        sock = _try(cached, port)
        if sock:
            return sock

    candidates = []
    try:
        for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
            ip = info[4][0]
            if ip not in candidates:
                candidates.append(ip)
    except socket.gaierror:
        pass
    candidates += [ip for ip in FALLBACK_IPS if ip not in candidates]

    for ip in candidates:
        sock = _try(ip, port)
        if sock:
            print("relay: %s:%s -> %s:OK" % (host, port, ip), flush=True)
            with _cache_lock:
                _cache[key] = ip
            return sock
    raise OSError("所有候选 IP 均无法连接 %s:%s" % (host, port))


def pipe(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def handle(client):
    try:
        client.settimeout(20)
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = client.recv(4096)
            if not chunk:
                client.close()
                return
            buf += chunk
        line = buf.split(b"\r\n", 1)[0].decode("latin-1")
        parts = line.split()
        if len(parts) < 2 or parts[0].upper() != "CONNECT":
            client.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            client.close()
            return
        host, _, port = parts[1].rpartition(":")
        upstream = connect(host, int(port or 443))
        client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        client.settimeout(None)
        upstream.settimeout(None)
        threading.Thread(target=pipe, args=(client, upstream), daemon=True).start()
        pipe(upstream, client)
    except OSError as exc:
        try:
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
        except OSError:
            pass
        print("relay error:", exc, file=sys.stderr, flush=True)
        client.close()


def main():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(LISTEN)
    srv.listen(64)
    print("relay listening on %s:%d" % LISTEN, flush=True)
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    main()
