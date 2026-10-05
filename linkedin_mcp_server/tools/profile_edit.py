"""Tools for reading and, with explicit approval, editing your own LinkedIn profile.

Only ``apply_profile_changes`` modifies LinkedIn. Every other tool here reads
LinkedIn or manages local change-set records. A change is applied only from a
stored change set, with ``confirm=true``, while the server runs with
``MCP_LINKEDIN_WRITE_ENABLED=true``, and only if the profile still matches what
the change set was planned against.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal

import logging

from fastmcp import Context, FastMCP
from pydantic import BaseModel, ConfigDict, Field

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.core.exceptions import (
    AccountRestrictedError,
    AuthenticationError,
    RateLimitError,
)
from linkedin_mcp_server.dependencies import (
    get_ready_profile_editor,
    handle_auth_error,
)
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.profile_edit import settings
from linkedin_mcp_server.profile_edit.errors import (
    ProfileEditError,
    ProfileEditErrorCode,
)
from linkedin_mcp_server.profile_edit.model import NewCompanyPage
from linkedin_mcp_server.profile_edit.service import (
    ExperienceEdit,
    NewExperienceRequest,
    ProfileEditService,
    Proposal,
)
from linkedin_mcp_server.profile_edit.store import ProfileEditStore
from linkedin_mcp_server.tools.approval import requires_user_interaction

logger = logging.getLogger(__name__)


class ExperienceMatch(BaseModel):
    model_config = ConfigDict(extra="allow")
    company: str | None = None
    title: str | None = None
    startDate: str | None = Field(
        default=None, description="Text from the date range, e.g. 'Jan 2021' or '2021'."
    )


class ExperienceChange(BaseModel):
    model_config = ConfigDict(extra="allow")
    experienceId: str | None = Field(
        default=None, description="id from get_my_experience. Preferred over match."
    )
    match: ExperienceMatch | None = Field(
        default=None,
        description="Used only when experienceId is absent; must match exactly one experience.",
    )
    title: str | None = Field(
        default=None, description="New job title, as approved by the user."
    )
    description: str | None = Field(
        default=None, description="New description, as approved by the user."
    )
    startMonth: int | None = Field(
        default=None, ge=1, le=12, description="New start month, with startYear."
    )
    startYear: int | None = Field(
        default=None, description="New start year, with startMonth."
    )


class SkillChanges(BaseModel):
    model_config = ConfigDict(extra="allow")
    add: list[str] = Field(default_factory=list, max_length=20)
    remove: list[str] = Field(default_factory=list, max_length=20)


class NewExperienceChange(BaseModel):
    model_config = ConfigDict(extra="allow")
    title: str = Field(description="Job title, as approved by the user.")
    company: str | None = Field(
        default=None,
        description="Exact company name. Omit when using sameCompanyAs.",
    )
    sameCompanyAs: str | None = Field(
        default=None,
        description=(
            "experienceId of an existing position whose company is copied "
            "exactly, so the new role is grouped under the same company."
        ),
    )
    startMonth: int = Field(ge=1, le=12, description="1 to 12.")
    startYear: int = Field(description="Four-digit year.")
    employmentType: (
        Literal[
            "full_time",
            "part_time",
            "self_employed",
            "freelance",
            "contract",
            "internship",
            "apprenticeship",
        ]
        | None
    ) = None
    locationType: Literal["on_site", "hybrid", "remote"] | None = None
    description: str = Field(
        default="", description="Description, as approved by the user."
    )


class NewCompanyPageChange(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str = Field(description="Organization name, as approved by the user.")
    publicUrl: str = Field(
        description="The page address after linkedin.com/company/; must be free."
    )
    industry: str = Field(
        description="Exactly one of LinkedIn's own industry names, in the page's language."
    )
    size: Literal[
        "0-1",
        "2-10",
        "11-50",
        "51-200",
        "201-500",
        "501-1000",
        "1001-5000",
        "5001-10000",
        "10001+",
    ] = Field(description="Number of employees.")
    organizationType: Literal[
        "public_company",
        "self_employed",
        "government_agency",
        "nonprofit",
        "sole_proprietorship",
        "privately_held",
        "partnership",
    ]
    website: str = Field(
        default="", description="Starts with http://, https:// or www."
    )
    tagline: str = Field(default="", description="Up to 120 characters.")
    logoPath: str = Field(default="", description="Local .jpg, .jpeg or .png file.")
    authorizedRepresentative: bool = Field(
        default=False,
        description=(
            "The user's own statement that they officially represent this "
            "organization and accept LinkedIn's Pages terms. Ask the user; "
            "never assume it."
        ),
    )


class ProfileChanges(BaseModel):
    # Extra keys are accepted so an unsupported field (location, education,
    # featured, ...) is reported back as UNSUPPORTED_FIELD instead of failing
    # schema validation with no explanation.
    model_config = ConfigDict(extra="allow")
    headline: str | None = None
    about: str | None = None
    experiences: list[ExperienceChange] = Field(default_factory=list, max_length=20)
    skills: SkillChanges | None = None
    # One per change set: each add reads the position list before and after
    # and takes about two minutes, so two in one apply outlast a tool call.
    newExperiences: list[NewExperienceChange] = Field(
        default_factory=list, max_length=1
    )
    newCompanyPage: NewCompanyPageChange | None = None


def _unsupported_fields(changes: ProfileChanges) -> list[str]:
    """Every key the server cannot act on, with its path, at any depth."""
    found = [str(k) for k in changes.model_extra or {}]
    for i, exp in enumerate(changes.experiences):
        found += [f"experiences[{i}].{k}" for k in exp.model_extra or {}]
        if exp.match is not None:
            found += [
                f"experiences[{i}].match.{k}" for k in exp.match.model_extra or {}
            ]
    if changes.skills is not None:
        found += [f"skills.{k}" for k in changes.skills.model_extra or {}]
    for i, new in enumerate(changes.newExperiences):
        found += [f"newExperiences[{i}].{k}" for k in new.model_extra or {}]
    if changes.newCompanyPage is not None:
        found += [
            f"newCompanyPage.{k}" for k in changes.newCompanyPage.model_extra or {}
        ]
    return sorted(found)


def _service(editor: Any | None) -> ProfileEditService:
    return ProfileEditService(
        editor,
        ProfileEditStore(settings.edits_root()),
        writes_enabled=settings.writes_enabled,
        pacing_seconds=settings.WRITE_PACING_SECONDS,
    )


async def _run(
    ctx: Context,
    tool: str,
    body: Callable[[ProfileEditService], Awaitable[dict[str, Any]]],
    *,
    needs_browser: bool = True,
) -> dict[str, Any]:
    try:
        editor = (
            await get_ready_profile_editor(ctx, tool_name=tool)
            if needs_browser
            else None
        )
        return await body(_service(editor))
    except ProfileEditError as e:
        return e.to_result()
    except AuthenticationError as e:
        try:
            await handle_auth_error(e, ctx)
        except Exception as relogin_exc:
            raise_tool_error(relogin_exc, tool)
    except (RateLimitError, AccountRestrictedError) as e:
        # A checkpoint or restriction is never worked around; the user clears it.
        return ProfileEditError(
            ProfileEditErrorCode.AUTHENTICATION_REQUIRED, detail=str(e)[:200]
        ).to_result()
    except Exception as e:
        raise_tool_error(e, tool)  # NoReturn


def register_profile_edit_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register own-profile reading and approval-gated editing tools."""

    @mcp.tool(
        timeout=tool_timeout,
        title="Get My Editable Profile",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"person", "profile-edit"},
    )
    async def get_my_editable_profile(ctx: Context) -> dict[str, Any]:
        """
        Read your own profile as structured, editable fields. Read-only.

        Returns name, headline, location, about, experiences (each with a
        stable experienceId taken from LinkedIn's position id), skills with
        their order, and the character limits LinkedIn's forms enforce.
        Headline and about come from LinkedIn's edit forms, so they are exact.
        Use this before propose_profile_changes. For the raw text of profile
        sections use get_my_profile instead.
        """
        return await _run(ctx, "get_my_editable_profile", lambda s: s.get_profile())

    @mcp.tool(
        timeout=tool_timeout,
        title="Get My Experience",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"person", "profile-edit"},
    )
    async def get_my_experience(
        ctx: Context, experienceId: str | None = None
    ) -> dict[str, Any]:
        """
        Read your experience entries. Read-only.

        Without experienceId: every position with its experienceId and display
        fields. With experienceId: that position's exact title and full
        description from its edit form, plus their character limits.

        Args:
            ctx: FastMCP context
            experienceId: Optional id from this tool or get_my_editable_profile
        """
        return await _run(
            ctx, "get_my_experience", lambda s: s.get_experiences(experienceId)
        )

    @mcp.tool(
        timeout=tool_timeout,
        title="Get My Skills",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"person", "profile-edit"},
    )
    async def get_my_skills(ctx: Context) -> dict[str, Any]:
        """Read your skills in LinkedIn's order. Read-only."""
        return await _run(ctx, "get_my_skills", lambda s: s.get_skills())

    @mcp.tool(
        timeout=tool_timeout,
        title="Propose Profile Changes",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"profile-edit"},
    )
    async def propose_profile_changes(
        ctx: Context, changes: ProfileChanges
    ) -> dict[str, Any]:
        """
        Creates a previewable profile change set. Does not modify LinkedIn.

        Reads the current values of the targeted fields, validates every
        requested value (including LinkedIn's character limits; over-long text
        is refused, never truncated), and stores a change set with before and
        after values and a fingerprint of the profile it was planned against.
        Show the returned diff to the user. Nothing is applied until the user
        approves and apply_profile_changes is called with confirm=true.

        Only send values the user has written or approved. This tool carries
        content; it must not be used to invent jobs, employers, dates,
        qualifications, skills or achievements. Supported: headline, about,
        title, description and start date of an existing experience, adding
        a current position (newExperiences, one per change set; positions are
        never ended or deleted), adding and removing skills, and creating a
        LinkedIn Page for an organization the user represents
        (newCompanyPage). Anything else returns UNSUPPORTED_FIELD.

        A company page is public and cannot be undone from here. Before
        proposing one, ask the user whether they officially represent the
        organization; authorizedRepresentative carries their answer and is
        never assumed.

        Args:
            ctx: FastMCP context
            changes: headline, about, experiences [{experienceId | match, title,
                description, startMonth, startYear}], newExperiences [{title, company |
                sameCompanyAs, startMonth, startYear, employmentType,
                locationType, description}], skills {add, remove},
                newCompanyPage {name, publicUrl, industry, size,
                organizationType, website, tagline, logoPath,
                authorizedRepresentative}
        """
        extra = _unsupported_fields(changes)
        if extra:
            return ProfileEditError(
                ProfileEditErrorCode.UNSUPPORTED_FIELD,
                "These fields cannot be edited by this server: "
                + ", ".join(extra)
                + ".",
                fields=extra,
            ).to_result()
        proposal = Proposal(
            headline=changes.headline,
            about=changes.about,
            experiences=[
                ExperienceEdit(
                    experience_id=e.experienceId,
                    company=e.match.company if e.match else None,
                    match_title=e.match.title if e.match else None,
                    start_date=e.match.startDate if e.match else None,
                    title=e.title,
                    description=e.description,
                    start_month=e.startMonth,
                    start_year=e.startYear,
                )
                for e in changes.experiences
            ],
            skills_add=changes.skills.add if changes.skills else [],
            skills_remove=changes.skills.remove if changes.skills else [],
            new_experiences=[
                NewExperienceRequest(
                    title=n.title,
                    start_month=n.startMonth,
                    start_year=n.startYear,
                    company=n.company,
                    same_company_as=n.sameCompanyAs,
                    employment_type=n.employmentType,
                    location_type=n.locationType,
                    description=n.description,
                )
                for n in changes.newExperiences
            ],
            new_company_page=(
                NewCompanyPage(
                    name=page.name,
                    public_url=page.publicUrl,
                    industry=page.industry,
                    size=page.size,
                    organization_type=page.organizationType,
                    website=page.website,
                    tagline=page.tagline,
                    logo_path=page.logoPath,
                    representative_declared=page.authorizedRepresentative,
                )
                if (page := changes.newCompanyPage) is not None
                else None
            ),
        )
        return await _run(ctx, "propose_profile_changes", lambda s: s.propose(proposal))

    @mcp.tool(
        timeout=tool_timeout,
        title="Preview Profile Changes",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"profile-edit"},
    )
    async def preview_profile_changes(ctx: Context, changeSetId: str) -> dict[str, Any]:
        """
        Show a change set's exact before/after values. Does not modify LinkedIn.

        Re-reads the targeted fields: if anything changed since the proposal
        (for example an edit made by hand on linkedin.com), returns
        STALE_CHANGE_SET with the changed fields and the change set can no
        longer be applied; propose again.

        Args:
            ctx: FastMCP context
            changeSetId: id returned by propose_profile_changes
        """
        return await _run(
            ctx, "preview_profile_changes", lambda s: s.preview(changeSetId)
        )

    @mcp.tool(
        timeout=tool_timeout,
        title="Apply Profile Changes",
        annotations={
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": True,
        },
        tags={"profile-edit", "actions"},
        meta=requires_user_interaction(),
    )
    async def apply_profile_changes(
        ctx: Context,
        changeSetId: str,
        confirm: bool = False,
        notifyNetwork: bool | None = None,
    ) -> dict[str, Any]:
        """
        Applies a previously created and explicitly approved change set to the authenticated user's own LinkedIn profile. This modifies external state.

        Call only after the user has seen the preview and said to apply it.
        Before calling, ask the user whether LinkedIn should notify their
        network about these changes and pass their answer as notifyNetwork;
        never choose for them. Requires confirm=true, an explicit
        notifyNetwork, and a server started with
        MCP_LINKEDIN_WRITE_ENABLED=true. Applies exactly the stored changes,
        one field at a time, re-reading each from LinkedIn to verify it; a
        field counts as done only when the saved value is observed. Refuses a
        change set that is not pending (each is applied at most once) or whose
        profile changed since it was proposed (STALE_CHANGE_SET). Stops at the
        first failure and reports PARTIAL_FAILURE with per-field results; it
        never retries a failed write and never rolls back automatically.

        Args:
            ctx: FastMCP context
            changeSetId: id returned by propose_profile_changes
            confirm: must be true; the user's explicit approval
            notifyNetwork: the user's own answer to whether LinkedIn should
                notify their network: true sets each form's notify switch on,
                false sets it off. Forms without the switch report
                "not_offered". Without it nothing is written.
        """
        try:
            _service(None).precheck_apply(
                changeSetId, confirm=confirm, notify_network=notifyNetwork
            )
        except ProfileEditError as e:
            return e.to_result()
        return await _run(
            ctx,
            "apply_profile_changes",
            lambda s: s.apply(
                changeSetId, confirm=confirm, notify_network=notifyNetwork
            ),
        )

    @mcp.tool(
        timeout=tool_timeout,
        title="Discard Profile Changes",
        annotations={
            "readOnlyHint": False,
            "destructiveHint": False,
            "openWorldHint": False,
        },
        tags={"profile-edit"},
    )
    async def discard_profile_changes(ctx: Context, changeSetId: str) -> dict[str, Any]:
        """
        Discard a pending change set so it can never be applied. Does not modify LinkedIn.

        Args:
            ctx: FastMCP context
            changeSetId: id returned by propose_profile_changes
        """

        async def body(s: ProfileEditService) -> dict[str, Any]:
            return s.discard(changeSetId)

        return await _run(ctx, "discard_profile_changes", body, needs_browser=False)
