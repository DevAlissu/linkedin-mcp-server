"""Creating a LinkedIn Page for an organization: propose, apply and verify, in memory.

A company page is a change like any other: validated at proposal time, created
only from an approved change set, and counted as done only when its public
address shows the approved name and tagline. LinkedIn requires the user to
state that they represent the organization, so a proposal without that
statement is refused.
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
from linkedin_mcp_server.profile_edit.model import NewCompanyPage
from linkedin_mcp_server.profile_edit.service import ProfileEditService, Proposal
from linkedin_mcp_server.profile_edit.store import ProfileEditStore
from profile_edit_fakes import FakeEditor


@pytest.fixture
def store(tmp_path: Path) -> ProfileEditStore:
    return ProfileEditStore(tmp_path / "edits")


@pytest.fixture
def logo(tmp_path: Path) -> Path:
    path = tmp_path / "bedoc-logo.jpg"
    path.write_bytes(b"\xff\xd8\xff\xe0 not a real jpeg, never opened")
    return path


def service(editor: FakeEditor, store: ProfileEditStore) -> ProfileEditService:
    return ProfileEditService(
        editor,
        store,
        writes_enabled=lambda: True,
        pacing_seconds=3.0,
        clock=lambda: "2026-10-04T22:30:00+00:00",
    )


BEDOC = NewCompanyPage(
    name="BeDoc",
    public_url="sejabedoc",
    industry="Hospitais e atividades de atenção à saúde humana",
    size="2-10",
    organization_type="privately_held",
    website="https://sejabedoc.com.br",
    tagline="Triagem por IA que leva o paciente ao especialista certo.",
    representative_declared=True,
)


def bedoc(**overrides: Any) -> Proposal:
    return Proposal(new_company_page=replace(BEDOC, **overrides))


async def problems_of(awaitable) -> list[dict[str, Any]]:
    with pytest.raises(ProfileEditError) as e:
        await awaitable
    assert e.value.code is ProfileEditErrorCode.VALIDATION_ERROR
    return e.value.details.get("problems", [])


async def test_the_page_is_proposed_in_full_and_nothing_is_created(store, logo):
    ed = FakeEditor()
    cs = await service(ed, store).propose(bedoc(logo_path=str(logo)))

    [change] = cs["changes"]
    assert change["field"] == "company/new" and change["action"] == "add"
    for shown in (
        "+ NEW COMPANY PAGE: BeDoc",
        "linkedin.com/company/sejabedoc",
        "Hospitais e atividades de atenção à saúde humana",
        "2-10 employees, type: privately_held",
        str(logo.resolve()),
        "officially represent this organization",
    ):
        assert shown in cs["diff"]
    assert ed.writes == [] and ed.company_pages == {}


async def test_the_page_is_created_and_read_back(store):
    ed = FakeEditor()
    s = service(ed, store)
    cs = await s.propose(bedoc())

    out = await s.apply(cs["changeSetId"], confirm=True, notify_network=False)

    assert out["status"] == "APPLIED"
    [result] = out["results"]
    assert result["status"] == "CREATED" and result["verified"] is True
    assert result["companyUrl"] == "https://www.linkedin.com/company/sejabedoc/"
    assert list(ed.company_pages) == ["sejabedoc"]


async def test_without_the_users_statement_nothing_is_proposed(store):
    problems = await problems_of(
        service(FakeEditor(), store).propose(bedoc(representative_declared=False))
    )
    assert [p["field"] for p in problems] == [
        "New company page: BeDoc authorizedRepresentative"
    ]


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"name": ""}, "name"),
        ({"industry": ""}, "industry"),
        ({"public_url": "seja bedoc"}, "publicUrl"),
        ({"public_url": "-bedoc"}, "publicUrl"),
        ({"size": "3-9"}, "size"),
        ({"organization_type": "startup"}, "organizationType"),
        ({"website": "sejabedoc.com.br"}, "website"),
        ({"tagline": "x" * 121}, "tagline"),
        ({"tagline": "two\nlines"}, "tagline"),
        ({"logo_path": "logo.gif"}, "logoPath"),
    ],
)
async def test_invalid_pages_are_refused(store, tmp_path, overrides, field):
    if "logo_path" in overrides:  # an existing file, refused for its type
        gif = tmp_path / overrides["logo_path"]
        gif.write_bytes(b"GIF89a")
        overrides = {"logo_path": str(gif)}
    problems = await problems_of(
        service(FakeEditor(), store).propose(bedoc(**overrides))
    )
    assert any(p["field"].endswith(field) for p in problems), problems


async def test_a_missing_logo_file_is_refused(store, tmp_path):
    with pytest.raises(ProfileEditError) as e:
        await service(FakeEditor(), store).propose(
            bedoc(logo_path=str(tmp_path / "missing.png"))
        )
    assert e.value.code is ProfileEditErrorCode.VALIDATION_ERROR


async def test_the_address_is_compared_in_lower_case(store):
    cs = await service(FakeEditor(), store).propose(bedoc(public_url="SejaBeDoc"))
    assert "linkedin.com/company/sejabedoc" in cs["diff"]


async def test_a_page_that_does_not_show_the_approved_name_is_not_verified(store):
    ed = FakeEditor()
    s = service(ed, store)
    cs = await s.propose(bedoc())
    ed.mangle["page_name"] = "Bedoc"

    out = await s.apply(cs["changeSetId"], confirm=True, notify_network=False)

    assert out["status"] == "FAILED"
    assert out["results"][0]["error"] == "VERIFICATION_FAILED"


async def test_an_address_taken_before_apply_creates_nothing(store):
    ed = FakeEditor()
    s = service(ed, store)
    cs = await s.propose(bedoc())
    ed.company_pages["sejabedoc"] = replace(BEDOC, name="Someone else")

    out = await s.apply(cs["changeSetId"], confirm=True, notify_network=False)

    assert out["results"][0]["error"] == "LINKEDIN_SAVE_FAILED"
    assert ed.company_pages["sejabedoc"].name == "Someone else"


async def test_a_page_without_the_approved_tagline_is_not_verified(store):
    ed = FakeEditor()
    s = service(ed, store)
    cs = await s.propose(bedoc())
    ed.mangle["page_tagline"] = "Outro slogan"

    out = await s.apply(cs["changeSetId"], confirm=True, notify_network=False)

    assert out["results"][0]["error"] == "VERIFICATION_FAILED"
