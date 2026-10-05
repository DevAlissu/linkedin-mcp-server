"""READ -> PROPOSE -> PREVIEW -> APPLY -> VERIFY, against an editor port.

The service never decides content. It carries values the caller supplied,
refuses anything it cannot target unambiguously, and only writes from a stored,
pending change set whose baseline still matches LinkedIn.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

import json
import logging
import re

from linkedin_mcp_server.core.exceptions import (
    AccountRestrictedError,
    AuthenticationError,
    RateLimitError,
)
from linkedin_mcp_server.profile_edit.changeset import (
    ChangeSet,
    ChangeSetStatus,
    FieldChange,
    SkillsRequest,
    TextRequest,
    build_change_set,
    render_diff,
    skill_key,
    stale_fields,
)
from linkedin_mcp_server.profile_edit.errors import (
    ProfileEditError,
    ProfileEditErrorCode,
)
from linkedin_mcp_server.profile_edit.model import (
    NewPosition,
    format_start,
    ExperienceForm,
    ExperienceSummary,
    OwnProfile,
    Skill,
    TextField,
    normalize_text,
)
from linkedin_mcp_server.profile_edit.store import ProfileEditStore

logger = logging.getLogger(__name__)

ExperienceField = Literal["title", "description"]


class ProfileEditorPort(Protocol):
    """What the service needs from LinkedIn. Implemented by linkedin.profile_editor."""

    async def read_identity(self) -> tuple[str, str | None, str | None]: ...
    async def account(self) -> str: ...
    async def read_headline(self) -> TextField: ...
    async def read_location(self) -> str | None: ...
    async def read_about(self) -> TextField: ...
    async def list_experiences(self) -> list[ExperienceSummary]: ...
    async def read_experience(self, experience_id: str) -> ExperienceForm: ...
    async def list_skills(self) -> list[Skill]: ...
    async def write_headline(self, *, expected: str, value: str) -> None: ...
    async def write_about(self, *, expected: str, value: str) -> None: ...
    async def write_experience(
        self, experience_id: str, *, field: ExperienceField, expected: str, value: str
    ) -> None: ...
    async def add_skill(self, name: str) -> str: ...
    async def remove_skill(self, skill: Skill) -> None: ...
    async def add_experience(self, position: NewPosition) -> None: ...
    async def write_experience_start(
        self, experience_id: str, *, expected: str, month: int, year: int
    ) -> None: ...
    async def pause(self, seconds: float) -> None: ...
    def set_network_notification(self, notify: bool | None) -> None: ...
    def last_network_notification(self) -> str | None: ...


@dataclass(frozen=True, slots=True)
class ExperienceEdit:
    experience_id: str | None = None
    company: str | None = None
    match_title: str | None = None
    start_date: str | None = None
    title: str | None = None
    description: str | None = None
    start_month: int | None = None
    start_year: int | None = None


@dataclass(frozen=True, slots=True)
class NewExperienceRequest:
    """A current position to add.

    The company is given by name, or copied from an existing position
    (``same_company_as``) so the new role is filed under exactly the same
    company and groups with it.
    """

    title: str
    start_month: int
    start_year: int
    company: str | None = None
    same_company_as: str | None = None
    employment_type: str | None = None
    location_type: str | None = None
    description: str = ""


@dataclass(frozen=True, slots=True)
class Proposal:
    headline: str | None = None
    about: str | None = None
    experiences: Sequence[ExperienceEdit] = ()
    skills_add: Sequence[str] = ()
    skills_remove: Sequence[str] = ()
    new_experiences: Sequence[NewExperienceRequest] = ()

    def is_empty(self) -> bool:
        return (
            self.headline is None
            and self.about is None
            and not any(
                e.title is not None
                or e.description is not None
                or e.start_month is not None
                or e.start_year is not None
                for e in self.experiences
            )
            and not self.skills_add
            and not self.skills_remove
            and not self.new_experiences
        )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _casefold(v: str | None) -> str:
    return " ".join((v or "").casefold().split())


_RANGE_SEPARATOR = re.compile(r"\s+[-\u2013\u2014]\s+")


def _range_start(date_range: str | None) -> str:
    """The start of a displayed range such as "May 2022 - Jul 2023 · 1 yr"."""
    return _RANGE_SEPARATOR.split(date_range or "", maxsplit=1)[0]


def resolve_experience(
    ref: ExperienceEdit, listed: Sequence[ExperienceSummary]
) -> ExperienceSummary:
    """Find exactly one experience, or refuse. Array position is never used."""

    def candidates(items: Sequence[ExperienceSummary]) -> list[dict[str, Any]]:
        return [e.as_dict() for e in items]

    if ref.experience_id:
        found = [e for e in listed if e.id == ref.experience_id]
        if not found:
            raise ProfileEditError(
                ProfileEditErrorCode.EXPERIENCE_NOT_FOUND,
                experienceId=ref.experience_id,
                candidates=candidates(listed),
            )
        match = found[0]
    else:
        if not (ref.company or ref.match_title):
            raise ProfileEditError(
                ProfileEditErrorCode.VALIDATION_ERROR,
                "Each experience edit needs an experienceId, or a company and/or title to match.",
            )
        found = [
            e
            for e in listed
            if (not ref.company or _casefold(ref.company) == _casefold(e.company))
            and (
                not ref.match_title or _casefold(ref.match_title) == _casefold(e.title)
            )
            and (
                not ref.start_date
                or _casefold(ref.start_date) in _casefold(_range_start(e.date_range))
            )
        ]
        if not found:
            raise ProfileEditError(
                ProfileEditErrorCode.EXPERIENCE_NOT_FOUND,
                company=ref.company,
                title=ref.match_title,
                startDate=ref.start_date,
                candidates=candidates(listed),
            )
        if len(found) > 1:
            raise ProfileEditError(
                ProfileEditErrorCode.AMBIGUOUS_EXPERIENCE,
                candidates=candidates(found),
            )
        match = found[0]
    if not match.editable:
        raise ProfileEditError(
            ProfileEditErrorCode.UNSUPPORTED_FIELD,
            "LinkedIn shows no edit control for this experience.",
            experienceId=match.id,
        )
    return match


@dataclass(slots=True)
class _Reads:
    """Per-operation cache, so one experience form is opened once per step."""

    editor: ProfileEditorPort
    experiences: dict[str, ExperienceForm] = field(default_factory=dict)

    async def experience(self, experience_id: str) -> ExperienceForm:
        if experience_id not in self.experiences:
            self.experiences[experience_id] = await self.editor.read_experience(
                experience_id
            )
        return self.experiences[experience_id]


class ProfileEditService:
    def __init__(
        self,
        editor: ProfileEditorPort | None,
        store: ProfileEditStore,
        *,
        writes_enabled: Callable[[], bool],
        pacing_seconds: float,
        clock: Callable[[], str] = _utc_now,
    ) -> None:
        self._maybe_editor = editor
        self._store = store
        self._writes_enabled = writes_enabled
        self._pacing = pacing_seconds
        self._clock = clock

    @property
    def _editor(self) -> ProfileEditorPort:
        # None only for the browser-free calls (discard, apply prechecks).
        if self._maybe_editor is None:
            raise RuntimeError("this operation needs a LinkedIn page")
        return self._maybe_editor

    # ── READ ────────────────────────────────────────────────────────────────
    async def get_profile(self) -> dict[str, Any]:
        url, name, location = await self._editor.read_identity()
        headline = await self._editor.read_headline()
        profile = OwnProfile(
            url=url,
            name=name,
            headline=headline,
            about=await self._editor.read_about(),
            location=location or await self._editor.read_location(),
            experiences=await self._editor.list_experiences(),
            skills=await self._editor.list_skills(),
        )
        return profile.as_dict()

    async def get_experiences(self, experience_id: str | None = None) -> dict[str, Any]:
        listed = await self._editor.list_experiences()
        if experience_id is None:
            return {"experiences": [e.as_dict() for e in listed]}
        summary = resolve_experience(
            ExperienceEdit(experience_id=experience_id), listed
        )
        form = await self._editor.read_experience(summary.id)
        return {
            "experience": {
                **summary.as_dict(),
                "title": form.title.value,
                "description": form.description.value,
                "start": form.start or None,
                "limits": {
                    "title": form.title.limit("experience_title"),
                    "description": form.description.limit("experience_description"),
                },
            }
        }

    async def get_skills(self) -> dict[str, Any]:
        return {"skills": [s.as_dict() for s in await self._editor.list_skills()]}

    # ── PROPOSE ─────────────────────────────────────────────────────────────
    async def propose(self, proposal: Proposal) -> dict[str, Any]:
        if proposal.is_empty():
            raise ProfileEditError(
                ProfileEditErrorCode.VALIDATION_ERROR, "No changes were requested."
            )
        reads = _Reads(self._editor)
        texts: list[TextRequest] = []
        if proposal.headline is not None:
            h = await self._editor.read_headline()
            texts.append(
                TextRequest(
                    "headline",
                    "headline",
                    "headline",
                    "Headline",
                    proposal.headline,
                    h.value,
                    h.max_length,
                )
            )
        if proposal.about is not None:
            a = await self._editor.read_about()
            texts.append(
                TextRequest(
                    "about",
                    "about",
                    "about",
                    "About",
                    proposal.about,
                    a.value,
                    a.max_length,
                )
            )
        if proposal.experiences:
            listed = await self._editor.list_experiences()
            for edit in proposal.experiences:
                exp = resolve_experience(edit, listed)
                form = await reads.experience(exp.id)
                label = " · ".join(
                    x for x in (exp.company, exp.title, exp.date_range) if x
                )
                if edit.title is not None:
                    texts.append(
                        TextRequest(
                            f"experience/{exp.id}/title",
                            "experience",
                            "experience_title",
                            f"{label} — title",
                            edit.title,
                            form.title.value,
                            form.title.max_length,
                            exp.id,
                        )
                    )
                if (edit.start_month is None) != (edit.start_year is None):
                    raise ProfileEditError(
                        ProfileEditErrorCode.VALIDATION_ERROR,
                        "A start date needs both startMonth and startYear.",
                        experienceId=exp.id,
                    )
                if edit.start_month is not None and edit.start_year is not None:
                    this_year = int(self._clock()[:4])
                    if not 1 <= edit.start_month <= 12 or not (
                        1950 <= edit.start_year <= this_year
                    ):
                        raise ProfileEditError(
                            ProfileEditErrorCode.VALIDATION_ERROR,
                            "startMonth must be 1 to 12 and startYear a plausible year.",
                            experienceId=exp.id,
                        )
                    if not form.start:
                        raise ProfileEditError(
                            ProfileEditErrorCode.UNSUPPORTED_FIELD,
                            "This position's form shows no start date to change.",
                            experienceId=exp.id,
                        )
                    texts.append(
                        TextRequest(
                            f"experience/{exp.id}/start",
                            "experience",
                            "experience_start",
                            f"{label} start date (MM/YYYY)",
                            format_start(edit.start_month, edit.start_year),
                            form.start,
                            None,
                            exp.id,
                        )
                    )
                if edit.description is not None:
                    texts.append(
                        TextRequest(
                            f"experience/{exp.id}/description",
                            "experience",
                            "experience_description",
                            f"{label} — description",
                            edit.description,
                            form.description.value,
                            form.description.max_length,
                            exp.id,
                        )
                    )
        current_skills: list[str] = []
        if proposal.skills_add or proposal.skills_remove:
            current_skills = [s.name for s in await self._editor.list_skills()]
        new_positions: list[NewPosition] = []
        experience_ids: list[str] = []
        if proposal.new_experiences:
            listed = await self._editor.list_experiences()
            experience_ids = [e.id for e in listed]
            for request in proposal.new_experiences:
                new_positions.append(
                    NewPosition(
                        title=request.title,
                        company=await self._company_for(request, listed, reads),
                        start_month=request.start_month,
                        start_year=request.start_year,
                        employment_type=request.employment_type,
                        location_type=request.location_type,
                        description=request.description,
                    )
                )
        cs = build_change_set(
            texts,
            SkillsRequest(proposal.skills_add, proposal.skills_remove),
            current_skills,
            now=self._clock(),
            new_positions=new_positions,
            current_experience_ids=experience_ids,
        )
        cs.account = await self._editor.account()
        self._store.save(cs)
        self._store.audit(
            at=self._clock(),
            tool="propose_profile_changes",
            changeSetId=cs.id,
            status=str(cs.status),
        )
        return self._presented(cs)

    # ── PREVIEW ─────────────────────────────────────────────────────────────
    async def preview(self, change_set_id: str) -> dict[str, Any]:
        cs = self._store.load(change_set_id)
        out = self._presented(cs)
        if cs.status is not ChangeSetStatus.PENDING_APPROVAL:
            out["applicable"] = False
            return out
        await self._check_account(cs)
        stale = stale_fields(cs, await self._current_values(cs))
        if stale:
            cs.transition(ChangeSetStatus.STALE, self._clock())
            self._store.save(cs)
            self._store.audit(
                at=self._clock(),
                tool="preview_profile_changes",
                changeSetId=cs.id,
                status=str(cs.status),
            )
            raise ProfileEditError(
                ProfileEditErrorCode.STALE_CHANGE_SET,
                changeSetId=cs.id,
                changedFields=stale,
            )
        out["applicable"] = True
        out["profileUnchangedSinceProposal"] = True
        out["writesEnabled"] = self._writes_enabled()
        return out

    # ── DISCARD ─────────────────────────────────────────────────────────────
    def discard(self, change_set_id: str) -> dict[str, Any]:
        cs = self._store.load(change_set_id)
        cs.transition(ChangeSetStatus.DISCARDED, self._clock())
        self._store.save(cs)
        self._store.audit(
            at=self._clock(),
            tool="discard_profile_changes",
            changeSetId=cs.id,
            status=str(cs.status),
        )
        out: dict[str, Any] = {"changeSetId": cs.id, "status": str(cs.status)}
        if cs.results:
            out["results"] = cs.results
        return out

    # ── APPLY ───────────────────────────────────────────────────────────────
    def precheck_apply(
        self,
        change_set_id: str,
        *,
        confirm: bool,
        notify_network: bool | None = None,
    ) -> ChangeSet:
        """Every refusal that needs no browser: run before one is acquired.

        ``notify_network`` is the user's own answer to whether LinkedIn should
        notify their network; an apply without it is refused before any write.
        """
        cs = self._store.load(change_set_id)
        if cs.status is not ChangeSetStatus.PENDING_APPROVAL:
            raise ProfileEditError(
                ProfileEditErrorCode.CHANGE_SET_NOT_PENDING,
                f"This change set is {cs.status}; a change set is applied at most once.",
                changeSetId=cs.id,
                status=str(cs.status),
            )
        if confirm is not True:
            self._store.audit(
                at=self._clock(),
                tool="apply_profile_changes",
                changeSetId=cs.id,
                result="refused",
                error="CONFIRMATION_REQUIRED",
            )
            raise ProfileEditError(
                ProfileEditErrorCode.CONFIRMATION_REQUIRED, changeSetId=cs.id
            )
        if not self._writes_enabled():
            self._store.audit(
                at=self._clock(),
                tool="apply_profile_changes",
                changeSetId=cs.id,
                result="refused",
                error="WRITES_DISABLED",
            )
            raise ProfileEditError(
                ProfileEditErrorCode.WRITES_DISABLED, changeSetId=cs.id
            )
        if notify_network not in (True, False):
            self._store.audit(
                at=self._clock(),
                tool="apply_profile_changes",
                changeSetId=cs.id,
                result="refused",
                error="NOTIFY_DECISION_REQUIRED",
            )
            raise ProfileEditError(
                ProfileEditErrorCode.NOTIFY_DECISION_REQUIRED, changeSetId=cs.id
            )
        return cs

    async def apply(
        self,
        change_set_id: str,
        *,
        confirm: bool,
        notify_network: bool | None = None,
    ) -> dict[str, Any]:
        cs = self.precheck_apply(
            change_set_id, confirm=confirm, notify_network=notify_network
        )
        self._editor.set_network_notification(notify_network)
        await self._check_account(cs)
        try:
            current = await self._current_values(cs)
        except (RateLimitError, AccountRestrictedError) as e:
            raise ProfileEditError(
                ProfileEditErrorCode.AUTHENTICATION_REQUIRED, detail=type(e).__name__
            ) from e
        stale = stale_fields(cs, current)
        if stale:
            cs.transition(ChangeSetStatus.STALE, self._clock())
            self._store.save(cs)
            self._store.audit(
                at=self._clock(),
                tool="apply_profile_changes",
                changeSetId=cs.id,
                status=str(cs.status),
                error="STALE_CHANGE_SET",
            )
            raise ProfileEditError(
                ProfileEditErrorCode.STALE_CHANGE_SET,
                changeSetId=cs.id,
                changedFields=stale,
            )

        cs.snapshot_path = str(
            self._store.save_snapshot(
                self._clock(), cs.id, {k: current[k] for k in cs.baseline}
            )
        )
        cs.transition(ChangeSetStatus.APPLYING, self._clock())
        self._store.save(cs)

        results: list[dict[str, Any]] = []
        stop: ProfileEditError | None = None
        in_flight: FieldChange | None = None
        try:
            skills = (
                {skill_key(s.name): s for s in await self._editor.list_skills()}
                if any(c.section == "skills" for c in cs.changes)
                else {}
            )
            for i, change in enumerate(cs.changes):
                if stop is not None:
                    results.append(
                        {
                            "field": change.key,
                            "status": "NOT_ATTEMPTED",
                            "verified": False,
                        }
                    )
                    continue
                if i:
                    await self._editor.pause(self._pacing)
                in_flight = change
                results.append(await self._attempt(change, skills))
                in_flight = None
                if not results[-1]["verified"]:
                    stop = ProfileEditError(ProfileEditErrorCode(results[-1]["error"]))
                # Saved after every field, so a process that dies mid-apply
                # still leaves a record of what is already live.
                cs.results = list(results)
                self._store.save(cs)
                self._audit_field(cs, change, results[-1])
        except BaseException:
            # Cancelled or timed out mid-apply. The field being written may or
            # may not have saved on LinkedIn; say so rather than guess, and
            # close the record so it is not left at APPLYING.
            attempted = {r["field"] for r in results}
            for change in cs.changes:
                if change.key in attempted:
                    continue
                unknown = in_flight is not None and change.key == in_flight.key
                results.append(
                    {
                        "field": change.key,
                        "status": "OUTCOME_UNKNOWN" if unknown else "NOT_ATTEMPTED",
                        "verified": False,
                    }
                )
            cs.results = results
            cs.transition(
                ChangeSetStatus.PARTIAL_FAILURE
                if any(r["verified"] for r in results)
                else ChangeSetStatus.FAILED,
                self._clock(),
            )
            self._store.save(cs)
            self._store.audit(
                at=self._clock(),
                tool="apply_profile_changes",
                changeSetId=cs.id,
                status=str(cs.status),
                error="INTERRUPTED",
            )
            raise

        done = [r for r in results if r["verified"]]
        if stop is None:
            final = ChangeSetStatus.APPLIED
        elif done:
            final = ChangeSetStatus.PARTIAL_FAILURE
        elif stop.code is ProfileEditErrorCode.STALE_CHANGE_SET:
            final = ChangeSetStatus.STALE
        else:
            final = ChangeSetStatus.FAILED
        cs.results = results
        cs.transition(final, self._clock())
        self._store.save(cs)
        self._store.audit(
            at=self._clock(),
            tool="apply_profile_changes",
            changeSetId=cs.id,
            status=str(final),
        )

        out: dict[str, Any] = {
            "changeSetId": cs.id,
            "status": str(final),
            "results": results,
            "snapshotPath": cs.snapshot_path,
        }
        if final is not ChangeSetStatus.APPLIED:
            out["error"] = str(
                ProfileEditErrorCode.PARTIAL_FAILURE
                if done
                else (stop.code if stop else ProfileEditErrorCode.LINKEDIN_SAVE_FAILED)
            )
            out["recovery"] = (
                "Fields marked UPDATED/ADDED/REMOVED are live and verified. Nothing was rolled back. "
                "The snapshot holds the values from before this apply, for a manual restore; "
                "propose a new change set for anything still to do."
            )
        return out

    async def _attempt(
        self, change: FieldChange, skills: dict[str, Skill]
    ) -> dict[str, Any]:
        """Apply one field; turn every expected failure into a result row."""
        try:
            result = await self._apply_one(change, skills)
            notification = self._editor.last_network_notification()
            if notification is not None:
                result["networkNotification"] = notification
            return result
        except ProfileEditError as e:
            return {
                "field": change.key,
                "status": "FAILED",
                "verified": False,
                "error": str(e.code),
                "message": e.message,
                **({"details": e.details} if e.details else {}),
            }
        except (AuthenticationError, RateLimitError, AccountRestrictedError) as e:
            err = ProfileEditError(
                ProfileEditErrorCode.AUTHENTICATION_REQUIRED, detail=type(e).__name__
            )
            return {
                "field": change.key,
                "status": "FAILED",
                "verified": False,
                "error": str(err.code),
                "message": err.message,
            }
        except Exception as e:  # recorded, never retried
            logger.exception("Unexpected failure applying %s", change.key)
            return {
                "field": change.key,
                "status": "FAILED",
                "verified": False,
                "error": str(ProfileEditErrorCode.LINKEDIN_SAVE_FAILED),
                "message": f"Unexpected {type(e).__name__} while applying {change.label}.",
            }

    def _audit_field(
        self, cs: ChangeSet, change: FieldChange, result: dict[str, Any]
    ) -> None:
        self._store.audit(
            at=self._clock(),
            tool="apply_profile_changes",
            changeSetId=cs.id,
            section=change.section,
            field=change.key,
            result=result["status"],
            verified=result["verified"],
            **({"error": result["error"]} if "error" in result else {}),
        )

    async def _check_account(self, cs: ChangeSet) -> None:
        """Refuse a change set planned against a different LinkedIn account."""
        current = await self._editor.account()
        if cs.account is None or cs.account != current:
            raise ProfileEditError(
                ProfileEditErrorCode.ACCOUNT_MISMATCH,
                changeSetId=cs.id,
                proposedFor=cs.account,
                signedIn=current,
            )

    async def _company_for(
        self,
        request: NewExperienceRequest,
        listed: Sequence[ExperienceSummary],
        reads: _Reads,
    ) -> str:
        """The exact company name to file a new position under."""
        if (request.company is None) == (request.same_company_as is None):
            raise ProfileEditError(
                ProfileEditErrorCode.VALIDATION_ERROR,
                "Each new experience needs exactly one of company or sameCompanyAs.",
                title=request.title,
            )
        if request.company is not None:
            return request.company
        source = resolve_experience(
            ExperienceEdit(experience_id=request.same_company_as), listed
        )
        company = (await reads.experience(source.id)).company
        if not company:
            raise ProfileEditError(
                ProfileEditErrorCode.VALIDATION_ERROR,
                "That experience's form shows no company to copy.",
                experienceId=source.id,
            )
        return company

    async def _add_position(self, change: FieldChange) -> dict[str, Any]:
        """Add one position and prove it: exactly one new id, read back intact."""
        position = NewPosition.from_dict(json.loads(change.after or "{}"))
        before = {e.id for e in await self._editor.list_experiences()}
        await self._editor.add_experience(position)
        added = [e for e in await self._editor.list_experiences() if e.id not in before]
        if len(added) != 1:
            raise ProfileEditError(
                ProfileEditErrorCode.VERIFICATION_FAILED,
                field=change.key,
                expected="exactly one new position",
                newPositions=[e.as_dict() for e in added],
            )
        form = await self._editor.read_experience(added[0].id)
        observed = {
            "title": normalize_text(form.title.value),
            "company": normalize_text(form.company or ""),
            "description": normalize_text(form.description.value),
        }
        wanted = {
            "title": position.title,
            "company": position.company,
            "description": position.description,
        }
        if observed != wanted:
            raise ProfileEditError(
                ProfileEditErrorCode.VERIFICATION_FAILED,
                field=change.key,
                experienceId=added[0].id,
                expected=wanted,
                observed=observed,
            )
        return {
            "field": change.key,
            "status": "ADDED",
            "verified": True,
            "experienceId": added[0].id,
        }

    async def _apply_one(
        self, change: FieldChange, skills: dict[str, Skill]
    ) -> dict[str, Any]:
        if change.kind == "experience_new":
            return await self._add_position(change)
        if change.key == "headline":
            await self._editor.write_headline(
                expected=change.before or "", value=change.after or ""
            )
            observed = normalize_text((await self._editor.read_headline()).value)
        elif change.key == "about":
            await self._editor.write_about(
                expected=change.before or "", value=change.after or ""
            )
            observed = normalize_text((await self._editor.read_about()).value)
        elif change.key.endswith("/start") and change.target and change.after:
            month, year = (int(x) for x in change.after.split("/"))
            await self._editor.write_experience_start(
                change.target, expected=change.before or "", month=month, year=year
            )
            observed = (await self._editor.read_experience(change.target)).start
        elif change.section == "experience" and change.target:
            field_name: ExperienceField = (
                "title" if change.key.endswith("/title") else "description"
            )
            await self._editor.write_experience(
                change.target,
                field=field_name,
                expected=change.before or "",
                value=change.after or "",
            )
            form = await self._editor.read_experience(change.target)
            observed = normalize_text(
                (form.title if field_name == "title" else form.description).value
            )
        elif change.action == "add" and change.after:
            canonical = await self._editor.add_skill(change.after)
            now = {skill_key(s.name): s.name for s in await self._editor.list_skills()}
            if skill_key(change.after) not in now:
                raise ProfileEditError(
                    ProfileEditErrorCode.VERIFICATION_FAILED,
                    field=change.key,
                    expected=change.after,
                    observedSkills=list(now.values()),
                )
            return {
                "field": change.key,
                "status": "ADDED",
                "verified": True,
                "value": canonical,
            }
        elif change.action == "remove" and change.before:
            skill = skills.get(skill_key(change.before))
            if skill is None:
                raise ProfileEditError(
                    ProfileEditErrorCode.SKILL_NOT_FOUND, skill=change.before
                )
            await self._editor.remove_skill(skill)
            now_keys = {skill_key(s.name) for s in await self._editor.list_skills()}
            if skill_key(change.before) in now_keys:
                raise ProfileEditError(
                    ProfileEditErrorCode.VERIFICATION_FAILED,
                    field=change.key,
                    expected="removed",
                )
            return {"field": change.key, "status": "REMOVED", "verified": True}
        else:
            raise ProfileEditError(
                ProfileEditErrorCode.UNSUPPORTED_FIELD, field=change.key
            )
        if observed != (change.after or ""):
            raise ProfileEditError(
                ProfileEditErrorCode.VERIFICATION_FAILED,
                field=change.key,
                expected=change.after,
                observed=observed,
            )
        return {"field": change.key, "status": "UPDATED", "verified": True}

    # ── helpers ─────────────────────────────────────────────────────────────
    async def _current_values(self, cs: ChangeSet) -> dict[str, Any]:
        reads = _Reads(self._editor)
        current: dict[str, Any] = {}
        for key in cs.baseline:
            if key == "headline":
                current[key] = normalize_text(
                    (await self._editor.read_headline()).value
                )
            elif key == "about":
                current[key] = normalize_text((await self._editor.read_about()).value)
            elif key.startswith("experience/"):
                _, exp_id, field_name = key.split("/", 2)
                try:
                    form = await reads.experience(exp_id)
                except ProfileEditError as e:
                    if e.code is ProfileEditErrorCode.EXPERIENCE_NOT_FOUND:
                        continue  # reported by stale_fields as no longer readable
                    raise
                if field_name == "start":
                    current[key] = form.start
                else:
                    current[key] = normalize_text(
                        (
                            form.title if field_name == "title" else form.description
                        ).value
                    )
            elif key == "skills":
                current[key] = sorted(
                    skill_key(s.name) for s in await self._editor.list_skills()
                )
            elif key == "experiences":
                current[key] = sorted(
                    e.id for e in await self._editor.list_experiences()
                )
        return current

    def _presented(self, cs: ChangeSet) -> dict[str, Any]:
        sections = list(dict.fromkeys(c.section for c in cs.changes))
        warnings = list(cs.warnings)
        for c in cs.changes:
            # A start date is always "MM/YYYY", seven of seven characters: its
            # limit is a format, not a budget the user could run out of.
            if c.kind == "experience_start":
                continue
            if c.after and c.max_length and len(c.after) > 0.9 * c.max_length:
                warnings.append(
                    f"{c.label}: {len(c.after)}/{c.max_length} characters, close to LinkedIn's limit."
                )
        return {
            "changeSetId": cs.id,
            "status": str(cs.status),
            "createdAt": cs.created_at,
            "affectedSections": sections,
            "changes": [c.as_dict() for c in cs.changes],
            "diff": render_diff(cs),
            "warnings": warnings,
            "unsupported": [],
            "note": "Nothing has been changed on LinkedIn. apply_profile_changes(changeSetId, confirm=true, notifyNetwork=...) applies exactly these changes after the user approves them and says whether LinkedIn should notify their network.",
        }
