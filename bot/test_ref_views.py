"""Offline checks on the referee accept/decline buttons.

Same reasoning as test_views.py: the callbacks need a gateway, but a custom_id
that doesn't match its own routing template produces a button that looks fine
and does nothing. Here that would mean a referee tapping Accept and the fixture
never being confirmed, so it is worth pinning down without Discord.

Run with:  python -m bot.test_ref_views
"""

from __future__ import annotations

import sys

from bot.ref_views import REF_DYNAMIC_ITEMS, AcceptButton, DeclineButton, offer_view
from bot.views import DYNAMIC_ITEMS

FAILURES = []


def check(name, got, want):
    if got == want:
        print("  ok   {}".format(name))
    else:
        print("  FAIL {}\n         got:  {!r}\n         want: {!r}".format(name, got, want))
        FAILURES.append(name)


print("\nreferee offer buttons route back to themselves")
accept, decline = AcceptButton(1024), DeclineButton(1024)
check("accept id", accept.custom_id, "ra:1024")
check("decline id", decline.custom_id, "rd:1024")
check("accept routes",
      bool(AcceptButton.__discord_ui_compiled_template__.fullmatch(accept.custom_id)), True)
check("decline routes",
      bool(DeclineButton.__discord_ui_compiled_template__.fullmatch(decline.custom_id)), True)
check("accept template rejects a decline id",
      bool(AcceptButton.__discord_ui_compiled_template__.fullmatch("rd:1024")), False)
check("fixture id survives the round trip",
      AcceptButton.__discord_ui_compiled_template__.fullmatch("ra:1024")["fixture"], "1024")
check("a non-numeric fixture is rejected",
      bool(AcceptButton.__discord_ui_compiled_template__.fullmatch("ra:abc")), False)

print("\nnothing collides with the availability selector's buttons")
every = DYNAMIC_ITEMS + REF_DYNAMIC_ITEMS
for cls in REF_DYNAMIC_ITEMS:
    custom_id = cls(7).custom_id
    hits = [c.__name__ for c in every
            if c.__discord_ui_compiled_template__.fullmatch(custom_id)]
    check("{} -> {}".format(custom_id, hits), len(hits), 1)

print("\nthe offer view")
view = offer_view(1024)
check("two buttons", len(view.children), 2)
check("labelled for a human", [c.item.label for c in view.children], ["Accept", "Decline"])
check("accept is the affirmative style",
      str(view.children[0].item.style).endswith("success"), True)
check("every button carries the fixture",
      {c.custom_id for c in view.children}, {"ra:1024", "rd:1024"})

print("")
if FAILURES:
    print("{} FAILED: {}".format(len(FAILURES), ", ".join(FAILURES)))
    sys.exit(1)
print("all referee view checks passed")
