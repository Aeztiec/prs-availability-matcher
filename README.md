# PRS Officials Bot

The Discord bot Professional Roblox Soccer's Officials run the league with. It
schedules fixtures from what managers say they can play, runs the referee
board, and posts results.

- **Scheduling.** Each gameweek is opened with `/gw open`. Managers set their
  timings with `/availability`; when both managers of a fixture have answered,
  the best mutual kickoff time is picked. A manager who misses the deadline
  falls back to the timings their team already saved in the sheet.
- **Boards.** A public fixture board and a referee board are posted once and
  edit themselves as games get times and claims come in.
- **Officiating.** Referees claim games from a menu (first come, first served:
  one referee and up to two assistants), and can drop out again.
- **Results.** `/result` opens a form and posts a finished game in the league's
  results format. Every result is saved: it drives suspensions and the referee
  leaderboard.
- **Suspensions.** A red card, a second yellow in one game, or yellows in two
  consecutive games mean the player misses their club's next game in the same
  competition (domestic or UEFA, kept separate). The referees of that game are
  pinged with who is suspended, both when the result is posted and when someone
  claims or is assigned the game.

## Commands

| Command | Who | What it does |
| --- | --- | --- |
| `/availability` | Managers | Set your timings for the week |
| `/ref dropout` | Referees | Drop out of a game you are officiating |
| `/gw list` `open` `close` | Staff | The calendar; open a gameweek (creates its fixtures and posts the announcement); close it |
| `/fixture list` `show` `set` `board` `suspensions` | Staff | Scheduling status, one game's log (with its suspended players), set a kickoff by hand, publish the fixture board, list current suspensions |
| `/managers set` `list` | Staff | Say who manages a team; see every team and its manager |
| `/refs register` `tier` `list` `assign` `board` `leaderboard` | Staff | Register referees (with a Roblox name and tier), assign one by hand, publish the referee board, see games officiated per referee |
| `/result` | Staff | Post a finished game's result |
| `/test managers` `referees` `schedule` | Staff | Fake activity to exercise the whole flow |

Staff commands need the role in `DISCORD_STAFF_ROLE_ID`, or Manage Server if no
role is set. Games are picked by name (`ARS vs MCI - Sat 16:30 - PL`), never by
number.

## Setting up

1. Create a bot at <https://discord.com/developers/applications>, add it to your
   server, and copy its token.
2. `cp .env.example .env` and fill it in. `.env` is git-ignored; the token must
   never be committed.
3. Put the player sheet at `data/players.csv` (columns `C, USERID, USERNAME,
   CLUB, ROLE, WAGE`). It holds user IDs and wages, so it is git-ignored too.
   `/result` reads it to pre-fill lineups and check usernames.
4. Install and run:

```
pip install -r requirements.txt
python -m bot            # run the bot
python -m bot --check    # load everything and exit without connecting
```

New slash commands can take a while to reach every client. Restart the bot from
a single terminal (two processes answer the same command twice), and reload
Discord if a stale definition sticks. `python -m bot --fast-sync` registers
commands in the server straight away for testing; it shows every command twice
until the next normal restart.

## Layout

```
bot/
  main.py, client.py     entry point; the Discord client and scheduling runner
  commands/              one module per slash command group
  domain/                scheduling, calendar, referees, the timings sheet (no Discord)
  ui/                    embeds, buttons, selectors and the result post
  tools/                 rehearse (dry run), sync_emoji, seed_managers (test accounts)
  db.py, config.py, paths.py
data/
  timings/               saved timings sheet for each competition
  players.csv            the player sheet (git-ignored)
  fixtures.db            the bot's database (git-ignored)
assets/                  images the bot links to
tests/                   python -m tests
```

Everything under `bot/domain` and the builders in `bot/ui` work on plain data,
so they are tested without a Discord connection.

### Suspensions and the leaderboard

Both are worked out from the saved results, never stored separately, so
submitting `/result` again for a game corrects everything that came from it.
The rules are in `bot/domain/discipline.py`. The leaderboard counts each game a
person is listed in on a result's officiating team, as referee or assistant;
a name that matches a registered referee (by Roblox or Discord name) adds up
under that referee.

### The timings sheet

Each competition's timings are saved as one page in `data/timings/`, named
`<SEASON>_<COMPETITION>_timings.html` (for example `S17_Clubs_timings.html`).
Every team has a row of half-hourly kickoff times coloured green (free) or red
(busy). The fallback path reads it, and the slot list the selector offers comes
from it, so the two can never disagree. `PRS_COMPETITION` in `.env` picks a
sheet by name; by default the newest season is used, preferring Clubs.

### Emoji

Team and league badges are the server's custom emoji. `python -m
bot.tools.sync_emoji` matches the server's emoji to the teams and leagues and
writes the ids into `bot/domain/season.py`; without `--write` it only prints
what it would change.

## Tests

```
python -m tests            # every test file
python -m tests.test_board # one of them
python -m bot.tools.rehearse   # walks a fixture through the whole flow, printing what people would see
```
