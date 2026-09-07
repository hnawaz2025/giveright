"""Phrasing for text a donor reads.

"The car seats should not be passed on" about a single car seat is the kind of
small wrongness that makes a person trust the rest of the message less, and
these strings are the product's actual surface. Categories are stored plural
because that is how an organisation lists a need; this turns them back.
"""

from __future__ import annotations

# Categories whose plural is not formed by the rules below, or which are not
# plural at all.
IRREGULAR: dict[str, str] = {
    "childrens_clothing": "children's clothing",
    "clothing": "clothing",
    "kitchenware": "kitchenware",
    "canned_food": "canned food",
    "open_food": "open food",
    "bedding": "bedding",
    "furniture": "furniture",
}


def plural(category: str) -> str:
    """The category as an organisation would list it."""
    return IRREGULAR.get(category, category.replace("_", " "))


def singular(category: str) -> str:
    if category in IRREGULAR:
        return IRREGULAR[category]
    word = category.replace("_", " ")
    if word.endswith(("sses", "shes", "ches", "xes", "zes")):
        return word[:-2]
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def label(category: str, quantity: int = 1) -> str:
    return singular(category) if quantity == 1 else plural(category)


def verb(quantity: int, singular_form: str, plural_form: str) -> str:
    """`verb(1, "is", "are")`. Uncountable categories read as singular."""
    return singular_form if quantity == 1 else plural_form
