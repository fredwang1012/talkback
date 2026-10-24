import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from run_all import blocked_reason, build_plan, key_state, read_env  # noqa: E402


def names(stages, keys, env):
    return [s.name for s in stages if not blocked_reason(s, keys, env)]


def test_nothing_spends_without_keys_or_the_anthropic_flag():
    stages = build_plan(False)
    env = {"ANTHROPIC_API_KEY": "x"}
    ran = names(stages, key_state(env, allow_anthropic=False), env)
    assert ran == ["chunk-novel", "index-novel"]


def test_boson_key_unlocks_probes_and_prerender_but_not_anthropic():
    stages = build_plan(False)
    env = {"BOSON_API_KEY": "b", "ANTHROPIC_API_KEY": "a"}
    ran = names(stages, key_state(env, allow_anthropic=False), env)
    assert "probe-realtime" in ran and "prerender-novel" in ran
    assert not {"cast", "segment", "summaries", "eval-spoilers"} & set(ran)


def test_probes_come_before_prerender_and_cast_before_segment_before_chunking():
    order = [s.name for s in build_plan(True)]
    assert order.index("probe-tts") < order.index("prerender-novel")
    assert order.index("cast") < order.index("segment") < order.index("chunk-novel") < order.index("prerender-novel")
    assert "prerender-textbook" in order and "prerender-textbook" not in [s.name for s in build_plan(False)]


def test_read_env_ignores_comments_and_quotes(tmp_path):
    f = tmp_path / ".env"
    f.write_text('# c\nBOSON_API_KEY="abc"\nEMPTY=\n', encoding="utf-8")
    assert read_env(f) == {"BOSON_API_KEY": "abc", "EMPTY": ""}
