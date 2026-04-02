"""
Rate Limiter - Token Bucket Algorithm

Provides a thread-safe token bucket rate limiter that can be used across
multiple modules in the RepoForge system.
"""

import time
import threading
from typing import Optional


class TokenBucketRateLimiter:
    """
    Thread-safe token bucket rate limiter.

    The token bucket algorithm works by:
    - Adding tokens at a constant rate (tokens_per_second)
    - Each operation consumes a specified number of tokens
    - If insufficient tokens, the operation waits or is rejected

    This implementation supports:
    - Concurrency via threading.Lock
    - Burst capacity (max_tokens)
    - Optional timeout for token acquisition
    """

    def __init__(self, tokens_per_second: float, max_tokens: int, initial_tokens: Optional[int] = None):
        """
        Initialize the token bucket rate limiter.

        Args:
            tokens_per_second: Rate at which tokens are added (refill rate)
            max_tokens: Maximum capacity of the bucket (burst size)
            initial_tokens: Initial number of tokens (default: max_tokens)
        """
        if tokens_per_second <= 0:
            raise ValueError("tokens_per_second must be positive")
        if max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        if initial_tokens is not None and not (0 <= initial_tokens <= max_tokens):
            raise ValueError("initial_tokens must be between 0 and max_tokens")

        self.tokens_per_second = tokens_per_second
        self.max_tokens = max_tokens
        self.tokens = initial_tokens if initial_tokens is not None else max_tokens
        self.last_refill_time = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        """Refill tokens based on elapsed time (must be called with lock held)."""
        now = time.monotonic()
        elapsed = now - self.last_refill_time
        if elapsed > 0:
            # Calculate how many tokens to add
            new_tokens = elapsed * self.tokens_per_second
            self.tokens = min(self.max_tokens, self.tokens + new_tokens)
            self.last_refill_time = now

    def try_acquire(self, tokens: int = 1) -> bool:
        """
        Try to acquire tokens without blocking.

        Args:
            tokens: Number of tokens to acquire (default: 1)

        Returns:
            True if tokens were acquired, False otherwise
        """
        if tokens <= 0:
            raise ValueError("tokens must be positive")
        if tokens > self.max_tokens:
            return False  # Request exceeds max capacity

        with self._lock:
            self._refill()
            if self.tokens >= tokens:
                self.tokens -= tokens
                return True
            return False

    def acquire(self, tokens: int = 1, timeout: Optional[float] = None) -> bool:
        """
        Acquire tokens, blocking until available or timeout.

        Args:
            tokens: Number of tokens to acquire (default: 1)
            timeout: Maximum time to wait in seconds (None = wait forever)

        Returns:
            True if tokens were acquired, False if timeout occurred
        """
        if tokens <= 0:
            raise ValueError("tokens must be positive")
        if tokens > self.max_tokens:
            return False  # Request exceeds max capacity

        start_time = time.monotonic()
        while True:
            with self._lock:
                self._refill()
                if self.tokens >= tokens:
                    self.tokens -= tokens
                    return True

            # Check timeout
            if timeout is not None:
                elapsed = time.monotonic() - start_time
                if elapsed >= timeout:
                    return False

            # Sleep a bit to avoid busy-wait (but check again frequently)
            time.sleep(0.001)  # 1ms sleep

    def get_available_tokens(self) -> int:
        """Get current number of available tokens (non-blocking, for monitoring)."""
        with self._lock:
            self._refill()
            return int(self.tokens)

    def reset(self) -> None:
        """Reset the bucket to full capacity."""
        with self._lock:
            self.tokens = self.max_tokens
            self.last_refill_time = time.monotonic()
