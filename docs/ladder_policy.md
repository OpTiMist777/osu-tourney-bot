# Ladder policy (draft)

This document records the agreed starting rules for the all-modes osu! ladder.
It is a product policy, not yet implemented matchmaking code.

## Scope

- The ladder supports `std`, `taiko`, `ctb`, and `mania` as separate modes.
- Each mode has an independent Glicko-2 rating.
- Rating is unbounded: wins raise it and losses lower it.
- Display ranks have 7 ranks with 5 divisions each; one rank spans 1000 rating points.

## Match format by rating

The match format is selected from the relevant rating range at match creation.

| Rating range | Format | Bans per player |
| --- | --- | --- |
| 0–1999 | BO5 | 1 |
| 2000–3999 | BO7 | 1 |
| 4000–5999 | BO7 | 2 |
| 6000+ | BO9 | 2 |

## Pool eligibility

- Any valid pool may be used in casual matches.
- A pool may be used in rated ladder matches only when it passes the selected
  mode's ranked-slot validation and is assigned to the applicable rating range.
- A match stores a pool snapshot so later edits never rewrite match history.

## Roll and pick/ban order

The bot rolls automatically for both participants and records both results.
On a tie it rolls again.

- **A** is the player who loses the roll.
- **B** is the player who wins the roll.
- A takes the first ban.
- B takes the first pick.
- There is no protect phase in ladder matches.

### One-ban flow

Used for the 0–1999 and 2000–3999 ranges:

```text
A ban → B ban → B pick → A pick → remaining picks alternate B/A
```

### Two-ban flow

Used for the 4000–5999 and 6000+ ranges:

```text
A ban → B ban → B pick → A pick → A ban → B ban → B pick → A pick
→ remaining picks alternate B/A
```

## Slot input rule

During a pick/ban turn, the bot accepts only a message from the player whose
turn it is. The full trimmed message must equal one currently available slot,
case-insensitively (for example, `NM1` or `hd2`). The first valid action is
final; invalid text is ignored and does not consume the turn.

## Tiebreaker

- `TB` is not part of the pick order.
- The bot selects it automatically only when the series reaches a deciding tie:
  `2–2` in BO5, `3–3` in BO7, or `4–4` in BO9.

## Lobby safety rule

Before automatic start, after BanchoBot reports all players ready, the bot
verifies the selected beatmap, mode, required mods, and participants. On a
mismatch it re-applies the expected lobby configuration and does not start the
map until verification passes.

## Disconnect policy

If a completed map has results for the other participant but no result for a
disconnected participant, record the DC and use an effective score of `1` for
that map. Never apply this replacement to an aborted or incomplete map.
