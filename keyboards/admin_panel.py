"""Inline keyboards for the private admin panel."""
from __future__ import annotations

from typing import Iterable, List, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

B = InlineKeyboardButton
PANEL_TEXT = "🔥 <b>FREE FIRE ADMIN PANEL</b>\n\nChoose an option:"


def back_row() -> List[InlineKeyboardButton]:
    return [B("⬅️ Back to Panel", callback_data="adm:home")]


def panel(is_owner: bool) -> InlineKeyboardMarkup:
    rows = [
        [B("➕ Create Match", callback_data="adm:cm"), B("📋 Manage Matches", callback_data="adm:mm")],
        [B("👥 Manage Players", callback_data="adm:mp"), B("🎯 Slots and Teams", callback_data="adm:st")],
        [B("📝 Submit Results", callback_data="adm:sr"), B("🏆 Leaderboards", callback_data="adm:lb")],
        [B("⚙️ Match Settings", callback_data="adm:set"), B("📡 Groups", callback_data="adm:gr")],
    ]
    if is_owner:
        rows.append([B("🛡 Manage Admins", callback_data="adm:ad")])
    return InlineKeyboardMarkup(rows)


def with_back(rows: Iterable[List[InlineKeyboardButton]], extra_back: Optional[List] = None) -> InlineKeyboardMarkup:
    out = [list(r) for r in rows]
    if extra_back:
        out.append(extra_back)
    out.append(back_row())
    return InlineKeyboardMarkup(out)


def confirm(yes_data: str, no_data: str = "adm:home", yes_label: str = "✅ Yes, do it") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[B(yes_label, callback_data=yes_data), B("✖️ No", callback_data=no_data)]])


def cancel_flow() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[B("✖️ Cancel", callback_data="adm:cancel")]])


def pager(prefix: str, page: int, pages: int) -> Optional[List[InlineKeyboardButton]]:
    if pages <= 1:
        return None
    row = []
    if page > 1:
        row.append(B("◀️", callback_data=f"{prefix}:{page - 1}"))
    row.append(B(f"{page}/{pages}", callback_data="adm:noop"))
    if page < pages:
        row.append(B("▶️", callback_data=f"{prefix}:{page + 1}"))
    return row


def match_menu(m) -> InlineKeyboardMarkup:
    mid, st = m["id"], m["status"]
    ops = []
    if st in ("draft", "registration_closed"):
        ops.append(B("🟢 Open registration", callback_data=f"adm:ms:{mid}:open"))
    if st == "registration_open":
        ops.append(B("🔒 Close registration", callback_data=f"adm:ms:{mid}:close"))
    if st in ("draft", "registration_open", "registration_closed"):
        ops.append(B("▶️ Start match", callback_data=f"adm:ms:{mid}:start"))
    if st in ("registration_closed", "in_progress"):
        ops.append(B("🏁 End match", callback_data=f"adm:c:endm:{mid}"))
    if st in ("draft", "registration_open", "registration_closed", "in_progress"):
        ops.append(B("❌ Cancel match", callback_data=f"adm:c:cancelm:{mid}"))
    rows = [ops[i:i + 2] for i in range(0, len(ops), 2)]
    rows += [
        [B("📌 Select as current", callback_data=f"adm:sel:{mid}:m")],
        [B("👥 Players", callback_data=f"adm:pm:{mid}"), B("🎯 Slots", callback_data=f"adm:sl:{mid}:1")],
        [B("🧩 Teams", callback_data=f"adm:tm:{mid}"), B("📝 Results", callback_data=f"adm:rm:{mid}")],
        [B("🏆 Leaderboard", callback_data=f"adm:lbp:{mid}"), B("⚙️ Settings", callback_data=f"adm:set:{mid}")],
        [B("📜 Audit log", callback_data=f"adm:au:{mid}")],
    ]
    return with_back(rows, [B("📋 All matches", callback_data="adm:mm")])
