"""
The process-wide store singletons, in one place so that the routers can
share them without importing each other (the user router lists a user's
watchlists; the watchlist router checks that a user exists).

Both are in-memory: process-local and empty after a restart.
"""

from user_store import InMemoryUserStore
from watchlist_store import InMemoryWatchlistStore

users = InMemoryUserStore()
watchlists = InMemoryWatchlistStore()
