"""Persistent request budget shared by every run that uses the same database.

Requests are reserved before they are sent, so failed or interrupted requests still
count. The ledger cannot see traffic from tools that do not use it; headroom covers those.
"""
import time
import uuid

DAY = 86400


class RequestBudget:
    def __init__(self, store, limits, api="courtlistener", clock=time.time, sleep=time.sleep):
        self.store, self.limits, self.api = store, limits, api
        self.clock, self.sleep = clock, sleep
        self.used = 0

    def _last(self):
        return self.store.execute("SELECT MAX(attempted_at) AS t FROM api_requests WHERE api=?",
                                  (self.api,)).fetchone()["t"]

    def acquire(self):
        """Reserve one request, pacing if needed. False means stop for now; never retries."""
        limits = self.limits
        if self.used >= limits.max_requests_per_run:
            return False
        last = self._last()
        if last is not None:
            wait = limits.min_interval_seconds - (self.clock() - last)
            if wait > limits.min_interval_seconds:
                return False  # Ledger time is in the future; refuse rather than sleep long.
            if wait > 0:
                self.sleep(wait)
        at = self.clock()
        caps = ((60, limits.per_minute), (3600, limits.per_hour - limits.headroom_per_hour),
                (DAY, limits.per_day - limits.headroom_per_day))
        with self.store.transaction():
            self.store.execute("INSERT INTO api_control VALUES (?, 0) ON CONFLICT(api) DO NOTHING", (self.api,))
            suffix = " FOR UPDATE" if self.store.backend == "postgres" else ""
            blocked = self.store.execute("SELECT blocked_until FROM api_control WHERE api=?" + suffix,
                                         (self.api,)).fetchone()["blocked_until"]
            if at < blocked:
                return False
            for window, cap in caps:
                count = self.store.execute("SELECT COUNT(*) AS n FROM api_requests WHERE api=? AND attempted_at>?",
                                           (self.api, at - window)).fetchone()["n"]
                if count >= cap:
                    return False
            last = self._last()
            if last is not None and at - last < limits.min_interval_seconds:
                return False  # Another run reserved in the meantime.
            self.store.execute("INSERT INTO api_requests VALUES (?,?,?)", (uuid.uuid4().hex, self.api, at))
            self.store.execute("DELETE FROM api_requests WHERE api=? AND attempted_at<?", (self.api, at - 2 * DAY))
        self.used += 1
        return True

    def block(self, seconds=DAY):
        """Pause all requests to this API, e.g. after HTTP 429. Never shortens an existing pause."""
        until = self.clock() + seconds
        with self.store.transaction():
            self.store.execute("INSERT INTO api_control VALUES (?, 0) ON CONFLICT(api) DO NOTHING", (self.api,))
            self.store.execute("UPDATE api_control SET blocked_until=? WHERE api=? AND blocked_until<?",
                               (until, self.api, until))

    def blocked_until(self):
        row = self.store.execute("SELECT blocked_until FROM api_control WHERE api=?", (self.api,)).fetchone()
        return row["blocked_until"] if row else 0
