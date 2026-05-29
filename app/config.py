"""Runtime configuration loaded from environment variables (defaults match docker-compose)."""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    mysql_host: str
    mysql_port: int
    mysql_user: str
    mysql_password: str
    mysql_db: str
    rabbitmq_url: str
    grummer_key_path: str
    sms_webhook_url: str


def load_settings() -> Settings:
    return Settings(
        mysql_host=os.getenv("MYSQL_HOST", "localhost"),
        mysql_port=int(os.getenv("MYSQL_PORT", "3306")),
        mysql_user=os.getenv("MYSQL_USER", "root"),
        mysql_password=os.getenv("MYSQL_PASSWORD", "root"),
        mysql_db=os.getenv("MYSQL_DB", "gex"),
        rabbitmq_url=os.getenv("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/"),
        grummer_key_path=os.getenv("GRUMMER_KEY_PATH", "docs/grummer_secret.txt"),
        sms_webhook_url=os.getenv("SMS_WEBHOOK_URL", ""),
    )
