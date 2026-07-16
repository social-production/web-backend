from __future__ import annotations

from sqlalchemy.orm import Session


def get_onboarding(db: Session) -> dict[str, object]:
    return {
        "title": "Login",
        "intro": "Sign in to post, follow people, and create projects, threads, and events.",
        "accountModes": [
            {
                "value": "signup",
                "label": "Sign up",
                "description": "Create a new account.",
            },
            {
                "value": "login",
                "label": "Log in",
                "description": "Use an existing account.",
            },
        ],
        "starterChannels": [],
        "starterCommunities": [],
    }
