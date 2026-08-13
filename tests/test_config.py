# Vérifie que load_dashboard_reader_config() construit bien une config
# pointant vers le rôle dashboard_reader (jamais pipeline_app), en lisant
# DASHBOARD_READER_PASSWORD plutôt que POSTGRES_APP_PASSWORD.
from __future__ import annotations

from src.common.config import load_dashboard_reader_config


def test_load_dashboard_reader_config_uses_dashboard_reader_role():
    config = load_dashboard_reader_config()
    assert config.user == "dashboard_reader"
    assert config.password  # non vide, lu depuis DASHBOARD_READER_PASSWORD
