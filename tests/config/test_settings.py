"""Tests for config.build_mem0_config() across EXTRACTION_MODE settings.

No live network/LLM calls: this only checks the shape of the config dict
that gets handed to mem0.Memory.from_config, for each of the three supported
extraction modes (anthropic / ollama / none).
"""

import importlib

import pytest

MODE_EXPECTATIONS = {
    "anthropic": {"llm_provider": "anthropic", "infer": True},
    "ollama": {"llm_provider": "ollama", "infer": True},
    "none": {"llm_provider": "openai", "infer": False},
}


@pytest.fixture
def config_module(monkeypatch):
    """Yield the context_layer.config module, reloaded fresh after the test
    so a later test (or the module cache) never sees a mode this test set."""
    from context_layer import config

    yield config

    # Restore the session baseline before reloading. NOT delenv+reload: config
    # defaults EXTRACTION_MODE to "anthropic" when the var is unset, so a
    # deleted-then-reloaded config would leave EXTRACTION_MODE="anthropic" and
    # leak infer=True into later tests (e.g. the real-store isolation test).
    # conftest pins the suite baseline to "none"; put config back there.
    monkeypatch.setenv("EXTRACTION_MODE", "none")
    importlib.reload(config)


@pytest.mark.parametrize("mode", ["anthropic", "ollama", "none"])
def test_build_mem0_config_shape_per_extraction_mode(monkeypatch, config_module, mode):
    monkeypatch.setenv("EXTRACTION_MODE", mode)
    importlib.reload(config_module)

    cfg = config_module.build_mem0_config()
    expected = MODE_EXPECTATIONS[mode]

    assert cfg["llm"]["provider"] == expected["llm_provider"]
    assert config_module.infer_enabled() is expected["infer"]

    # embedder + vector_store are independent of extraction mode and should
    # always be well-formed local (chroma/fastembed) defaults.
    assert cfg["embedder"]["provider"] == "fastembed"
    assert cfg["embedder"]["config"]["embedding_dims"] == 384
    assert cfg["vector_store"]["provider"] == "chroma"
    assert "path" in cfg["vector_store"]["config"]

    if mode == "anthropic":
        assert cfg["llm"]["config"]["model"] == config_module.ANTHROPIC_MODEL
    elif mode == "ollama":
        assert cfg["llm"]["config"]["model"] == config_module.OLLAMA_MODEL
    else:
        # none: the LLM is never actually invoked (infer=False), but the
        # block must still construct without requiring a real API key.
        assert "api_key" in cfg["llm"]["config"]


def test_build_mem0_config_rejects_invalid_extraction_mode(monkeypatch, config_module):
    monkeypatch.setenv("EXTRACTION_MODE", "not-a-real-mode")
    importlib.reload(config_module)

    with pytest.raises(ValueError, match="EXTRACTION_MODE"):
        config_module.build_mem0_config()
