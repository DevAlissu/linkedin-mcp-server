"""Changing an existing position's start date, in memory.

The start date is planned like any text field: "MM/YYYY" before and after, the
current value as the baseline, and verified by reading the form back.
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


@pytest.fixture
def store(tmp_path: Path) -> ProfileEditStore:
    return ProfileEditStore(tmp_path)


def service(editor: FakeEditor, store: ProfileEditStore) -> ProfileEditService:
    return ProfileEditService(
        editor,
        store,
        writes_enabled=lambda: True,
        pacing_seconds=3.0,
        clock=lambda: "2026-10-04T18:30:00+00:00",
    )


def editor() -> FakeEditor:
    return FakeEditor(
        positions=[
            FakePosition(
                "3036399820",
                "Líder Técnico Frontend",
                "INOVA",
                "abr. de 2026",
                start="04/2026",
            )
        ]
    )


OCTOBER_2025 = ExperienceEdit(
    experience_id="3036399820", start_month=10, start_year=2025
)


def move_to_october_2025(**overrides: Any) -> Proposal:
    return Proposal(experiences=[replace(OCTOBER_2025, **overrides)])


async def code_of(awaitable) -> ProfileEditErrorCode:
    with pytest.raises(ProfileEditError) as e:
        await awaitable
    return e.value.code


async def test_the_start_date_is_proposed_with_its_before_and_after(store):
    ed = editor()
    cs = await service(ed, store).propose(move_to_october_2025())

    [change] = cs["changes"]
    assert (change["before"], change["after"]) == ("04/2026", "10/2025")
    assert cs["warnings"] == []  # a full "MM/YYYY" is not "close to the limit"
    assert ed.writes == []


async def test_the_start_date_is_applied_and_read_back(store):
    ed = editor()
    s = service(ed, store)
    cs = await s.propose(move_to_october_2025())

    out = await s.apply(cs["changeSetId"], confirm=True, notify_network=False)

    assert out["status"] == "APPLIED" and out["results"][0]["verified"] is True
    assert ed.positions[0].start == "10/2025"


async def test_a_date_changed_by_hand_makes_the_change_set_stale(store):
    ed = editor()
    s = service(ed, store)
    cs = await s.propose(move_to_october_2025())
    ed.positions[0].start = "03/2026"

    assert (
        await code_of(s.apply(cs["changeSetId"], confirm=True, notify_network=False))
        is ProfileEditErrorCode.STALE_CHANGE_SET
    )
    assert ed.positions[0].start == "03/2026"


async def test_a_date_stored_differently_is_not_verified(store):
    ed = editor()
    s = service(ed, store)
    cs = await s.propose(move_to_october_2025())
    ed.mangle["start"] = "11/2025"

    out = await s.apply(cs["changeSetId"], confirm=True, notify_network=False)

    assert out["results"][0]["error"] == "VERIFICATION_FAILED"


@pytest.mark.parametrize(
    "overrides",
    [
        {"start_month": 13},
        {"start_month": 0},
        {"start_year": 2027},
        {"start_year": 1900},
        {"start_year": None},
        {"start_month": None},
    ],
)
async def test_invalid_or_partial_dates_are_refused(store, overrides):
    # A title rides along so a half date could not hide behind "nothing to change".
    overrides = {"title": "Líder Técnico", **overrides}
    assert (
        await code_of(
            service(editor(), store).propose(move_to_october_2025(**overrides))
        )
        is ProfileEditErrorCode.VALIDATION_ERROR
    )
