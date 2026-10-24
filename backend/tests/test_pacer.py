"""The process-wide adaptive Gemini limiter (app/pacer.py): one AIMD concurrency limit shared by every Gemini call site,
so parallel uploads never stampede the project's quota. Fakes only (fake clock, fake 429-ing server): no network, no
credit. The real error, read once live: google.genai ClientError, code 429, status RESOURCE_EXHAUSTED, no Retry-After."""
import asyncio
import logging
from types import SimpleNamespace

import httpx
import pytest
from google.genai import errors as genai_errors

from app import face_detect, multilingual, pacer, retrying
from app.config import settings
from app.enrich import summaries as sum_mod
from app.enrich.llm import LLM, GeminiProvider
from tests.test_novel_pipeline import data_dir, make_book_on_disk, story  # noqa: F401


def rate_limited(retry_after: float | None = None) -> genai_errors.ClientError:
    """The error Vertex really returns for a per-minute quota hit (shape confirmed live)."""
    headers = {"retry-after": str(retry_after)} if retry_after is not None else {}
    resp = httpx.Response(429, headers=headers, request=httpx.Request("POST", "https://x"))
    return genai_errors.ClientError(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                                                    "message": "Resource exhausted. Please try again later."}}, resp)


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.now += s
        await asyncio.sleep(0)


class Server:
    """A fake Gemini that serves at most `capacity` calls at once and answers 429 to the rest (what the quota does)."""

    def __init__(self, capacity: int, text: str = '{"summary":"hi"}'):
        self.capacity, self.text = capacity, text
        self.running = self.peak = self.calls = self.n429 = 0

    async def generate_content(self, model, contents, config):
        self.calls += 1
        self.running += 1
        self.peak = max(self.peak, self.running)
        try:
            if self.running > self.capacity:
                self.n429 += 1
                raise rate_limited()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            return SimpleNamespace(
                text=self.text, candidates=[SimpleNamespace(finish_reason="STOP")],
                usage_metadata=SimpleNamespace(prompt_token_count=10, candidates_token_count=5, thoughts_token_count=0))
        finally:
            self.running -= 1

    def client(self):
        return SimpleNamespace(aio=SimpleNamespace(models=self))


def make_pacer(clock: FakeClock, **kw) -> pacer.Pacer:
    kw.setdefault("start", 8)
    return pacer.Pacer(clock=clock, sleep=clock.sleep, **kw)


def run(coro, timeout: float = 10.0):
    return asyncio.run(asyncio.wait_for(coro, timeout))


# ---------- the log names the status code (never content) ----------

def test_describe_names_the_type_and_status_code_only():
    assert retrying.describe(rate_limited()) == "ClientError 429"
    wrapped = RuntimeError("secret book text")
    wrapped.__cause__ = rate_limited()
    assert retrying.describe(wrapped) == "RuntimeError 429"
    assert retrying.describe(httpx.ReadError("host")) == "ReadError"


def test_retry_log_line_says_429(caplog):
    fails = [rate_limited()]

    async def fn():
        if fails:
            raise fails.pop()
        return 1

    async def no_sleep(_):
        pass
    with caplog.at_level("WARNING", logger="talkback.retry"):
        assert run(retrying.retry_async(fn, what="gemini call", sleep=no_sleep)) == 1
    msgs = [r.getMessage() for r in caplog.records]
    assert any("ClientError 429" in m for m in msgs) and not any("Resource exhausted" in m for m in msgs)


# ---------- AIMD: halve on a 429 (once per generation), grow back after consecutive successes ----------

def test_limit_halves_on_429_once_per_generation_then_grows_back():
    clock = FakeClock()
    p = make_pacer(clock, grow_after=3, ceiling=10)

    async def go():
        slots = [await p.acquire("background") for _ in range(4)]
        slots[0].throttled(rate_limited())
        slots[0].release()
        assert p.limit == 4
        slots[1].throttled(rate_limited())   # started before the cut: the same overload, not a second one
        slots[1].release()
        assert p.limit == 4
        s = await p.acquire("background")    # a call from the new generation
        s.throttled(rate_limited())
        s.release()
        assert p.limit == 2
        for sl in slots[2:]:
            sl.release()
        for _ in range(3):
            s = await p.acquire("background")
            s.ok()
            s.release()
        assert p.limit == 3
        for _ in range(3 * 7):
            s = await p.acquire("background")
            s.ok()
            s.release()
        assert p.limit == 10   # never above the ceiling
        assert p.in_flight == 0 and p.queued() == 0
    run(go())


