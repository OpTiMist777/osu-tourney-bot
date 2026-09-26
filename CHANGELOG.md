# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions use Semantic Versioning where practical.

## [0.1.6.1] - 2026-09-26

Hotfix for live match output and in-game forced starts.

### Fixed

- Reworked the live match feed to preserve the configured action order,
  display bans in a compact single-line block, and show map results with clear
  win/loss indicators.
- Simplified active-match status output to a concise `Live` indicator and kept
  the series score and winner in their dedicated sections.
- Fixed forced starts after failed `!mp settings` validation: the match no
  longer returns to the ready timer and starts the map after the force-start
  check, including when the settings request itself fails.

## [0.1.6] - 2026-09-26

English UI and configurable FreeMod score-multiplier release.

### Changed

- Fully translated player-facing Discord, osu! PM, Bancho IRC, API, startup,
  validation, and diagnostic messages to English.
- Translated legacy base commands and pool-management help text.
- Added category-aware manual FreeMod score multipliers to the rulesets.
- STD FreeMod/TB now exposes NoMod, Easy, Hidden, Hard Rock, and Flashlight
  multipliers (`Easy ×1.75`; other mods currently `×1.0`).
- Taiko and CTB FreeMod categories now explicitly expose NoMod, Hidden, and
  Hard Rock multipliers (`×1.0`).
- Fixed the Pool Commands cog lookup after its English rename.

### Testing

- `venv\\Scripts\\python.exe -m unittest discover -s tests -q` — 21 tests passed.
- Python bytecode compilation, no-Cyrillic source scan, and `git diff --check`
  completed successfully.

## [0.1.5.2] - 2026-09-20

Player-facing UX and match-creation reliability hotfix.

### Added

- Added a dedicated match-created embed with participants, pool, format,
  multiplayer-room link, and join status.
- Added status-based colors to the live match embed for waiting, active,
  completed, and cancelled matches.

### Changed

- Renamed the live pick/ban section to `Action history` and clarified
  automatic actions in the Discord output.
- Localized remaining mixed-language labels in the live match status.
- Restricted Draft and Pending pool lists to ephemeral responses.

### Fixed

- Added an in-process creation lock and a second active-match check to prevent
  the same osu! player from being placed into concurrent matches.
- Closed an orphaned Bancho lobby when MongoDB match persistence fails.
- Corrected Bancho room-creation logging so unrelated MP links are not marked
  as confirmed room creation.

### Testing

- `venv\\Scripts\\python.exe -m unittest discover -s tests -q` — 21 tests passed.
- Python bytecode compilation and `git diff --check` completed successfully.

## [0.1.5.1] - 2026-09-20

Small multiplayer usability hotfix for the alpha test build.

### Changed

- Moved the full live match embed from the match output channel to the
  dedicated score-watch channel.
- Displayed pick/ban history one action per line in the live match embed.
- Added an explicit available-slot list after every pick/ban prompt.
- Restarted the 90-second Bancho timer after a completed map, a ban, the first
  lobby roll, and IRC recovery.
- Kept the original match output channel limited to a short match-creation
  confirmation instead of the continuously edited embed.

### Testing

- `venv\\Scripts\\python.exe -m unittest discover -s tests -v` — 21 tests passed.
- Python bytecode compilation and `git diff --check` completed successfully.

## [0.1.5] - 2026-09-20

Match-flow reliability and mode-specific FreeMod rules for the alpha test build.

### Added

- Added a ready-window timeout: after 90 seconds, the bot validates the lobby
  and force-starts only when the selected map, mode, participants, and mods
  are valid.
- Added ruleset-level declarations for allowed and required personal FreeMod
  mods, including hybrid CTB categories.
- Added manual STD Easy result adjustment (`×1.75`) while preserving the raw
  score and applied multiplier in match history.
- Added regression tests for delayed `!mp settings` replies, ready-timeout
  flow, FreeMod validation, CTB hybrid slots, and STD Easy scoring.

### Changed

- `!mp settings` now waits for BanchoBot's first response line instead of
  relying on a fixed delay, then collects the remaining snapshot.
- FreeMod rules are enforced per mode:
  - STD: NoFail required; Easy, Hidden, Hard Rock, and Flashlight allowed.
  - Taiko: NoFail required; Hidden and Hard Rock allowed.
  - CTB FM/TB: NoFail required; Hidden and Hard Rock allowed.
  - CTB HR: FreeMod room; each player must take NoFail and Hard Rock, with
    optional Hidden.
  - CTB DT: Double Time is global; each player must take NoFail, with optional
    Hidden.
  - Mania: NoFail required; Mirror, Fade In, Hidden, and Flashlight allowed.
- A valid-looking unavailable slot now receives a concise in-lobby response
  listing the currently available slots.

### Fixed

