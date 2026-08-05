from sqlalchemy import func

from app.models import users


def username_matches(value: str):
    """Case-insensitive username equality filter."""
    return func.lower(users.c.username) == value.strip().lower()
