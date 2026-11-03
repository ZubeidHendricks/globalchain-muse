"""Per-IP rate limits for the public endpoints that run models or move test funds.

A sliding one-minute window per client IP, in memory (one server process). Adapted from VeriChainX
Sol's MCP limiter. Pure ASGI, so MCP's streamed responses pass through untouched.
"""

import json
import time
from collections import defaultdict, deque

# Paths whose requests can call Muse Spark / SAM 3.1 or write to Hedera.
LIMITED_PREFIXES = ("/mcp", "/demo/", "/v1/verify-product", "/v1/seller-risk", "/v1/registrations", "/v1/records",
                    "/v1/orders/")


def limited(method: str, path: str) -> bool:
    if path.startswith("/mcp"):
        return True
    return method == "POST" and path.startswith(LIMITED_PREFIXES)


class RateLimiter:
    def __init__(self, per_minute: int, window: float = 60.0):
        self.per_minute, self.window = per_minute, window
        self.hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, now: float | None = None) -> float | None:
        """None if allowed, else the seconds until the next request would be."""
        now = time.monotonic() if now is None else now
        q = self.hits[key]
        while q and q[0] <= now - self.window:
            q.popleft()
        if len(q) >= self.per_minute:
            return q[0] + self.window - now
        q.append(now)
        if len(self.hits) > 10_000:  # forget idle clients
            for k in [k for k, v in self.hits.items() if not v or v[-1] <= now - self.window]:
                del self.hits[k]
        return None


def client_ip(scope: dict, trust_proxy: bool) -> str:
    if trust_proxy:
        for name, value in scope.get("headers") or []:
            if name == b"x-forwarded-for":
                # The proxy appends the address it saw, so the last entry is the one a client can't forge.
                return value.decode("latin-1").split(",")[-1].strip()
    client = scope.get("client")
    return client[0] if client else "unknown"


class RateLimitMiddleware:
    def __init__(self, app, per_minute: int, trust_proxy: bool = False):
        self.app, self.trust_proxy = app, trust_proxy
        self.limiter = RateLimiter(per_minute) if per_minute > 0 else None

    async def __call__(self, scope, receive, send):
        if self.limiter and scope["type"] == "http" and limited(scope["method"], scope["path"]):
            wait = self.limiter.allow(client_ip(scope, self.trust_proxy))
            if wait is not None:
                retry = max(1, round(wait))
                body = json.dumps({"detail": f"Too many requests: {self.limiter.per_minute} a minute per client. "
                                             f"Try again in {retry}s."}).encode()
                await send({"type": "http.response.start", "status": 429,
                            "headers": [(b"content-type", b"application/json"), (b"retry-after", str(retry).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)
