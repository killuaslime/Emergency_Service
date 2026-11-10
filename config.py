import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("MAX_BOT_TOKEN")
GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID")

if not BOT_TOKEN:
    raise ValueError("Не указан MAX_BOT_TOKEN")