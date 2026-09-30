# Availability Matcher

Pick two teams, get the times both of them can play — read from a PRS
availability spreadsheet.

## Adding a competition

Save the sheet's page into `timings/` named
`<SEASON>_<COMPETITION>_timings.html` and re-run `python build_data.py`. That
is the whole process — no code to edit.

| File | Heading it produces |
| --- | --- |
| `timings/S17_WC_timings.html` | PRS S17 WC Availability Matcher |
| `timings/S17_Clubs_timings.html` | PRS S17 Clubs Availability Matcher |
| `timings/S18_Euros_timings.html` | PRS S18 Euros Availability Matcher |

The season is everything before the first underscore, the competition is
everything between that and `_timings.html` (underscores become spaces), and
`PRS` is the `LEAGUE` constant in `availability.py`.

With more than one file present, a **Competition** dropdown appears at the top
of the page and the URL carries the choice (`?c=S17_Clubs`). With one file it
stays hidden.

### Which one it opens on

Newest season, preferring **Clubs**; if that season has no Clubs file, whatever
else it has — the international competition, whatever it happens to be called
that season (WC, Euros, ...). It is matched by *not* being Clubs rather than by
name, so a new name needs no code change.

Seasons compare numerically, so `S10` beats `S9`. A `?c=` link always wins over
the default, which keeps shared fixture links pointing where you sent them.

The choice is worked out in `default_competition()` in `availability.py` and
baked into `data.js`, so the page and the CLI can't disagree about it.

Competitions whose sheet is published publicly can also be listed in `SOURCES`
in `availability.py`, keyed by filename stem; those get re-downloaded by
`--refresh` and by the scheduled Action. Anything not listed is used exactly as
saved, which is what you want for a sheet that isn't shared publicly.

Run `python availability.py --competitions` to see what's currently found.

Available as a **web page** (`docs/`) and a **command-line tool**
(`availability.py`). Both share the same parser and produce identical
results.

