# OsuTourneyBot

Discord bot for osu! map-pool management and no-rating ladder match testing
through Bancho IRC. The project is under active development and currently
serves as a test build for the ladder infrastructure before the rating system
is introduced.

See [CHANGELOG.md](CHANGELOG.md) for the English release notes.

## Current features

- MongoDB as the primary storage for pools, maps, moderation history, and
  matches.
- osu! API OAuth token acquisition and background refresh.
- Pools for osu!standard (STD), Taiko, Catch the Beat (CTB), and Mania 4K/7K.
- Parsed beatmap data cached in MongoDB.
- Slash commands for creating, viewing, editing, deleting, and submitting
  pools for moderation.
- Pool statuses: `Draft`, `Pending`, `Ranked`, and `Unranked`.
- Persistent `Rank`/`Unrank` moderation buttons restored after a restart.
- Bancho IRC integration: MP-room creation, player invitations, join tracking,
  roll, pick/ban, and action timers.
- Automatic Bancho IRC recovery after a short network or VPN interruption,
  including rejoining active MP rooms and restoring match state.
- `/osu-connect`: one-time Discord-to-osu! account linking through a code sent
  in an osu! PM.
- Beatmap and mod setup through `!mp map` and `!mp mods`.
- `!mp settings` verification after every `All players are ready` message and
  before starting each map, including map, players, readiness, and NoFail checks
  for FreeMod rooms.
- A 90-second ready window that safely force-starts only after the same lobby
  validation succeeds.
- Series scores and results for completed maps.

The current match system does not calculate ratings. Glicko-2 and ladder
ratings will be added in a separate development stage.

## Project structure

```text
bot.py                 Discord bot entry point
database.py            MongoDB persistence
osu_api.py             osu! API integration and beatmap-data cache
bancho_irc.py          Bancho IRC client
utils.py               Pool parsing and formatting helpers
launcher.ps1           Windows GUI launcher
start_launcher.vbs     Launch the GUI launcher without a console window
autostart_launcher.vbs Launch the launcher and start the bot automatically
install_autostart.ps1  Enable startup when the current user signs in to Windows
remove_autostart.ps1   Disable Windows startup
cogs/                  Discord command modules
rulesets/              Mode-specific category rules
docs/                  Project documentation
tests/                 Automated rules and parser tests
```

## Requirements

- Windows PowerShell
- Python 3.12+
- A Discord application and bot token
- An osu! OAuth client ID and client secret
- Local or server-hosted MongoDB
- An osu! account with IRC access for the Bancho bot

## Installation

```powershell
py -m venv venv
.\venv\Scripts\Activate.ps1
py -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Fill in the private `.env` file locally. It is excluded from the repository
and contains Discord, osu!, MongoDB, and Bancho IRC credentials. Never publish
it or send it in chat.

For a local MongoDB instance, use a connection string such as:

```env
MONGODB_URI=mongodb://127.0.0.1:27017/osu_tourney_dev
```

Enable `Message Content Intent` and `Server Members Intent` in the Discord
Developer Portal.

## Running the bot

### Windows GUI launcher

Double-click `start_launcher.vbs`. It opens a window with `Start`, `Stop`, and
`Restart` buttons and a live process-log view.

The launcher uses `venv\\Scripts\\python.exe` and starts `bot.py` without a
separate console window.

### Windows startup

To start the bot automatically when the current user signs in to Windows, run
once:

```powershell
.\install_autostart.ps1
```

This creates an `OsuTourneyBot.lnk` shortcut in the current user's Startup
folder. To disable it:

```powershell
.\remove_autostart.ps1
```

The startup entry uses the launcher and does not put tokens or passwords in the
shortcut.

### PowerShell

```powershell
.\venv\Scripts\Activate.ps1
py bot.py
```

The following helper commands are also available:

```powershell
.\Make.ps1 install
.\Make.ps1 run
```

## Pool commands

```text
/pool_create          Create a Draft pool for STD, Taiko, or CTB
/pool_create_mania    Create a Draft pool for Mania 4K or 7K
/pool_view             View a pool; Draft pools show Submit to their author
/pool_edit             Edit a Draft or Unranked pool
/pool_delete           Delete your own Draft pool
/pool_list             List pools with optional filters
/pool_formats          Show category requirements for a selected mode
/pool_help             Show help for pool commands
/pool_repost_pending   Repost Pending pools to the moderator channel
/osu-connect           Link an osu! account through a one-time osu! PM code
```

Pool names are the user-facing identifier. Internal pool IDs are hidden from
normal output.

## Test match

`/match_create` accepts two members of the current Discord server who have
linked osu! accounts, a Ranked pool, and a BO5/BO7/BO9 format. The bot creates
an MP room in Bancho, invites players using their confirmed `osu_user_id`
values in the `#<userid>` format, waits for both players to join, performs the
roll, and accepts pick/ban actions only as slot messages such as `NM1`.

Bancho IRC credentials in `.env`:

```env
BANCHO_USERNAME=your_osu_username
BANCHO_IRC_PASSWORD=your_irc_password
```

Gameplay messages are sent only to the corresponding Bancho MP chat. Discord
receives match status updates and results.

### FreeMod rules

NoFail is required for every player in every FreeMod room. Additional personal
mods are validated by ruleset:

- **STD:** Easy, Hidden, Hard Rock, and Flashlight. Easy scores receive the
  ladder adjustment of `×1.75`; the game already applies its own score effects
  for Hidden and Hard Rock.
- **Taiko:** Hidden and Hard Rock.
- **CTB FM/TB:** Hidden and Hard Rock. CTB HR is a FreeMod room where each
  player must take Hard Rock and may add Hidden; CTB DT uses global Double Time
  with FreeMod, where players may add Hidden.
- **Mania 4K/7K:** Mirror, Fade In, Hidden, and Flashlight.

## Linking an osu! account

`/osu-connect` creates a private, one-time challenge code. The user sends the
code in a direct message to the osu! account configured in `BANCHO_USERNAME`.
The bot then obtains the public `osu_user_id` through the osu! API and stores
the Discord-to-osu mapping.

The challenge is stored in MongoDB only as a hash, expires after 10 minutes,
and is never written to logs. Before a match is registered, the bot refreshes
the username using the stored `osu_user_id`, so changing an osu! nickname does
not require linking the account again. Bancho IRC remains connected for this
command even when no MP room is active.

## Checks

Tests run without a Discord or MongoDB connection:

```powershell
.\venv\Scripts\python.exe -m unittest discover -s tests -v
```

Syntax check:

```powershell
.\venv\Scripts\python.exe -m py_compile bot.py database.py bancho_irc.py osu_api.py utils.py cogs\*.py rulesets\*.py
```

## Publishing

Before pushing to GitHub, check the working tree:

```powershell
git status
```

Do not commit `.env`, `venv/`, logs, or local databases. Keep secrets only in
the local `.env` file or in CI/CD secrets.