def test_limit_never_drops_below_the_floor():
    clock = FakeClock()
    p = make_pacer(clock, start=2)

    async def go():
        for _ in range(4):
            s = await p.acquire("background")
            s.throttled(rate_limited())
            s.release()
        assert p.limit == 1
        s = await p.acquire("background")
        s.ok()
        s.release()
    run(go())


# ---------- shared across books: two LLMs with 24-wide semaphores never exceed one limit, every call finishes ----------

def test_two_books_share_one_limit_and_no_call_is_lost():
    clock = FakeClock()
    srv = Server(capacity=5)
    p = make_pacer(clock, start=8, ceiling=32, grow_after=4)
    books = [LLM("gemini", {"gemini": GeminiProvider(srv.client(), sleep=clock.sleep, pacer=p)}, concurrency=24)
             for _ in range(2)]

    async def go():
        outs = await asyncio.gather(*(llm.complete_json("sys", f"u{i}", sum_mod.NoteResult)
                                      for llm in books for i in range(60)))
        return outs
    outs = run(go())
    assert len(outs) == 120 and all(o.summary == "hi" for o in outs)
    assert srv.n429 > 0                       # the burst really hit the quota
    assert srv.peak <= 8 + p.reserved         # the two 24-wide books were held to the shared limit
    assert p.low_water <= 4 < p.limit         # it halved, then grew back
    assert p.in_flight == 0 and p.queued() == 0


# ---------- interactive calls (answers, translation) never wait behind a book's batch ----------

def test_interactive_waiters_are_served_first_and_keep_a_reserved_slot():
    clock = FakeClock()
    p = make_pacer(clock, start=4, reserved=1)

    async def go():
        held = [await p.acquire("background") for _ in range(3)]   # 3 of 4: the last slot is reserved
        bg_fut = asyncio.ensure_future(p.acquire("background"))
        await asyncio.sleep(0)
        assert not bg_fut.done() and p.queued() == 1          # background waits for the reserved slot
        inter = await asyncio.wait_for(p.acquire("interactive"), 1)   # interactive takes it at once
        second = asyncio.ensure_future(p.acquire("interactive"))
        await asyncio.sleep(0)
        assert not second.done() and p.queued() == 2
        inter.release()                                        # one slot frees: the interactive waiter gets it first
        got = await asyncio.wait_for(second, 1)
        assert not bg_fut.done()
        got.release()
        held[0].release()                                      # now 2 held of 4: background may have one more
        bg = await asyncio.wait_for(bg_fut, 1)
        for s in (bg, held[1], held[2]):
            s.release()
        assert p.in_flight == 0 and p.queued() == 0
    run(go())


def test_interactive_can_still_run_when_the_limit_is_at_its_floor():
    clock = FakeClock()
    p = make_pacer(clock, start=1, reserved=1)

    async def go():
        bg = await p.acquire("background")
        inter = await asyncio.wait_for(p.acquire("interactive"), 1)
        bg.release()
        inter.release()
    run(go())


def test_answerer_translation_and_face_detect_use_the_interactive_lane_and_enrichment_the_background_lane(monkeypatch):
    seen: list[str] = []

    class Recording(pacer.Pacer):
        async def call(self, fn, *, priority="background", **kw):
            seen.append(priority)
            return await super().call(fn, priority=priority, **kw)
    monkeypatch.setattr(pacer, "shared", Recording())

    from app.gcp import llm as gllm
    from tests.test_web_search import run_answerer

    class Models:
        async def generate_content_stream(self, model, contents, config):
            from tests.test_web_search import chunk

            async def gen():
                yield chunk("Hello.")
            return gen()
    run_answerer(Models())

    class TModels:
        async def generate_content(self, model, contents, config):
            return SimpleNamespace(text="Who?", usage_metadata=None)
    monkeypatch.setattr(settings, "gcp_project", "p")
    monkeypatch.setattr(multilingual, "_client", SimpleNamespace(aio=SimpleNamespace(models=TModels())))
    multilingual._cache.clear()
    run(multilingual.translate("¿Quién?"))

    srv = Server(capacity=9)
    run(LLM("gemini", {"gemini": GeminiProvider(srv.client())}).complete_json("s", "u", sum_mod.NoteResult))
    assert seen == ["interactive", "interactive", "background"]
    del gllm


