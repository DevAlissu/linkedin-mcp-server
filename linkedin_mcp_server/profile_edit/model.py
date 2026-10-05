"""Read model for the editable parts of the member's own profile.

Values that a change set compares against are read from LinkedIn's own edit
forms, not from the rendered profile: the profile truncates ("…see more"),
duplicates text for screen readers and reflows whitespace, while a form field
holds exactly what is stored. The form also carries ``maxlength``, which is the
character limit LinkedIn enforces today.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import re

# Used only when the form exposes no maxlength. Measured on LinkedIn's edit
# forms; a UI-reported limit always wins (see TextField.max_length).
DEFAULT_LIMITS: dict[str, int] = {
    "headline": 220,
    "about": 2600,
    "experience_title": 100,
    "experience_description": 2000,
    "skill": 80,
    "company": 100,
    "experience_start": 7,
    # The company-page form states 120 for the tagline ("0/120", measured
    # 4 October 2026) and no limit for the name, kept at a company's 100.
    "company_page_name": 100,
    "company_tagline": 120,
}

# Single-line fields; newlines are refused rather than silently joined.
SINGLE_LINE = frozenset(
    {
        "headline",
        "experience_title",
        "skill",
        "company",
        "experience_start",
        "company_page_name",
        "company_tagline",
    }
)

# Canonical values for the new-position form's two dropdowns. Each locale maps
# them to the option text it shows (profile_selectors.OPTION_TEXT).
EMPLOYMENT_TYPES = (
    "full_time",
    "part_time",
    "self_employed",
    "freelance",
    "contract",
    "internship",
    "apprenticeship",
)
LOCATION_TYPES = ("on_site", "hybrid", "remote")

# The company-page form's dropdowns. Sizes are chosen by position, because the
# bands run from the smallest up with the placeholder first; types go through
# the locale table like the new-position dropdowns.
ORGANIZATION_SIZES = (
    "0-1",
    "2-10",
    "11-50",
    "51-200",
    "201-500",
    "501-1000",
    "1001-5000",
    "5001-10000",
    "10001+",
)
ORGANIZATION_TYPES = (
    "public_company",
    "self_employed",
    "government_agency",
    "nonprofit",
    "sole_proprietorship",
    "privately_held",
    "partnership",
)
# What the logo input accepts (its accept attribute, measured).
LOGO_SUFFIXES = (".jpg", ".jpeg", ".png")


def normalize_text(value: str) -> str:
    """The form of a value we propose, write and compare.

    Line endings are unified, outer whitespace removed, and a run of blank lines
    reduced to one paragraph break, because LinkedIn's editors store none of
    those distinctions: a paragraph break is an empty paragraph, which reads
    back as several newlines. Other inner whitespace is the user's text and is
    kept.
    """
    text = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    return _BLANK_LINES.sub("\n\n", text)


def format_start(month: int, year: int) -> str:
    return f"{month:02d}/{year}"


_BLANK_LINES = re.compile(r"\n[ \t]*\n(?:[ \t]*\n)+")


@dataclass(frozen=True, slots=True)
class TextField:
    value: str
    max_length: int | None = None

    def limit(self, kind: str) -> int:
        return self.max_length or DEFAULT_LIMITS[kind]


@dataclass(frozen=True, slots=True)
class ExperienceSummary:
    """One position as listed on the experience page.

    ``id`` is LinkedIn's own position id, taken from the position's edit link,
    so it survives reordering and identical titles. ``editable`` is false when
    LinkedIn shows no edit link for it, in which case this server will not
    touch it.
    """

    id: str
    title: str
    company: str | None = None
    employment_type: str | None = None
    date_range: str | None = None
    location: str | None = None
    description_preview: str | None = None
    editable: bool = True

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ExperienceForm:
    """The editable values of one position, read from its edit form."""

    id: str
    title: TextField
    description: TextField
    company: str | None = None
    start_month: int | None = None
    start_year: int | None = None

    @property
    def start(self) -> str:
        """The start date as "MM/YYYY", the form a change set compares; "" if unread."""
        if self.start_month is None or self.start_year is None:
            return ""
        return format_start(self.start_month, self.start_year)


@dataclass(frozen=True, slots=True)
class NewPosition:
    """A current position to add, exactly as the user approved it.

    Only current roles are supported: the form's "I currently work here" box
    stays checked and no end date is set. ``company`` is the exact name the
    position is filed under, so it groups with other roles at that company.
    """

    title: str
    company: str
    start_month: int
    start_year: int
    employment_type: str | None = None
    location_type: str | None = None
    description: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> NewPosition:
        return cls(
            title=d["title"],
            company=d["company"],
            start_month=int(d["start_month"]),
            start_year=int(d["start_year"]),
            employment_type=d.get("employment_type"),
            location_type=d.get("location_type"),
            description=d.get("description") or "",
        )


@dataclass(frozen=True, slots=True)
class NewCompanyPage:
    """A LinkedIn Page to create for an organization, exactly as approved.

    ``public_url`` is the part after linkedin.com/company/. ``logo_path`` is a
    local image file. ``representative_declared`` is the user's own statement
    that they represent the organization, which LinkedIn's form requires; the
    server ticks that box only when it is true.
    """

    name: str
    public_url: str
    industry: str
    size: str
    organization_type: str
    website: str = ""
    tagline: str = ""
    logo_path: str = ""
    representative_declared: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> NewCompanyPage:
        return cls(
            name=d["name"],
            public_url=d["public_url"],
            industry=d["industry"],
            size=d["size"],
            organization_type=d["organization_type"],
            website=d.get("website") or "",
            tagline=d.get("tagline") or "",
            logo_path=d.get("logo_path") or "",
            representative_declared=bool(d.get("representative_declared")),
        )


@dataclass(frozen=True, slots=True)
class Skill:
    name: str
    position: int
    ref: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "position": self.position}


@dataclass(frozen=True, slots=True)
class OwnProfile:
    """The structured profile returned by get_my_editable_profile."""

    url: str
    name: str | None
    headline: TextField
    about: TextField
    location: str | None
    experiences: list[ExperienceSummary] = field(default_factory=list)
    skills: list[Skill] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "name": self.name,
            "headline": self.headline.value,
            "location": self.location,
            "about": self.about.value,
            "experiences": [e.as_dict() for e in self.experiences],
            "skills": [s.as_dict() for s in self.skills],
            "limits": {
                "headline": self.headline.limit("headline"),
                "about": self.about.limit("about"),
            },
        }
