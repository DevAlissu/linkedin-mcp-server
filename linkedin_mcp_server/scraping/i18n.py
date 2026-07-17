"""Localized UI-string aliases for LinkedIn's profile forms.

LinkedIn renders form labels, dropdown options, and action buttons in the
account's UI language, not the browser locale. The scraping and edit helpers
match against these strings, so every user-facing string they look for must be
expressed as a set of per-language aliases.

This module is the single source of truth for those aliases: add a language by
extending the lists here, never by hardcoding a translated string at a call
site. Keys are the canonical English label (what call sites pass); values list
the aliases to try, English first.

Matching is case-insensitive and substring-based downstream, so a shorter alias
(e.g. "Empresa") also matches a longer rendered label (e.g. "Nome da empresa").
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Field labels — text of a <label>, aria-label, aria-labelledby, or placeholder
# ---------------------------------------------------------------------------
FIELD_LABELS: dict[str, list[str]] = {
    # Intro
    "First name": ["First name", "Nome"],
    "Last name": ["Last name", "Sobrenome"],
    "Headline": ["Headline", "Título", "Cargo"],
    "City": ["City", "Cidade"],
    "Country/Region": ["Country/Region", "País/Região", "País ou região"],
    "Location": ["Location", "Localização", "Local"],
    "Industry": ["Industry", "Setor"],
    # Experience / generic
    "Title": ["Title", "Cargo", "Título"],
    "Company name": ["Company name", "Nome da empresa", "Empresa"],
    "Description": ["Description", "Descrição"],
    "Employment type": ["Employment type", "Tipo de vínculo", "Tipo de emprego"],
    # Education
    "School": ["School", "Instituição de ensino", "Escola"],
    "Degree": ["Degree", "Grau acadêmico", "Grau", "Diploma"],
    "Field of study": [
        "Field of study",
        "Área de estudo",
        "Curso ou área de formação",
    ],
    "Grade": ["Grade", "Nota", "Conceito"],
    "Activities and societies": [
        "Activities and societies",
        "Atividades e grupos",
        "Atividades e sociedades",
    ],
    # Certification
    "Name": ["Name", "Nome"],
    "Issuing organization": [
        "Issuing organization",
        "Organização emissora",
        "Empresa emissora",
    ],
    "Credential ID": ["Credential ID", "ID da credencial", "Número da credencial"],
    "Credential URL": ["Credential URL", "URL da credencial"],
    # Volunteer
    "Organization": ["Organization", "Organização"],
    "Role": ["Role", "Função", "Cargo"],
    "Cause": ["Cause", "Causa"],
    # Project / publication / course
    "Project URL": ["Project URL", "URL do projeto"],
    "Publisher": ["Publisher", "Editora", "Editor"],
    "Publication URL": ["Publication URL", "URL da publicação"],
    "Course name": ["Course name", "Nome do curso", "Nome"],
    "Number": ["Number", "Número"],
    "Associated with": ["Associated with", "Associado a", "Associada a"],
    # Skill / language
    "Skill": ["Skill", "Competência", "Habilidade"],
    "Language": ["Language", "Idioma"],
    "Proficiency": ["Proficiency", "Nível de proficiência", "Proficiência"],
    # Date sub-labels (month/year selects share generic PT labels "Mês"/"Ano")
    "Start date month": ["Start date month", "Mês da data de início", "Mês de início"],
    "Start date year": ["Start date year", "Ano da data de início", "Ano de início"],
    "End date month": ["End date month", "Mês da data de término", "Mês de término"],
    "End date year": ["End date year", "Ano da data de término", "Ano de término"],
    "Issue date month": ["Issue date month", "Mês da emissão"],
    "Issue date year": ["Issue date year", "Ano da emissão"],
    "Expiration date month": ["Expiration date month", "Mês de expiração"],
    "Expiration date year": ["Expiration date year", "Ano de expiração"],
    "Publication date month": ["Publication date month", "Mês da publicação"],
    "Publication date year": ["Publication date year", "Ano da publicação"],
}

# ---------------------------------------------------------------------------
# Dropdown option VALUES that LinkedIn localizes
# ---------------------------------------------------------------------------
_MONTHS: dict[str, str] = {
    "January": "Janeiro",
    "February": "Fevereiro",
    "March": "Março",
    "April": "Abril",
    "May": "Maio",
    "June": "Junho",
    "July": "Julho",
    "August": "Agosto",
    "September": "Setembro",
    "October": "Outubro",
    "November": "Novembro",
    "December": "Dezembro",
}

OPTION_VALUES: dict[str, list[str]] = {
    **{en: [en, pt] for en, pt in _MONTHS.items()},
    # Employment type
    "Full-time": ["Full-time", "Tempo integral"],
    "Part-time": ["Part-time", "Meio período"],
    "Contract": ["Contract", "Contrato"],
    "Temporary": ["Temporary", "Temporário"],
    "Volunteer": ["Volunteer", "Voluntário"],
    "Internship": ["Internship", "Estágio"],
    "Freelance": ["Freelance", "Autônomo"],
    "Self-employed": ["Self-employed", "Autônomo"],
    # Language proficiency
    "Native or bilingual": ["Native or bilingual", "Nativo ou bilíngue"],
    "Full professional": [
        "Full professional",
        "Proficiência profissional completa",
    ],
    "Professional working": [
        "Professional working",
        "Proficiência profissional de trabalho",
    ],
    "Limited working": [
        "Limited working",
        "Proficiência básica a intermediária",
    ],
    "Elementary": ["Elementary", "Nível básico"],
}

# ---------------------------------------------------------------------------
# Action-button accessible names and pagination controls
# ---------------------------------------------------------------------------
SAVE_BUTTONS: list[str] = [
    "Salvar",
    "Save",
    "Guardar",
    "Aplicar",
    "Apply",
    "Concluir",
    "Concluído",
    "Done",
]

# "Show more"/"Show all" pagination on detail (experience, education, …) pages.
SHOW_MORE: list[str] = [
    "Show more",
    "Show all",
    "Ver mais",
    "Ver todos",
    "Ver todas",
    "Mostrar tudo",
    "Exibir tudo",
    "Carregar mais",
]


def field_aliases(label: str) -> list[str]:
    """Localized aliases for a field label (the label itself if unmapped)."""
    return FIELD_LABELS.get(label, [label])


def value_aliases(value: str) -> list[str]:
    """Localized aliases for a dropdown option value (itself if unmapped)."""
    return OPTION_VALUES.get(value, [value])