# ---------- patient background retries, short interactive budget ----------

def test_background_retries_are_long_and_patient_interactive_short():
    clock = FakeClock()
    p = make_pacer(clock)
    calls = []

    async def always_429():
        calls.append(1)
        raise rate_limited()
    with pytest.raises(genai_errors.ClientError):
        run(p.call(always_429, priority="background", what="t", sleep=clock.sleep))
    assert len(calls) == 6 and len(clock.sleeps) == 5
    assert 15 <= max(clock.sleeps) <= 30 and all(s > 0 for s in clock.sleeps)
    assert p.in_flight == 0

    calls.clear()
    clock.sleeps.clear()
    with pytest.raises(genai_errors.ClientError):
        run(p.call(always_429, priority="interactive", what="t", sleep=clock.sleep))
    assert len(calls) == 4 and max(clock.sleeps) <= 8.0


def test_a_slot_is_not_held_while_a_retry_sleeps():
    clock = FakeClock()
    p = make_pacer(clock, start=1)
    seen_in_flight = []

    async def once_429():
        seen_in_flight.append(p.in_flight)
        if len(seen_in_flight) == 1:
            raise rate_limited()
        return "ok"

    async def sleep(s):
        assert p.in_flight == 0   # the slot was given back before the backoff
        await clock.sleep(s)
    assert run(p.call(once_429, priority="background", what="t", sleep=sleep)) == "ok"
    assert seen_in_flight == [1, 1]


def test_gemini_provider_tries_six_times_on_429_before_giving_up():
    fails = [rate_limited()] * 20
    srv = Server(capacity=100)

    class Models(Server):
        async def generate_content(self, model, contents, config):
            self.calls += 1
            raise fails.pop(0)
    srv = Models(capacity=100)
    clock = FakeClock()
    llm = LLM("gemini", {"gemini": GeminiProvider(srv.client(), sleep=clock.sleep, pacer=make_pacer(clock))})
    with pytest.raises(genai_errors.ClientError):
        run(llm.complete_json("sys", "usr", sum_mod.NoteResult))
    assert srv.calls == GeminiProvider.RETRIES == 6


# ---------- a burst never skips notes ----------

def test_no_summary_notes_are_skipped_under_a_burst(data_dir):  # noqa: F811
    book = make_book_on_disk(data_dir, story(chapters=12))   # a dozen notes at once against a quota of three
    notes, _, _ = sum_mod.estimate_calls(book)
    assert notes >= 10
    clock = FakeClock()
    srv = Server(capacity=3)
    p = make_pacer(clock, start=8, grow_after=4)
    llm = LLM("gemini", {"gemini": GeminiProvider(srv.client(), sleep=clock.sleep, pacer=p)}, concurrency=24)
    stats = {}
    out = run(sum_mod.build_summaries(book, llm, stats=stats), timeout=30)
    assert srv.n429 > 0 and stats["skipped"] == 0 and len(out) == notes


# ---------- the retry-after hint ----------

def test_retry_after_hint_holds_every_new_grant_until_it_passes():
    clock = FakeClock()
    p = make_pacer(clock, start=4)

    async def go():
        s = await p.acquire("background")
        s.throttled(rate_limited(retry_after=5))
        s.release()
        assert p.hold_until == pytest.approx(5.0)
        got = asyncio.ensure_future(p.acquire("interactive"))
        await asyncio.sleep(0)
        assert not got.done()
        slot = await asyncio.wait_for(got, 1)   # the pacer's own sleep advanced the fake clock
        assert clock.now >= 5.0
        slot.release()
    run(go())
    assert pacer.retry_after_s(rate_limited()) is None
    assert pacer.retry_after_s(rate_limited(retry_after=3)) == 3.0
    e = genai_errors.ClientError(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "x",
                                                 "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo",
                                                              "retryDelay": "2.5s"}]}})
    assert pacer.retry_after_s(e) == 2.5


# ---------- no deadlocks, cancellation gives slots back ----------

