from stoai_kernel.prompt import build_system_prompt
from stoai_kernel.prompt import SystemPromptManager


def test_build_system_prompt_minimal():
    mgr = SystemPromptManager()
    prompt = build_system_prompt(mgr)
    # Manifesto is always present
    assert "private" in prompt
    assert "tools" in prompt


def test_build_system_prompt_with_sections():
    mgr = SystemPromptManager()
    mgr.write_section("role", "You are a test agent")
    mgr.write_section("memory", "Remember: user likes concise")
    prompt = build_system_prompt(mgr)
    assert "You are a test agent" in prompt
    assert "Remember: user likes concise" in prompt


def test_get_manifesto_chinese():
    from stoai_kernel.prompt import get_manifesto
    text = get_manifesto("zh")
    assert "你的思维是私密的" in text

def test_get_manifesto_unknown_falls_back_to_en():
    from stoai_kernel.prompt import get_manifesto
    text = get_manifesto("xx")
    assert "Your mind is private" in text
