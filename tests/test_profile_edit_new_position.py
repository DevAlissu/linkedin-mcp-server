"""Adding a current position: propose, preview, apply and verify, in memory.

A new position is a change like any other: it is validated at proposal time,
planned against the list of positions it joins, applied only from an approved
change set, and counted as done only when exactly one new position appears and
reads back with the approved title, company and description.
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
    NewExperienceRequest,
    ProfileEditService,
    Proposal,
)
from linkedin_mcp_server.profile_edit.store import ProfileEditStore
from profile_edit_fakes import FakeEditor, FakePosition

INOVA = "INOVA - Polo de Inovação IFAM"


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
                "2787721506", "Desenvolvedor Frontend Pleno", INOVA, "out. de 2025"
            ),
            FakePosition(
                "2741078679", "Desenvolvedor full stack", "UEA", "out. de 2025"
            ),
        ]
    )


FOGOBIO = NewExperienceRequest(
    title="Líder Técnico Frontend",
    start_month=4,
    start_year=2026,
    same_company_as="2787721506",
    employment_type="part_time",
    location_type="remote",
    description="Plataforma que monitora e prevê queimadas na Amazônia.",
)


def fogobio(**overrides: Any) -> NewExperienceRequest:
    return replace(FOGOBIO, **overrides)


async def code_of(awaitable) -> ProfileEditErrorCode:
    with pytest.raises(ProfileEditError) as e:
        await awaitable
    return e.value.code


class TestPropose:
    async def test_the_company_is_copied_exactly_from_an_existing_position(self, store):
        ed = editor()
        cs = await service(ed, store).propose(Proposal(new_experiences=[fogobio()]))

        assert cs["status"] == "PENDING_APPROVAL"
        assert ed.writes == [], "proposing writes nothing"
        [change] = cs["changes"]
        assert change["action"] == "add" and change["section"] == "experience"
        assert f"company: {INOVA}" in cs["diff"]
        assert "NEW POSITION: Líder Técnico Frontend" in cs["diff"]

    async def test_a_company_can_also_be_named(self, store):
        cs = await service(editor(), store).propose(
            Proposal(new_experiences=[fogobio(same_company_as=None, company="Nansen")])
        )
        assert "company: Nansen" in cs["diff"]

    @pytest.mark.parametrize(
        "overrides",
        [
            {"start_month": 13},
            {"start_month": 0},
            {"start_year": 2027},
            {"start_year": 1900},
            {"title": "   "},
            {"title": "Linha\nquebrada"},
            {"employment_type": "volunteer"},
            {"location_type": "anywhere"},
            {"description": "x" * 2001},
        ],
    )
    async def test_invalid_positions_are_refused_before_anything_is_stored(
        self, store, overrides
    ):
        ed = editor()
        assert (
            await code_of(
                service(ed, store).propose(
                    Proposal(new_experiences=[fogobio(**overrides)])
                )
            )
            is ProfileEditErrorCode.VALIDATION_ERROR
        )
        assert ed.writes == []

    @pytest.mark.parametrize(
        "company",
        [{"company": None, "same_company_as": None}, {"company": "Nansen"}],
    )
    async def test_exactly_one_of_company_and_same_company_as(self, store, company):
        assert (
            await code_of(
                service(editor(), store).propose(
                    Proposal(new_experiences=[fogobio(**company)])
                )
            )
            is ProfileEditErrorCode.VALIDATION_ERROR
        )

    async def test_the_same_position_twice_is_refused(self, store):
        assert (
            await code_of(
                service(editor(), store).propose(
                    Proposal(new_experiences=[fogobio(), fogobio()])
                )
            )
            is ProfileEditErrorCode.VALIDATION_ERROR
        )


class TestApply:
    async def test_the_position_is_added_and_read_back(self, store):
        ed = editor()
        s = service(ed, store)
        cs = await s.propose(Proposal(new_experiences=[fogobio()]))

        out = await s.apply(cs["changeSetId"], confirm=True, notify_network=False)

        assert out["status"] == "APPLIED"
        [result] = out["results"]
        assert result["status"] == "ADDED" and result["verified"] is True
        assert result["networkNotification"] == "off"
        added = [p for p in ed.positions if p.id == result["experienceId"]]
        assert [(p.title, p.company) for p in added] == [
            ("Líder Técnico Frontend", INOVA)
        ]
        assert ed.notify is False

    async def test_a_position_added_by_hand_since_the_proposal_makes_it_stale(
        self, store
    ):
        ed = editor()
        s = service(ed, store)
        cs = await s.propose(Proposal(new_experiences=[fogobio()]))
        ed.positions.append(FakePosition("1", "Added by hand", INOVA, "2026"))

        assert (
            await code_of(
                s.apply(cs["changeSetId"], confirm=True, notify_network=False)
            )
            is ProfileEditErrorCode.STALE_CHANGE_SET
        )
        assert ed.writes == []

    async def test_a_position_stored_differently_is_not_verified(self, store):
        ed = editor()
        s = service(ed, store)
        cs = await s.propose(Proposal(new_experiences=[fogobio()]))
        ed.mangle["new_title"] = "Líder Técnico"

        out = await s.apply(cs["changeSetId"], confirm=True, notify_network=False)

        assert out["status"] == "FAILED"
        assert out["results"][0]["error"] == "VERIFICATION_FAILED"

    async def test_adding_a_position_keeps_the_headline(self, store):
        ed = editor()
        ed.headline = "Software Engineer | Mobile & Web Developer"
        s = service(ed, store)
        cs = await s.propose(Proposal(new_experiences=[fogobio()]))

        out = await s.apply(cs["changeSetId"], confirm=True, notify_network=False)

        assert out["status"] == "APPLIED"
        assert ed.headline == "Software Engineer | Mobile & Web Developer"

    async def test_a_headline_changed_by_the_add_is_reported(self, store):
        ed = editor()
        ed.headline = "Software Engineer | Mobile & Web Developer"
        s = service(ed, store)
        cs = await s.propose(Proposal(new_experiences=[fogobio()]))
        ed.mangle["headline_after_add"] = "Líder Técnico Frontend da empresa INOVA"

        out = await s.apply(cs["changeSetId"], confirm=True, notify_network=False)

        [result] = out["results"]
        assert result["error"] == "VERIFICATION_FAILED"
        assert result["details"]["headlineBefore"] == (
            "Software Engineer | Mobile & Web Developer"
        )
        assert result["details"]["headlineAfter"] == (
            "Líder Técnico Frontend da empresa INOVA"
        )
