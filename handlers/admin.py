"""Private admin panel: /admin, inline navigation, wizards (create match, results, settings…).

Every callback re-checks (private chat AND admin ID). Destructive actions go through
an explicit "Are you sure?" step. Text answers to wizard prompts are routed through
`context.user_data["flow"]`.
"""
from __future__ import annotations

import io
import logging
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from keyboards import admin_panel as kb
from services import formatting as fmt
from services.errors import PermissionDenied, ServiceError
from services.match_service import LIVE_STATUSES, MODES, STATUS_LABEL, format_time, parse_local_datetime
from services.scoring_service import Rules, parse_placement_list
from .announcements import (after_status_change, announce_new_match, authorized_group_ids,
                            refresh_announcements)
from .common import (App, admin_callback, admin_command, answer, e, get_app, reply, show)
from .results import live_text, publish_results

log = logging.getLogger(__name__)
PER_PAGE_PICK = 20


# =====================================================================
# Entry points
# =====================================================================
@admin_command
async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE, app: App) -> None:
    context.user_data.pop("flow", None)
    await reply(update, kb.PANEL_TEXT, kb.panel(app.perms.is_owner(update.effective_user.id)))


@admin_command
async def cmd_creatematch(update: Update, context: ContextTypes.DEFAULT_TYPE, app: App) -> None:
    await create_begin(update, context, app)


# =====================================================================
# Callback router
# =====================================================================
@admin_callback
async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE, app: App) -> None:
    q = update.callback_query
    parts = q.data.split(":")
    action, args = parts[1], parts[2:]
    handler = ROUTES.get(action)
    if handler is None:
        await answer(update, "Unknown action.", True)
        return
    await answer(update)
    await handler(update, context, app, args)


def _uid(update: Update) -> int:
    return update.effective_user.id


def _selected(app: App, context) -> Optional[dict]:
    sel = context.user_data.get("sel_match")
    if not sel:
        return None
    try:
        return app.ms.get(sel)
    except ServiceError:
        context.user_data.pop("sel_match", None)
        return None


async def _need_match(update, context, app: App, dest: str):
    """Return the selected match, or show a picker (callbacks come back as adm:sel:<id>:<dest>)."""
    m = _selected(app, context)
    if m:
        return m
    live = app.ms.list(statuses=LIVE_STATUSES, limit=20)
    if not live:
        await show(update, "There are no active matches yet. Create one first.",
                   kb.with_back([[B("➕ Create Match", callback_data="adm:cm")]]))
        return None
    rows = [[B(f"#{m['id']} {m['name'][:30]} · {m['status'].replace('registration_', 'reg ')}",
               callback_data=f"adm:sel:{m['id']}:{dest}")] for m in live]
    await show(update, "📌 <b>Which match?</b>", kb.with_back(rows))
    return None


# =====================================================================
# Home / matches
# =====================================================================
async def r_home(update, context, app, args):
    context.user_data.pop("flow", None)
    await show(update, kb.PANEL_TEXT, kb.panel(app.perms.is_owner(_uid(update))))


async def r_noop(update, context, app, args):
    return


async def r_cancel(update, context, app, args):
    context.user_data.pop("flow", None)
    await show(update, "✖️ Cancelled.", kb.with_back([]))


async def r_match_list(update, context, app, args):
    ms = app.ms.list(limit=30)
    if not ms:
        await show(update, "No matches yet.", kb.with_back([[B("➕ Create Match", callback_data="adm:cm")]]))
        return
    rows = [[B(f"#{m['id']} {m['name'][:28]} · {m['status'].replace('registration_', 'reg ')}",
               callback_data=f"adm:m:{m['id']}")] for m in ms]
    await show(update, "📋 <b>Manage Matches</b>\nTap a match:", kb.with_back(rows))


async def r_match(update, context, app, args):
    m = app.ms.get(int(args[0]))
    n = app.rs.slots_view(m["id"])["registered"]
    sel = "📌 (selected)\n" if context.user_data.get("sel_match") == m["id"] else ""
    await show(update, sel + fmt.match_card(m, n) + f"\nGroup: <code>{m['group_id']}</code>", kb.match_menu(m))


async def r_select(update, context, app, args):
    m = app.ms.get(int(args[0]))
    context.user_data["sel_match"] = m["id"]
    dest = args[1] if len(args) > 1 else "m"
    await ROUTES[{"m": "m", "mp": "mp", "st": "st", "sr": "sr", "lb": "lb", "set": "set"}[dest]](
        update, context, app, [str(m["id"])] if dest == "m" else [])


async def r_status_op(update, context, app, args):
    mid, op = int(args[0]), args[1]
    m = app.ms.transition(_uid(update), mid, op)
    await r_match(update, context, app, [str(mid)])
    await after_status_change(context.bot, app, m)


# =====================================================================
# Panel sections (use selected match)
# =====================================================================
async def r_players_section(update, context, app, args):
    m = await _need_match(update, context, app, "mp")
    if m:
        await r_pm(update, context, app, [str(m["id"])])


async def r_pm(update, context, app, args):
    m = app.ms.get(int(args[0]))
    mid = m["id"]
    rows = [
        [B("📄 View players", callback_data=f"adm:pl:{mid}:1"), B("🔎 Search", callback_data=f"adm:fl:search:{mid}")],
        [B("🗑 Remove player", callback_data=f"adm:fl:rmplayer:{mid}"), B("🎯 Assign slot", callback_data=f"adm:fl:assign:{mid}")],
        [B("📤 Export CSV", callback_data=f"adm:pe:{mid}"), B("📊 Statistics", callback_data=f"adm:pst:{mid}")],
        [B("🔢 Set capacity", callback_data=f"adm:fv:capacity:{mid}")],
    ]
    await show(update, f"👥 <b>Players — {e(m['name'])}</b> (ID {mid})", kb.with_back(rows, [B("↩️ Match", callback_data=f"adm:m:{mid}")]))


