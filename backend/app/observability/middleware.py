"""覆盖整个 ASGI 响应生命周期，包含 SSE 错误与断连。"""
from app.observability.tracing import bind_trace, exception_status, new_trace


class ChatTraceMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") != "/api/chat" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return
        trace = new_trace()
        if trace is None:
            await self.app(scope, receive, send)
            return
        status_code, complete, failure = None, False, None

        async def observed_receive():
            message = await receive()
            if message["type"] == "http.disconnect" and not complete:
                with trace.lock:
                    trace.disconnected = True
            return message

        async def observed_send(message):
            nonlocal status_code, complete
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                complete = True

        with bind_trace(trace):
            try:
                await self.app(scope, observed_receive, observed_send)
            except BaseException as exc:
                failure = exception_status(exc)
                if failure == "error":
                    trace.error_code = getattr(exc, "code", type(exc).__name__)
                else:
                    trace.disconnected = True
                raise
            finally:
                if failure == "error" or trace.error_code or (status_code or 0) >= 400:
                    status = "error"
                elif not trace.saved and (failure == "cancelled" or not complete or trace.disconnected):
                    status = "cancelled"
                else:
                    status = "ok"
                trace.finish(status, http_status=status_code, transport_complete=complete)
