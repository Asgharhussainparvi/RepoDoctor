import pytest

from repodoctor.api.settings import ApiSettings


def test_defaults_are_local_only():
    s = ApiSettings()
    assert (
        s.host == "127.0.0.1" and s.engine == "bedrock" and s.max_active_sessions == 2
    )
    assert "http://localhost:5173" in s.cors_origins and "localhost" in s.allowed_hosts


def test_from_env_overrides_and_lists():
    s = ApiSettings.from_env(
        {
            "REPODOCTOR_PORT": "9000",
            "REPODOCTOR_ENGINE": "demo",
            "REPODOCTOR_CORS_ORIGINS": "http://a.test, http://b.test",
            "REPODOCTOR_DATA_DIR": "x/y",
        }
    )
    assert (s.port, s.engine, s.cors_origins, str(s.data_dir)) == (
        9000,
        "demo",
        ("http://a.test", "http://b.test"),
        "x\\y" if "\\" in str(s.data_dir) else "x/y",
    )


@pytest.mark.parametrize(
    "env",
    [
        {"REPODOCTOR_PORT": "web"},
        {"REPODOCTOR_ENGINE": "magic"},
        {"REPODOCTOR_MAX_ACTIVE_SESSIONS": "0"},
    ],
)
def test_bad_values_are_rejected(env):
    with pytest.raises(ValueError):
        ApiSettings.from_env(env)
