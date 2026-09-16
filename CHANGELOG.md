# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions use Semantic Versioning where practical.

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
