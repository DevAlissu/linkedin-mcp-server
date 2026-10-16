"""Ending a position, or making it current again, in memory.

The end is planned like the start: "present" or "MM/YYYY" before and after, the
current value as the baseline, and verified by reading the form back. After an
apply that touched experience, every other position must read as before: a
save that also ends another role held now is reported, not passed over.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from linkedin_mcp_server.profile_edit.errors import (
    ProfileEditError,
    ProfileEditErrorCode,
)
from linkedin_mcp_server.profile_edit.service import (
    ExperienceEdit,
    ProfileEditService,
    Proposal,
)
from linkedin_mcp_server.profile_edit.store import ProfileEditStore
from profile_edit_fakes import FakeEditor, FakePosition

MYSQUARE, FOGOBIO = "2787721506", "3036399820"


@pytest.fixture
def store(tmp_path: Path) -> ProfileEditStore:
    return ProfileEditStore(tmp_path)


def service(editor: FakeEditor, store: ProfileEditStore) -> ProfileEditService:
    return ProfileEditService(
        editor,
        store,
        writes_enabled=lambda: True,
        pacing_seconds=3.0,
        clock=lambda: "2026-10-16T18:30:00+00:00",
    )


def editor() -> FakeEditor:
    return FakeEditor(
        positions=[
            FakePosition(
                MYSQUARE,
                "Líder Técnico Frontend",
                "INOVA",
                "out. de 2025 - o momento · 1 ano 1 mês",
                start="10/2025",
            ),
            FakePosition(
                FOGOBIO,
                "Líder Técnico Frontend",
                "INOVA",
                "out. de 2025 - out. de 2026 · 1 ano 1 mês",
                start="10/2025",
                end="10/2026",
            ),
        ]
    )


END_MYSQUARE = ExperienceEdit(experience_id=MYSQUARE, end_month=1, end_year=2026)
REOPEN_FOGOBIO = ExperienceEdit(experience_id=FOGOBIO, current=True)


def proposal(*edits: ExperienceEdit) -> Proposal:
    return Proposal(experiences=list(edits))


async def code_of(awaitable) -> ProfileEditErrorCode:
    with pytest.raises(ProfileEditError) as e:
        await awaitable
    return e.value.code


async def test_ending_and_reopening_are_proposed_with_before_and_after(store):
    ed = editor()
    cs = await service(ed, store).propose(proposal(END_MYSQUARE, REOPEN_FOGOBIO))

    assert [(c["field"], c["before"], c["after"]) for c in cs["changes"]] == [
        (f"experience/{MYSQUARE}/end", "present", "01/2026"),
        (f"experience/{FOGOBIO}/end", "10/2026", "present"),
    ]
    assert cs["warnings"] == [] and ed.writes == []


async def test_both_are_applied_and_read_back(store):
    ed = editor()
    s = service(ed, store)
    cs = await s.propose(proposal(END_MYSQUARE, REOPEN_FOGOBIO))

    out = await s.apply(cs["changeSetId"], confirm=True, notify_network=False)

    assert out["status"] == "APPLIED", out
    assert all(r["verified"] for r in out["results"])
    assert [p.end for p in ed.positions] == ["01/2026", "present"]
    assert "unexpectedChanges" not in out


async def test_an_end_changed_by_hand_makes_the_change_set_stale(store):
    ed = editor()
    s = service(ed, store)
    cs = await s.propose(proposal(END_MYSQUARE))
    ed.positions[0].end = "12/2025"

    assert (
        await code_of(s.apply(cs["changeSetId"], confirm=True, notify_network=False))
        is ProfileEditErrorCode.STALE_CHANGE_SET
    )
    assert ed.positions[0].end == "12/2025"


async def test_an_end_stored_differently_is_not_verified(store):
    ed = editor()
    s = service(ed, store)
    cs = await s.propose(proposal(END_MYSQUARE))
    ed.mangle["end"] = "02/2026"

    out = await s.apply(cs["changeSetId"], confirm=True, notify_network=False)

    assert out["results"][0]["error"] == "VERIFICATION_FAILED"


@pytest.mark.parametrize(
    "overrides",
    [
        {"end_month": None},  # half a date
        {"end_year": None},
        {"end_month": 13},
        {"end_year": 2027},  # after this month
        {"end_month": 11, "end_year": 2026},
        {"end_month": 9, "end_year": 2025},  # before the start, 10/2025
        {"current": True},  # an end and "current" at once
    ],
)
async def test_impossible_ends_are_refused(store, overrides: dict[str, Any]):
    edit = replace(END_MYSQUARE, **overrides)
    assert (
        await code_of(service(editor(), store).propose(proposal(edit)))
        is ProfileEditErrorCode.VALIDATION_ERROR
    )


async def test_an_end_before_a_new_start_is_refused(store):
    edit = replace(END_MYSQUARE, start_month=3, start_year=2026)
    assert (
        await code_of(service(editor(), store).propose(proposal(edit)))
        is ProfileEditErrorCode.VALIDATION_ERROR
    )


async def test_another_position_changed_by_the_save_is_reported(store):
    ed = editor()

    def linkedin_also_ends_fogobio(e: FakeEditor) -> None:
        e.positions[1].date_range = "out. de 2025 - jan. de 2026 · 4 meses"

    s = service(ed, store)
    cs = await s.propose(proposal(END_MYSQUARE))
    ed.after_write = linkedin_also_ends_fogobio

    out = await s.apply(cs["changeSetId"], confirm=True, notify_network=False)

    assert out["status"] == "PARTIAL_FAILURE"
    assert out["unexpectedChanges"] == [
        {
            "experienceId": FOGOBIO,
            "change": "dates",
            "before": "out. de 2025 - out. de 2026",
            "after": "out. de 2025 - jan. de 2026",
        }
    ]


async def test_the_targeted_position_dates_may_change(store):
    ed = editor()

    def dates_follow_the_end(e: FakeEditor) -> None:
        e.positions[0].date_range = "out. de 2025 - jan. de 2026 · 4 meses"

    s = service(ed, store)
    cs = await s.propose(proposal(END_MYSQUARE))
    ed.after_write = dates_follow_the_end

    out = await s.apply(cs["changeSetId"], confirm=True, notify_network=False)

    assert out["status"] == "APPLIED" and "unexpectedChanges" not in out


async def test_a_title_edit_that_changes_another_title_is_reported(store):
    ed = editor()

    def renames_the_other(e: FakeEditor) -> None:
        e.positions[1].title = "Algo"

    s = service(ed, store)
    cs = await s.propose(
        proposal(ExperienceEdit(experience_id=MYSQUARE, title="Tech Lead"))
    )
    ed.after_write = renames_the_other

    out = await s.apply(cs["changeSetId"], confirm=True, notify_network=False)

    assert [u["change"] for u in out["unexpectedChanges"]] == ["title"]
