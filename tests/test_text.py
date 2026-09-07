import pytest

from giveright.corpus import categories, load_orgs
from giveright.text import label, plural, singular


@pytest.mark.parametrize(
    "category,one,many",
    [
        ("winter_coats", "winter coat", "winter coats"),
        ("car_seats", "car seat", "car seats"),
        ("mattresses", "mattress", "mattresses"),
        ("books", "book", "books"),
        ("kitchenware", "kitchenware", "kitchenware"),
        ("childrens_clothing", "children's clothing", "children's clothing"),
        ("canned_food", "canned food", "canned food"),
    ],
)
def test_reads_correctly_either_way(category, one, many):
    assert singular(category) == one
    assert plural(category) == many
    assert label(category, 1) == one
    assert label(category, 3) == many


def test_no_corpus_category_singularises_into_nonsense():
    """A category in the corpus with no sensible singular needs an entry in
    IRREGULAR, or a donor reads 'the kitchenwar'."""
    for category in categories(load_orgs()):
        assert singular(category)
        assert not singular(category).endswith((" ", "_"))
