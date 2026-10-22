# Evil Donations: Implementation Plan (v5)

Status: decisions final. No open questions. Build starts at M0.

Changes from v2: guild XP removed, reset reminder pings added, hosting moved from Oracle VM to a Windows PC.
Changes in v4: auto-approve, `/log` for staff, downtime notice on `/stop`, one `Officer` staff role, `ingame guild member` role name.
Changes in v6: guild boss time polls (section 17).
Changes in v5: no screenshots. One button press logs a fixed 50,000 donation. `/proof`, amount modal, image hashing, and review channel removed.

Bot name: **Evil Donations**
Deadline: **00:00 UTC** (daily reset)
Scope: **one Discord server**, test server first, then live server.

## 1. Goal

A Discord bot for a game guild. Each member donates 50,000 coins per day in the game, once per player per UTC day. Members press a button to log the donation. The bot tracks donations and history, shows a live status board with today and yesterday totals, and pings members who have not donated before the daily reset.

## 2. Confirmed requirements

| Area | Requirement |
|---|---|
| Donation amount | Always 50,000 coins. No amount input. |
| Frequency | One approved donation per player per UTC day. |
| Day reset | 00:00 UTC. Members see reset time in their own local time (Discord `<t:UNIX:t>`). |
| Identity | Member links Discord user to in-game name with `/link`. One name per member, names unique per server (ignoring case). Staff load the guild's in-game names into a roster with `/roster add`. `/link ingame_username` lists the free roster names as the member types, and accepts only a name from the roster. Staff fix wrong links with `/unlink`. |
| Alts | Not supported. One account per player. |
| Who is tracked | Members with the Discord role **`ingame guild member`** only. Bots ignored. |
| Submission | Member presses "Log donation" button. Bot records an approved 50,000 donation for today. No proof, no review. |
| Trust | No check that the member really donated in the game. Accepted risk. Staff fix mistakes with `/undo`. |
| Notifications | No DMs. No notifications except reset reminder pings in the panel channel (section 9). Members check status with `/history` or the board. |
| Status board | Auto-updated embed. See section 7. Uses in-game names only. |
| History | Kept forever, including members who leave the server. |
| Data channel | A separate channel with a Data header and three lists: donated today (Discord name, in-game name, time), not donated yet (Discord name, in-game name), not linked (in-game names nobody has claimed yet). 15 rows per page with Previous and Next buttons. Section 18. |
| Export | CSV via `/export`, opens in Excel or Google Sheets. |
| Downtime notice | On a graceful stop (`/stop` or Ctrl+C in bot window), bot edits the board message to "Bot offline. Do not log donations. Contact an officer." and removes the button. On start, bot restores the board with the button. Crashes and power loss cannot post a notice. |
| Staff log | Officer can log a donation for a linked member with `/log`. Logged row is `approved`, `logged_by` set to staff. Same one-per-day rule applies. Day defaults to today UTC; yesterday allowed for late entries. Used after downtime: member tells an officer, officer logs it once the bot is back. |

Guild XP is not tracked. No XP column, no XP calculation, no starting XP.

## 3. Architecture

```
Discord gateway (WebSocket, outbound)
        │
   bot process (discord.py, Windows PC)
        │
   SQLite file (donations.db) in C:\EvilDonations\ (not in OneDrive)
        │
   daily backup copy → OneDrive folder (section 13)
```

- Single long-running process on the owner's Windows PC.
- Starts at user logon via Task Scheduler. Restarts itself after a crash (section 13).
- The bot is only online while the PC is on and awake. Reminders missed in the PC off-window are accepted (section 16).
- Bot needs **no inbound ports**. Only outbound HTTPS and WebSocket to Discord.
- SQLite is fine at this scale: one writer process, a few thousand rows per year.

## 4. Tech stack

