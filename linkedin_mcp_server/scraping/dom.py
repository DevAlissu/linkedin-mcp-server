"""Shared DOM selectors used across scraping modules.

Selectors here are deliberately structural (roles, open state) rather than
tied to LinkedIn class names, per the project's scraping rules.
"""

# Topmost modal dialog — LinkedIn renders overlays either as a native
# <dialog open> or as a div with role="dialog".
DIALOG_SELECTOR = 'dialog[open], [role="dialog"]'
