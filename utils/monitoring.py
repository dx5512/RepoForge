"""
Monitoring - Prometheus Metrics for RepoForge

Provides metrics collection and an HTTP endpoint for Prometheus scraping.
Call `setup_monitoring()` at application startup to begin collecting metrics.
"""

import threading
import time
from typing import Optional
from prometheus_client import Counter, Histogram, Gauge, start_http_server, REGISTRY

# === Metrics Definition ===

# Task metrics
tasks_started_total = Counter(
    'repoforge_tasks_started_total',
    'Total number of tasks started'
)
tasks_completed_total = Counter(
    'repoforge_tasks_completed_total',
    'Total number of tasks completed',
    ['status']  # status labels: success, failure
)
task_duration_seconds = Histogram(
    'repoforge_task_duration_seconds',
    'Task execution duration',
    buckets=[1, 5, 10, 30, 60, 120, 300, 600]  # up to 10 minutes
)

# Agent metrics
agent_iterations = Histogram(
    'repoforge_agent_iterations_total',
    'Number of iterations used by the agent',
    ['agent']  # agent labels: planner, coder, reviewer
)
agent_api_call_duration = Histogram(
    'repoforge_api_call_seconds',
    'Duration of OpenAI API calls',
    ['agent']
)

# Sandbox metrics
container_create_total = Counter(
    'repoforge_container_create_total',
    'Number of Docker container create operations'
)
container_destroy_total = Counter(
    'repoforge_container_destroy_total',
    'Number of Docker container destroy operations'
)
worktree_create_total = Counter(
    'repoforge_worktree_create_total',
    'Number of git worktree create operations'
)
worktree_remove_total = Counter(
    'repoforge_worktree_remove_total',
    'Number of git worktree remove operations'
)

# Queue/Concurrency metrics
active_tasks = Gauge(
    'repoforge_active_tasks',
    'Number of currently executing tasks'
)


# === Monitoring Server ===

_metrics_server_started = False
_metrics_server_lock = threading.Lock()


def setup_monitoring(port: int = 8000, disable: bool = False) -> None:
    """
    Start the Prometheus metrics HTTP server in a background thread.

    Args:
        port: Port to listen on (default: 8000)
        disable: If True, do not start the server (for disabling via config)
    """
    global _metrics_server_started
    if disable:
        return

    with _metrics_server_lock:
        if _metrics_server_started:
            return
        try:
            start_http_server(port)
            _metrics_server_started = True
            # Use a logger if available, otherwise print
            try:
                import structlog
                logger = structlog.get_logger(__name__)
                logger.info("Prometheus metrics server started", port=port)
            except Exception:
                print(f"[monitoring] Prometheus metrics server started on port {port}")
        except OSError as e:
            # Port already in use, etc.
            print(f"[monitoring] Failed to start metrics server on port {port}: {e}")


# === Context Managers for Timing ===

class Timer:
    """Context manager to time operations and record to Prometheus histogram."""
    def __init__(self, metric, labels: Optional[dict] = None):
        self.metric = metric
        self.labels = labels or {}
        self.start_time = None

    def __enter__(self):
        self.start_time = time.time()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.start_time is not None:
            duration = time.time() - self.start_time
            if self.labels:
                self.metric.labels(**self.labels).observe(duration)
            else:
                self.metric.observe(duration)


# Convenience functions for common timing
def time_agent_call(agent_name: str):
    """Decorator to time agent function calls."""
    def decorator(func):
        def wrapper(*args, **kwargs):
            with Timer(agent_api_call_duration, labels={'agent': agent_name}):
                return func(*args, **kwargs)
        return wrapper
    return decorator


def track_task_metrics():
    """Decorator to track task execution metrics (duration, count, etc)."""
    def decorator(func):
        def wrapper(*args, **kwargs):
            start = time.time()
            tasks_started_total.inc()
            active_tasks.inc()
            try:
                result = func(*args, **kwargs)
                duration = time.time() - start
                task_duration_seconds.observe(duration)
                tasks_completed_total.labels(status='success').inc()
                return result
            except Exception as e:
                duration = time.time() - start
                task_duration_seconds.observe(duration)
                tasks_completed_total.labels(status='failure').inc()
                raise e
            finally:
                active_tasks.dec()
        return wrapper
    return decorator