Each column of the sheet is a time a match could kick off, on the half hour,
coloured **green** (can play) or **red** (can't) per team. The tool finds the
times where both teams are available and merges back-to-back ones into windows.

The colours are a detail of *this* sheet, not of the tool, so none of the
on-screen wording mentions them. The parser still keys on the exact hex values
in `GREEN` / `RED` though — see the note at the end about sheets that use a
different palette.

> A window's end time is the **latest kickoff**, not the end of play.
> `8:00 PM – 10:00 PM` means any half hour from 8 to 10 works, 10 included.

Python 3.7+, no dependencies. No JavaScript build step, no framework.

## Deploying the web page (free, nothing to run)

The page is static and the data is baked into `docs/data.js`, so there is no
server and no cost. A scheduled GitHub Action re-reads the sheet and commits
the data when it changes; Pages redeploys on that commit.

1. Create a **public** repo on GitHub and push this folder to `main`.
2. **Settings → Pages → Build and deployment**: source *Deploy from a branch*,
   branch `main`, folder **`/docs`**. Save.
3. **Settings → Actions → General → Workflow permissions**: select
   *Read and write permissions*. Save. (The refresh job commits `data.js`.)
4. Wait a minute, then open `https://<you>.github.io/<repo>/`.

That's it. `.github/workflows/refresh.yml` then runs hourly, and you can also
trigger it by hand from the **Actions** tab (*Refresh availability data* → *Run
workflow*) right after someone updates the sheet.

Two things worth knowing:

- The build only rewrites `data.js` when someone's availability actually
  changed, so the history stays meaningful instead of filling up with hourly
  no-op commits.
- GitHub pauses scheduled workflows on public repos after **60 days with no
  activity**, and emails you when it does. Any push, or one manual run, resets
  the clock.

### Why it can't just read the sheet in the browser

Two blockers, both checked rather than assumed:

- The sheet URL sends no `Access-Control-Allow-Origin` header, so a browser
  refuses to fetch it cross-origin.
- Availability is stored as **cell background colour**, not cell values, so
  Google's easy data endpoints (published CSV, `gviz` JSON) don't carry it —
  they return values only.

So the parsing has to happen outside the visitor's browser. That's what the
scheduled Action is for.

## Web page

`docs/index.html` — two dropdowns and a result list, no dependencies beyond a
Google Font. Teams that haven't submitted are labelled in the dropdown, and
picking one says so rather than showing a misleading "no overlap".

The URL carries the selection (`?a=FRANCE&b=PORTUGAL`), so a fixture's times
are a link you can paste to the two players.

### Discord message

Under the results is a ready-made message with a **Copy for Discord** button.
It uses Discord's dynamic timestamps (`<t:1788033600:F>`), which each reader's
client renders **in their own timezone** — so nobody has to convert GMT+0 in
their head, which is the whole problem this tool exists to solve.

```
# FRANCE (Ieipzig)  vs  PORTUGAL (d3seried)
Times you're both free:

**Saturday**
<t:1788033600:F>  →  <t:1788040800:t>

**Friday**
<t:1787936400:F>  →  <t:1787954400:t>

-# Times show in your own timezone.
-# The second time is the latest you can kick off, not the end of the match.
```

The sheet has weekday names but **no dates**, and a Discord timestamp needs a
real date, so there's a date picker above the box. It defaults to the next
upcoming Saturday, and Friday and Sunday are anchored to that same weekend —
Friday is the day *before* the chosen Saturday, not a Friday five days later.
Pick a different date to schedule a future weekend. If you pick a day that
isn't a Saturday it still works, and says what it's doing.

The box only appears when there's actually something to send — not when a team
hasn't submitted, or when the two never overlap.

If the clipboard is unavailable (an insecure origin, or a browser wanting a
firmer gesture), the button selects the text and tells you to press Ctrl+C
instead of failing silently.

To preview locally, run a static server from `docs/` — or just open
`docs/index.html` directly, which also works, since `data.js` is loaded with a
`<script>` tag rather than `fetch()`.

## Command line

```bash
python availability.py FRANCE PORTUGAL
```

```
FRANCE (Ieipzig)  vs  PORTUGAL (d3seried)
=========================================

  Saturday
    8:00 PM - 10:00 PM  GMT+0   [3:00 PM - 5:00 PM EST | 9:00 PM - 11:00 PM BST]   5 kickoffs

  Friday (Low Priority)
    5:00 PM - 10:00 PM  GMT+0   [12:00 PM - 5:00 PM EST | 6:00 PM - 11:00 PM BST]   11 kickoffs

  2 windows, 16 possible kickoff times in total.
```

Run it with no arguments for an interactive prompt that keeps asking for pairs.

Either name works — the country or the player's username — and matching is
case-insensitive, accepts prefixes (`port` finds PORTUGAL), and suggests a
correction on a typo.

| Flag | What it does |
| --- | --- |
| `--list` | Every team, with `!` next to the ones who haven't filled anything in |
| `--refresh` | Re-download the sheet before matching |
| `--file PATH` | Parse a saved copy of the sheet instead of the cache |

The CLI caches the sheet as `timings.html` (git-ignored) and reuses it
until `--refresh`, so normal runs are offline and instant.

## Rebuilding the data by hand

```bash
python build_data.py
```

Downloads the sheet and rewrites `docs/data.js` if anything changed.
`--cached` rebuilds from `timings.html` without downloading.

## How the sheet is parsed

`SHEET_URL` in `availability.py` points at the Google Sheets `htmlview`
export of the availability tab, which is publicly readable. The parser
flattens that HTML table into a grid (expanding `colspan`/`rowspan` merges),
then:

- **Kickoff columns** are the ones with a GMT+0 time in sheet row 5. The narrow
  spacer columns between days carry no time, which is how one day's block is
  told from the next — day names come from row 2.
- **Teams** come from column D (player) and column E (country), row 6 onward.
- **Availability** is the cell background: `#00ff00` free, `#ff0000` busy,
  anything else means the cell was left blank.

A team whose whole row is blank hasn't submitted, and is reported that way.

If the sheet's layout changes, the row/column constants at the top of
`availability.py` are the only thing that needs updating.

## Sheets that use different colours

`GREEN` and `RED` in `availability.py` are exact hex matches (`#00ff00` /
`#ff0000`). A sheet built from a different template — a different shade of
green, or a different pair of colours entirely — will parse as "nobody
submitted anything" rather than failing loudly.

The fix when that happens is to classify by hue (is this cell mostly green, or
mostly red?) instead of matching exact values. That is deliberately not built
yet: it is part of the same job as supporting multiple competitions, and is
better done against a real second sheet than guessed at.