- Fixed valid lobby settings occasionally being read as an empty reply when
  BanchoBot responded later than the previous fixed wait period.
- Fixed games stalling after the ready timer elapsed by validating and starting
  a correct lobby instead of leaving it idle.
- Fixed FreeMod validation so hybrid CTB HR/DT rules are checked from players'
  actual selected mods rather than assuming every required mod is global.

### Testing

- `venv\\Scripts\\python.exe -m unittest discover -s tests -v` — 21 tests passed.
- Python bytecode compilation and `git diff --check` completed successfully.

## [0.1.4] - 2026-09-17

Mania FreeMod rules and lobby-mod validation update.

### Changed

- Moved multiplayer mod configuration to the per-mode rulesets. FreeMod slots
  and tiebreakers in every supported mode are now declared by their ruleset
  instead of being hard-coded in the match flow.
- Configured every Mania 4K category (`RC`, `HB`, `LN`, `SV`, `TB`) and every
  Mania 7K category (`RC`, `HB`, `LN`, `EX`, `TB`) as FreeMod.
- Mania maps now always use `!mp mods nf freemod`.

### Fixed

- Fixed H2H FreeMod `!mp settings` parsing to read personal player mods from
  the optional `[Mods]` suffix.
- Mania lobby validation now requires NoFail for every player and permits only
  the optional personal mods Mirror, Fade In, Hidden, and Flashlight.

### Testing

- `venv\\Scripts\\python.exe -m unittest discover -s tests -v` — 12 tests passed.
- Python bytecode compilation completed successfully for updated modules.

## [0.1.3] - 2026-09-17

Match observability and cross-ruleset pool parsing fixes for the no-rating
multiplayer test build.

### Added

- Added a live match-status embed in the dedicated staff match-log channel.
  It mirrors the public match output and is updated as players join, make
  choices, play maps, and finish the series.
- Added concise match-event entries to the staff log for lobby creation,
  player joins, actions, lobby validation, map outcomes, timeouts, and room
  closure.
- Added automatic posting of the selected pool in the Discord match channel
  once both participants have joined the Bancho lobby.
- Added regression coverage for Head-to-Head `!mp settings` player lines and
  STD-origin CTB convert parsing.

### Changed

- osu! difficulty-attribute requests now accept an explicit ruleset, allowing
  the stored star rating of a permitted convert to be calculated for its
  target mode.
- Replaced the low-contrast Unrank button cross with a readable downtrend
  icon.

### Fixed

- Fixed Head-to-Head lobby validation: BanchoBot omits the `[Team ... / Mods]`
  suffix in this mode, so ready players were previously parsed as absent and
  the bot repeatedly reset an already valid lobby instead of starting it.
- Fixed pool creation and editing for permitted STD-origin Taiko, CTB, and
  Mania converts. They are stored as converts and use target-ruleset star
  ratings; non-STD cross-mode maps remain rejected.
- Fixed staff match tracking so it receives the full live match embed instead
  of only textual event entries.

### Testing

- `venv\\Scripts\\python.exe -m unittest discover -s tests -v` — 8 tests passed.
- Python bytecode compilation completed successfully for the updated modules.

## [0.1.2] - 2026-09-17

Reliability and code-cleanup release for the no-rating multiplayer test build.

### Added

- Added restoration of active MP rooms after a full bot-process restart:
  - persisted live matches are rejoined through IRC;
  - the current pick/ban, ready-check, or running-game state is restored;
  - the five-minute player join deadline is stored in MongoDB and survives a
    restart.
- Added per-match action locks, preventing a player action and its timeout
  fallback from being applied at the same time.
- Added conditional pool-status updates, preventing two moderators from
  overwriting each other's decision for the same pending pool.
- Added validation that the two users currently in an MP room match the two
  persisted osu! account IDs for the match.

### Changed

- Tightened `!mp settings` validation: fixed-mod rooms must have exactly the
  expected mods, while FreeMod rooms must use FreeMod globally and NoFail for
  each player.
- A Discord channel may still host any number of concurrent matches; the
  one-active-match restriction applies only to participating osu! accounts.
- Disabled loading of the legacy `base_commands` cog while retaining its source
  for possible future work.
- Removed unused database helpers, pool-formatting helpers, an obsolete osu!
  API validation method, and the unused `aiosu` dependency.

### Fixed

- Fixed a race where a delayed BanchoBot room-creation response could be
  mistaken for the wrong `!mp make` request.
- Fixed recovered matches with missing players becoming stuck without a running
  invite timer.
- Fixed the five-minute join window so it does not restart after reconnecting
  or restarting the bot.
- Removed a duplicate SQLite-backup rule from `.gitignore`.

### Testing

- `py -m unittest discover -s tests -v` — 5 tests passed.
- Python bytecode compilation completed successfully for the project modules.

## [0.1.1] - 2026-09-17

