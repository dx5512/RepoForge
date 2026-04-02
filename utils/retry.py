"""
Retry Utilities for RepoForge

Provides decorators and functions for handling transient failures with
exponential backoff and configurable retry policies.
"""

import time
import random
import logging
from functools import wraps
from typing import Callable, TypeVar, Tuple, List, Optional, Any

logger = logging.getLogger(__name__)

T = TypeVar("T")


class RetryConfig:
    """Configuration for retry behavior."""

    def __init__(
        self,
        max_attempts: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        backoff_factor: float = 2.0,
        jitter: bool = True,
        exceptions: Tuple[type, ...] = (Exception,),
    ):
        """
        Initialize retry configuration.

        Args:
            max_attempts: Maximum number of attempts (including first try)
            base_delay: Initial delay in seconds
            max_delay: Maximum delay between retries in seconds
            backoff_factor: Multiplier for delay after each failure (exponential backoff)
            jitter: Add random jitter to avoid thundering herd
            exceptions: Tuple of exception types to retry on
        """
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.backoff_factor = backoff_factor
        self.jitter = jitter
        self.exceptions = exceptions

    def calculate_delay(self, attempt: int) -> float:
        """Calculate delay for given attempt number."""
        delay = min(self.base_delay * (self.backoff_factor ** (attempt - 1)), self.max_delay)
        if self.jitter:
            # Add ±20% jitter
            jitter_range = delay * 0.2
            delay += random.uniform(-jitter_range, jitter_range)
        return max(0, delay)