| Part | Choice | Why |
|---|---|---|
| Language | Python (3.12 planned; this PC has 3.14, check discord.py works) | Simple, readable, good Discord library |
| Discord library | discord.py 2.x | Mature, slash commands, persistent buttons (`DynamicItem`) |
| Config | `.env` via python-dotenv | Token stays out of code |
| Database | SQLite (stdlib `sqlite3`) | Zero setup, one file to back up. Backup uses the stdlib `Connection.backup()`, so no `sqlite3` CLI needed on Windows. |
| Export | stdlib `csv` | No extra dependency |
| Hosting | Windows PC, Python venv | Owner's machine, no cloud account |
| Process supervision | Task Scheduler at logon + restart loop script | Replaces systemd |

No ORM, no web framework, no Docker. Dependencies: `discord.py` and `python-dotenv`.

## 5. Data model

Six tables. Every row carries `guild_id`. Cheap insurance even though only one server runs now.

### `guilds`
| Column | Type | Notes |
|---|---|---|
| guild_id | INTEGER PK | Discord guild id |
| panel_channel_id | INTEGER | Donation button, status board, and reset reminders |
| status_message_id | INTEGER | Board message: embed plus the "Log donation" button. Edited in place, or reposted at the bottom (section 7). |
| officer_role_id | INTEGER | Staff role `Officer` |
| member_role_id | INTEGER | Role `ingame guild member`, looked up by name at `/setup` |
| board_day | TEXT NULL | UTC day the board was last refreshed for rollover |
| boss_channel_id | INTEGER NULL | Channel for boss time polls. NULL = boss polls off. |
| data_channel_id | INTEGER NULL | Channel for the data lists. NULL = off. |
| data_message_ids | TEXT NULL | JSON list of the four data message ids (header, donated, not donated, not linked) |

### `roster`
The in-game names members may pick in `/link`.
| Column | Type | Notes |
|---|---|---|
| guild_id | INTEGER | |
| ign | TEXT | In-game name, 2 to 32 chars, unique ignoring case |
| | | PK (guild_id, ign) |

### `links`
| Column | Type | Notes |
|---|---|---|
| guild_id | INTEGER | |
| discord_id | INTEGER | |
| ign | TEXT | In-game name, 2 to 32 chars, unique ignoring case |
| | | PK (guild_id, discord_id). UNIQUE (guild_id, ign). |

### `submissions`
One row per logged donation. This is the history table.
| Column | Type | Notes |
|---|---|---|
| id | INTEGER PK AUTOINCREMENT | Donation id shown to staff |
| guild_id | INTEGER | |
| discord_id | INTEGER | Stays after member leaves |
| discord_name | TEXT | Snapshot at log time (CSV only) |
| ign | TEXT | Snapshot of in-game name at log time |
| day_utc | TEXT | `YYYY-MM-DD`, day the donation counts toward |
| amount | INTEGER | Coins, always 50000 |
| status | TEXT | `approved`, `voided` |
| reviewed_by | INTEGER NULL | Staff discord id who voided it |
| logged_by | INTEGER NULL | Staff discord id when logged with `/log`. NULL for member button presses. |
| created_at | TEXT | ISO 8601 UTC |
| reviewed_at | TEXT NULL | ISO 8601 UTC, set when voided |

### `boss_polls`
One row per boss time poll. Drives the cycle after a restart.
| Column | Type | Notes |
|---|---|---|
| guild_id | INTEGER | |
| cycle_start | TEXT | ISO 8601 UTC spawn time the poll starts from, e.g. 12:00 |
| message_id | INTEGER | The poll message |
| resolved_time | TEXT NULL | ISO 8601 UTC time that won (or the last time if none was pinged). NULL while open. |
| | | PK (guild_id, cycle_start) |

### `reminders`
One row per reminder sent. Stops a repeat ping after a restart.
| Column | Type | Notes |
|---|---|---|
| guild_id | INTEGER | |
| day_utc | TEXT | `YYYY-MM-DD` the reminder is for |
| minutes_before | INTEGER | 60 |
| sent_at | TEXT | ISO 8601 UTC. Set even if the reminder was skipped as late. |
| | | PK (guild_id, day_utc, minutes_before) |

### Status lifecycle
```
approved → voided   (by /undo)
```

