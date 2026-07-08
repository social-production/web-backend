from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth.dependencies import get_optional_current_user_id
from app.dependencies import get_db
from app.services.bootstrap import get_bootstrap, get_bootstrap_summary, get_onboarding

router = APIRouter(tags=["bootstrap"])


class ViewerSummaryOut(BaseModel):
    id: UUID
    username: str
    bio: str | None = None
    profileImageUrl: str | None = None


class FeatureFlagsOut(BaseModel):
    assets: bool
    funding: bool
    platform: bool


class UnreadCountsOut(BaseModel):
    notifications: int
    messages: int


class DirectoryItemOut(BaseModel):
    slug: str
    label: str
    href: str
    visibility: str | None = None
    viewerIsMember: bool | None = None


class DirectoryOut(BaseModel):
    platform: DirectoryItemOut | None = None
    channels: list[DirectoryItemOut]
    communities: list[DirectoryItemOut]


class BootstrapResponse(BaseModel):
    viewer: ViewerSummaryOut | None = None
    featureFlags: FeatureFlagsOut
    unreadCounts: UnreadCountsOut
    directory: DirectoryOut
    suggestedContacts: list[ViewerSummaryOut]
    activityRail: list[dict[str, object]]


class OnboardingAccountModeOut(BaseModel):
    value: str
    label: str
    description: str


class OnboardingResponse(BaseModel):
    title: str
    intro: str
    accountModes: list[OnboardingAccountModeOut]
    starterChannels: list[str]
    starterCommunities: list[str]


@router.get("/bootstrap/summary", response_model=UnreadCountsOut)
async def bootstrap_summary(
    current_user_id: UUID | None = Depends(get_optional_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return get_bootstrap_summary(db=db, current_user_id=current_user_id)


@router.get("/bootstrap", response_model=BootstrapResponse)
async def bootstrap(
    current_user_id: UUID | None = Depends(get_optional_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return get_bootstrap(db=db, current_user_id=current_user_id)


@router.get("/onboarding", response_model=OnboardingResponse)
def onboarding(
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return get_onboarding(db=db)
