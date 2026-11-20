"""鲜菜价格指数：Flask 应用工厂。"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from flask import Flask

from . import db
from .routes import bp


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=False)

    project_root = Path(app.root_path).parent
    default_db = Path(
        os.environ.get("DATABASE_PATH", str(project_root / "data" / "prices.db"))
    )

    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "local-dev-secret-change-me"),
        DATABASE=str(default_db),
        MAX_CONTENT_LENGTH=4 * 1024 * 1024,  # 4 MiB
        DEMO_ENABLED=os.environ.get("DEMO_ENABLED", "1") != "0",
    )
    if test_config:
        app.config.update(test_config)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    db.init_db(app.config["DATABASE"])
    app.register_blueprint(bp)
    return app
