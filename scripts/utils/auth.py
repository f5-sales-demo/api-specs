"""Authentication and rate limiting for F5 XC API."""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field

import httpx
from rich.console import Console

console = Console()


@dataclass
class RateLimitConfig:
    """Rate limiting configuration with adaptive backoff."""

    # Initial conservative limits (will adapt based on API responses)
    requests_per_minute: int = 30
    requests_per_second: int = 2
    min_request_interval: float = 0.5  # seconds

    # Backoff configuration
    initial_backoff: float = 1.0
    max_backoff: float = 60.0
    backoff_multiplier: float = 2.0

    # Adaptive rate limiting
    adaptive: bool = True
    decrease_factor: float = 0.8  # Reduce rate by 20% on rate limit hit
    increase_factor: float = 1.1  # Increase rate by 10% on success streak
    success_streak_threshold: int = 50  # Successes before increasing rate


class RateLimiter:
    """Adaptive rate limiter with sliding window and exponential backoff."""

    def __init__(self, config: RateLimitConfig | None = None):
        self.config = config or RateLimitConfig()
        self._lock = threading.Lock()
        self._request_times: deque = deque(maxlen=100)
        self._current_backoff = self.config.initial_backoff
        self._success_streak = 0
        self._current_rpm = self.config.requests_per_minute

    def wait_if_needed(self) -> None:
        """Block until it's safe to make another request."""
        with self._lock:
            now = time.time()

            # Clean old entries (older than 60 seconds)
            while self._request_times and now - self._request_times[0] > 60:
                self._request_times.popleft()

            # Check requests per minute
            if len(self._request_times) >= self._current_rpm:
                oldest = self._request_times[0]
                wait_time = 60 - (now - oldest) + 0.1
                if wait_time > 0:
                    console.print(f"[yellow]Rate limit: waiting {wait_time:.1f}s[/yellow]")
                    time.sleep(wait_time)
                    now = time.time()

            # Enforce minimum interval between requests
            if self._request_times:
                last_request = self._request_times[-1]
                elapsed = now - last_request
                if elapsed < self.config.min_request_interval:
                    time.sleep(self.config.min_request_interval - elapsed)

            self._request_times.append(time.time())

    def record_success(self) -> None:
        """Record a successful request for adaptive rate limiting."""
        if not self.config.adaptive:
            return

        with self._lock:
            self._success_streak += 1
            self._current_backoff = self.config.initial_backoff

            # Gradually increase rate after success streak
            if self._success_streak >= self.config.success_streak_threshold:
                new_rpm = min(
                    int(self._current_rpm * self.config.increase_factor),
                    self.config.requests_per_minute * 2,  # Cap at 2x initial
                )
                if new_rpm > self._current_rpm:
                    self._current_rpm = new_rpm
                    console.print(f"[green]Rate limit increased to {self._current_rpm} RPM[/green]")
                self._success_streak = 0

    def record_rate_limit(self) -> float:
        """Record a rate limit hit and return backoff duration."""
        with self._lock:
            self._success_streak = 0

            # Decrease rate limit
            if self.config.adaptive:
                self._current_rpm = max(
                    5,  # Minimum 5 RPM
                    int(self._current_rpm * self.config.decrease_factor),
                )
                console.print(f"[red]Rate limit hit, reduced to {self._current_rpm} RPM[/red]")

            # Calculate backoff
            backoff = self._current_backoff
            self._current_backoff = min(
                self._current_backoff * self.config.backoff_multiplier,
                self.config.max_backoff,
            )

            return backoff

    def get_stats(self) -> dict:
        """Return current rate limiter statistics."""
        with self._lock:
            return {
                "current_rpm": self._current_rpm,
                "success_streak": self._success_streak,
                "current_backoff": self._current_backoff,
                "requests_in_window": len(self._request_times),
            }


