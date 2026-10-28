import importlib.util
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy.engine import make_url


CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.py"


def load_config(database_url):
    environment = {"BOT_TOKEN": "test-token"}
    if database_url is not None:
        environment["DB_URL"] = database_url
    spec = importlib.util.spec_from_file_location("test_bot_config", CONFIG_PATH)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(os.environ, environment, clear=True), patch("dotenv.load_dotenv"):
        spec.loader.exec_module(module)
    return module


class ConfigTests(unittest.TestCase):
    def test_relative_database_is_the_same_from_another_working_directory(self):
        original = Path.cwd()
        try:
            os.chdir(Path(__file__).resolve().parent)
            config = load_config("sqlite+aiosqlite:///talaba_bot.db")
            database = Path(make_url(config.DB_URL).database)
            self.assertEqual(database, CONFIG_PATH.parent / "talaba_bot.db")
            self.assertTrue(database.is_absolute())
        finally:
            os.chdir(original)

    def test_missing_database_url_uses_existing_bot_database_location(self):
        config = load_config(None)
        self.assertEqual(
            Path(make_url(config.DB_URL).database),
            CONFIG_PATH.parent / "talaba_bot.db",
        )

    def test_explicit_memory_database_remains_in_memory(self):
        config = load_config("sqlite+aiosqlite:///:memory:")
        self.assertEqual(config.DB_URL, "sqlite+aiosqlite:///:memory:")


if __name__ == "__main__":
    unittest.main()
