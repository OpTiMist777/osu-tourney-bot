# OsuTourneyBot

Discord bot for creating, reviewing, and displaying osu! tournament map pools.
The current project focuses on pool management. The `cogs/osu_commands.py` module
is reserved for the future Bancho multiplayer integration.

The current ladder policy draft is in [docs/ladder_policy.md](docs/ladder_policy.md).

## Current capabilities

- Prefix commands for map-pool creation, editing, viewing, submission, and moderation.
- Supported modes: osu!standard, taiko, catch, and mania.
- osu! API v2 lookup for beatmap metadata and pool-card display.
- Official modded star rating lookup for standard `HD`, `HR`, and `DT` slots.
- Convert marker and category-based pool display.
- MongoDB for pool data. SQLite is retained only as a one-time migration source.

## Requirements

- Windows PowerShell
- Python 3.12 or newer
- A Discord application and bot token
- osu! OAuth client ID and client secret
- A local MongoDB Community Server

## Setup

```powershell
py -m venv venv
.\venv\Scripts\Activate.ps1
py -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Fill the values in `.env`. Keep it private: it contains credentials and is ignored by Git.
Set `MONGODB_URI` to your local database, for example
`mongodb://127.0.0.1:27017/osu_tourney_dev`.

In the Discord Developer Portal, enable **Message Content Intent** and **Server Members Intent** for the bot.

## Run

```powershell
.\venv\Scripts\Activate.ps1
py bot.py
```

Or use the helper script:

```powershell
.\Make.ps1 install
.\Make.ps1 run
```

## Pool workflow

```text
/pool_create                 Create a STD, Taiko, or Catch draft pool
/pool_create_mania           Create a Mania draft pool
/pool_view                   View a pool; a Draft includes an author-only Submit button
/pool_edit                   Add or replace a map in your Draft/Unranked pool
/pool_list                   List pools by status and mode
/pool_delete                 Delete your Draft pool
/pool_formats                Show required categories for a mode
/pool_help                   Show the full pool-command reference
```

Pools start as **Draft**. Press **Submit** on the creation or `/pool_view`
response to send the pool for review. Moderators use persistent **Rank** and
**Unrank** buttons in the configured moderation channel; pending buttons are
restored when the bot restarts.

Pool slots are shown in uppercase, for example `NM1`, `HD1`, `RC1`, and `TB`.
Bancho multiplayer automation is planned, but is not yet implemented.

## Migrating existing SQLite pools

After configuring `MONGODB_URI`, migrate the current local `tourney.db` once:

```powershell
.\venv\Scripts\python.exe migrate_sqlite_to_mongo.py
```

The migration is safe to re-run: existing pool IDs are skipped. Keep the SQLite
file as a local backup until the migrated pools have been verified in MongoDB Compass.

Then cache osu! metadata for the migrated maps. This makes pool views read only
from MongoDB rather than issuing API requests for every card:

```powershell
.\venv\Scripts\python.exe backfill_pool_snapshots.py
```

## GitHub publishing checklist

1. Confirm `.env` is not staged: `git status` must not show it.
2. Confirm `tourney.db`, `venv`, and `venv.broken` are not staged.
3. Create a new empty GitHub repository without auto-generated files.
4. Run:

```powershell
git init
git add .gitignore .env.example README.md requirements.txt bot.py database.py osu_api.py utils.py Make.ps1 test.py cogs
git commit -m "Initial project import"
git branch -M main
git remote add origin https://github.com/<your-account>/<repository>.git
git push -u origin main
```

If any credential was ever pasted into a public place, revoke and regenerate it before publishing.