def test_cancelling_a_waiter_or_a_holder_releases_its_slot():
    clock = FakeClock()
    p = make_pacer(clock, start=2, reserved=0)

    async def go():
        a = await p.acquire("background")
        b = await p.acquire("background")
        waiter = asyncio.ensure_future(p.acquire("background"))
        await asyncio.sleep(0)
        assert p.queued() == 1
        waiter.cancel()
        await asyncio.sleep(0)
        assert p.queued() == 0

        started = asyncio.Event()

        async def hold_forever():
            async with p.slot("background"):
                started.set()
                await asyncio.sleep(3600)
        a.release()
        t = asyncio.ensure_future(hold_forever())
        await started.wait()
        assert p.in_flight == 2
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        assert p.in_flight == 1
        c = await asyncio.wait_for(p.acquire("background"), 1)   # the cancelled holder's slot is usable again
        c.release()
        b.release()
        assert p.in_flight == 0

        # a slot granted at the very moment its waiter is cancelled goes back to the pool
        d = await p.acquire("background")
        e = await p.acquire("background")
        w = asyncio.ensure_future(p.acquire("background"))
        await asyncio.sleep(0)
        d.release()            # grants w's future...
        w.cancel()             # ...but the task is cancelled before it resumes
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert p.in_flight == 1
        e.release()
        assert p.in_flight == 0 and p.queued() == 0
    run(go())


def test_limiter_never_deadlocks_under_a_storm_of_429s_at_the_floor():
    clock = FakeClock()
    srv = Server(capacity=1)
    p = make_pacer(clock, start=8, grow_after=2)
    llm = LLM("gemini", {"gemini": GeminiProvider(srv.client(), sleep=clock.sleep, pacer=p)}, concurrency=50)

    async def go():
        return await asyncio.gather(*(llm.complete_json("s", f"u{i}", sum_mod.NoteResult) for i in range(40)),
                                    return_exceptions=True)
    outs = run(go(), timeout=30)
    assert all(not isinstance(o, BaseException) for o in outs)
    assert p.low_water == 1 and p.in_flight == 0 and p.queued() == 0


# ---------- the debug line ----------

def test_debug_line_every_30s_while_throttled(caplog):
    clock = FakeClock()
    p = make_pacer(clock, start=4)

    async def go():
        with caplog.at_level("INFO", logger="talkback.pacer"):
            s = await p.acquire("background")
            s.throttled(rate_limited())
            s.release()
            s = await p.acquire("background")
            s.ok()
            s.release()
            clock.now += 31
            s = await p.acquire("background")
            s.ok()
            s.release()
            clock.now += 5
            s = await p.acquire("background")
            s.ok()
            s.release()
    run(go())
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("gemini pacer:")]
    assert len(lines) == 2   # at the cut and ~30 s later, not at every call
    assert "limit 2" in lines[1] and "queued 0" in lines[1]


# ---------- face detection: the Gemini region and the pacer ----------

def test_face_detect_builds_its_client_in_the_gemini_region_and_goes_through_the_pacer(monkeypatch):
    seen = {}
    srv = Server(capacity=9, text='{"found": true, "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}')

    class FakeClient:
        def __init__(self, **kw):
            seen.update(kw)
            self.aio = SimpleNamespace(models=srv)
    import google.genai as genai
    monkeypatch.setattr(genai, "Client", FakeClient)
    monkeypatch.setattr(settings, "gcp_project", "p")
    monkeypatch.setattr(settings, "gemini_location", "global")
    monkeypatch.setattr(face_detect.spend, "add", lambda *a, **k: 0.0)
    calls = []

    class Recording(pacer.Pacer):
        async def call(self, fn, *, priority="background", **kw):
            calls.append(priority)
            return await super().call(fn, priority=priority, **kw)
    monkeypatch.setattr(pacer, "shared", Recording())
    assert run(face_detect._gemini(b"img", "image/png")).startswith("{")
    assert seen.get("location") == "global" and calls == ["interactive"]


def test_shared_pacer_takes_its_bounds_from_settings():
    assert pacer.shared.limit == settings.gemini_concurrency_start == 8
    assert pacer.shared.ceiling == settings.gemini_concurrency_max == 32
    assert pacer.shared.floor == 1