def retry(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    backoff_factor: float = 2.0,
    jitter: bool = True,
    exceptions: Tuple[type, ...] = (Exception,),
    on_retry: Optional[Callable[[int, Exception, float], None]] = None,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """
    Decorator for automatic retry with exponential backoff.

    Usage:
        @retry(max_attempts=3, base_delay=2.0)
        def call_external_api():
            # network call
            pass

    Args:
        max_attempts: Maximum number of attempts
        base_delay: Initial delay in seconds
        max_delay: Maximum delay between retries
        backoff_factor: Exponential multiplier
        jitter: Add random jitter to delay
        exceptions: Which exceptions to retry on
        on_retry: Optional callback(attempt, exception, delay) for logging/metrics

    Returns:
        Decorated function with retry logic
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args, **kwargs) -> T:
            config = RetryConfig(
                max_attempts=max_attempts,
                base_delay=base_delay,
                max_delay=max_delay,
                backoff_factor=backoff_factor,
                jitter=jitter,
                exceptions=exceptions,
            )

            last_exception = None

            for attempt in range(1, config.max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except config.exceptions as e:
                    last_exception = e

                    # Don't retry if this was the last attempt
                    if attempt >= config.max_attempts:
                        logger.error(f"Retry exhausted for {func.__name__} after {attempt} attempts. Last error: {e}")
                        raise

                    delay = config.calculate_delay(attempt)
                    logger.warning(
                        f"Retry {attempt}/{config.max_attempts} for {func.__name__} "
                        f"after error: {e}. Waiting {delay:.2f}s..."
                    )

                    if on_retry:
                        on_retry(attempt, e, delay)

                    time.sleep(delay)

            # Should not reach here, but just in case
            if last_exception:
                raise last_exception
            raise RuntimeError("Retry logic error: no result and no exception")

        return wrapper

    return decorator


def retry_network(
    max_attempts: int = 3,
    base_delay: float = 2.0,
    on_retry: Optional[Callable[[int, Exception, float], None]] = None,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """
    Specialized retry decorator for network operations.

    Retries on common network-related exceptions with sensible defaults.
    """
    from openai import APIConnectionError, APITimeoutError, RateLimitError
    import httpx

    return retry(
        max_attempts=max_attempts,
        base_delay=base_delay,
        max_delay=120.0,
        backoff_factor=2.5,
        jitter=True,
        exceptions=(
            APIConnectionError,
            APITimeoutError,
            RateLimitError,
            httpx.RequestError,
            httpx.TimeoutException,
            ConnectionError,
            TimeoutError,
        ),
        on_retry=on_retry,
    )


def retry_git(max_attempts: int = 3, base_delay: float = 1.0) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """
    Retry decorator for git operations.

    Retries on subprocess.CalledProcessError with exponential backoff.
    """
    import subprocess

    return retry(
        max_attempts=max_attempts,
        base_delay=base_delay,
        max_delay=30.0,
        backoff_factor=2.0,
        jitter=True,
        exceptions=(subprocess.CalledProcessError,),
    )


def retry_docker(max_attempts: int = 2, base_delay: float = 2.0) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """
    Retry decorator for Docker operations.

    Docker operations are less frequent, so fewer retries but longer delays.
    """
    import docker

    return retry(
        max_attempts=max_attempts,
        base_delay=base_delay,
        max_delay=60.0,
        backoff_factor=2.0,
        jitter=True,
        exceptions=(
            docker.errors.APIError,
            docker.errors.DockerException,
        ),
    )


class RetryMetricCollector:
    """Collect metrics about retry behavior for monitoring."""

    def __init__(self):
        self.attempt_counts: List[int] = []
        self.total_retries: int = 0
        self.total_failures: int = 0
        self.total_successes: int = 0

    def record_attempts(self, attempts: int):
        """Record how many attempts were needed for success."""
        self.attempt_counts.append(attempts)
        self.total_successes += 1

    def record_failure(self, attempts: int):
        """Record a final failure after retries."""
        self.total_failures += 1

    @property
    def average_attempts(self) -> float:
        """Average number of attempts per successful call."""
        if not self.attempt_counts:
            return 0.0
        return sum(self.attempt_counts) / len(self.attempt_counts)

    @property
    def retry_rate(self) -> float:
        """Percentage of calls that needed at least one retry."""
        if not self.attempt_counts:
            return 0.0
        retried = sum(1 for a in self.attempt_counts if a > 1)
        return (retried / len(self.attempt_counts)) * 100

    def summary(self) -> dict:
        """Get summary statistics."""
        return {
            "total_successes": self.total_successes,
            "total_failures": self.total_failures,
            "average_attempts": round(self.average_attempts, 2),
            "retry_rate_percent": round(self.retry_rate, 2),
            "total_retries": sum(a - 1 for a in self.attempt_counts),
        }


# Global metric collector for convenience
global_metrics = RetryMetricCollector()


def metrics_aware_retry(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    exceptions: Tuple[type, ...] = (Exception,),
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """
    Retry decorator that automatically records metrics.

    Args:
        max_attempts: Maximum retry attempts
        base_delay: Base delay for backoff
        exceptions: Exception types to retry

    Returns:
        Decorated function
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args, **kwargs) -> T:
            last_exception = None

            for attempt in range(1, max_attempts + 1):
                try:
                    result = func(*args, **kwargs)
                    if attempt == 1:
                        global_metrics.total_successes += 1
                    else:
                        global_metrics.attempt_counts.append(attempt)
                    return result
                except exceptions as e:
                    last_exception = e

                    if attempt >= max_attempts:
                        global_metrics.total_failures += 1
                        raise

                    delay = min(base_delay * (2.0 ** (attempt - 1)), 60.0)
                    time.sleep(delay)

            if last_exception:
                raise last_exception
            raise RuntimeError("Retry logic error")

        return wrapper

    return decorator


if __name__ == "__main__":
    # Simple demonstration
    import sys

    logging.basicConfig(level=logging.INFO)

    @retry_network(max_attempts=3, base_delay=0.5)
    def flaky_function(should_fail: bool = True):
        if should_fail:
            raise ConnectionError("Simulated network failure")
        return "Success!"

    try:
        result = flaky_function(should_fail=True)
        print(result)
    except Exception as e:
        print(f"Failed after retries: {e}")

    print("\nMetrics:", global_metrics.summary())