@dataclass
class F5XCAuth:
    """F5 XC API authentication handler with rate limiting."""

    api_url: str = field(
        default_factory=lambda: os.getenv(
            "F5XC_API_URL", "https://f5-amer-ent.console.ves.volterra.io"
        )
    )
    api_token: str = field(default_factory=lambda: os.getenv("F5XC_API_TOKEN", ""))
    namespace: str = field(default_factory=lambda: os.getenv("F5XC_NAMESPACE", "r-mordasiewicz"))
    tenant: str = field(default_factory=lambda: os.getenv("F5XC_TENANT", "f5-amer-ent"))
    timeout: int = 30
    retries: int = 3

    _rate_limiter: RateLimiter = field(default_factory=RateLimiter, init=False)
    _client: httpx.Client | None = field(default=None, init=False)

    def __post_init__(self):
        if not self.api_token:
            raise ValueError(
                "F5XC_API_TOKEN environment variable not set. Please set it with your API token."
            )

    @property
    def headers(self) -> dict[str, str]:
        """Return authentication headers."""
        return {
            "Authorization": f"APIToken {self.api_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    @property
    def client(self) -> httpx.Client:
        """Return or create HTTP client."""
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.api_url,
                headers=self.headers,
                timeout=self.timeout,
            )
        return self._client

    def close(self) -> None:
        """Close HTTP client."""
        if self._client:
            self._client.close()
            self._client = None

    def request(
        self,
        method: str,
        path: str,
        **kwargs,
    ) -> httpx.Response:
        """Make an authenticated request with rate limiting and retries."""
        last_exception = None

        for attempt in range(self.retries):
            try:
                # Wait for rate limiter
                self._rate_limiter.wait_if_needed()

                # Make request
                response = self.client.request(method, path, **kwargs)

                # Check for rate limiting response
                if response.status_code == 429:
                    backoff = self._rate_limiter.record_rate_limit()

                    # Check for Retry-After header
                    retry_after = response.headers.get("Retry-After")
                    if retry_after:
                        try:
                            backoff = max(backoff, float(retry_after))
                        except ValueError:
                            pass

                    console.print(
                        f"[yellow]Rate limited, backing off {backoff:.1f}s "
                        f"(attempt {attempt + 1}/{self.retries})[/yellow]"
                    )
                    time.sleep(backoff)
                    continue

                # Success
                self._rate_limiter.record_success()
                return response

            except httpx.TimeoutException as e:
                last_exception = e
                console.print(f"[yellow]Timeout on attempt {attempt + 1}/{self.retries}[/yellow]")
                time.sleep(self._rate_limiter.config.initial_backoff * (attempt + 1))

            except httpx.RequestError as e:
                last_exception = e
                console.print(
                    f"[red]Request error: {e} (attempt {attempt + 1}/{self.retries})[/red]"
                )
                time.sleep(self._rate_limiter.config.initial_backoff * (attempt + 1))

        raise last_exception or RuntimeError("Request failed after all retries")

    def get(self, path: str, **kwargs) -> httpx.Response:
        """Make a GET request."""
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs) -> httpx.Response:
        """Make a POST request."""
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs) -> httpx.Response:
        """Make a PUT request."""
        return self.request("PUT", path, **kwargs)

    def delete(self, path: str, **kwargs) -> httpx.Response:
        """Make a DELETE request."""
        return self.request("DELETE", path, **kwargs)

    def test_connection(self) -> bool:
        """Test API connectivity and authentication."""
        try:
            # Use a lightweight endpoint to test
            response = self.get(f"/api/config/namespaces/{self.namespace}")
            if response.status_code == 200:
                console.print("[green]API connection successful[/green]")
                return True
            console.print(f"[red]API connection failed: {response.status_code}[/red]")
            return False
        except Exception as e:
            console.print(f"[red]API connection error: {e}[/red]")
            return False

    def format_endpoint(self, template: str, **kwargs) -> str:
        """Format an endpoint template with namespace and other params."""
        return template.format(
            namespace=self.namespace,
            tenant=self.tenant,
            **kwargs,
        )

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def load_auth_from_config(config: dict) -> F5XCAuth:
    """Create F5XCAuth from configuration dictionary."""
    api_config = config.get("api", {})
    return F5XCAuth(
        api_url=api_config.get("base_url", os.getenv("F5XC_API_URL", "")),
        api_token=os.getenv("F5XC_API_TOKEN", ""),
        namespace=api_config.get("namespace", os.getenv("F5XC_NAMESPACE", "")),
        tenant=api_config.get("tenant", os.getenv("F5XC_TENANT", "")),
        timeout=api_config.get("timeout", 30),
        retries=api_config.get("retries", 3),
    )