### Constraints (enforce in DB, not just code)
- At most one `approved` row per (guild_id, discord_id, day_utc):
  `CREATE UNIQUE INDEX one_active_per_day ON submissions (guild_id, discord_id, day_utc) WHERE status = 'approved';`

The index stops duplicates even if two presses overlap. A voided row does not block a new donation.

## 6. Discord setup (one-time, by you)

1. Developer Portal → New Application → name **Evil Donations**.
2. Bot tab → Reset Token → save to `.env`.
3. Bot tab → enable **Server Members Intent** (privileged, needed for the not-donated list and reminders).
4. OAuth2 → URL Generator:
   - Scopes: `bot`, `applications.commands`
   - Bot permissions: View Channels, Send Messages, Embed Links, Read Message History, Add Reactions
5. Invite to the **test server** first. Repeat for the live server at rollout.
6. In the server, create roles `ingame guild member` and `Officer` (exact names, used for lookup).
7. Make the panel channel read-only: channel settings → Permissions → `@everyone`: Send Messages **off**, Use Application Commands **on**. Add the Evil Donations bot there with Send Messages and Embed Links **on**.
8. For boss polls (section 17): the bot needs Add Reactions and Read Message History in the boss channel, and must be able to ping the `ingame guild member` role: turn on "Allow anyone to @mention this role" for it, or give the bot Mention Everyone. Members need Add Reactions in the boss channel.

## 7. Commands and UI

### Slash commands
| Command | Who | What it does |
|---|---|---|
| `/setup panel_channel officer_role` | Manage Server | Saves config, finds role `ingame guild member`, posts the board with its button. Fails with a clear message if `ingame guild member` is missing. |
| `/link ingame_username` | Any member | Links Discord user to an in-game name. The parameter autocompletes with roster names not yet taken (25 shown, narrowed as you type). Names not on the roster are refused. |
| `/roster add names` | Staff | Adds in-game names to the roster. Separate them with commas or new lines. Skips duplicates and names outside 2 to 32 characters. |
| `/roster remove name` | Staff | Removes a name from the roster. Anyone already linked to it stays linked. |
| `/roster list` | Staff | Shows the roster and which names are taken. Sent as a file when long. |
| `/unlink [member]` | Self, or staff for others | Removes link. Staff can paste the user ID of someone who left the server, to free their in-game name. |
| `/undo submission_id` | Staff | Sets approved donation to `voided`, refreshes board |
| `/stop` | Staff | Posts downtime notice on panel, then shuts the bot down cleanly. |
| `/boss start channel` | Staff | Starts boss time polls in a channel and posts the first poll right away (section 17). |
| `/boss stop` | Staff | Stops boss time polls. Old poll messages stay. |
| `/data start channel` | Staff | Posts the data lists in a channel (section 18). |
| `/data stop` | Staff | Stops updating the data channel. Its messages stay. |
| `/log member [day]` | Staff | Records an approved 50,000 donation for a linked member. `day` = `today` (default) or `yesterday`. Refreshes board. |
| `/history [member]` | Any member for self, staff for others | Lists donations 10 per page with Previous and Next buttons (ephemeral). Members see only their own history. Staff can pass `member` to see another member's. |
| `/export` | Staff | Sends CSV of all submissions (ephemeral) |

"Staff" = Manage Server, or `Officer` role.

### Buttons
- **Board message** (persistent): the board embed with a "Log donation" button under it.
  - Only members with role `ingame guild member` can press it. Others get a message.
  - Blocked with message if member is not linked.
  - Blocked with message if member already has an approved donation today.
  - Otherwise records an approved 50,000 donation for today, replies privately ("Logged 50,000 for today."), and refreshes the board.

### Status board (single message with the button)
```
Evil Donations — guild board

Today (UTC): 12 donated
Resets <t:…:t>  (shown in your local time)

Discord name (last 10)   In-game name   Donated at
DiscordA                 PlayerA         14:05      (three side-by-side columns, one row per donor;
...                                                  time shown in each viewer's own time zone)

Not donated yet (9)
PlayerX
PlayerY
DiscordName (not linked)
...                       (one per line, "+N more" if it no longer fits)

Yesterday (UTC)
Donated: 31 · Coins: 1,550,000
```

