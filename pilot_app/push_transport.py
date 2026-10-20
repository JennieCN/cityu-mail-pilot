"""Single POST transport with a pinned public IP, normal TLS, and no proxies."""
from __future__ import annotations

import http.client
import socket
import ssl
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

from .push import validate_endpoint


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self,host,address,*,port=443,timeout=3,context=None):
        super().__init__(host,port=port,timeout=timeout,context=context or ssl.create_default_context())
        self.address=address

    def connect(self):
        # Only the previously validated numeric address is passed to the socket.
        self.sock=socket.create_connection((self.address,self.port),self.timeout)
        try:
            self.sock=self._context.wrap_socket(self.sock,server_hostname=self.host,do_handshake_on_connect=False)
            self.sock.do_handshake()
        except Exception:
            self.close()
            raise


class PinnedSession:
    """The narrow session.post interface used by the inspected synchronous SDK.

    DNS is resolved once before connecting. The network deadline shuts down the
    actual socket, rather than abandoning a thread with a request still running.
    Resolver and connection injection are for explicit synthetic tests only.
    """
    def __init__(self,*,resolver=socket.getaddrinfo,connection_factory=PinnedHTTPSConnection,deadline=10):
        self.resolver=resolver
        self.connection_factory=connection_factory
        self.deadline=max(0.1,min(float(deadline),10))

    def post(self,url,*,data,headers,timeout=None,**kwargs):
        start=time.monotonic()
        budget=min(self.deadline,float(timeout) if timeout else self.deadline)
        if kwargs:
            raise ValueError("Unsupported push transport options")
        host,addresses=validate_endpoint(url,self.resolver)
        remaining=budget-(time.monotonic()-start)
        if remaining<=0:
            raise TimeoutError("Push resolution deadline")
        if not isinstance(data,bytes) or len(data)>4096:
            raise ValueError("Invalid encrypted push payload")
        if any("\r" in str(k)+str(v) or "\n" in str(k)+str(v) for k,v in headers.items()):
            raise ValueError("Invalid push headers")
        connection=self.connection_factory(host,addresses[0],timeout=min(3,remaining))
        expired=threading.Event()
        def abort():
            expired.set()
            sock=connection.sock
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                connection.close()
        timer=threading.Timer(remaining,abort)
        timer.daemon=True
        timer.start()
        try:
            parsed=urlsplit(url)
            path=parsed.path+("?"+parsed.query if parsed.query else "")
            connection.connect()
            remaining=budget-(time.monotonic()-start)
            if remaining<=0 or expired.is_set():
                raise TimeoutError("Push connection deadline")
            connection.sock.settimeout(remaining)
            # A deadline can close the socket between this check and request().
            # HTTPConnection must not silently reconnect after the timer fired.
            connection.auto_open=0
            # HTTP/TLS framing and certificate verification stay in the standard library.
            connection.request("POST",path,body=data,headers=dict(headers,Host=host))
            remaining=budget-(time.monotonic()-start)
            if remaining<=0 or expired.is_set():
                raise TimeoutError("Push deadline")
            if connection.sock:
                connection.sock.settimeout(remaining)
            response=connection.getresponse()
            retry_after=response.getheader("Retry-After","")[:128]
            # The SDK eagerly evaluates .text even when verbose=False. Read zero
            # body bytes, close immediately, and never expose provider error text.
            result=SimpleNamespace(status_code=response.status,reason="Push service response",
                text="",headers={"Retry-After":retry_after} if retry_after else {})
            if expired.is_set():
                raise TimeoutError("Push deadline")
            return result
        finally:
            timer.cancel()
            connection.close()
