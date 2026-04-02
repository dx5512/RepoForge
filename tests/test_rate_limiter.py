"""
Unit tests for TokenBucketRateLimiter

Comprehensive test coverage including:
- Normal token acquisition
- Token exhaustion and rejection
- Multi-threaded concurrent access
- Timeout behavior
- Refill rate accuracy
- Edge cases and boundary conditions
"""

import pytest
import time
import threading
from utils.rate_limiter import TokenBucketRateLimiter


class TestTokenBucketRateLimiter:
    """Test suite for TokenBucketRateLimiter."""

    def test_init_default_values(self):
        """Test initialization with default initial tokens."""
        limiter = TokenBucketRateLimiter(tokens_per_second=10, max_tokens=100)
        assert limiter.tokens == 100
        assert limiter.max_tokens == 100
        assert limiter.tokens_per_second == 10

    def test_init_custom_initial(self):
        """Test initialization with custom initial tokens."""
        limiter = TokenBucketRateLimiter(tokens_per_second=10, max_tokens=100, initial_tokens=50)
        assert limiter.tokens == 50

    def test_init_invalid_tokens_per_second(self):
        """Test that negative tokens_per_second raises ValueError."""
        with pytest.raises(ValueError, match="tokens_per_second must be positive"):
            TokenBucketRateLimiter(tokens_per_second=0, max_tokens=100)
        with pytest.raises(ValueError):
            TokenBucketRateLimiter(tokens_per_second=-5, max_tokens=100)

    def test_init_invalid_max_tokens(self):
        """Test that negative max_tokens raises ValueError."""
        with pytest.raises(ValueError, match="max_tokens must be positive"):
            TokenBucketRateLimiter(tokens_per_second=10, max_tokens=0)
        with pytest.raises(ValueError):
            TokenBucketRateLimiter(tokens_per_second=10, max_tokens=-1)

    def test_init_invalid_initial_tokens(self):
        """Test that invalid initial_tokens raises ValueError."""
        with pytest.raises(ValueError, match="initial_tokens must be between"):
            TokenBucketRateLimiter(tokens_per_second=10, max_tokens=100, initial_tokens=-1)
        with pytest.raises(ValueError):
            TokenBucketRateLimiter(tokens_per_second=10, max_tokens=100, initial_tokens=101)

    def test_try_acquire_single_token(self):
        """Test acquiring a single token."""
        limiter = TokenBucketRateLimiter(tokens_per_second=10, max_tokens=10, initial_tokens=10)
        assert limiter.try_acquire() is True
        assert limiter.get_available_tokens() == 9

    def test_try_acquire_multiple_tokens(self):
        """Test acquiring multiple tokens at once."""
        limiter = TokenBucketRateLimiter(tokens_per_second=10, max_tokens=100, initial_tokens=100)
        assert limiter.try_acquire(10) is True
        assert limiter.get_available_tokens() == 90
        assert limiter.try_acquire(89) is True
        assert limiter.get_available_tokens() == 1
        assert limiter.try_acquire(2) is False  # Not enough tokens
        assert limiter.get_available_tokens() == 1  # No change

    def test_try_acquire_exhausts_tokens(self):
        """Test that try_acquire returns False when tokens are depleted."""
        limiter = TokenBucketRateLimiter(tokens_per_second=10, max_tokens=5, initial_tokens=5)
        assert limiter.try_acquire(5) is True
        assert limiter.get_available_tokens() == 0
        assert limiter.try_acquire(1) is False
        assert limiter.get_available_tokens() == 0

    def test_try_acquire_negative_tokens(self):
        """Test that acquiring negative tokens raises ValueError."""
        limiter = TokenBucketRateLimiter(tokens_per_second=10, max_tokens=100)
        with pytest.raises(ValueError, match="tokens must be positive"):
            limiter.try_acquire(-1)

    def test_try_acquire_exceeds_max(self):
        """Test that trying to acquire more than max_tokens fails immediately."""
        limiter = TokenBucketRateLimiter(tokens_per_second=10, max_tokens=50, initial_tokens=50)
        assert limiter.try_acquire(51) is False
        assert limiter.get_available_tokens() == 50  # No tokens consumed

    def test_refill_replenishes_tokens(self):
        """Test that tokens are replenished after time passes."""
        limiter = TokenBucketRateLimiter(tokens_per_second=10, max_tokens=100, initial_tokens=0)
        assert limiter.get_available_tokens() == 0
        time.sleep(0.1)  # Wait 100ms -> should add ~1 token
        assert limiter.try_acquire(1) is True  # At least 1 token should be available
        # More accurate: 0.1s * 10 tokens/s = 1 token
        time.sleep(0.5)  # Wait 500ms -> should add ~5 tokens
        assert 4 <= limiter.get_available_tokens() <= 6  # Allow some timing variance

    def test_refill_respects_max_capacity(self):
        """Test that refill does not exceed max_tokens."""
        limiter = TokenBucketRateLimiter(tokens_per_second=100, max_tokens=10, initial_tokens=10)
        time.sleep(0.2)  # Would add 20 tokens if uncapped
        tokens = limiter.get_available_tokens()
        assert tokens <= 10  # Should be capped at max

    def test_acquire_blocking_until_available(self):
        """Test that acquire() blocks until tokens are available."""
        limiter = TokenBucketRateLimiter(tokens_per_second=10, max_tokens=5, initial_tokens=0)

        def waiter():
            start = time.monotonic()
            success = limiter.acquire(1)
            elapsed = time.monotonic() - start
            assert success is True
            assert elapsed >= 0.09  # ~100ms wait for 1 token at 10/s
            return True

        thread = threading.Thread(target=waiter)
        thread.start()
        time.sleep(0.15)  # Let it wait
        # Now add tokens from another "thread" (refill happens automatically)
        # The waiter should unblock after ~100ms
        thread.join(timeout=2.0)
        assert not thread.is_alive(), "Thread did not unblock in time"

    def test_acquire_with_timeout(self):
        """Test that acquire() respects timeout and returns False."""
        limiter = TokenBucketRateLimiter(tokens_per_second=10, max_tokens=5, initial_tokens=0)

        def waiter():
            start = time.monotonic()
            result = limiter.acquire(1, timeout=0.1)
            elapsed = time.monotonic() - start
            assert result is False
            assert 0.09 <= elapsed <= 0.15  # Timeout ~100ms

        thread = threading.Thread(target=waiter)
        thread.start()
        thread.join(timeout=1.0)
        assert not thread.is_alive(), "Thread did not timeout"

    def test_acquire_multiple_tokens_blocking(self):
        """Test blocking acquire with multiple tokens."""
        limiter = TokenBucketRateLimiter(tokens_per_second=100, max_tokens=100, initial_tokens=50)

        def waiter():
            # Should block until enough tokens accumulate
            start = time.monotonic()
            success = limiter.acquire(100)  # Need 100, only 50 available
            elapsed = time.monotonic() - start
            assert success is True
            assert elapsed >= 0.4  # Need 50 more tokens at 100/s = 0.5s
            assert elapsed < 1.0

        # First consume some tokens
        assert limiter.try_acquire(50) is True
        assert limiter.get_available_tokens() == 0

        thread = threading.Thread(target=waiter)
        thread.start()
        time.sleep(0.6)  # Let it wait enough time
        thread.join(timeout=2.0)
        assert not thread.is_alive()

    def test_reset_restores_full_capacity(self):
        """Test that reset() restores the bucket to full capacity."""
        limiter = TokenBucketRateLimiter(tokens_per_second=10, max_tokens=100, initial_tokens=100)
        limiter.try_acquire(50)
        assert limiter.get_available_tokens() == 50
        limiter.reset()
        assert limiter.get_available_tokens() == 100

    def test_concurrent_acquire_thread_safety(self):
        """
        Test thread safety: multiple threads acquiring tokens concurrently
        should not corrupt state and total consumed should not exceed total.
        """
        limiter = TokenBucketRateLimiter(tokens_per_second=1000, max_tokens=1000, initial_tokens=1000)
        num_threads = 20
        tokens_per_thread = 50
        total_expected = num_threads * tokens_per_thread
        successes = []
        threads = []

        def worker():
            success = limiter.acquire(tokens_per_thread, timeout=1.0)
            successes.append(success)

        for _ in range(num_threads):
            t = threading.Thread(target=worker)
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        # All threads should succeed because we have enough tokens
        assert len(successes) == num_threads
        assert all(successes)
        # Total tokens remaining should be 0 or small (due to refill during test)
        remaining = limiter.get_available_tokens()
        # Allow some refill (1000 tokens/s * ~0.1s test duration = ~100 tokens)
        assert 0 <= remaining <= 200
        # The sum of consumed tokens is approximately total_expected
        # (we can't verify exactly due to possible timing of refill)

    def test_concurrent_try_acquire_contention(self):
        """
        Test high-contention scenario with try_acquire (non-blocking).
        Some threads should succeed, others fail depending on token availability.
        """
        limiter = TokenBucketRateLimiter(tokens_per_second=10, max_tokens=100, initial_tokens=100)
        num_threads = 50
        results = []
        threads = []

        def worker():
            success = limiter.try_acquire(2)  # Each tries to take 2
            results.append(success)

        for _ in range(num_threads):
            t = threading.Thread(target=worker)
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        # With 100 tokens and 50 threads each taking 2, max possible successes = 50
        # But due to race conditions, we expect ~50 successes but less than or equal to 50
        assert len(results) == num_threads
        success_count = sum(results)
        # Expect close to 50 but may be slightly less due to race conditions
        assert 45 <= success_count <= 50
        # Total tokens consumed should be success_count * 2, should not exceed capacity
        assert success_count * 2 <= 100 + 5  # Allow small overage due to timing

    def test_acquire_with_very_low_rate(self):
        """Test behavior with very low refill rate (edge case)."""
        limiter = TokenBucketRateLimiter(tokens_per_second=0.1, max_tokens=10, initial_tokens=10)
        assert limiter.try_acquire(10) is True
        assert limiter.get_available_tokens() == 0
        # Wait for 10 seconds to get 1 token back
        start = time.monotonic()
        assert limiter.acquire(1, timeout=12) is True
        elapsed = time.monotonic() - start
        assert elapsed >= 9  # At least 9 seconds (close to 10)

    def test_get_available_tokens_without_refill_race(self):
        """
        Test that get_available_tokens correctly reports current tokens
        even when called concurrently with acquires.
        """
        limiter = TokenBucketRateLimiter(tokens_per_second=100, max_tokens=100, initial_tokens=100)
        results = []

        def reader():
            for _ in range(50):
                results.append(limiter.get_available_tokens())
                time.sleep(0.001)

        def writer():
            for _ in range(50):
                limiter.try_acquire(1)
                time.sleep(0.001)

        t1 = threading.Thread(target=reader)
        t2 = threading.Thread(target=writer)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # All readings should be between 0 and max_tokens
        assert all(0 <= r <= 100 for r in results)

    def test_timeout_precision(self):
        """Test that timeout is reasonably accurate (not much longer)."""
        limiter = TokenBucketRateLimiter(tokens_per_second=10, max_tokens=5, initial_tokens=0)

        start = time.monotonic()
        result = limiter.acquire(1, timeout=0.2)
        elapsed = time.monotonic() - start

        assert result is False
        assert 0.18 <= elapsed <= 0.3  # Allow some tolerance but not excessive

    def test_acquire_after_partial_wait(self):
        """
        Test that if tokens become available during a blocking acquire,
        it returns immediately without waiting for full timeout.
        """
        limiter = TokenBucketRateLimiter(tokens_per_second=100, max_tokens=10, initial_tokens=5)

        results = {}

        def waiter():
            start = time.monotonic()
            limiter.acquire(5)  # Will need to wait for ~50ms at 100/s
            elapsed = time.monotonic() - start
            results['waiter_elapsed'] = elapsed

        def filler():
            # Wait a bit then fill the bucket
            time.sleep(0.1)
            # No action needed - refill happens automatically in waiter's loop

        t1 = threading.Thread(target=waiter)
        t2 = threading.Thread(target=filler)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Waiter should complete quickly (~50-100ms) not wait full timeout
        assert 'waiter_elapsed' in results
        assert 0.04 <= results['waiter_elapsed'] <= 0.2

    def test_concurrent_multiple_token_requests(self):
        """
        Stress test: many threads requesting different token amounts.
        Verify no deadlock and total tokens used is consistent.
        """
        limiter = TokenBucketRateLimiter(tokens_per_second=1000, max_tokens=500, initial_tokens=500)
        num_threads = 10
        tokens_list = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
        results = [None] * num_threads
        threads = []

        def worker(idx):
            tokens = tokens_list[idx]
            success = limiter.acquire(tokens, timeout=2.0)
            results[idx] = (tokens if success else 0)

        for i in range(num_threads):
            t = threading.Thread(target=worker, args=(i,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        total_consumed = sum(r for r in results if r is not None)
        # Should consume all 500 tokens (or close to it)
        assert 400 <= total_consumed <= 500


class TestTokenBucketEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_single_token_operations(self):
        """Test that single-token operations work correctly."""
        limiter = TokenBucketRateLimiter(tokens_per_second=1, max_tokens=1, initial_tokens=1)
        assert limiter.try_acquire() is True
        assert limiter.try_acquire() is False

    def test_large_bucket(self):
        """Test with very large max_tokens."""
        limiter = TokenBucketRateLimiter(tokens_per_second=1000, max_tokens=10**6, initial_tokens=10**6)
        assert limiter.try_acquire(10**5) is True
        assert limiter.get_available_tokens() == 900000

    def test_fractional_refill_rate(self):
        """Test with fractional tokens per second."""
        limiter = TokenBucketRateLimiter(tokens_per_second=0.5, max_tokens=10, initial_tokens=10)
        limiter.try_acquire(10)
        time.sleep(2.1)  # Should replenish ~1 token
        tokens = limiter.get_available_tokens()
        assert 0 <= tokens <= 2  # Allow some tolerance

    def test_immediate_refill_on_access(self):
        """Test that tokens are refilled immediately before any operation."""
        limiter = TokenBucketRateLimiter(tokens_per_second=10, max_tokens=10, initial_tokens=5)
        time.sleep(1.0)  # Should have refilled to 10+ (but capped at 10)
        assert limiter.get_available_tokens() == 10  # Should be capped at max

    def test_multiple_acquires_with_same_lock(self):
        """
        Test that multiple consecutive acquires from same thread
        work correctly (lock is properly released between calls).
        """
        limiter = TokenBucketRateLimiter(tokens_per_second=100, max_tokens=100, initial_tokens=100)
        assert limiter.try_acquire(10)
        assert limiter.try_acquire(20)
        assert limiter.try_acquire(30)
        assert limiter.get_available_tokens() == 40
