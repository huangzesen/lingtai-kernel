from lingtai_kernel.prompt import build_system_prompt
from lingtai_kernel.prompt import SystemPromptManager


def test_build_system_prompt_minimal():
    mgr = SystemPromptManager()
    prompt = build_system_prompt(mgr)
    # Without a registered manifesto, prompt is empty or just sections
    assert isinstance(prompt, str)


def test_build_system_prompt_with_sections():
    mgr = SystemPromptManager()
    mgr.write_section("role", "You are a test agent")
    mgr.write_section("memory", "Remember: user likes concise")
    prompt = build_system_prompt(mgr)
    assert "You are a test agent" in prompt
    assert "Remember: user likes concise" in prompt


def test_set_manifesto_and_get():
    from lingtai_kernel.prompt import set_manifesto, get_manifesto, _MANIFESTO_CACHE
    set_manifesto("test_lang", "Test manifesto content")
    assert get_manifesto("test_lang") == "Test manifesto content"
    # Clean up
    _MANIFESTO_CACHE.pop("test_lang", None)


def test_get_manifesto_unknown_returns_empty():
    from lingtai_kernel.prompt import get_manifesto, _MANIFESTO_CACHE
    # Clear cache to test fallback
    _MANIFESTO_CACHE.pop("nonexistent_lang", None)
    text = get_manifesto("nonexistent_lang")
    assert text == ""
    # Clean up
    _MANIFESTO_CACHE.pop("nonexistent_lang", None)
