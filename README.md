# 🔥 FFregister — Free Fire Custom Room Manager (Telegram bot)

Registration, automatic slots, solo/team/custom matches, admin-entered results, configurable scoring and
leaderboards for Free Fire custom rooms. Python 3.11+, `python-telegram-bot` (async), SQLite, one polling process.

> **Honesty notes**
> * Results are **only what an admin types in**. The bot never fetches or invents game data.
> * Railway has **no permanent free 24/7 tier**. See [Cost](#cost).
> * SQLite is fine for one bot process and a few hundred players; see [Limits](#sqlite-limits).

---

## 1. Features at a glance

| Area | What you get |
|---|---|
| Players (in the group) | `/register` (guided: name → UID → confirm), `/myregistration`, `/cancelregistration`, `/slots`, `/count`, `/teams`, `/myteam`, `/jointeamN`, `/leaveteam`, `/matches`, `/activematch`, `/resultmatch 1`, `/points`, `/cancel` |
| Admins (private chat only) | `/admin` panel, `/creatematch` wizard, `/open /close /startmatch /endmatch /cancelmatch`, `/setmode`, `/setslots`, `/players`, `/selectmatch`, result commands |
| Slots | unique per match, lowest free slot first, released on removal, capacity changes never delete registrations |
| Modes | **Solo**, **Team** (any team count/size), **Custom** (any combination, or no teams) |
| Scoring | configurable placement table + kill points; default 12/9/8/7/6/5/4/3/2/1 and 1 per kill |
| Safety | numeric-ID admin auth, private-chat-only admin, per-callback re-checks, confirmations, audit log |

Team commands are generated from the match: a match with 12 teams accepts `/jointeam1` … `/jointeam12`.

## 2. Command reference (one consistent syntax)

Player commands work **in an authorized group**. Optional `[id]` = match ID in *that* group; without it the group's active match is used
(open registration → in progress → closed; one match per group may have registration open at a time).

```
/register  /myregistration  /cancelregistration  /slots [id]  /count [id]
/teams [id]  /myteam  /jointeamN  /leaveteam  /matches  /activematch
/resultmatch <id>   /points   /points <id>   /points solo   /points team   /points all
/cancel   (stops the current registration step; to withdraw use /cancelregistration)
```

Admin commands (private chat with the bot, numeric user ID in `OWNER_ID`/`ADMIN_IDS`/added by owner).
**Ambiguity rule:** an explicit ID wins → otherwise the match you chose with `/selectmatch` (or by opening it in the panel)
→ otherwise the only live match → otherwise the bot refuses and lists the matches. Every reply names the match it changed.

```
/admin                       open the panel
/creatematch                 wizard
/matches                     all matches (private) / announced matches (group)
/selectmatch <id>
/open [id]  /close [id]  /startmatch [id]  /endmatch [id]  /cancelmatch [id]
/setmode [id] solo|team|custom [team_count team_size]
/setslots [id] <capacity>
/players [id]
/submitresult [id]           guided result entry
/editresult <id> <UID|T#> <placement> <kills>     e.g. /editresult 1 123456789 1 8
/deleteresult <id> <UID|T#>                        (asks for confirmation)
/publishresult <id>                                (preview + confirmation)
/playerkills <id> <UID> <kills>                    (team matches: informational individual kills)
/resultmatch <id>            live preview, flagged UNPUBLISHED / EDITED SINCE PUBLISH
```
Result targets: players are identified by **UID**; teams by **T1, T2, …**.

## 3. Rules the bot follows

* **Match statuses:** Draft → Registration open ⇄ Registration closed → In progress → Completed; Cancelled from any live state.
* **Registration:** one per Telegram account per match, one per UID per match, capacity enforced inside a `BEGIN IMMEDIATE` transaction (race-safe).
* **Custom validation:** `capacity ≤ team_count × team_size` when teams are used; capacity 1–100; team mode capacity = teams × size.
* **Results:** only when a match is *In progress* or *Completed*; placement must be 1…participants and unique; kills 0–99; one result per player/team (use edit to change).
  Editing rescores; changing scoring rules re-scores every saved result.
* **Publishing:** players only ever see a **snapshot taken at publish time**. If you edit afterwards, admins see
  "🟠 PUBLISHED, BUT EDITED SINCE" and players keep seeing the old snapshot until you publish again.
* **Leaderboard order:** total points ↓, kills ↓, best placement ↑, then name. Rows equal on the first three share a rank (1, 2, 2, 4…).
* **Team matches:** the team's placement and team kills are scored once. `/playerkills` stats are shown separately and are *never* added to team points (no double counting).
* **`/points all` (cumulative):** counts **published results of completed matches in this group**. Player identity = **Free Fire UID**.
  Solo result → that player. Team result → the team's points and kills are credited **to every member** of the team in that match (shared, not split).
  Each (match, UID) pair is counted once. The name shown is the most recent one used.
* **Group isolation:** matches belong to the group they were created for; players can only read/join matches of their own group, and results are published only to the match's group.

## 4. Architecture

```
freefire-bot/
├── main.py                 start-up, logging (token redaction), handler registration
├── config.py               env vars → Config
├── database/
│   ├── connection.py       short-lived connections, WAL, foreign keys, BEGIN IMMEDIATE transactions
│   ├── migrations.py       versioned schema, auto-applied at start
│   └── repositories.py     SQL helpers (take a connection; services own the transactions)
├── services/               all business rules (no Telegram imports)
│   ├── match_service.py  registration_service.py  team_service.py
│   ├── result_service.py scoring_service.py  permission_service.py
│   └── formatting.py       HTML message builders, 4096-limit splitting
├── handlers/               Telegram layer
│   ├── common.py (guards)  registration.py  slots.py  teams.py  matches.py
│   ├── admin.py (panel + wizards)  results.py  leaderboards.py
├── keyboards/              admin_panel.py  player_menu.py
├── tests/                  pytest suite (services, handlers with mocked Telegram)
├── requirements.txt  Procfile  railway.json  .env.example  .gitignore  .python-version
```

**Tables:** `groups`, `admins`, `matches`, `registrations`, `teams`, `team_memberships`, `scoring_rules`, `results`,
`player_kills`, `published_results`, `audit_logs` (+ `schema_version`). Foreign keys are enforced; `UNIQUE(match_id, slot)`,
`UNIQUE(match_id, telegram_id)`, `UNIQUE(match_id, uid)`, `UNIQUE(match_id, placement)` and one membership per player per match are database constraints,
so duplicates are impossible even if application code had a bug. Matches and results are never deleted when registrations are removed;
a player who already has a saved result cannot be removed until the result is deleted.

**Audit log:** every admin change (match create/status/settings, removals, slot assignment, team moves, results add/edit/delete/publish, exports, admin changes)
stores admin ID, match ID, action, target, details and UTC timestamp (view it per match in the panel).

## 5. Setup on Android (step by step)

### 5.1 Create the bot and find your IDs
1. In Telegram open **@BotFather** → `/newbot` → name it **FFregister** → choose a username ending in `bot`. Copy the **token** (keep it secret).
2. Find your numeric user ID with a bot such as **@userinfobot** (send it any message). That is your `OWNER_ID`.
3. (Optional) In BotFather: `/setjoingroups` → Enable. Leave **Privacy mode ON** (default) — see [Privacy mode](#7-adding-the-bot-to-your-group).

### 5.2 Get the project files onto your phone
Download `freefire-bot.zip` from this chat and extract it (the Files app, **ZArchiver**, or Termux `unzip`). You now have a `freefire-bot` folder.

### 5.3 Create the GitHub repository
1. In Chrome open github.com → sign in → **+** → **New repository** → name `freefire-bot` → **Private** → *Create repository*.

### 5.4 Upload the files
**Option A — Termux (recommended, keeps folders intact).** Install Termux from F-Droid, then:
```bash
pkg update && pkg install -y git unzip
termux-setup-storage                       # allow storage access
cd ~/storage/downloads
unzip freefire-bot.zip && cd freefire-bot
git init -b main
git config user.name "Your Name" && git config user.email "you@example.com"
git add . && git commit -m "Initial commit"
git remote add origin https://github.com/<your-username>/freefire-bot.git
git push -u origin main                    # username = GitHub name, password = Personal Access Token
```
Create the token at GitHub → Settings → Developer settings → Personal access tokens → *Fine-grained* → only this repo → **Contents: Read and write**.
For later changes: `git add . && git commit -m "msg" && git push`.

**Option B — browser only.** On the repo page: *Add file → Upload files*, select the files from the extracted root folder and commit.
Then repeat for each sub-folder: *Add file → Create new file*, type `database/` in the name box (the slash creates the folder), then
use *Upload files* inside that folder. Do this for `database`, `handlers`, `services`, `keyboards`, `tests`. Check the repo tree matches section 4.
**Never upload `.env` or any real token.**

### 5.5 Deploy on Railway
1. railway.com → sign in with GitHub → **New Project → Deploy from GitHub repo** → pick `freefire-bot` (authorize the Railway GitHub app for that repo).
2. Open the service → **Variables** and add:

| Variable | Value |
|---|---|
| `BOT_TOKEN` | token from BotFather |
| `OWNER_ID` | your numeric ID |
| `ADMIN_IDS` | optional, comma-separated numeric IDs |
| `DATABASE_PATH` | `/data/freefire.db` |
| `TIMEZONE` | `Asia/Kolkata` (default for new matches) |
| `GROUP_IDS` | optional, pre-authorized group IDs (negative numbers) |

3. **Attach a persistent volume** (this is essential — the container's own disk is wiped on every deploy):
   open the project canvas → **New / + Create → Volume** → attach it to the bot service → **Mount path `/data`**.
   `DATABASE_PATH` must point **inside** that path (`/data/freefire.db`). Without the volume you will lose all data on each redeploy.
4. Keep the service at **one replica** (`railway.json` already says so). Two instances polling the same token cause Telegram *409 Conflict* errors.
5. Deploy → open **Deployments → View logs**. Success looks like:
   `Database ready at /data/freefire.db (schema v1)` then `Bot started as @YourBot`.
6. The bot has no web port, so no HTTP health check is needed; the log line above is your health signal, and `restartPolicyType: ON_FAILURE` restarts it if the process crashes.

### 5.6 Test it
1. DM the bot `/start`, then `/admin` — you should see the panel (anyone else gets "not authorized").
2. Add the bot to your group and send `/help` there → the bot records the group and DMs you; in `/admin` → **📡 Groups** tap **Authorize**.
3. `/admin` → **➕ Create Match** → follow the wizard → **🟢 Open registration now**.
4. In the group, from a player account: `/register`, then `/slots`.
5. Start the match, enter results (**📝 Submit Results**), preview, then **📢 Publish**; check `/points` in the group.

### 5.7 Updating and troubleshooting
* **Update:** change code → `git push` → Railway redeploys automatically (data persists on the volume).
* **Crash on start with "Configuration error":** a variable is missing/invalid (`OWNER_ID` must be digits; `TIMEZONE` must be a valid name like `Asia/Kolkata`).
* **`Conflict: terminated by other getUpdates request`:** another copy of the bot is running (old Railway deploy, a Termux/local run, or 2 replicas). Stop the other one.
* **`unable to open database file`:** `DATABASE_PATH` is not inside the mounted volume or the volume isn't attached.
* **`database is locked`:** shouldn't happen at this scale (30 s busy timeout); if it does, make sure only one instance runs.
* **Bot silent in the group:** group not authorized yet, or the bot lacks permission to send messages.
* **Data safety:** SQLite lives on the volume. Use the panel's **📤 Export CSV** before big events as a simple backup.

## 6. Local development / tests
```bash
python -m venv .venv && source .venv/bin/activate     # Termux: python -m venv .venv
pip install -r requirements.txt
cp .env.example .env   # fill in a TEST bot token; DATABASE_PATH=./data/dev.db
python main.py
pytest -q
```
Use a different bot token and database file for development than for production. Tests use temporary databases, fake IDs and a mocked Telegram API — never real tokens or player data.

## 7. Adding the bot to your group
1. Open the group → *Add member* → search your bot → add it.
2. Make sure it may **send messages** (it does not need to be an admin).
3. **Privacy mode.** With Privacy ON (default) the bot receives commands and *replies to its own messages*. `/register` asks questions with
   Telegram's "reply" prompt, so it works with privacy ON — **users must reply to the bot's question** (the keyboard prompt does this automatically).
   If you want users to answer without replying, use BotFather → `/setprivacy` → select the bot → **Disable**, then **remove and re-add** the bot to the group (Telegram applies it on re-join).
4. Authorize the group in `/admin → 📡 Groups` (owner only). Unauthorized groups cannot use the bot.
5. To use several groups: add the bot to each and authorize each; matches, slots and leaderboards are fully separate per group.

## 8. Cost
Railway's pricing changes. At the time of writing it offers a limited trial credit and then paid plans with usage-based billing; **there is no guaranteed
permanent free 24/7 tier**. A tiny polling bot uses very little CPU/RAM, but a 24/7 service plus a volume still counts toward your plan's usage.
Check <https://railway.com/pricing> before relying on it. GitHub (private repos) is free. If cost matters, any always-on small VPS or similar host that gives you
a persistent disk also works — the bot only needs Python and a writable folder.

## SQLite limits
* SQLite allows **one writer at a time**; this app serialises writes with `BEGIN IMMEDIATE` and a 30 s busy timeout, which is plenty for tournament traffic (hundreds of registrations).
* Run **exactly one bot process** against the database file. Don't point several replicas/services at the same volume.
* The database must be on a **local persistent volume**, not a network share.
* For several thousand concurrent users or multiple instances you'd move to PostgreSQL (the repository layer is plain SQL, so the migration is mostly mechanical).

## Security summary
Admins are authorised by **numeric Telegram user ID only** (never username). Every admin command and every admin button re-checks *private chat + admin ID*.
Only the owner can add/remove admins or authorize groups. Announcements go only to the match's own authorized group. Secrets live in environment variables;
the log filter redacts the bot token; user-facing errors are generic while details go to logs. CSV exports neutralise spreadsheet formula injection.
