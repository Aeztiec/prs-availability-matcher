"""The availability selector's behaviour, with no Discord in it.

The Discord layer in views.py is a shell: it draws buttons and forwards clicks
here. Everything that decides what a click means, what gets stored, and what
the message should say lives in this module so it can be tested without a
gateway connection, a token, or a live server.

Managers use it per fixture; referees use the same thing per week. The only
difference is what the saved state is keyed against, so both share this code.
"""

from __future__ import annotations

from .scheduling import Pref

# Discord allows 5 action rows of 5 components each. One row goes to the day
# tabs and one to the submit/clear pair, which leaves 3 rows - 15 buttons - for
# slots. Hourly slots over a 4pm-10pm window come to 7 a day, well inside that.
MAX_SLOT_BUTTONS = 15


class SelectorState:
    """One person's in-progress picks for one set of slots."""

    def __init__(self, slots, saved=None, submitted=False):
        self.slots = list(slots)
        self.submitted = submitted
        saved = saved or {}
        # Unset slots default to NO. A half-finished submission must never read
        # as "available" - see the same rule in fallback.availability().
        self.picks = {
            slot.key: Pref(int(saved.get(slot.key, Pref.NO))) for slot in self.slots
        }

    # ------------------------------------------------------------ mutation
    def cycle(self, slot_key):
        """Advance one slot: NO -> IDEAL -> FINE -> NO. Returns the new value."""
        if slot_key not in self.picks:
            raise KeyError(slot_key)
        self.picks[slot_key] = self.picks[slot_key].cycled()
        return self.picks[slot_key]

    def set_all(self, value):
        value = Pref(int(value))
        for key in self.picks:
            self.picks[key] = value

    def clear(self):
        self.set_all(Pref.NO)

    # ------------------------------------------------------------- reading
    def as_dict(self):
        """What goes in the database: plain ints, JSON-safe."""
        return {key: int(value) for key, value in self.picks.items()}

    def get(self, slot_key):
        return self.picks[slot_key]

    @property
    def days(self):
        seen = []
        for slot in self.slots:
            if slot.day not in seen:
                seen.append(slot.day)
        return seen

    def slots_for(self, day):
        return [s for s in self.slots if s.day == day]

    @property
    def chosen(self):
        """Slots the person is actually offering, best first."""
        offered = [s for s in self.slots if self.picks[s.key] != Pref.NO]
        return sorted(offered, key=lambda s: (-int(self.picks[s.key]), s.day_index, s.minutes))

    @property
    def any_chosen(self):
        return bool(self.chosen)

    # ------------------------------------------------------------ validity
    def blocking_problem(self):
        """Why this can't be submitted yet, or None if it can.

        Refusing an all-NO submission is the important one. "I submitted and
        said no to everything" and "I never replied" look identical to the
        scheduler otherwise, and they should not be treated the same - the
        first is a real answer that needs a human, the second is a no-show that
        the fallback can handle.
        """
        if not self.any_chosen:
            return ("You haven't marked any times as FINE or IDEAL yet. "
                    "Pick at least one, or leave this and we'll use your team's "
                    "saved timings instead.")
        return None

    # ------------------------------------------------------------ rendering
    def day_summary(self, day):
        """'17:00 🟡  18:00 🟢  19:00 🟢' for one day, or '-' if nothing set."""
        parts = [
            "{} {}".format(s.label, self.picks[s.key].emoji)
            for s in self.slots_for(day)
            if self.picks[s.key] != Pref.NO
        ]
        return "  ".join(parts) if parts else "-"

    def summary_lines(self):
        return ["**{}** · {}".format(day, self.day_summary(day)) for day in self.days]

    def button_label(self, slot):
        return "{} {}".format(self.picks[slot.key].emoji, slot.label)


def describe_choice(state):
    """One-line summary for the confirmation and for the staff log."""
    if not state.any_chosen:
        return "nothing offered"
    return ", ".join(
        "{} {} {}".format(s.day, s.label, state.get(s.key).word) for s in state.chosen
    )


def rows_needed(slot_count):
    """How many action rows a day's slots will take (5 buttons per row)."""
    return (slot_count + 4) // 5


def fits_on_one_message(state):
    """True if every day's slots fit inside Discord's component budget.

    Checked at startup rather than discovered when a manager opens a broken
    selector: exceeding the limit makes Discord reject the message outright.
    """
    return all(len(state.slots_for(day)) <= MAX_SLOT_BUTTONS for day in state.days)
