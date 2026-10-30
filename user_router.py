"""
User endpoints - the lightest possible user management.

    POST /user                      register a user id        {userId}
    GET  /user/{userId}/watchlists  the user's watchlists     [{watchListId, watchListName}]

A user is just a registered id (no auth, no profile). `POST /watchlist`
refuses a userId that has not been registered here, so watchlists cannot
be created for users that don't exist. User ids are trimmed and compared
case-insensitively; responses echo the spelling used at registration.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Path
from pydantic import BaseModel, Field, field_validator

from stores import users, watchlists
from user_store import DuplicateUser, UserNotFound

router = APIRouter(prefix="/user", tags=["Users"])

MAX_USER_ID_LENGTH = 100


def clean_user_id(value: str) -> str:
    """A user id as accepted anywhere: trimmed, non-blank, no whitespace
    or '/' (it is used in URL paths), at most MAX_USER_ID_LENGTH chars."""
    value = value.strip()
    if not value:
        raise ValueError("userId must not be blank")
    if any(ch.isspace() for ch in value) or "/" in value:
        raise ValueError("userId must not contain whitespace or '/'")
    if len(value) > MAX_USER_ID_LENGTH:
        raise ValueError(f"userId is longer than {MAX_USER_ID_LENGTH} characters")
    return value


class UserCreate(BaseModel):
    userId: str = Field(..., description="The id to register; used as `userId` in POST /watchlist.")

    @field_validator("userId")
    @classmethod
    def _user_id(cls, value: str) -> str:
        return clean_user_id(value)


_USER_PATH = Path(..., description="A registered userId (case-insensitive).")


@router.post("", status_code=201)
def create_user(body: UserCreate):
    """Register a user id.

    Request::

        {"userId": "alice"}

    Response (201)::

        {"userId": "alice"}

    * 409 if the id is already registered (case-insensitive: `Alice` and
      `alice` are the same user); 422 for a blank id, one containing
      whitespace or '/', or longer than 100 characters.
    """
    try:
        user = users.create(body.userId)
    except DuplicateUser as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"userId": user.id}


@router.get("/{user_id}/watchlists")
def list_user_watchlists(user_id: str = _USER_PATH):
    """The user's watchlists, oldest first, without symbols (use
    `GET /watchlist/{id}` for the live view). No upstream calls.

    Response::

        {
          "userId": "alice",
          "count": 2,
          "watchlists": [
            {"watchListId": "k3Zp9QaB", "watchListName": "Tech"},
            {"watchListId": "Qw8rT2vM", "watchListName": "ETFs"}
          ]
        }

    * 404 if the user is not registered.
    """
    try:
        user = users.get(user_id)
    except UserNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    items = watchlists.list_for_user(user.id)
    return {
        "userId": user.id,
        "count": len(items),
        "watchlists": [{"watchListId": item.id, "watchListName": item.name} for item in items],
    }
