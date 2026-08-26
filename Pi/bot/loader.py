"""Dynamic module loader.

Every file inside bot/modules/ is auto-discovered, imported and
registered via its setup(app) function. Drop a new module in the
folder and it loads automatically — no wiring needed.
"""

import importlib
import pkgutil

from telegram.ext import Application

from bot import modules
from bot.logger import log_load, logger


def load_modules(app: Application) -> int:
    """Import every module in bot/modules and call its setup(app).

    Each setup() must return a list of route descriptions (strings)
    which are printed in the startup log.
    """
    loaded = 0
    for module_info in sorted(pkgutil.iter_modules(modules.__path__), key=lambda m: m.name):
        module = importlib.import_module(f"bot.modules.{module_info.name}")

        if not hasattr(module, "setup"):
            logger.warning(f"module '{module_info.name}' has no setup() — skipped")
            continue

        routes = module.setup(app)
        log_load(logger, f"{module_info.name:<12} → {', '.join(routes)}")
        loaded += 1

    return loaded