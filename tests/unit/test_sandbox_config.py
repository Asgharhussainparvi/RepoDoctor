import pytest

from repodoctor.sandbox.config import SandboxConfig


def test_defaults_are_valid():
    cfg = SandboxConfig()
    assert cfg.memory == "512m" and cfg.default_timeout_seconds == 60


def test_from_env_overrides_and_ignores_blank():
    cfg = SandboxConfig.from_env(
        {"REPODOCTOR_SANDBOX_MEMORY": "256m", "REPODOCTOR_TEST_TIMEOUT_SECONDS": "30", "REPODOCTOR_SANDBOX_IMAGE": ""}
    )
    assert (cfg.memory, cfg.default_timeout_seconds, cfg.image) == ("256m", 30, "repodoctor-sandbox:0.1")


def test_from_env_names_the_bad_variable():
    with pytest.raises(ValueError, match="REPODOCTOR_TEST_TIMEOUT_SECONDS"):
        SandboxConfig.from_env({"REPODOCTOR_TEST_TIMEOUT_SECONDS": "soon"})


@pytest.mark.parametrize(
    "kwargs",
    [
        {"memory": "lots"},
        {"memory": "0m"},
        {"image": "--privileged"},
        {"image": "bad image"},
        {"cpus": 0},
        {"cpus": 64},
        {"pids_limit": 1},
        {"default_timeout_seconds": 0},
        {"default_timeout_seconds": 100, "max_timeout_seconds": 50},
        {"max_output_bytes": 10},
    ],
)
def test_invalid_config_rejected(kwargs):
    with pytest.raises(ValueError):
        SandboxConfig(**kwargs)