Refresh triggers: button press, `/log`, undo, setup, reminders sent, and day rollover (section 9).

**Always at the bottom:** on each refresh, if the board is not the last message in the panel channel, the bot deletes it and posts a fresh copy (embed and button) at the bottom. Otherwise it edits in place. The panel channel should be read-only for members (see section 6) so only the bot posts there. Slash commands and the button still work in a read-only channel.

Board names are in-game names only. A tracked member without a link appears as `Discord name (not linked)`.

## 8. Permissions and safety

- **Staff check:** Manage Server, or `Officer` role. Checked on every staff command.
- **Trust:** a button press is not verified against the game. Staff fix mistakes with `/undo`.
- **CSV export:** cells starting with `=`, `+`, `-`, or `@` get a leading `'` before writing, so Excel and Sheets do not run them as formulas.
- **Bot token:** only in `.env`, never committed or pasted into chat. `.env` and `donations.db` live in `C:\EvilDonations\`, outside OneDrive, so they do not sync to the cloud.
- **Input validation:** IGN length 2 to 32.
- **Ephemeral replies** for member errors, `/history`, and `/export`.
- **Mentions:** `<@id>` only in reset reminders. Board uses plain names so nobody gets pinged.

## 9. Time handling

- All stored times are UTC ISO 8601.
- "Today" = current UTC date. Day key stored as `day_utc`.
- Reset is implicit: a new UTC date means a new key. Yesterday's approved rows stop counting as "today" and become "yesterday".
- **Rollover refresh:** a task loop runs every 60 seconds. If `board_day` in `guilds` differs from today's UTC date, it refreshes the board once and sets `board_day`.
- Reset timestamp on the board is next midnight UTC as Unix epoch, rendered by Discord in each viewer's time zone.

### Reset reminder pings
- Time: one reminder, **60 minutes** before 00:00 UTC (23:00 UTC). Set in `config.py` as `REMINDER_MINUTES = (60,)`.
- Where: the panel channel, as a plain message.
- Who: tracked members (role `ingame guild member`, not bots) with no `approved` donation for the UTC day. Linked or not, they are pinged by `<@id>`.
- Format: one message per 50 members. Skipped entirely when the list is empty.
- The loop in the 60-second task sends a reminder when `now` is at or after its time and before time + 5 minutes. It writes a `reminders` row either way. A row already present means no send.
- Late (bot started after the 5-minute window): row written, nothing sent. No catch-up pings in the last minutes before reset.
- Maximum one reminder message group per day.

### Daily backup
- At 04:00 UTC, the same 60-second loop writes a backup with `sqlite3.Connection.backup()` to `C:\EvilDonations\backups\donations-YYYY-MM-DD.db`, then copies it to the OneDrive backup folder (section 13). The loop only writes a file that does not exist yet, so it runs once a day and also catches up if the bot was off at 04:00.
- Prunes local and OneDrive copies older than 30 days.

## 10. Error handling and edge cases

| Case | Behavior |
|---|---|
| Button pressed without role `ingame guild member` | "Only guild members can log donations." |
| Button pressed, member not linked | "Link your in-game name first with /link." |
| Button pressed, already donated today | "You already logged a donation today." |
| `ingame guild member` role missing or renamed | `/setup` refuses with a message. Board does not guess. Reminders send nothing and log a warning. |
| Member leaves server | Rows kept. Not in the not-donated list or reminders (not a current member). |
| Member rejoins | Link and history still exist. |
| PC asleep or bot off at reminder time | No reminder. Row written as late when the bot next runs. |
| Reminder list over 50 members | Split across messages. |
| Discord API rate limit | discord.py retries. Board edit is the only frequent call. |
| Bot crashed or PC off | No notice possible. Panel still shows button. Press shows Discord "application did not respond". Member tells an officer, who logs with `/log` later. |
| `/log` for member not linked | "Member has no linked IGN." Nothing stored. |
| `/log` for member already approved on that day | "Member already has an approved donation that day." Nothing stored. |
| `/log` with `day` older than yesterday | "Only today or yesterday can be logged." Nothing stored. |
| `/undo` on a row that is not approved | "That donation is not active." Nothing changed. |

## 11. Project layout

Runtime folder: `C:\EvilDonations\` (outside OneDrive). Source can stay in the project folder.

```
Discordbot/
├── bot.py                 # entry point, commands, views, 60 s loop (rollover, reminders, backup)
├── db.py                  # SQLite schema, queries, CSV export
├── board.py               # board data, not-donated list, join_capped, paginate (no Discord imports, testable)
├── reminders.py           # reminder window check, 50-per-message split
├── boss.py                # boss poll cycle, winning-time rule, poll and ping text (no Discord imports)
├── backup.py              # daily SQLite backup, copy to OneDrive, 30-day prune
├── config.py              # .env reading, constants (donation amount, recent 10, role names, REMINDER_MINUTES)
├── tests/
│   ├── test_db.py         # constraints, status transitions, CSV export
│   ├── test_boss.py       # cycle times, tie and late-ping rules, poll text
│   ├── test_backup.py     # backup copy, once-a-day, prune
│   ├── test_board.py      # not-donated list, yesterday totals, join_capped
│   └── test_reminders.py  # reminder targets, window check, 50-per-message split
├── scripts/
│   ├── run_bot.ps1        # restart loop around bot.py
│   └── install_task.ps1   # registers the logon task in Task Scheduler
├── requirements.txt
├── .env.example
├── README.md
└── PLAN.md
```

## 12. Testing

### Automated (pytest)
- DB constraints: second approved row for same day rejected by index. Voided row does not block a new donation.
- Status transitions: `approved → voided`. Voiding a voided or missing row is refused.
- Board builder: last 10 ordering, not-donated list excludes donors, bots, and non-`ingame guild member` members. Yesterday totals sum correctly. `join_capped` adds `+N more`.
- Reminders: targets exclude donors (approved), bots, and non-`ingame guild member` members. A reminder fires once (`reminders` PK). A late reminder writes the row and sends nothing. Lists over 50 split into multiple messages.

### Manual (test server)
1. `/setup` → panel and board posted. Missing `ingame guild member` role → refused.
2. Member with role, not linked, presses button → blocked message.
3. `/roster add` with 3 names → `/link` lists them. Pick one → linked. Second member: that name is gone from the list, and typing it by hand is refused. A name not on the roster is refused. `/unlink` → the name is back on the list.
4. Linked member without the role presses button → blocked message.
5. Linked member with the role presses button → logged 50,000, board shows donor in last 10, count +1.
6. Same member presses again → "already logged".
7. Officer runs `/log` for a linked member → same result, `logged_by` set.
8. `/undo` → board updates, member can press the button again.
9. Simulate rollover (set `board_day` old or change system clock in test) → board shows yesterday totals and resets today.
10. Set the clock to 22:59 UTC (or set `REMINDER_MINUTES` for the test) → reminder pings only members without a donation today.
11. Restart bot inside the reminder window → no second ping.
12. `/history` shows approved and voided rows.
13. `/export` → CSV opens in Excel, non-ASCII names correct. No XP column.
14. Restart bot → button still works.
15. Member leaves → history still in `/history` and `/export`.
16. `/boss start channel` → poll posted at once with six numbered reactions, times in local time, role pinged.
17. React with 3 and 4 as a role member, 4 as a second member. Shorten the wait for the test with a cycle starting 1 hour ahead. Ten minutes before time 4 → ping naming time 4.
18. React as someone without the role → vote not counted.
19. Five minutes after the winning time → next poll for the next cycle appears.
20. Restart the bot inside the 10 minutes before a time → exactly one ping.
21. `/data start channel` → four messages appear: Data header, Donated today, Not donated yet, Not linked. Press the donate button → the member moves from Not donated to Donated, with a local time.
22. With more than 15 names in a section (add test names with `/roster add` and `/link`) → page 1 of 2 shows 15 rows. Next shows the rest, Previous goes back. Delete one data message → the next refresh reposts all four.

## 13. PC hosting plan

1. **Prep the PC**
   - Install Python (python.org, "Add to PATH").
   - Create `C:\EvilDonations\`. Copy the project in (or clone it). Create `C:\EvilDonations\venv`, run `pip install -r requirements.txt`.
   - Put `.env` in `C:\EvilDonations\` with the token. Do not put the runtime folder in OneDrive.
   - Create the OneDrive backup folder, e.g. `OneDrive\EvilDonations-backups\`.
2. **Power settings:** Settings → System → Power. Sleep: Never while plugged in. Hibernate off. The PC is off about 4 hours a day, so the bot is offline then. Donations cannot be logged during that window. Reminders due in the off-window are accepted as missed (section 16).
3. **Auto-start:** `scripts/install_task.ps1` registers a Task Scheduler task "Evil Donations" that runs at logon and starts `scripts/run_bot.ps1`. Run it once with PowerShell as your user. Also set Windows auto-logon (Sysinternals Autologon or `netplwiz`) so the task runs after a reboot with no one logging in.
4. **Restart loop:** `scripts/run_bot.ps1` runs `venv\Scripts\python.exe bot.py` in a loop. On crash (nonzero exit), wait 5 seconds and start it again. On clean exit from `/stop` (exit code 0), the loop ends and the bot stays off until started by hand or at next logon. Logs go to `C:\EvilDonations\logs\bot.log` (rotated by size, plain text).
5. **Backups:** Built into the bot (section 9). Copies land in `C:\EvilDonations\backups\` and in the OneDrive backup folder. OneDrive is the off-PC copy.
6. **Test one restore** on the PC: stop the bot, copy a backup over a scratch `donations-test.db`, open it, count rows, compare to the live DB. Then delete the scratch file.
7. **Updates:** stop the bot with `/stop` (posts downtime notice first), copy or `git pull`, `pip install -r requirements.txt`, start the bot again (it restores the board button on start).
8. **Secrets:** rotate bot token if exposed (Developer Portal → Reset Token), then update `.env`.

## 14. Milestones

Order is fixed.

| # | Milestone | Done when |
|---|---|---|
| M0 | Accounts ready | Discord app, test server, `ingame guild member` and `Officer` roles exist. PC prepared (section 13 step 1). |
| M1 | Schema and DB layer | Automated tests in section 12 pass for the DB |
| M2 | Setup, link, unlink, history | Manual steps 1 to 3 pass in test server |
| M3 | Donation flow: button, `/log` | Manual steps 4 to 7 pass |
| M4 | Board, yesterday totals, undo, export | Manual steps 8, 12, 13 pass |
| M5 | Rollover, reminders, backups, restart safety | Manual steps 9 to 11 and 14 pass. Backup file lands in OneDrive folder. |
| M6 | PC auto-start and unattended run | Bot runs unattended for 48 h on the PC, survives a forced restart and a logoff/logon. One backup restored locally. |
| M7 | Live rollout | Live server configured, officers briefed on `/log` and `/undo` |
| M8 | Boss time polls | Manual steps 16 to 20 pass |
| M9 | Data channel | Manual steps 21 and 22 pass |

## 15. Out of scope (for now)

- DMs and reject reasons.
- Screenshots, proof, and OCR.
- Web dashboard.
- Polls.
- Per-player leaderboard beyond "last 10".
- Guild XP tracking.
- Multiple servers in practice (data model allows it).

## 16. Decisions

Resolved in v3:
- Unlinked tracked members show on the board as `Discord name (not linked)`.
- Backup destination: OneDrive folder copy, daily at 04:00 UTC, 30-day retention.
- Reminders: one ping, 60 minutes before reset (23:00 UTC) in v5 and later, in the panel channel, mentioning only members with no donation today. Earlier drafts had three.
- PC uptime: on about 20 hours a day, off about 4.

Resolved in v4:
- Staff: one `Officer` role.
- IGN: first-come `/link`, no staff approval.
- Button: only role `ingame guild member` can press it.
- Reboot: Windows auto-logon so the bot restarts after reboot.
- `/stop`: clean exit ends the restart loop.
- CSV: formula-prefix escape.
- PC off-window: reminders missed while the PC is off are accepted. No schedule change.
- Go-live date: not needed to start the build.
- Downtime donations: member tells an officer, officer logs with `/log` once the bot is back.

Resolved in v5:
- No screenshots. One button press logs a fixed 50,000 donation.
- Accepted risk: nothing checks that the member really donated in the game.

Resolved in v6:
- Roster: `/link` only accepts names staff added with `/roster add`. Chosen from an autocomplete list of free names.
- Boss time polls use a message with numbered reactions, not a Discord poll, because poll options cannot show local time.

## 17. Boss time polls

The guild boss spawns globally at 00:00, 06:00, 12:00 and 18:00 UTC. Members choose the time in each 6-hour window that suits them. The bot picks the winner and pings the guild 10 minutes before it.

- **Poll message:** posted in the boss channel, pings the `ingame guild member` role. Six options for the cycle: the spawn hour and the next five hours (12:00 cycle: 12:00, 13:00, 14:00, 15:00, 16:00, 17:00). The next spawn hour is never an option. Each option is `1️⃣ <t:UNIX:t>`, so Discord shows only the time in the viewer's own time zone, with no UTC text. The bot adds the six number reactions.
- **Voting:** a member reacts with every number that works for them. Only members with the `ingame guild member` role count. The bot's own reactions never count.
- **Winner:** only times still ahead can win. Most votes wins. A tie goes to the earliest. No votes anywhere means the first time ahead wins.
- **Ping:** `@ingame guild member Guild boss at <time> (<relative time>). Go to the guild island now.` sent 10 minutes before the winning time. If the bot was off and comes back after that point but before the time, it still sends the ping. Once the time has started, no ping for it.
- **Counting votes:** the 60-second loop counts reactions only inside the 10 minutes before the earliest time still ahead. No ping can be due earlier, so counting is skipped the rest of the hour.
- **Next poll:** 5 minutes after the winning time, the bot posts the poll for the next cycle (winner 14:00 → poll at 14:05 with 18:00 to 23:00). If every time passed without a ping, the poll ends and the next poll follows. If the bot was off through whole cycles, it posts the first cycle that still has a time ahead.
- **Start and stop:** `/boss start channel` (staff) saves the channel and posts the poll for the next spawn time right away (at 10:30 UTC: the 12:00 cycle). `/boss stop` turns it off.
- **State:** `boss_polls` keeps each poll, so a restart carries on. The end of a poll is claimed in the database before the ping is sent, so a restart cannot ping twice.
- **Deleted poll message:** votes count as zero, so the first time ahead gets the ping.

## 18. Data channel

A read-only channel that shows who has and has not donated today. `/data start channel` (staff) posts four messages and keeps them updated.

1. **Data** (header): today's UTC date, the reset time in each viewer's local time, and the three counts.
2. **Donated today (N):** Discord name, in-game name, and the time the donation was logged (Discord local-time tag). Newest first.
3. **Not donated yet (N):** Discord name and in-game name of linked members with no approved donation today. Sorted by in-game name.
4. **Not linked (N):** in-game name only. The roster names nobody has claimed with `/link` yet. Sorted by name.

- List 3 holds only linked members, so every row has both names. List 4 comes from the roster, not from Discord members: a role member who is not on the roster appears nowhere until staff add their name.
- Tracked = role `ingame guild member`, bots ignored. Names longer than 16 characters are cut with an ellipsis so the columns line up.
- Each list shows 15 rows per page (`DATA_PAGE` in `config.py`). Previous and Next buttons under it change the shared message for everyone. The page resets to 1 after a bot restart.
- Refresh triggers are the same as the board: donate button, `/log`, `/undo`, setup, new day, reminders sent. The messages are edited in place. If one is deleted, the next refresh deletes the rest and posts all four again.
- The bot needs View Channel, Send Messages, and Embed Links in the channel. Make it read-only for members (Send Messages off). The buttons still work.
