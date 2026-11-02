"""Private bot access requests and administrator management."""

import os
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

from bot.scanner import Scanner


def parse_ids(value: str) -> set[int]:
    parts = [part for part in re.split(r"[,\s]+", value.strip()) if part]
    if any(not part.isdecimal() or int(part) <= 0 for part in parts):
        raise ValueError("Admin and whitelist IDs must be positive Telegram user IDs")
    return {int(part) for part in parts}


class AccessControl:
    def __init__(self, scanner: Scanner) -> None:
        self.scanner = scanner
        self.configured_admins = parse_ids(os.getenv("TELEGRAM_ADMIN_IDS", ""))
        self.initial_ids = parse_ids(os.getenv("TELEGRAM_WHITELIST_IDS", ""))

    def admin_ids(self) -> set[int]:
        return self.configured_admins | self.scanner.state.admin_ids()

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.configured_admins or self.scanner.state.is_admin(user_id)

    def is_allowed(self, user_id: int) -> bool:
        if self.is_admin(user_id):
            return True
        override = self.scanner.state.access_override(user_id)
        return override if override is not None else user_id in self.initial_ids

    async def guard(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user, chat = update.effective_user, update.effective_chat
        if user is None or chat is None:
            raise ApplicationHandlerStop
        if chat.type != "private":
            if update.message:
                await update.message.reply_text("Please use this bot in a private chat.")
            elif update.callback_query:
                await update.callback_query.answer("Please use this bot in a private chat.", show_alert=True)
            raise ApplicationHandlerStop
        if update.message and self.scanner.state.claim_first_admin(user.id, bool(self.configured_admins)):
            await update.message.reply_text(
                "You are the first user of this bot, so you are now an admin. "
                "Use /requests to review access requests and /promote <user ID> to add admins."
            )
        command = ""
        if update.message and update.message.text:
            command = update.message.text.split()[0].split("@")[0]
        if command == "/id":
            await update.message.reply_text(f"Your Telegram user ID: {user.id}")
            raise ApplicationHandlerStop
        if self.is_allowed(user.id):
            return
        if command == "/request":
            await self.request_command(update, context)
        elif update.message:
            await update.message.reply_text(
                "This bot requires approval. Send /request followed by your name, "
                "for example: /request Jane Smith. An admin will review it."
            )
        elif update.callback_query:
            await update.callback_query.answer("Access requires approval. Send /request with your name.", show_alert=True)
        raise ApplicationHandlerStop

    async def request_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        name = update.message.text.partition(" ")[2].strip()
        if not name or len(name) > 80 or "\n" in name or "\r" in name:
            await update.message.reply_text("Send your name like this: /request Jane Smith")
            return
        user_id = update.effective_user.id
        self.scanner.state.request_access(user_id, name)
        await update.message.reply_text("Your access request was sent to the admins.")
        await self._notify_admins(context, user_id, name)

    async def _notify_admins(self, context: ContextTypes.DEFAULT_TYPE, user_id: int, name: str) -> None:
        buttons = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=f"access_approve:{user_id}"),
            InlineKeyboardButton("❌ Deny", callback_data=f"access_deny:{user_id}"),
        ]])
        for admin_id in self.admin_ids():
            try:
                await context.bot.send_message(
                    admin_id, f"Access request from {name}\nTelegram ID: {user_id}",
                    reply_markup=buttons,
                )
            except Exception:
                # Admin may not have opened the bot yet; /requests still lists it.
                pass

    async def request_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not self.is_admin(update.effective_user.id):
            await query.answer("Only admins can review requests.", show_alert=True)
            return
        action, raw_id = query.data.split(":", 1)
        user_id = int(raw_id)
        approved = action == "access_approve"
        request = self.scanner.state.access_request(user_id)
        if not request or not self.scanner.state.resolve_request(user_id, approved):
            await query.answer("This request was already handled.", show_alert=True)
            return
        await query.answer()
        await query.edit_message_text(
            f"{'Approved' if approved else 'Denied'} {request[0]} (ID {user_id})."
        )
        try:
            await context.bot.send_message(
                user_id,
                "Your access request was approved. Send /start to use the bot."
                if approved else "Your access request was denied.",
            )
        except Exception:
            pass

    async def admin_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.is_admin(update.effective_user.id):
            await update.message.reply_text("Only admins can manage access.")
            return
        command = update.message.text.split()[0].split("@")[0]
        if command == "/requests":
            pending = self.scanner.state.pending_requests()
            await update.message.reply_text(
                "Pending requests:\n" + "\n".join(
                    f"{name} — {user_id} (approve: /allow {user_id})"
                    for user_id, name in pending
                ) if pending else "No pending requests."
            )
            return
        if command == "/whitelist":
            overrides = self.scanner.state.access_overrides()
            allowed = (self.initial_ids | {uid for uid, value in overrides.items() if value}) - {
                uid for uid, value in overrides.items() if not value
            }
            await update.message.reply_text(
                "Admins: " + (", ".join(map(str, sorted(self.admin_ids()))) or "none") + "\n"
                "Allowed users: " + (", ".join(map(str, sorted(allowed))) or "none")
            )
            return
        if len(context.args) != 1 or not context.args[0].isdecimal() or int(context.args[0]) <= 0:
            await update.message.reply_text(f"Usage: {command} <Telegram user ID>")
            return
        user_id = int(context.args[0])
        if command == "/promote":
            if not self.is_allowed(user_id):
                await update.message.reply_text("Approve this user before promoting them.")
                return
            self.scanner.state.set_admin(user_id, True)
            await update.message.reply_text(f"Promoted {user_id} to admin.")
            return
        if command == "/demote":
            if user_id in self.configured_admins:
                await update.message.reply_text("Remove this admin from TELEGRAM_ADMIN_IDS in .env.")
                return
            if not self.scanner.state.is_admin(user_id):
                await update.message.reply_text("That user is not an admin.")
                return
            if len(self.admin_ids()) <= 1:
                await update.message.reply_text("Promote another admin first.")
                return
            self.scanner.state.set_admin(user_id, False)
            await update.message.reply_text(f"Demoted {user_id} to an allowed user.")
            return
        if command == "/deny" and self.is_admin(user_id):
            await update.message.reply_text("Demote this admin before removing access.")
            return
        allowed = command == "/allow"
        self.scanner.state.set_access(user_id, allowed)
        if allowed:
            self.scanner.state.resolve_request(user_id, True)
        else:
            self.scanner.state.stop_search(user_id)
            for suffix in ("", "_once"):
                for job in context.job_queue.get_jobs_by_name(f"scan_{user_id}{suffix}"):
                    job.schedule_removal()
        await update.message.reply_text(f"{'Allowed' if allowed else 'Removed'} user {user_id}.")
        try:
            await context.bot.send_message(
                user_id,
                "Your access was approved. Send /start to use the bot."
                if allowed else "Your bot access was removed.",
            )
        except Exception:
            pass
