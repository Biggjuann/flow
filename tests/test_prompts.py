import pytest

from adengine.prompts import load_prompt, prompts_dir


def test_all_pipeline_prompts_exist():
    for name in ("extract_brand_dna", "generate_ads", "score_ad"):
        text = load_prompt(name)
        assert len(text) > 200


def test_template_substitution(tmp_path, monkeypatch):
    (tmp_path / "demo.md").write_text("Hello $name, braces {stay} intact")
    monkeypatch.setenv("ADENGINE_PROMPTS_DIR", str(tmp_path))
    assert prompts_dir() == tmp_path
    out = load_prompt("demo", name="world")
    assert out == "Hello world, braces {stay} intact"


def test_missing_prompt_raises():
    with pytest.raises(FileNotFoundError):
        load_prompt("does_not_exist")
