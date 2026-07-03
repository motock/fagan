class RateLimiter:
    """A simple token-bucket rate limiter."""

    def __init__(self, capacity, refill_rate, now=0.0):
        if capacity <= 0 or refill_rate <= 0:
            raise ValueError("capacity and refill_rate must be positive")
        self.capacity = float(capacity)
        self.refill_rate = float(refill_rate)
        self.tokens = float(capacity)
        self.last_time = float(now)

    def allow(self, cost=1.0, now=None):
        if cost < 0:
            raise ValueError("cost must be non-negative")
        current = self.last_time if now is None else float(now)
        elapsed = max(0.0, current - self.last_time)
        refill = elapsed * self.refill_rate
        self.tokens = min(self.capacity, self.tokens + refill)
        if cost <= self.tokens:
            self.tokens -= cost
            return True
        self.last_time = current
        return False
