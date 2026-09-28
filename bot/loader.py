"""Dynamic module loader.

Every file inside bot/modules/ is auto-discovered, imported and
registered via its setup() function (which queues handlers on
bot.pipeline). Drop a new module in the folder and it loads
automatically — no wiring needed.
"""

import importlib
import pkgutil

from bot import modules, pipeline
from bot.logger import log_load, logger


def load_modules() -> int:
    """Import every module in bot/modules and call its setup().

    Each setup() must return a list of route descriptions (strings)
    which are printed in the startup log.  The pipeline queue is
    cleared first so repeated calls (tests) never double-register.
    """
    pipeline.clear()
    loaded = 0
    for module_info in sorted(pkgutil.iter_modules(modules.__path__), key=lambda m: m.name):
        module = importlib.import_module(f"bot.modules.{module_info.name}")

        if not hasattr(module, "setup"):
            logger.warning(f"module '{module_info.name}' has no setup() — skipped")
            continue

        routes = module.setup()
        log_load(logger, f"{module_info.name:<12} → {', '.join(routes)}")
        loaded += 1

    return loaded
