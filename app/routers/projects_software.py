from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.auth.dependencies import bearer_scheme, get_current_user_id, get_current_user_token_payload
from app.dependencies import get_db
from app.services.projects_software import (
    get_project_software_governance,
    record_pull_request_merge,
    request_merge_capability_change,
    request_repository_replacement,
    submit_pull_request,
    vote_merge_capability_change,
    vote_pull_request,
    vote_repository_replacement,
)

router = APIRouter(prefix="/projects", tags=["projects-software"])


class DetailMemberOut(BaseModel):
    id: str
    username: str
    bio: str = ""


class ProjectPlanVoteSummaryOut(BaseModel):
    yesCount: int
    noCount: int
    totalVotes: int
    approvalPercent: float
    activeVote: str | None = None
    meetsQuorum: bool
    eligibleVoterCount: int
    quorumThresholdPercent: float
    votesRequired: int
    votesRemaining: int
    remainingEligibleVotes: int


class ProjectSoftwareMergeCapabilityMemberOut(DetailMemberOut):
    sourceLabel: str


class ProjectSoftwarePullRequestOut(BaseModel):
    id: str
    decisionId: str | None = None
    title: str
    summary: str
    pullRequestId: str
    pullRequestUrl: str
    authorUsername: str
    createdAt: str
    stage: str
    stageLabel: str
    mergeId: str | None = None
    mergedByUsername: str | None = None
    approvalThresholdPercent: float
    voteSummary: ProjectPlanVoteSummaryOut | None = None
    passesApprovalThreshold: bool
    canStillPass: bool
    viewerCanRecordMerge: bool
    viewerCanVote: bool = False


class ProjectSoftwareBlockedPullRequestOut(BaseModel):
    id: str
    title: str
    pullRequestId: str
    stage: str
    stageLabel: str


class ProjectSoftwareMergeCapabilityChangeRequestOut(BaseModel):
    id: str
    decisionId: str
    action: str
    actionLabel: str
    targetMember: DetailMemberOut
    authorUsername: str
    createdAt: str
    approvalThresholdPercent: float
    voteSummary: ProjectPlanVoteSummaryOut | None = None
    passesApprovalThreshold: bool
    canStillPass: bool
    viewerCanVote: bool = False


class ProjectSoftwareRepositoryReplacementRequestOut(BaseModel):
    id: str
    decisionId: str
    repositoryUrl: str
    previousRepositoryUrl: str
    reason: str
    relatedPullRequestId: str
    authorUsername: str
    createdAt: str
    approvalThresholdPercent: float
    voteSummary: ProjectPlanVoteSummaryOut | None = None
    passesApprovalThreshold: bool
    canStillPass: bool
    viewerCanVote: bool = False


class ProjectSoftwareRepositoryRecordOut(BaseModel):
    id: str
    repositoryUrl: str
    previousRepositoryUrl: str
    reason: str
    relatedPullRequestId: str
    replacedAt: str
    replacedByUsername: str


class ProjectSoftwareGovernanceDataOut(BaseModel):
    repositoryUrl: str
    licenseLabel: str
    isPlatformTagged: bool = False
    mergeCapabilityManagedByPlatform: bool = False
    mergeCapabilityMembers: list[ProjectSoftwareMergeCapabilityMemberOut]
    availableMergeCapabilityCandidates: list[DetailMemberOut]
    mergeCapabilityChangeRequests: list[ProjectSoftwareMergeCapabilityChangeRequestOut]
    repositoryReplacementRequests: list[ProjectSoftwareRepositoryReplacementRequestOut]
    replaceablePullRequests: list[ProjectSoftwareBlockedPullRequestOut]
    repositoryHistory: list[ProjectSoftwareRepositoryRecordOut]
    pullRequests: list[ProjectSoftwarePullRequestOut]
    viewerCanCreatePullRequests: bool
    viewerCanRequestMergeCapabilityChanges: bool
    viewerCanRequestRepositoryReplacement: bool


class PullRequestSubmitIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1)
    pullRequestId: str = Field(min_length=1, max_length=120)
    pullRequestUrl: str = Field(min_length=1)


class MergeCapabilityRequestIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    targetUserId: UUID
    action: str = Field(pattern="^(grant|revoke)$")


class RepositoryReplacementRequestIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    repositoryUrl: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    relatedPullRequestId: UUID


class VoteRequestIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    vote: str = Field(pattern="^(yes|no)$")


class MergeRecordIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    mergeId: str = Field(min_length=1)


async def _get_optional_user_id(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> UUID | None:
    if credentials is None or not credentials.credentials:
        return None

    try:
        payload = await get_current_user_token_payload(credentials)
    except HTTPException:
        return None

    subject = payload.get("sub")
    if not isinstance(subject, str) or not subject:
        return None

    try:
        return UUID(subject)
    except ValueError:
        return None


@router.get("/{slug}/software", response_model=ProjectSoftwareGovernanceDataOut)
def get_project_software(
    slug: str,
    viewer_user_id: UUID | None = Depends(_get_optional_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return get_project_software_governance(db=db, project_slug=slug, current_user_id=viewer_user_id)


@router.post("/{slug}/software/pull-requests", response_model=ProjectSoftwareGovernanceDataOut)
def create_pull_request(
    slug: str,
    payload: PullRequestSubmitIn,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return submit_pull_request(
        db=db,
        current_user_id=current_user_id,
        project_slug=slug,
        title=payload.title,
        summary=payload.summary,
        pull_request_id=payload.pullRequestId,
        pull_request_url=payload.pullRequestUrl,
    )


@router.post("/{slug}/software/pull-requests/{request_id}/vote", response_model=ProjectSoftwareGovernanceDataOut)
def vote_on_pull_request(
    slug: str,
    request_id: UUID,
    payload: VoteRequestIn,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return vote_pull_request(
        db=db,
        current_user_id=current_user_id,
        project_slug=slug,
        request_id=request_id,
        vote=payload.vote,
    )


@router.post("/{slug}/software/pull-requests/{request_id}/merge", response_model=ProjectSoftwareGovernanceDataOut)
def merge_pull_request(
    slug: str,
    request_id: UUID,
    payload: MergeRecordIn,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return record_pull_request_merge(
        db=db,
        current_user_id=current_user_id,
        project_slug=slug,
        request_id=request_id,
        merge_id=payload.mergeId,
    )


@router.post("/{slug}/software/merge-capability-requests", response_model=ProjectSoftwareGovernanceDataOut)
def create_merge_capability_request(
    slug: str,
    payload: MergeCapabilityRequestIn,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return request_merge_capability_change(
        db=db,
        current_user_id=current_user_id,
        project_slug=slug,
        target_user_id=payload.targetUserId,
        action=payload.action,
    )


@router.post("/{slug}/software/merge-capability-requests/{request_id}/vote", response_model=ProjectSoftwareGovernanceDataOut)
def vote_on_merge_capability_request(
    slug: str,
    request_id: UUID,
    payload: VoteRequestIn,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return vote_merge_capability_change(
        db=db,
        current_user_id=current_user_id,
        project_slug=slug,
        request_id=request_id,
        vote=payload.vote,
    )


@router.post("/{slug}/software/repository-replacement-requests", response_model=ProjectSoftwareGovernanceDataOut)
def create_repository_replacement_request(
    slug: str,
    payload: RepositoryReplacementRequestIn,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return request_repository_replacement(
        db=db,
        current_user_id=current_user_id,
        project_slug=slug,
        repository_url=payload.repositoryUrl,
        reason=payload.reason,
        related_pull_request_id=payload.relatedPullRequestId,
    )


@router.post("/{slug}/software/repository-replacement-requests/{request_id}/vote", response_model=ProjectSoftwareGovernanceDataOut)
def vote_on_repository_replacement_request(
    slug: str,
    request_id: UUID,
    payload: VoteRequestIn,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return vote_repository_replacement(
        db=db,
        current_user_id=current_user_id,
        project_slug=slug,
        request_id=request_id,
        vote=payload.vote,
    )
