"""sites.yaml parsing and env-driven settings."""

from __future__ import annotations

import pytest

from site_monitor.config import ConfigError, Settings, load_sites


def write(tmp_path, text: str):
    path = tmp_path / "sites.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_loads_sites_with_defaults(tmp_path):
    path = write(
        tmp_path,
        """
sites:
  - domain: dvlfirm.com
    sitemap: https://dvlfirm.com/sitemap_index.xml
  - domain: example.com
    sitemap: https://example.com/wp-sitemap.xml
    max_pages: 50
    enabled: false
""",
    )

    sites = load_sites(path)

    assert [site.domain for site in sites] == ["dvlfirm.com", "example.com"]
    assert sites[0].enabled is True
    assert sites[0].max_pages is None
    assert sites[1].enabled is False
    assert sites[1].max_pages == 50


def test_top_level_list_is_also_accepted(tmp_path):
    path = write(
        tmp_path,
        "- domain: a.com\n  sitemap: https://a.com/sitemap.xml\n",
    )

    assert load_sites(path)[0].domain == "a.com"


@pytest.mark.parametrize(
    "text, message",
    [
        ("sites: []", "no sites defined"),
        ("sites:\n  - sitemap: https://a.com/s.xml", "missing 'domain'"),
        ("sites:\n  - domain: a.com", "missing 'sitemap'"),
        ("sites:\n  - domain: a.com\n    sitemap: /s.xml", "absolute URL"),
        (
            "sites:\n  - domain: a.com\n    sitemap: https://a.com/s.xml\n"
            "  - domain: a.com\n    sitemap: https://a.com/t.xml",
            "duplicate site",
        ),
    ],
)
def test_invalid_configs_are_rejected_with_a_useful_message(tmp_path, text, message):
    path = write(tmp_path, text)

    with pytest.raises(ConfigError) as info:
        load_sites(path)

    assert message in str(info.value)


def test_missing_file_names_the_example(tmp_path):
    with pytest.raises(ConfigError) as info:
        load_sites(tmp_path / "nope.yaml")

    assert "sites.example.yaml" in str(info.value)


def test_settings_read_from_env_file(tmp_path):
    sites = write(
        tmp_path, "sites:\n  - domain: a.com\n    sitemap: https://a.com/s.xml\n"
    )
    env = tmp_path / ".env"
    env.write_text(
        f"SITES_FILE={sites}\n"
        f"DATABASE_PATH={tmp_path / 'db.sqlite'}\n"
        "PAGE_CONCURRENCY=4\n"
        "RETRY_BACKOFF=0.25\n"
        "TELEGRAM_BOT_TOKEN=token\n"
        "TELEGRAM_CHAT_ID=-100123\n",
        encoding="utf-8",
    )
    settings = Settings.from_env(env)

    assert settings.page_concurrency == 4
    assert settings.retry_backoff == 0.25
    assert settings.telegram_enabled
    assert [site.domain for site in settings.sites] == ["a.com"]


def test_non_numeric_env_value_is_rejected(tmp_path, monkeypatch):
    sites = write(
        tmp_path, "sites:\n  - domain: a.com\n    sitemap: https://a.com/s.xml\n"
    )
    monkeypatch.setenv("SITES_FILE", str(sites))
    monkeypatch.setenv("PAGE_CONCURRENCY", "many")

    with pytest.raises(ConfigError):
        Settings.from_env(None)


def test_telegram_disabled_when_credentials_missing():
    assert Settings().telegram_enabled is False
