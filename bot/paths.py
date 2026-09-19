"""Where things live on disk, in one place.

    <project>/
        .env                    secrets (git-ignored)
        assets/                 images the bot links to
        data/
            timings/            saved timings sheets, one page per competition
            players.csv         the player sheet (git-ignored: IDs and wages)
            fixtures.db         the bot's database (git-ignored)
"""

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_FILE = os.path.join(ROOT, ".env")
DATA_DIR = os.path.join(ROOT, "data")
TIMINGS_DIR = os.path.join(DATA_DIR, "timings")
PLAYERS_CSV = os.path.join(DATA_DIR, "players.csv")
DB_PATH = os.path.join(DATA_DIR, "fixtures.db")
ASSETS_DIR = os.path.join(ROOT, "assets")

# Before the reorganisation the database sat inside the package.
LEGACY_DB_PATH = os.path.join(ROOT, "bot", "fixtures.db")
