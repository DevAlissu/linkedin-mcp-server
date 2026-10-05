"""Change sets: what a proposal would change, against which baseline.

A change set is created from the profile as read at proposal time and records
three things that make application safe: the exact before/after of every
field, a fingerprint of the baseline it was planned against, and a status that
only moves forward through ``_TRANSITIONS``. Nothing here touches LinkedIn.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import hashlib
import json
import re
import secrets
import unicodedata

from linkedin_mcp_server.profile_edit.errors import (
    ProfileEditError,
    ProfileEditErrorCode,
)
from linkedin_mcp_server.profile_edit.model import (
    DEFAULT_LIMITS,
    EMPLOYMENT_TYPES,
    LOCATION_TYPES,
    LOGO_SUFFIXES,
    ORGANIZATION_SIZES,
    ORGANIZATION_TYPES,
    SINGLE_LINE,
    NewCompanyPage,
    NewPosition,
    normalize_text,
)


class ChangeSetStatus(StrEnum):
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPLYING = "APPLYING"
    APPLIED = "APPLIED"
    PARTIAL_FAILURE = "PARTIAL_FAILURE"
    FAILED = "FAILED"
    DISCARDED = "DISCARDED"
    STALE = "STALE"


_TRANSITIONS: dict[ChangeSetStatus, frozenset[ChangeSetStatus]] = {
    ChangeSetStatus.PENDING_APPROVAL: frozenset(
        {ChangeSetStatus.APPLYING, ChangeSetStatus.DISCARDED, ChangeSetStatus.STALE}
    ),
    ChangeSetStatus.APPLYING: frozenset(
        {
            # A record left at APPLYING by a process that died mid-apply can be
            # closed by discarding it; its per-field results are kept.
            ChangeSetStatus.DISCARDED,
            ChangeSetStatus.APPLIED,
            ChangeSetStatus.PARTIAL_FAILURE,
            ChangeSetStatus.FAILED,
            # Stale is found inside the apply step too: the editor checks the
            # visible value again before typing, after the pre-flight read.
            ChangeSetStatus.STALE,
        }
    ),
    ChangeSetStatus.STALE: frozenset({ChangeSetStatus.DISCARDED}),
    ChangeSetStatus.APPLIED: frozenset(),
    ChangeSetStatus.PARTIAL_FAILURE: frozenset(),
    ChangeSetStatus.FAILED: frozenset(),
    ChangeSetStatus.DISCARDED: frozenset(),
}

# Control characters other than newline and tab are never valid profile text.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
# A page's public address after linkedin.com/company/, and a website as the
# form's own hint asks for it ("Comece com http://, https:// ou www.").
_PUBLIC_URL = re.compile(r"[a-z0-9][a-z0-9-]{1,99}")
_WEBSITE = re.compile(r"(https?://|www\.)\S+", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class FieldChange:
    """One intended edit.

    ``key`` names the field uniquely and is what baselines and results are
    keyed on: ``headline``, ``about``, ``experience/<id>/title``,
    ``experience/<id>/description``, ``experience/new/<n>``,
    ``skills/add/<name>``, ``skills/remove/<name>``. A new position's ``after``
    is the approved ``NewPosition`` as JSON.
    """

    key: str
    section: str
    kind: str
    label: str
    action: str  # set | add | remove
    before: str | None
    after: str | None
    target: str | None = None
    max_length: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "field": self.key,
            "section": self.section,
            "label": self.label,
            "action": self.action,
            "target": self.target,
            "before": self.before,
            "after": self.after,
            "maxLength": self.max_length,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> FieldChange:
        return cls(
            key=d["field"],
            section=d["section"],
            kind=d.get("kind", d["section"]),
            label=d["label"],
            action=d["action"],
            before=d["before"],
            after=d["after"],
            target=d.get("target"),
            max_length=d.get("maxLength"),
        )


@dataclass(frozen=True, slots=True)
class TextRequest:
    """A requested value for one text field, with the value it replaces."""

    key: str
    section: str
    kind: str
    label: str
    after: str
    current: str
    max_length: int | None
    target: str | None = None


@dataclass(frozen=True, slots=True)
class SkillsRequest:
    add: Sequence[str] = ()
    remove: Sequence[str] = ()


@dataclass(slots=True)
class ChangeSet:
    id: str
    created_at: str
    status: ChangeSetStatus
    changes: list[FieldChange]
    baseline: dict[str, Any]
    fingerprint: str
    field_hashes: dict[str, str]
    warnings: list[str] = field(default_factory=list)
    results: list[dict[str, Any]] = field(default_factory=list)
    history: list[dict[str, str]] = field(default_factory=list)
    snapshot_path: str | None = None
    # The profile URL of the account the change set was planned against.
    account: str | None = None

    def transition(self, to: ChangeSetStatus, at: str) -> None:
        if to not in _TRANSITIONS[self.status]:
            raise ProfileEditError(
                ProfileEditErrorCode.CHANGE_SET_NOT_PENDING,
                f"A change set in status {self.status} cannot move to {to}.",
                changeSetId=self.id,
                status=str(self.status),
            )
        self.history.append({"from": str(self.status), "to": str(to), "at": at})
        self.status = to

    def as_dict(self) -> dict[str, Any]:
        return {
            "changeSetId": self.id,
            "createdAt": self.created_at,
            "status": str(self.status),
            "changes": [{**c.as_dict(), "kind": c.kind} for c in self.changes],
            "baseline": self.baseline,
            "fingerprint": self.fingerprint,
            "fieldHashes": self.field_hashes,
            "warnings": self.warnings,
            "results": self.results,
            "history": self.history,
            "snapshotPath": self.snapshot_path,
            "account": self.account,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> ChangeSet:
        return cls(
            id=d["changeSetId"],
            created_at=d["createdAt"],
            status=ChangeSetStatus(d["status"]),
            changes=[FieldChange.from_dict(c) for c in d["changes"]],
            baseline=dict(d["baseline"]),
            fingerprint=d["fingerprint"],
            field_hashes=dict(d["fieldHashes"]),
            warnings=list(d.get("warnings", [])),
            results=list(d.get("results", [])),
            history=list(d.get("history", [])),
            snapshot_path=d.get("snapshotPath"),
            account=d.get("account"),
        )


def new_change_set_id() -> str:
    return f"cs_{secrets.token_hex(8)}"


def _hash(value: Any) -> str:
    canonical = json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def fingerprint(baseline: Mapping[str, Any]) -> tuple[str, dict[str, str]]:
    """Whole-baseline hash plus one hash per field, so a mismatch can be named."""
    return _hash(dict(baseline)), {k: _hash(v) for k, v in baseline.items()}


def skill_key(name: str) -> str:
    """Case- and width-insensitive identity of a skill name."""
    return " ".join(unicodedata.normalize("NFKC", name).casefold().split())


def _check_text(
    kind: str, label: str, value: str, problems: list[dict[str, Any]]
) -> None:
    if _CONTROL.search(value):
        problems.append({"field": label, "reason": "contains control characters"})
    if kind in SINGLE_LINE and "\n" in value:
        problems.append({"field": label, "reason": "must be a single line"})


def _length_problem(label: str, value: str, limit: int) -> dict[str, Any] | None:
    if len(value) <= limit:
        return None
    return {
        "field": label,
        "reason": "too long",
        "proposedLength": len(value),
        "allowedLength": limit,
        "overflow": len(value) - limit,
    }


def _plan_new_positions(
    positions: Sequence[NewPosition],
    current_experience_ids: Sequence[str],
    this_year: int | None,
    problems: list[dict[str, Any]],
) -> tuple[list[FieldChange], dict[str, Any]]:
    """Validate new positions; the baseline is the set of positions they join.

    Adding changes the experience list, so the whole list of position ids is
    the baseline: if a position is added or removed by hand before apply, the
    change set goes stale instead of adding next to an unseen change.
    """
    changes: list[FieldChange] = []
    seen: set[tuple[str, str]] = set()
    for n, raw in enumerate(positions, start=1):
        p = NewPosition(
            title=normalize_text(raw.title),
            company=normalize_text(raw.company),
            start_month=raw.start_month,
            start_year=raw.start_year,
            employment_type=raw.employment_type,
            location_type=raw.location_type,
            description=normalize_text(raw.description),
        )
        label = f"New position {n}: {p.title or '(no title)'}"
        for kind, value in (("experience_title", p.title), ("company", p.company)):
            _check_text(kind, f"{label} {kind}", value, problems)
            if not value:
                problems.append(
                    {"field": f"{label} {kind}", "reason": "cannot be empty"}
                )
            elif (
                q := _length_problem(f"{label} {kind}", value, DEFAULT_LIMITS[kind])
            ) is not None:
                problems.append(q)
        _check_text(
            "experience_description", f"{label} description", p.description, problems
        )
        if (
            q := _length_problem(
                f"{label} description",
                p.description,
                DEFAULT_LIMITS["experience_description"],
            )
        ) is not None:
            problems.append(q)
        if not 1 <= p.start_month <= 12:
            problems.append(
                {"field": f"{label} startMonth", "reason": "must be 1 to 12"}
            )
        if p.start_year < 1950 or (this_year is not None and p.start_year > this_year):
            problems.append(
                {"field": f"{label} startYear", "reason": "not a plausible start year"}
            )
        if p.employment_type is not None and p.employment_type not in EMPLOYMENT_TYPES:
            problems.append(
                {
                    "field": f"{label} employmentType",
                    "reason": f"one of {', '.join(EMPLOYMENT_TYPES)}",
                }
            )
        if p.location_type is not None and p.location_type not in LOCATION_TYPES:
            problems.append(
                {
                    "field": f"{label} locationType",
                    "reason": f"one of {', '.join(LOCATION_TYPES)}",
                }
            )
        identity = (p.title.casefold(), p.company.casefold())
        if identity in seen:
            problems.append({"field": label, "reason": "requested more than once"})
        seen.add(identity)
        changes.append(
            FieldChange(
                key=f"experience/new/{n}",
                section="experience",
                kind="experience_new",
                label=label,
                action="add",
                before=None,
                after=json.dumps(p.as_dict(), ensure_ascii=False, sort_keys=True),
            )
        )
    baseline = {"experiences": sorted(current_experience_ids)} if positions else {}
    return changes, baseline


def _plan_new_company_page(
    raw: NewCompanyPage, problems: list[dict[str, Any]]
) -> FieldChange:
    """Validate a company page to create.

    It has no baseline: nothing on the profile changes. The one thing that can
    change under it, its public address being taken, is checked by LinkedIn's
    own form at apply time, before anything is submitted.
    """
    page = NewCompanyPage(
        name=normalize_text(raw.name),
        public_url=normalize_text(raw.public_url).lower(),
        industry=normalize_text(raw.industry),
        size=raw.size,
        organization_type=raw.organization_type,
        website=normalize_text(raw.website),
        tagline=normalize_text(raw.tagline),
        logo_path=raw.logo_path,
        representative_declared=raw.representative_declared,
    )
    label = f"New company page: {page.name or '(no name)'}"
    for kind, what, value in (
        ("company_page_name", "name", page.name),
        ("company_tagline", "tagline", page.tagline),
    ):
        _check_text(kind, f"{label} {what}", value, problems)
        if (
            q := _length_problem(f"{label} {what}", value, DEFAULT_LIMITS[kind])
        ) is not None:
            problems.append(q)
    for what, value in (("name", page.name), ("industry", page.industry)):
        if not value:
            problems.append({"field": f"{label} {what}", "reason": "cannot be empty"})
    if not _PUBLIC_URL.fullmatch(page.public_url):
        problems.append(
            {
                "field": f"{label} publicUrl",
                "reason": "2 to 100 letters, digits or hyphens, starting with a letter or digit",
            }
        )
    if page.website and not _WEBSITE.fullmatch(page.website):
        problems.append(
            {
                "field": f"{label} website",
                "reason": "must start with http://, https:// or www.",
            }
        )
    if page.size not in ORGANIZATION_SIZES:
        problems.append(
            {
                "field": f"{label} size",
                "reason": f"one of {', '.join(ORGANIZATION_SIZES)}",
            }
        )
    if page.organization_type not in ORGANIZATION_TYPES:
        problems.append(
            {
                "field": f"{label} organizationType",
                "reason": f"one of {', '.join(ORGANIZATION_TYPES)}",
            }
        )
    if page.logo_path and not page.logo_path.lower().endswith(LOGO_SUFFIXES):
        problems.append(
            {"field": f"{label} logoPath", "reason": "a .jpg, .jpeg or .png file"}
        )
    if not page.representative_declared:
        problems.append(
            {
                "field": f"{label} authorizedRepresentative",
                "reason": "LinkedIn requires the user to state that they officially "
                "represent the organization; ask the user, never assume it",
            }
        )
    return FieldChange(
        key="company/new",
        section="company",
        kind="company_page_new",
        label=label,
        action="add",
        before=None,
        after=json.dumps(page.as_dict(), ensure_ascii=False, sort_keys=True),
    )


def plan_changes(
    texts: Sequence[TextRequest],
    skills: SkillsRequest,
    current_skills: Sequence[str],
    new_positions: Sequence[NewPosition] = (),
    current_experience_ids: Sequence[str] = (),
    this_year: int | None = None,
    new_company_page: NewCompanyPage | None = None,
) -> tuple[list[FieldChange], dict[str, Any], list[str]]:
    """Validate requests against current values; return changes, baseline, warnings.

    Over-long values are refused with their lengths, never truncated. A value
    equal to the current one is dropped with a warning rather than proposed.
    """
    problems: list[dict[str, Any]] = []
    warnings: list[str] = []
    changes: list[FieldChange] = []
    baseline: dict[str, Any] = {}
    seen: set[str] = set()

    for t in texts:
        if t.key in seen:
            problems.append({"field": t.label, "reason": "requested more than once"})
            continue
        seen.add(t.key)
        after = normalize_text(t.after)
        before = normalize_text(t.current)
        _check_text(t.kind, t.label, after, problems)
        if not after and t.kind in {"headline", "experience_title"}:
            problems.append({"field": t.label, "reason": "cannot be empty"})
        limit = t.max_length or DEFAULT_LIMITS[t.kind]
        if (p := _length_problem(t.label, after, limit)) is not None:
            problems.append(p)
        if after == before:
            warnings.append(
                f"{t.label}: proposed value is identical to the current one; skipped."
            )
            continue
        if not after:
            warnings.append(f"{t.label}: the proposal clears this field.")
        baseline[t.key] = before
        changes.append(
            FieldChange(
                key=t.key,
                section=t.section,
                kind=t.kind,
                label=t.label,
                action="set",
                before=before,
                after=after,
                target=t.target,
                max_length=limit,
            )
        )

    by_key = {skill_key(s): s for s in current_skills}
    add_keys: set[str] = set()
    adds: list[str] = []
    for raw in skills.add:
        name = normalize_text(raw)
        k = skill_key(name)
        if not name or k in add_keys:
            continue
        add_keys.add(k)
        _check_text("skill", f"skill '{name}'", name, problems)
        if (
            p := _length_problem(f"skill '{name}'", name, DEFAULT_LIMITS["skill"])
        ) is not None:
            problems.append(p)
        if k in by_key:
            warnings.append(
                f"Skill '{by_key[k]}' is already on the profile; not added again."
            )
            continue
        adds.append(name)
    removes: list[str] = []
    for raw in skills.remove:
        k = skill_key(raw)
        if k in add_keys:
            problems.append(
                {"field": f"skill '{raw}'", "reason": "both added and removed"}
            )
            continue
        if k not in by_key:
            raise ProfileEditError(
                ProfileEditErrorCode.SKILL_NOT_FOUND,
                f"'{raw}' is not one of the profile's skills.",
                skill=raw,
                currentSkills=list(current_skills),
            )
        if by_key[k] not in removes:
            removes.append(by_key[k])

    position_changes, position_baseline = _plan_new_positions(
        new_positions, current_experience_ids, this_year, problems
    )
    page_change = (
        _plan_new_company_page(new_company_page, problems)
        if new_company_page is not None
        else None
    )

    if problems:
        raise ProfileEditError(ProfileEditErrorCode.VALIDATION_ERROR, problems=problems)

    changes.extend(position_changes)
    if page_change is not None:
        changes.append(page_change)
    baseline.update(position_baseline)
    if adds or removes:
        baseline["skills"] = sorted(skill_key(s) for s in current_skills)
    for name in adds:
        changes.append(
            FieldChange(
                key=f"skills/add/{skill_key(name)}",
                section="skills",
                kind="skill",
                label=f"Skill: {name}",
                action="add",
                before=None,
                after=name,
                target=name,
            )
        )
    for name in removes:
        changes.append(
            FieldChange(
                key=f"skills/remove/{skill_key(name)}",
                section="skills",
                kind="skill",
                label=f"Skill: {name}",
                action="remove",
                before=name,
                after=None,
                target=name,
            )
        )
    return changes, baseline, warnings


def build_change_set(
    texts: Sequence[TextRequest],
    skills: SkillsRequest,
    current_skills: Sequence[str],
    *,
    now: str,
    change_set_id: str | None = None,
    new_positions: Sequence[NewPosition] = (),
    current_experience_ids: Sequence[str] = (),
    new_company_page: NewCompanyPage | None = None,
) -> ChangeSet:
    changes, baseline, warnings = plan_changes(
        texts,
        skills,
        current_skills,
        new_positions,
        current_experience_ids,
        this_year=int(now[:4]) if now[:4].isdigit() else None,
        new_company_page=new_company_page,
    )
    if not changes:
        raise ProfileEditError(
            ProfileEditErrorCode.VALIDATION_ERROR,
            "Nothing to change: every requested value already matches the profile.",
            warnings=warnings,
        )
    fp, hashes = fingerprint(baseline)
    return ChangeSet(
        id=change_set_id or new_change_set_id(),
        created_at=now,
        status=ChangeSetStatus.PENDING_APPROVAL,
        changes=changes,
        baseline=baseline,
        fingerprint=fp,
        field_hashes=hashes,
        warnings=warnings,
    )


def stale_fields(cs: ChangeSet, current: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Fields whose current value differs from the change set's baseline."""
    stale: list[dict[str, Any]] = []
    for key, expected in cs.baseline.items():
        if key not in current:
            stale.append({"field": key, "reason": "no longer readable"})
        elif _hash(current[key]) != cs.field_hashes[key]:
            stale.append({"field": key, "expected": expected, "actual": current[key]})
    return stale


