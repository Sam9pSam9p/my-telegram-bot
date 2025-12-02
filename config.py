import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("BOT_TOKEN")
MORALIS_API_KEY = os.getenv("MORALIS_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
API_REQUEST_TIMEOUT = 30
