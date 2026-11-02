"""Telegram's built-in command menu for users and admins."""

from telegram import Bot, BotCommand, BotCommandScopeChat, MenuButtonCommands

USER_COMMANDS = [
    BotCommand("start", "Open the bot"),
    BotCommand("help", "Show available commands"),
    BotCommand("request", "Request access with your name"),
    BotCommand("id", "Show your Telegram user ID"),
    BotCommand("search", "Choose car search filters"),
    BotCommand("status", "Show your active search"),
    BotCommand("stop", "Stop your alerts"),
]

ADMIN_COMMANDS = USER_COMMANDS + [
    BotCommand("requests", "Show pending access requests"),
    BotCommand("whitelist", "Show allowed users and admins"),
    BotCommand("allow", "Approve a Telegram user ID"),
    BotCommand("deny", "Remove a user's access"),
    BotCommand("promote", "Make an approved user an admin"),
    BotCommand("demote", "Remove an admin role"),
]


async def configure_menu(bot: Bot, admin_ids: set[int]) -> None:
    await bot.set_my_commands(USER_COMMANDS)
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    for admin_id in admin_ids:
        await set_admin_menu(bot, admin_id, True)


async def set_admin_menu(bot: Bot, user_id: int, admin: bool) -> None:
    scope = BotCommandScopeChat(chat_id=user_id)
    if admin:
        await bot.set_my_commands(ADMIN_COMMANDS, scope=scope)
    else:
        await bot.delete_my_commands(scope=scope)
