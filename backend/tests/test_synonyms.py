import numpy as np

from app import retrieval
from app.retrieval import EXPAND_WEIGHT, Chunk, Index, tokenize
from app.synonyms import MAX_EXPANSIONS_PER_TOKEN, STOPWORDS, SYNONYMS, expand_tokens


def test_map_loads_and_is_modest_and_lowercase():
    assert 150 <= len(SYNONYMS) <= 400
    for k, v in SYNONYMS.items():
        assert k == k.lower() and v and k not in v


def test_stopwords_are_not_expanded_and_never_appear_as_expansions():
    assert expand_tokens(["what", "the", "did", "she", "how"]) == []
    for v in SYNONYMS.values():
        assert not (set(v) & STOPWORDS)


def test_expansion_caps_and_excludes_originals():
    assert "queer" in expand_tokens(["weird"])
    assert len(expand_tokens(["big"])) <= MAX_EXPANSIONS_PER_TOKEN
    assert "strange" not in expand_tokens(["weird", "strange"])  # already in the query
    assert expand_tokens(["zzzunknown"]) == []
    assert expand_tokens(["shouts"]) == expand_tokens(["shout"])  # inflection fallback


def _index():
    return Index([Chunk("a", 0, 1, "the girl said it was queer and curious"),
                  Chunk("b", 2, 3, "the weird machine was weird in a weird way"),
                  Chunk("c", 4, 5, "nothing relevant here at all")])


def test_originals_dominate_expansions(monkeypatch):
    ix = _index()
    assert ix.mode == "bm25"
    s = ix.scores("weird")
    assert s[1] == 1.0 and s[0] > 0 and s[0] < s[1]  # exact match outranks the synonym-only match
    assert s[2] == 0.0


def test_expansion_helps_vague_query_and_can_be_switched_off(monkeypatch):
    ix = _index()
    assert ix.scores("queer")[0] == 1.0
    monkeypatch.setattr(retrieval, "EXPAND_WEIGHT", 0.0)
    off = ix.scores("weird")
    assert off[0] == 0.0 and off[1] == 1.0
    assert 0 < EXPAND_WEIGHT < 0.5


def test_hybrid_mode_does_not_expand():
    class E:
        def embed_query(self, t): return np.ones(4)
    chunks = _index().chunks
    ix = Index(chunks, np.ones((3, 4)), E())
    assert ix.mode == "hybrid"
    raw = np.asarray(ix._bm25.get_scores(tokenize("weird")))
    assert np.allclose(ix._bm25_scores("weird"), raw)