async def r_player_list(update, context, app, args):
    mid, page = int(args[0]), int(args[1])
    v = app.rs.slots_view(mid)
    regs = v["regs"]
    pages = max(1, -(-len(regs) // 15))
    page = min(max(page, 1), pages)
    lines = [f"👥 <b>{e(v['match']['name'])}</b> — {len(regs)}/{v['total']}"]
    lines += [f"Slot {r['slot']} · {e(r['ign'])} · <code>{r['uid']}</code>" for r in regs[(page - 1) * 15: page * 15]] or ["No players yet."]
    await show(update, "\n".join(lines), kb.with_back([x for x in [kb.pager(f"adm:pl:{mid}", page, pages)] if x],
                                                      [B("↩️ Players", callback_data=f"adm:pm:{mid}")]))


async def r_export(update, context, app, args):
    mid = int(args[0])
    csv_text = app.rs.export_csv(_uid(update), mid)
    try:
        await context.bot.send_document(update.effective_chat.id, document=csv_text.encode("utf-8-sig"),
                                        filename=f"match_{mid}_registrations.csv",
                                        caption=f"📤 Registrations for match #{mid}")
    except TelegramError:
        log.exception("CSV export failed")
        await reply(update, "⚠️ Could not send the file. Please try again.")


async def r_stats(update, context, app, args):
    mid = int(args[0])
    m = app.ms.get(mid)
    v = app.rs.slots_view(mid)
    lines = [f"📊 <b>Statistics — {e(m['name'])}</b>", f"Status: {STATUS_LABEL[m['status']]}",
             f"Registered: {v['registered']}/{v['total']} ({v['empty']} empty)"]
    if m["team_count"]:
        tv = app.ts.view(mid)
        lines.append(f"Teams: {len(tv['teams'])} x {m['team_size']} · unassigned players: {len(tv['unassigned'])}")
        lines += [f"  {e(t['team']['name'])}: {len(t['members'])}/{m['team_size']}" for t in tv["teams"][:30]]
    snap = app.res.live_snapshot(mid)
    lines.append(f"Results entered: {len(snap['rows'])}")
    lines.append("Published: " + ("no" if app.res.published_snapshot(mid) is None else
                                  ("yes (edited since)" if app.res.has_unpublished_changes(mid) else "yes")))
    await show(update, "\n".join(lines), kb.with_back([], [B("↩️ Players", callback_data=f"adm:pm:{mid}")]))


async def r_slots_section(update, context, app, args):
    m = await _need_match(update, context, app, "st")
    if m:
        rows = [
            [B("🎯 Slots", callback_data=f"adm:sl:{m['id']}:1"), B("🧩 Teams", callback_data=f"adm:tm:{m['id']}:1")],
            [B("↔️ Move player to team", callback_data=f"adm:fl:mvteam:{m['id']}")],
            [B("➖ Remove from team", callback_data=f"adm:fl:rmteam:{m['id']}")],
        ]
        await show(update, f"🎯 <b>Slots and Teams — {e(m['name'])}</b> (ID {m['id']})", kb.with_back(rows))


async def r_slots(update, context, app, args):
    mid, page = int(args[0]), int(args[1])
    text, page, pages = fmt.slots_page(app.rs.slots_view(mid), page)
    pg = kb.pager(f"adm:sl:{mid}", page, pages)
    await show(update, text, kb.with_back([pg] if pg else [], [B("↩️ Slots & Teams", callback_data="adm:st")]))


async def r_teams(update, context, app, args):
    mid, page = int(args[0]), int(args[1]) if len(args) > 1 else 1
    text, page, pages = fmt.teams_page(app.ts.view(mid), page)
    pg = kb.pager(f"adm:tm:{mid}", page, pages)
    await show(update, text, kb.with_back([pg] if pg else [], [B("↩️ Slots & Teams", callback_data="adm:st")]))


async def r_results_section(update, context, app, args):
    m = await _need_match(update, context, app, "sr")
    if m:
        await r_results_menu(update, context, app, [str(m["id"])])


async def r_results_menu(update, context, app, args):
    m = app.ms.get(int(args[0]))
    mid = m["id"]
    rows = [
        [B("➕ Submit / edit result", callback_data=f"adm:rstart:{mid}")],
        [B("🗑 Delete result", callback_data=f"adm:fl:delres:{mid}"), B("🎯 Player kills", callback_data=f"adm:fl:pkills:{mid}")],
        [B("👀 Preview leaderboard", callback_data=f"adm:lbp:{mid}"), B("📢 Publish", callback_data=f"adm:c:pub:{mid}")],
    ]
    await show(update, f"📝 <b>Results — {e(m['name'])}</b> (ID {mid})\nAdmins enter real placements and kills; "
                       "nothing is fetched automatically.", kb.with_back(rows, [B("↩️ Match", callback_data=f"adm:m:{mid}")]))


async def r_lb_section(update, context, app, args):
    m = await _need_match(update, context, app, "lb")
    if m:
        await r_lb_preview(update, context, app, [str(m["id"])])


async def r_lb_preview(update, context, app, args):
    m = app.ms.get(int(args[0]))
    rows = [[B("📢 Publish to group", callback_data=f"adm:c:pub:{m['id']}")],
            [B("📈 Cumulative (group)", callback_data=f"adm:lbc:{m['id']}")]]
    await show(update, live_text(app, m), kb.with_back(rows, [B("↩️ Match", callback_data=f"adm:m:{m['id']}")]))


async def r_lb_cumulative(update, context, app, args):
    m = app.ms.get(int(args[0]))
    rows = app.res.cumulative(m["group_id"])
    await show(update, fmt.leaderboard("Cumulative leaderboard (published, completed matches)", {}, rows, cumulative=True),
               kb.with_back([], [B("↩️ Leaderboard", callback_data=f"adm:lbp:{m['id']}")]))


async def r_audit(update, context, app, args):
    from database import repositories as repo
    mid = int(args[0])
    with app.db.read() as conn:
        rows = repo.list_audit(conn, mid, 15)
    text = "📜 <b>Audit log</b> (latest 15)\n" + ("\n".join(fmt.audit_line(a) for a in rows) or "Nothing yet.")
    await show(update, text, kb.with_back([], [B("↩️ Match", callback_data=f"adm:m:{mid}")]))


# =====================================================================
# Settings
# =====================================================================
async def r_settings_section(update, context, app, args):
    if args:  # adm:set:<mid>
        return await _settings_screen(update, context, app, int(args[0]))
    m = await _need_match(update, context, app, "set")
    if m:
        await _settings_screen(update, context, app, m["id"])


async def _settings_screen(update, context, app: App, mid: int):
    m = app.ms.get(mid)
    rules = app.ms.rules(mid)
    pts = ",".join(str(v) for _, v in sorted(rules.placement.items()))
    text = (fmt.match_card(m, app.rs.slots_view(mid)["registered"]) +
            f"\nCancel allowed: {'yes' if m['allow_cancel'] else 'no'} · Team switching: {'yes' if m['allow_switch'] else 'no'}"
            f"\nPlacement points: {pts}\nKill points: {rules.kill_points}")
    rows = [
        [B("✏️ Name", callback_data=f"adm:fv:name:{mid}"), B("⏰ Start time", callback_data=f"adm:fv:time:{mid}")],
        [B("🌍 Timezone", callback_data=f"adm:fv:tz:{mid}"), B("⌛ Deadline", callback_data=f"adm:fv:deadline:{mid}")],
        [B("🔢 Capacity", callback_data=f"adm:fv:capacity:{mid}"), B("🧩 Teams/size", callback_data=f"adm:fv:teams:{mid}")],
        [B("Mode: Solo", callback_data=f"adm:mode:{mid}:solo"), B("Mode: Team", callback_data=f"adm:fv:mode_team:{mid}"),
         B("Mode: Custom", callback_data=f"adm:fv:mode_custom:{mid}")],
        [B("🏅 Placement pts", callback_data=f"adm:fv:placement:{mid}"), B("💥 Kill pts", callback_data=f"adm:fv:killpts:{mid}")],
        [B(f"Player cancel: {'ON' if m['allow_cancel'] else 'OFF'}", callback_data=f"adm:tg:{mid}:cancel"),
         B(f"Team switch: {'ON' if m['allow_switch'] else 'OFF'}", callback_data=f"adm:tg:{mid}:switch")],
    ]
    await show(update, "⚙️ <b>Match Settings</b>\n\n" + text, kb.with_back(rows, [B("↩️ Match", callback_data=f"adm:m:{mid}")]))


async def r_toggle(update, context, app, args):
    mid, what = int(args[0]), args[1]
    m = app.ms.get(mid)
    key = "allow_cancel" if what == "cancel" else "allow_switch"
    app.ms.update_settings(_uid(update), mid, **{key: 0 if m[key] else 1})
    await _settings_screen(update, context, app, mid)


async def r_mode_solo(update, context, app, args):
    app.ms.update_settings(_uid(update), int(args[0]), mode=args[1])
    await _settings_screen(update, context, app, int(args[0]))
    await refresh_announcements(context.bot, app, int(args[0]))


def _apply_setting(app: App, admin_id: int, mid: int, field: str, text: str) -> str:
    m = app.ms.get(mid)
    text = text.strip()
    nums = text.replace(",", " ").split()
    if field == "name":
        app.ms.update_settings(admin_id, mid, name=text)
    elif field == "time":
        app.ms.update_settings(admin_id, mid, start_utc=parse_local_datetime(text, m["timezone"]))
    elif field == "tz":
        try:
            ZoneInfo(text)
        except (ZoneInfoNotFoundError, ValueError):
            raise ServiceError("Unknown timezone. Example: Asia/Kolkata")
        app.ms.update_settings(admin_id, mid, timezone=text)
    elif field == "deadline":
        app.ms.update_settings(admin_id, mid, reg_deadline_utc=None if text in ("-", "none") else parse_local_datetime(text, m["timezone"]))
    elif field == "capacity":
        if not text.isdigit():
            raise ServiceError("Send a whole number.")
        app.rs.set_capacity(admin_id, mid, int(text))
    elif field in ("teams", "mode_team", "mode_custom"):
        want = 3 if field == "mode_custom" else 2
        if len(nums) != want or not all(x.isdigit() for x in nums):
            raise ServiceError("Send " + ("three numbers: team_count team_size capacity" if want == 3 else "two numbers: team_count team_size"))
        changes = {"team_count": int(nums[0]), "team_size": int(nums[1])}
        if field == "mode_team":
            changes["mode"] = "team"
        elif field == "mode_custom":
            changes.update(mode="custom", capacity=int(nums[2]))
        elif m["mode"] == "solo":
            raise ServiceError("This is a solo match. Switch the mode to Team or Custom first.")
        app.ms.update_settings(admin_id, mid, **changes)
    elif field == "placement":
        try:
            placement = parse_placement_list(text)
        except ValueError as exc:
            raise ServiceError(str(exc))
        app.ms.set_rules(admin_id, mid, Rules(placement, app.ms.rules(mid).kill_points))
    elif field == "killpts":
        if not text.isdigit() or int(text) > 100:
            raise ServiceError("Send a whole number from 0 to 100.")
        app.ms.set_rules(admin_id, mid, Rules(app.ms.rules(mid).placement, int(text)))
    else:
        raise ServiceError("Unknown setting.")
    return "✅ Setting updated."


FIELD_PROMPTS = {
    "name": "✏️ Send the new match name:",
    "time": "⏰ Send the new start time (match timezone): <code>YYYY-MM-DD HH:MM</code>",
    "tz": "🌍 Send the timezone, e.g. <code>Asia/Kolkata</code>:",
    "deadline": "⌛ Send the registration deadline <code>YYYY-MM-DD HH:MM</code>, or <code>-</code> to remove it:",
    "capacity": "🔢 Send the new maximum number of players (existing registrations are never deleted):",
    "teams": "🧩 Send <code>team_count team_size</code> (e.g. <code>12 4</code>):",
    "mode_team": "Team mode — send <code>team_count team_size</code> (e.g. <code>12 4</code>):",
    "mode_custom": "Custom mode — send <code>team_count team_size capacity</code> (use <code>0 0 25</code> for no teams):",
    "placement": "🏅 Send placement points for 1st, 2nd, 3rd… separated by commas, e.g. <code>12,9,8,7,6,5,4,3,2,1</code>:",
    "killpts": "💥 Send points per kill (0-100):",
}


# =====================================================================
# Flow engine (simple "collect N answers, then act" wizards)
# =====================================================================
SIMPLE_FLOWS = {
    "search": (["term"], {"term": "🔎 Send a UID or part of a name:"}),
    "rmplayer": (["uid"], {"uid": "🗑 Send the UID of the player to remove:"}),
    "assign": (["uid", "slot"], {"uid": "🎯 Send the player's UID:", "slot": "Send the new slot number (must be empty):"}),
    "mvteam": (["uid", "team"], {"uid": "↔️ Send the player's UID:", "team": "Send the target team number:"}),
    "rmteam": (["uid"], {"uid": "➖ Send the UID of the player to take out of their team:"}),
    "delres": (["token"], {"token": "🗑 Send the UID (or team token like T1) whose result should be deleted:"}),
    "pkills": (["uid", "kills"], {"uid": "🎯 Send the player's UID:", "kills": "Send their kill count:"}),
    "addadmin": (["id"], {"id": "🛡 Send the numeric Telegram user ID of the new admin:"}),
    "rmadmin": (["id"], {"id": "🛡 Send the numeric Telegram user ID of the admin to remove:"}),
    "setval": (["value"], {}),
}
OWNER_ONLY_FLOWS = {"addadmin", "rmadmin"}


async def r_flow_start(update, context, app, args):
    kind, mid = args[0], int(args[1]) or None
    await start_flow(update, context, app, kind, mid)


async def r_field_start(update, context, app, args):
    field, mid = args[0], int(args[1])
    await start_flow(update, context, app, "setval", mid, field=field)


async def start_flow(update, context, app: App, kind: str, mid: Optional[int], field: Optional[str] = None):
    if kind in OWNER_ONLY_FLOWS:
        app.perms.require_owner_context(update.effective_chat.type, _uid(update))
    keys, prompts = SIMPLE_FLOWS[kind]
    if mid:
        app.ms.get(mid)
    context.user_data["flow"] = {"kind": kind, "mid": mid, "field": field, "keys": keys, "data": {}}
    prompt = FIELD_PROMPTS[field] if kind == "setval" else prompts[keys[0]]
    await show(update, prompt, kb.cancel_flow())


async def flow_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles plain text in private chat for the active wizard (silent for non-admins)."""
    chat, user, msg = update.effective_chat, update.effective_user, update.effective_message
    if chat is None or user is None or msg is None or not msg.text:
        return
    app = get_app(context)
    try:
        app.perms.require_admin_context(chat.type, user.id)
    except PermissionDenied:
        return
    flow = context.user_data.get("flow")
    if not flow:
        return
    text = msg.text.strip()
    try:
        if flow["kind"] == "create":
            await create_text(update, context, app, flow, text)
        elif flow["kind"] == "result":
            await result_text(update, context, app, flow, text)
        else:
            await simple_text(update, context, app, flow, text)
    except ServiceError as exc:
        await reply(update, f"⚠️ {e(exc.message)}\nTry again, or tap Cancel.", kb.cancel_flow())


async def simple_text(update, context, app: App, flow: dict, text: str) -> None:
    keys = flow["keys"]
    flow["data"][keys[len(flow["data"])]] = text
    if len(flow["data"]) < len(keys):
        nxt = keys[len(flow["data"])]
        await reply(update, SIMPLE_FLOWS[flow["kind"]][1][nxt], kb.cancel_flow())
        return
    data, mid, admin = flow["data"], flow["mid"], _uid(update)
    context.user_data.pop("flow", None)
    kind = flow["kind"]
    try:
        await FINALS[kind](update, context, app, flow, data, mid, admin)
    except ServiceError as exc:
        await reply(update, f"⚠️ {e(exc.message)}", kb.with_back([], [B("↩️ Match", callback_data=f"adm:m:{mid}")] if mid else None))


def _need_int(text: str, what: str) -> int:
    if not text.strip().isdigit():
        raise ServiceError(f"{what} must be a whole number.")
    return int(text.strip())


async def f_search(update, context, app, flow, d, mid, admin):
    regs = app.rs.search(mid, d["term"])
    lines = [f"🔎 <b>{len(regs)} result(s)</b>"] + [f"Slot {r['slot']} · {e(r['ign'])} · <code>{r['uid']}</code>" for r in regs]
    await reply(update, "\n".join(lines), kb.with_back([], [B("↩️ Players", callback_data=f"adm:pm:{mid}")]))


async def f_rmplayer(update, context, app, flow, d, mid, admin):
    r = app.rs.find_by_uid(mid, d["uid"])
    if not r:
        raise ServiceError("No player with that UID in this match.")
    await reply(update, f"⚠️ Remove <b>{e(r['ign'])}</b> (UID {r['uid']}, slot {r['slot']}) from match #{mid}? Their slot will be freed.",
                kb.confirm(f"adm:y:rmp:{mid}:{r['id']}"))


async def f_assign(update, context, app, flow, d, mid, admin):
    r = app.rs.find_by_uid(mid, d["uid"])
    if not r:
        raise ServiceError("No player with that UID in this match.")
    app.rs.admin_set_slot(admin, mid, r["id"], _need_int(d["slot"], "Slot"))
    await reply(update, f"✅ {e(r['ign'])} is now in slot {d['slot']}.", kb.with_back([], [B("↩️ Players", callback_data=f"adm:pm:{mid}")]))


async def f_mvteam(update, context, app, flow, d, mid, admin):
    r = app.rs.find_by_uid(mid, d["uid"])
    if not r:
        raise ServiceError("No player with that UID in this match.")
    team, _ = app.ts.admin_move(admin, mid, r["id"], _need_int(d["team"], "Team number"))
    await reply(update, f"✅ {e(r['ign'])} moved to {e(team['name'])}.", kb.with_back([], [B("↩️ Slots & Teams", callback_data="adm:st")]))


async def f_rmteam(update, context, app, flow, d, mid, admin):
    r = app.rs.find_by_uid(mid, d["uid"])
    if not r:
        raise ServiceError("No player with that UID in this match.")
    await reply(update, f"⚠️ Take <b>{e(r['ign'])}</b> out of their team?", kb.confirm(f"adm:y:rmt:{mid}:{r['id']}"))


async def f_delres(update, context, app, flow, d, mid, admin):
    await reply(update, f"⚠️ Delete the result of <b>{e(d['token'])}</b> in match #{mid}?", kb.confirm(f"adm:y:delres:{mid}:{d['token'][:20]}"))


async def f_pkills(update, context, app, flow, d, mid, admin):
    name = app.res.save_player_kills(admin, mid, d["uid"], _need_int(d["kills"], "Kills"))
    await reply(update, f"🎯 Saved {d['kills']} kill(s) for {e(name)}.", kb.with_back([], [B("↩️ Results", callback_data=f"adm:rm:{mid}")]))


async def f_addadmin(update, context, app, flow, d, mid, admin):
    app.perms.add_admin(admin, _need_int(d["id"], "User ID"))
    await reply(update, f"✅ User {d['id']} is now an admin.", kb.with_back([], [B("🛡 Admins", callback_data="adm:ad")]))


async def f_rmadmin(update, context, app, flow, d, mid, admin):
    await reply(update, f"⚠️ Remove admin {e(d['id'])}?", kb.confirm(f"adm:y:rmadm:{_need_int(d['id'], 'User ID')}"))


async def f_setval(update, context, app, flow, d, mid, admin):
    msg = _apply_setting(app, admin, mid, flow["field"], d["value"])
    await refresh_announcements(context.bot, app, mid)  # capacity/mode/time changes show up everywhere
    await reply(update, msg, kb.with_back([[B("⚙️ Back to settings", callback_data=f"adm:set:{mid}")]]))


FINALS = {"search": f_search, "rmplayer": f_rmplayer, "assign": f_assign, "mvteam": f_mvteam, "rmteam": f_rmteam,
          "delres": f_delres, "pkills": f_pkills, "addadmin": f_addadmin, "rmadmin": f_rmadmin, "setval": f_setval}


# =====================================================================
# Confirmations for destructive actions: adm:c:<action>:<args> asks, adm:y:<action>:<args> executes
# =====================================================================
ASK = {
    "endm": "🏁 End match #{0}? Registration and result entry rules will change.",
    "cancelm": "❌ Cancel match #{0}? This cannot be undone.",
    "pub": "📢 Publish the current results of match #{0} to its group?",
}


async def r_confirm_ask(update, context, app, args):
    action = args[0]
    if action == "pub":
        m = app.ms.get(int(args[1]))
        await show(update, live_text(app, m) + "\n\n" + ASK["pub"].format(m["id"]),
                   kb.confirm(f"adm:y:pub:{m['id']}", f"adm:m:{m['id']}", "📢 Publish"))
        return
    await show(update, ASK[action].format(args[1]), kb.confirm(f"adm:y:{action}:{args[1]}", f"adm:m:{args[1]}"))


async def r_confirm_yes(update, context, app, args):
    action, admin = args[0], _uid(update)
    back = lambda mid: kb.with_back([], [B("↩️ Match", callback_data=f"adm:m:{mid}")])  # noqa: E731
    if action in ("endm", "cancelm"):
        m = app.ms.transition(admin, int(args[1]), "end" if action == "endm" else "cancel")
        await show(update, f"✅ <b>{e(m['name'])}</b> → {STATUS_LABEL[m['status']]}", back(m["id"]))
        await after_status_change(context.bot, app, m)
    elif action == "rmp":
        r = app.rs.admin_remove(admin, int(args[1]), int(args[2]))
        await show(update, f"🗑 Removed {e(r['ign'])}; slot {r['slot']} is free again.", back(args[1]))
        await refresh_announcements(context.bot, app, int(args[1]))
    elif action == "rmt":
        r = app.ts.admin_remove(admin, int(args[1]), int(args[2]))
        await show(update, f"➖ {e(r['ign'])} is no longer in a team.", back(args[1]))
    elif action == "delres":
        label = app.res.delete(admin, int(args[1]), args[2])
        await show(update, f"🗑 Result of {e(label)} deleted. 🟠 Republish if it was already published.", back(args[1]))
    elif action == "pub":
        msg = await publish_results(context.bot, app, admin, int(args[1]))
        await show(update, msg, back(args[1]))
    elif action == "rmadm":
        app.perms.remove_admin(admin, int(args[1]))
        await show(update, f"✅ Admin {args[1]} removed.", kb.with_back([], [B("🛡 Admins", callback_data="adm:ad")]))
    else:
        await show(update, "Unknown action.", kb.with_back([]))


# =====================================================================
# Create-match wizard
# =====================================================================
CREATE_PROMPTS = {
    "name": "📝 <b>New match</b>\nSend the match name (e.g. Friday Night Custom):",
    "team_count": "🧩 How many teams? (Custom mode: <code>0</code> for no teams)",
    "team_size": "👥 Players per team?",
    "capacity": "🔢 Maximum number of players (1-100)?",
    "time": "⏰ Start time in the match timezone: <code>YYYY-MM-DD HH:MM</code> (24-hour)",
    "tz": "🌍 Send the timezone (e.g. <code>Asia/Kolkata</code>) or tap the default:",
    "deadline": "⌛ Registration deadline <code>YYYY-MM-DD HH:MM</code>, or tap Skip:",
}
STRUCT = ("team_count", "team_size", "capacity")
PRESETS = {"duo": 2, "squad": 4}


def _steps(mode: str) -> list:
    core = {"solo": ["capacity"], "team": ["team_count", "team_size"],
            "custom": ["team_count", "team_size", "capacity"]}[mode]
    return core + ["time", "tz", "deadline"]


async def create_begin(update, context, app: App) -> None:
    groups = authorized_group_ids(app)
    if not groups:
        await show(update, "⚠️ No authorized groups yet. Add me to your group(s), send any command there, then authorize "
                           "each one under Panel → 📡 Groups. Matches are announced in ALL authorized groups.",
                   kb.with_back([[B("📡 Groups", callback_data="adm:gr")]]))
        return
    # `group` only records where the match was created; every authorized group shares the match.
    context.user_data["flow"] = {"kind": "create", "data": {"group": groups[0]}, "steps": [], "stage": "name"}
    await show(update, CREATE_PROMPTS["name"], kb.cancel_flow())


async def r_create_start(update, context, app, args):
    await create_begin(update, context, app)


def _flow(context, kind: str) -> dict:
    f = context.user_data.get("flow")
    if not f or f["kind"] != kind:
        raise ServiceError("That wizard has expired. Start again from the panel.")
    return f


async def r_create_cb(update, context, app, args):
    f = _flow(context, "create")
    what = args[0]
    if what == "g":
        raise ServiceError("Matches are announced in every authorized group; no group choice is needed.")
    elif what == "m":
        pick = args[1]
        if pick in PRESETS:  # Duo / Squad = team mode with a fixed team size; admin picks max players
            f["data"]["mode"], f["data"]["preset_size"] = "team", PRESETS[pick]
            f["steps"] = ["capacity", "time", "tz", "deadline"]
        elif pick in MODES:
            f["data"]["mode"], f["steps"] = pick, _steps(pick)
        else:
            raise ServiceError("Unknown mode.")
        f["stage"] = "steps"
        await create_next(update, context, app, f)
    elif what == "tz":
        f["data"]["tz"] = app.config.timezone
        await create_next(update, context, app, f)
    elif what == "nodl":
        f["data"]["deadline"] = None
        await create_next(update, context, app, f)
    elif what == "go":
        await create_commit(update, context, app, f, open_now=(len(args) > 1 and args[1] == "open"))


async def create_text(update, context, app: App, flow: dict, text: str) -> None:
    d = flow["data"]
    if flow["stage"] == "name":
        if not 2 <= len(text) <= 60:
            raise ServiceError("Match name must be 2-60 characters.")
        d["name"], flow["stage"] = text, "mode"
        await reply(update, f"Name: <b>{e(text)}</b>\n\n🎮 <b>Choose the mode:</b>",
                    InlineKeyboardMarkup([[B("Solo", callback_data="adm:cf:m:solo"), B("Duo", callback_data="adm:cf:m:duo"),
                                           B("Squad", callback_data="adm:cf:m:squad")],
                                          [B("Team (custom size)", callback_data="adm:cf:m:team"),
                                           B("Custom", callback_data="adm:cf:m:custom")],
                                          [B("✖️ Cancel", callback_data="adm:cancel")]]))
        return
    if flow["stage"] != "steps":
        raise ServiceError("Please use the buttons above, or tap Cancel.")
    step = next((s for s in flow["steps"] if s not in d), None)
    if step in STRUCT:
        d[step] = _need_int(text, "That")
    elif step == "time":
        parse_local_datetime(text, d.get("tz") or app.config.timezone)  # validate early
        d["time"] = text
    elif step == "tz":
        try:
            ZoneInfo(text)
        except (ZoneInfoNotFoundError, ValueError):
            raise ServiceError("Unknown timezone. Example: Asia/Kolkata")
        d["tz"] = text
    elif step == "deadline":
        parse_local_datetime(text, d.get("tz") or app.config.timezone)
        d["deadline"] = text
    await create_next(update, context, app, flow)


async def create_next(update, context, app: App, flow: dict) -> None:
    d = flow["data"]
    size = d.get("preset_size")
    if size and "capacity" in d and "team_count" not in d:  # Duo/Squad: capacity must fit whole teams
        cap = d["capacity"]
        if cap < size or cap % size:
            d.pop("capacity")
            await reply(update, f"⚠️ Capacity must be a multiple of {size} (players per team), e.g. {size * 12}.")
        else:
            d["team_count"], d["team_size"] = cap // size, size
    if all(k in d for k in STRUCT if k in flow["steps"]) and any(k in flow["steps"] for k in STRUCT):
        from services.match_service import validate_config
        try:
            validate_config(d["mode"], d.get("capacity"), d.get("team_count", 0), d.get("team_size", 0))
        except ServiceError as exc:
            for k in STRUCT:
                d.pop(k, None)
            await reply(update, f"⚠️ {e(exc.message)}\nLet's re-enter the structure.")
    step = next((s for s in flow["steps"] if s not in d), None)
    if step is None:
        await create_preview(update, context, app, flow)
        return
    markup = kb.cancel_flow()
    if step == "capacity" and size:
        await reply(update, f"🔢 Maximum number of players (a multiple of {size}, up to 100):", markup)
        return
    if step == "tz":
        markup = InlineKeyboardMarkup([[B(f"Use {app.config.timezone}", callback_data="adm:cf:tz")], [B("✖️ Cancel", callback_data="adm:cancel")]])
    elif step == "deadline":
        markup = InlineKeyboardMarkup([[B("Skip (no deadline)", callback_data="adm:cf:nodl")], [B("✖️ Cancel", callback_data="adm:cancel")]])
    await reply(update, CREATE_PROMPTS[step], markup)


async def create_preview(update, context, app: App, flow: dict) -> None:
    d = flow["data"]
    tz = d.get("tz") or app.config.timezone
    d["tz"] = tz
    cap_txt = d.get("capacity") or (d.get("team_count", 0) * d.get("team_size", 0))
    label = {2: "duo", 4: "squad"}.get(d.get("preset_size"), d["mode"])
    text = (f"🔎 <b>Review new match</b>\n\nName: {e(d['name'])}\nAnnounced in: all {len(authorized_group_ids(app))} authorized group(s)\nMode: {label}\n"
            f"Teams: {d.get('team_count', 0)} x {d.get('team_size', 0)}\nCapacity: {cap_txt}\n"
            f"Start: {e(d['time'])} ({e(tz)})\nDeadline: {e(d.get('deadline') or 'none')}")
    await reply(update, text, InlineKeyboardMarkup([
        [B("✅ Create & open registration", callback_data="adm:cf:go:open")],
        [B("📝 Create as draft", callback_data="adm:cf:go:draft")],
        [B("✖️ Cancel", callback_data="adm:cancel")]]))


async def create_commit(update, context, app: App, flow: dict, open_now: bool = False) -> None:
    d = flow["data"]
    mid = app.ms.create_match(_uid(update), d["group"], d["name"], d["mode"], d["time"], d["tz"], d.get("capacity"),
                              d.get("team_count", 0), d.get("team_size", 0), d.get("deadline"))
    if open_now:
        app.ms.transition(_uid(update), mid, "open")
    context.user_data.pop("flow", None)
    context.user_data["sel_match"] = mid
    delivered = await announce_new_match(context.bot, app, mid)  # every authorized group, one shared match
    ok = sum(1 for v in delivered.values() if v)
    bad = [str(c) for c, v in delivered.items() if not v]
    note = f"\n📣 Announced in {ok}/{len(delivered)} group(s)."
    if bad:
        note += "\n⚠️ Failed (bot removed/blocked or no permission): " + ", ".join(bad)
    status = "Registration open" if open_now else "Draft"
    rows = [] if open_now else [[B("🟢 Open registration now", callback_data=f"adm:ms:{mid}:open")]]
    rows.append([B("📋 Match menu", callback_data=f"adm:m:{mid}")])
    await show(update, f"✅ Match <b>{e(d['name'])}</b> created as #{mid} ({status}) and selected.{note}", kb.with_back(rows))


# =====================================================================
# Result-entry wizard
# =====================================================================
async def start_result_flow(update, context, app: App, mid: int) -> None:
    m = app.ms.get(mid)
    if m["status"] not in ("in_progress", "completed"):
        raise ServiceError("Start the match first (/startmatch) — results are entered once it is in progress.")
    context.user_data["sel_match"] = mid
    context.user_data["flow"] = {"kind": "result", "mid": mid, "step": "pick", "data": {}}
    await result_picker(update, context, app, mid, 1)


async def r_result_start(update, context, app, args):
    await start_result_flow(update, context, app, int(args[0]))


async def result_picker(update, context, app: App, mid: int, page: int) -> None:
    ps = app.res.participants(mid)
    if not ps:
        raise ServiceError("There is nobody to enter results for yet (team matches need teams with players).")
    pages = max(1, -(-len(ps) // PER_PAGE_PICK))
    page = min(max(page, 1), pages)
    chunk = ps[(page - 1) * PER_PAGE_PICK: page * PER_PAGE_PICK]
    rows, row = [], []
    for p in chunk:
        row.append(B(("✅ " if p["done"] else "") + p["name"][:18], callback_data=f"adm:rsel:{mid}:{p['key']}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    pg = kb.pager(f"adm:rp:{mid}", page, pages)
    if pg:
        rows.append(pg)
    rows.append([B("👀 Preview leaderboard", callback_data=f"adm:lbp:{mid}"), B("📢 Publish", callback_data=f"adm:c:pub:{mid}")])
    done = sum(1 for p in ps if p["done"])
    await show(update, f"📝 <b>Submit result</b> — match #{mid}\nPick who to enter (✅ = already has a result; picking edits it). "
                       f"You can also type a UID / T#.\nEntered: {done}/{len(ps)}", kb.with_back(rows))


async def r_result_page(update, context, app, args):
    _flow(context, "result")
    await result_picker(update, context, app, int(args[0]), int(args[1]))


async def _result_choose(update, context, app: App, flow: dict, key: str) -> None:
    mid = flow["mid"]
    ps = {p["key"].lower(): p for p in app.res.participants(mid)}
    p = ps.get(key.lower())
    if not p:
        raise ServiceError("That participant is not in this match (use a UID, or T# for teams).")
    flow["data"] = {"key": p["key"], "name": p["name"], "done": p["done"]}
    flow["step"] = "placement"
    from services.result_service import max_placement
    await reply(update, f"🏅 <b>{e(p['name'])}</b> — send the <b>placement</b> (1-{max_placement(app.ms.get(mid))}):", kb.cancel_flow())


async def r_result_select(update, context, app, args):
    flow = _flow(context, "result")
    await _result_choose(update, context, app, flow, args[1])


async def result_text(update, context, app: App, flow: dict, text: str) -> None:
    step, d = flow["step"], flow["data"]
    if step == "pick":
        await _result_choose(update, context, app, flow, text)
    elif step == "placement":
        d["placement"] = _need_int(text, "Placement")
        flow["step"] = "kills"
        await reply(update, f"💥 How many <b>kills</b> did {e(d['name'])} get?", kb.cancel_flow())
    elif step == "kills":
        d["kills"] = _need_int(text, "Kills")
        score = app.res.preview(flow["mid"], d["key"], d["placement"], d["kills"])["score"]
        flow["step"] = "review"
        note = "\n⚠️ This replaces the existing result." if d["done"] else ""
        await reply(update, f"🔎 <b>Review</b>\n{e(d['name'])}: placement {d['placement']}, {d['kills']} kills\n"
                            f"Placement pts {score.placement_points} + kill pts {score.kill_points} = <b>{score.total}</b>{note}",
                    kb.confirm("adm:rsave", "adm:rdiscard", "💾 Save"))
    else:
        raise ServiceError("Use the buttons, or tap Cancel.")


async def r_result_save(update, context, app, args):
    flow = _flow(context, "result")
    d = flow["data"]
    if flow["step"] != "review":
        raise ServiceError("Nothing to save.")
    label, score = app.res.save(_uid(update), flow["mid"], d["key"], d["placement"], d["kills"], overwrite=d["done"])
    flow["step"], flow["data"] = "pick", {}
    mid = flow["mid"]
    await show(update, f"💾 Saved <b>{e(label)}</b>: {score.total} pts.",
               kb.with_back([[B("➕ Add another", callback_data=f"adm:rp:{mid}:1")],
                             [B("👀 Preview leaderboard", callback_data=f"adm:lbp:{mid}"), B("📢 Publish", callback_data=f"adm:c:pub:{mid}")]]))


async def r_result_discard(update, context, app, args):
    flow = _flow(context, "result")
    flow["step"], flow["data"] = "pick", {}
    await result_picker(update, context, app, flow["mid"], 1)


# =====================================================================
# Groups and admins
# =====================================================================
async def r_groups(update, context, app, args):
    from database import repositories as repo
    with app.db.read() as conn:
        groups = repo.list_groups(conn)
    owner = app.perms.is_owner(_uid(update))
    lines = ["📡 <b>Groups</b>", "Add the bot to a group and send any command there; it will appear here.", ""]
    rows = []
    for g in groups:
        lines.append(f"{'✅' if g['authorized'] else '⏳'} {e(g['title'] or 'Untitled')} — <code>{g['chat_id']}</code>")
        if owner:
            rows.append([B(("Revoke: " if g["authorized"] else "Authorize: ") + (g["title"] or str(g["chat_id"]))[:28],
                           callback_data=f"adm:gt:{g['chat_id']}")])
    if not groups:
        lines.append("None yet.")
    if not owner:
        lines.append("\nOnly the owner can authorize groups.")
    await show(update, "\n".join(lines), kb.with_back(rows))


async def r_group_toggle(update, context, app, args):
    from database import repositories as repo
    app.perms.require_owner_context(update.effective_chat.type, _uid(update))
    cid = int(args[0])
    with app.db.transaction() as conn:
        g = repo.get_group(conn, cid)
        if not g:
            raise ServiceError("Group not found.")
        repo.upsert_group(conn, cid, g["title"], not g["authorized"])
        repo.add_audit(conn, _uid(update), None, "group_authorize" if not g["authorized"] else "group_revoke", str(cid))
    await r_groups(update, context, app, [])


async def r_admins(update, context, app, args):
    app.perms.require_owner_context(update.effective_chat.type, _uid(update))
    a = app.perms.list_all_admins()
    text = (f"🛡 <b>Admins</b>\nOwner: <code>{a['owner']}</code>\nFrom ADMIN_IDS: {', '.join(map(str, a['env'])) or '—'}\n"
            f"Added in bot: {', '.join(map(str, a['db'])) or '—'}")
    rows = [[B("➕ Add admin", callback_data="adm:fl:addadmin:0"), B("➖ Remove admin", callback_data="adm:fl:rmadmin:0")]]
    await show(update, text, kb.with_back(rows))


async def track_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """When the bot is added to a group, remember it (unauthorized) and tell the owner."""
    from database import repositories as repo
    ev = update.my_chat_member
    if ev is None or ev.chat.type not in ("group", "supergroup"):
        return
    if ev.new_chat_member.status not in ("member", "administrator"):
        return
    app = get_app(context)
    with app.db.transaction() as conn:
        repo.upsert_group(conn, ev.chat.id, ev.chat.title or "")
    try:
        await context.bot.send_message(app.config.owner_id, f"➕ I was added to <b>{e(ev.chat.title or '')}</b> "
                                       f"(<code>{ev.chat.id}</code>). Authorize it in /admin → 📡 Groups.", parse_mode="HTML")
    except TelegramError:
        log.info("Could not notify owner about new group")


ROUTES = {
    "home": r_home, "noop": r_noop, "cancel": r_cancel,
    "cm": r_create_start, "cf": r_create_cb,
    "mm": r_match_list, "m": r_match, "sel": r_select, "ms": r_status_op,
    "mp": r_players_section, "pm": r_pm, "pl": r_player_list, "pe": r_export, "pst": r_stats,
    "st": r_slots_section, "sl": r_slots, "tm": r_teams,
    "sr": r_results_section, "rm": r_results_menu, "rstart": r_result_start, "rp": r_result_page,
    "rsel": r_result_select, "rsave": r_result_save, "rdiscard": r_result_discard,
    "lb": r_lb_section, "lbp": r_lb_preview, "lbc": r_lb_cumulative, "au": r_audit,
    "set": r_settings_section, "tg": r_toggle, "mode": r_mode_solo,
    "fl": r_flow_start, "fv": r_field_start,
    "c": r_confirm_ask, "y": r_confirm_yes,
    "gr": r_groups, "gt": r_group_toggle, "ad": r_admins,
}
