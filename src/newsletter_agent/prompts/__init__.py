"""The prompt templates, versioned by file name."""

import re
from importlib import resources

_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


def load(name: str) -> str:
    return (resources.files(__name__) / f"{name}.md").read_text(encoding="utf-8")


def render(template: str, /, **values: str) -> str:
    """Load a template and fill every {{placeholder}} in one pass, so text that was filled in is
    never searched for placeholders itself. Raises KeyError for a placeholder with no value.
    """
    return _PLACEHOLDER.sub(lambda m: values[m[1]], load(template))
