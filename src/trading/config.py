from dataclasses import dataclass
import os
from dotenv import load_dotenv

load_dotenv()

@dataclass(frozen=True)
class Settings:
    env: str
    db_path: str

def get_settings() -> Settings:
    env = os.getenv("TRADING_ENV", "dev")
    db_path = os.getenv("TRADING_DB_PATH", "").strip()

    if not db_path:
        raise RuntimeError(
            "TRADING_DB_PATH is not set. Create a .env file and set an absolute path "
            "to your SQLite file outside the git repo."
        )
    
    return Settings(env=env, db_path=db_path)