def render_diff(cs: ChangeSet) -> str:
    """The human-readable diff shown to the user before approval."""
    lines: list[str] = []
    for section in dict.fromkeys(c.section for c in cs.changes):
        lines.append(section.upper())
        for c in (c for c in cs.changes if c.section == section):
            if c.kind == "experience_new" and c.after:
                lines.extend(
                    _new_position_lines(NewPosition.from_dict(json.loads(c.after)))
                )
            elif c.kind == "company_page_new" and c.after:
                lines.extend(
                    _new_company_page_lines(
                        NewCompanyPage.from_dict(json.loads(c.after))
                    )
                )
            elif c.action == "add":
                lines.append(f"+ {c.after}")
            elif c.action == "remove":
                lines.append(f"- {c.before}")
            else:
                if c.section == "experience":
                    lines.append(f"[{c.label}]")
                lines.append(f"before: {c.before or '(empty)'}")
                lines.append(f"after:  {c.after or '(empty)'}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _new_position_lines(p: NewPosition) -> list[str]:
    details = [f"since {p.start_month:02d}/{p.start_year} (current)"]
    details += [x for x in (p.employment_type, p.location_type) if x]
    lines = [
        f"+ NEW POSITION: {p.title}",
        f"  company: {p.company}",
        f"  {', '.join(details)}",
    ]
    if p.description:
        lines.append("  description: " + p.description.replace("\n", "\n  "))
    return lines


def _new_company_page_lines(p: NewCompanyPage) -> list[str]:
    lines = [
        f"+ NEW COMPANY PAGE: {p.name}",
        f"  address: linkedin.com/company/{p.public_url}",
        f"  industry: {p.industry}",
        f"  size: {p.size} employees, type: {p.organization_type}",
    ]
    lines += [
        f"  {k}: {v}"
        for k, v in (
            ("website", p.website),
            ("tagline", p.tagline),
            ("logo", p.logo_path),
        )
        if v
    ]
    lines.append(
        "  the user states they officially represent this organization and accept "
        "LinkedIn's Pages terms"
    )
    return lines
