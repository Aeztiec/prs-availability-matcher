"""Small wording helpers shared by everything that talks to people."""

from __future__ import annotations


def plural(count, singular, plural_form=None):
    """'1 game', '3 games' - the count with the right form of the word, so no
    message ever ships a literal "game(s)"."""
    word = singular if count == 1 else (plural_form or singular + "s")
    return "{} {}".format(count, word)
