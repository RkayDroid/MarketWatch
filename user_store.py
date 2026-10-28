"""
In-memory user registry - deliberately minimal.

A "user" here is nothing more than a registered id: there is no
authentication, profile or password; the id exists so that a watchlist can
only be created for a user the service knows about, and so a user's
watchlists can be listed. Same shape as ``watchlist_store``: a dict behind
a lock, frozen records, an interface a database can implement later.

Identity: user ids are trimmed and compared case-insensitively (``alice``,
``Alice`` and ``alice `` are one user), the rule the watchlist store's
per-user uniqueness already relies on. The id is stored as first
registered and that canonical spelling is what every response echoes.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List


class UserNotFound(LookupError):
    def __init__(self, user_id: str):
        self.user_id = user_id
        super().__init__(f"User {user_id} not found.")


class DuplicateUser(ValueError):
    def __init__(self, user_id: str):
        self.user_id = user_id
        super().__init__(f"User {user_id} already exists.")


@dataclass(frozen=True)
class User:
    id: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def user_key(user_id: str) -> str:
    """The identity of a user id: trimmed, case-folded."""
    return user_id.strip().casefold()


class UserStore:
    """What the API needs from a user repository."""

    def create(self, user_id: str) -> User:
        raise NotImplementedError

    def get(self, user_id: str) -> User:
        raise NotImplementedError

    def list(self) -> List[User]:
        raise NotImplementedError


class InMemoryUserStore(UserStore):
    def __init__(self, time_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self._now = time_fn
        self._users: Dict[str, User] = {}  # user_key -> User
        self._lock = threading.Lock()

    def create(self, user_id: str) -> User:
        user_id = user_id.strip()
        if not user_id:
            raise ValueError("user id must be non-empty")
        key = user_key(user_id)
        with self._lock:
            if key in self._users:
                raise DuplicateUser(user_id)
            user = User(id=user_id, created_at=self._now())
            self._users[key] = user
            return user

    def get(self, user_id: str) -> User:
        """The registered user for `user_id` in any case/spacing."""
        with self._lock:
            user = self._users.get(user_key(user_id))
        if user is None:
            raise UserNotFound(user_id.strip())
        return user

    def list(self) -> List[User]:
        with self._lock:
            return sorted(self._users.values(), key=lambda u: u.created_at)

    def clear(self) -> int:
        with self._lock:
            count = len(self._users)
            self._users.clear()
            return count

    def __len__(self) -> int:
        with self._lock:
            return len(self._users)