Maintenance release for the first no-rating match test build, focused on
concurrent match safety and match-flow corrections.

### Added

- Added a live-match lookup by stable osu! user ID. A player cannot be added to
  a second active match, while a Discord channel can host any number of matches.
- Added a MongoDB index for active-match lookups by participating osu! IDs.
- Serialized BanchoBot `!mp make` requests and matched each room-creation reply
  to its requested lobby name, so concurrent match creation cannot assign a
  room to the wrong match.

### Changed

- A tied map is now replayed with the same beatmap and mods until it produces a
  winner. The draw does not change the series score or consume another pick.
- Corrected the player-invite window to last the full five minutes.
- Updated `/pool_list` to filter Mania 4K and Mania 7K independently.
- Removed the remaining loaded legacy prefix-command cog; the bot now exposes
  the maintained slash-command workflow.
- Removed internal pool IDs from remaining user-facing pool creation,
  moderation, and list output.

### Fixed

- Fixed the active Discord-channel query to use the persisted
  `discord_channel_id` field.
- Fixed stale `approved`/`rejected` status labels in the legacy pool listing
  implementation.

## [0.1.0] - 2026-09-16

This release turns the initial pool-management prototype into a working
no-rating ladder match test build.

### Added

- Added Bancho IRC lifecycle management:
  - connection and IRC authentication;
  - automatic `PART #osu` to avoid unrelated global-channel traffic;
  - MP-room join tracking;
  - reconnect with exponential backoff;
  - room-state recovery after a transient connection loss;
  - explicit connection and failure logging.
- Added the `/osu-connect` Discord command for one-time Discord-to-osu!
  account linking through an osu! PM challenge.
- Added hashed, expiring, one-time login challenges and unique MongoDB indexes
  for Discord and osu! account mappings.
- Added `/match_create` for no-rating test matches with:
  - Ranked-pool validation;
  - BO5, BO7, and BO9 formats;
  - automatic roll after both players join;
  - in-game pick/ban processing;
  - action timers and pseudo-random fallback picks;
  - automatic tiebreaker selection;
  - map and mod setup through Bancho commands;
  - `All players are ready` handling;
  - `!mp settings` verification before every game start;
  - map scores, series scores, and lobby close handling.
- Added stable osu! ID-based MP invitations using `!mp invite #<userid>`.
- Added pre-match osu! profile refresh by stable user ID, so renamed accounts
  use their current osu! username before the lobby is created.
- Added a defensive in-match rename resolver for active lobbies and score
  messages.
- Added Windows launcher and autostart helpers:
  - `start_launcher.vbs`;
  - `autostart_launcher.vbs`;
  - `install_autostart.ps1`;
  - `remove_autostart.ps1`.
- Added automated tests for Bancho settings parsing and mode/ruleset logic.

### Changed

- Switched the active match and account-linking flow to MongoDB-backed state.
- Migrated legacy pool status names from `approved`/`rejected` to
  `ranked`/`unranked` during database initialization.
- Restricted `/match_create` to Discord server members with linked osu!
  accounts; arbitrary osu! usernames are no longer accepted.
- Updated osu! API integration to refresh OAuth tokens at startup and in the
  background, and to calculate modded star ratings for supported pool cards.
- Added mandatory lobby validation for the selected beatmap, mode, players,
  readiness, required mods, and NoFail in FreeMod rooms.
- Expanded operational logging across IRC, match state transitions, timers,
  player actions, settings checks, scores, reconnects, and errors.
- Updated README documentation for the current commands, setup, launcher,
  authentication, and test workflow.

### Fixed

- Fixed stale osu! usernames after an account rename breaking invitations,
  lobby joins, pick/ban actions, or score matching.
- Fixed IRC EOF and unexpected reader errors becoming silent failures; they now
  enter the normal reconnect path and are logged.
- Fixed incomplete lobby setup by verifying `!mp settings` before starting a
  map and reapplying the expected map/mod configuration on mismatch.
- Fixed disconnect score handling for a completed map: a missing participant
  receives the configured effective score of `1`, while incomplete/aborted
  maps still require review.
- Fixed automatic lobby cleanup after a completed or cancelled match.

### Security

- Kept `.env`, virtual environments, local databases, logs, and credentials out
  of Git through `.gitignore`.
- Login challenge values are never written to logs; only their hashes are
  stored in MongoDB.
- IRC authentication values and Discord tokens are not included in operational
  logs.

### Testing

- `python -m unittest discover -s tests -v` — 5 tests passed.
- Python bytecode compilation completed successfully for the project modules.

### Not included yet

- Glicko-2 rating calculations.
- Ladder rank/division updates.
- Rated matchmaking and rating-based pool selection.
- Production deployment configuration for the future server PC.

## [0.0.1]

- Initial working Discord pool-management prototype.
