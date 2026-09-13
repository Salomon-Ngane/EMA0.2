from telegram import InlineKeyboardButton, InlineKeyboardMarkup

def get_approval_keyboard(user_id: int, username: str):
    keyboard = [
        [InlineKeyboardButton("✅ Approuver (Free)", callback_data=f"allow_{user_id}_free_{username}")],
        [InlineKeyboardButton("🌟 Approuver (Premium)", callback_data=f"allow_{user_id}_premium_{username}")]
    ]
    return InlineKeyboardMarkup(keyboard)
