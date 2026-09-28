"""Stock short answers cached in my voice (phrases.PhraseCache)."""
import hashlib

import phrases

KEY = "soniox|tts-rt-v2|Adrian|en|1.1"


def test_about_thirty_phrases_with_variants_for_frequent_ones():
    assert 25 <= len(phrases.PHRASES) <= 40
    assert phrases.PHRASES["Yes."] == 2 and phrases.PHRASES["Could you repeat the question?"] == 1


def test_normalize_keeps_the_question_flag():
    assert phrases.normalize("  Yes! ") == ("yes", False)
    assert phrases.normalize("That’s right.") == ("that's right", False)
    assert phrases.normalize("Really?") == ("really", True)
    assert phrases.normalize('"Really?"') == ("really", True)
    assert phrases.normalize("Really.") != phrases.normalize("Really?")


def test_path_depends_on_key_text_and_variant(tmp_path):
    cache = phrases.PhraseCache(tmp_path, KEY)
    assert cache.path("Yes.") == tmp_path / f"{hashlib.sha1(f'{KEY}|Yes.'.encode()).hexdigest()}.pcm"
    other = phrases.PhraseCache(tmp_path, KEY.replace("1.1", "1.0"))
    assert len({cache.path("Yes."), cache.path("Yes.", 1), cache.path("No."), other.path("Yes.")}) == 4


def test_render_order_store_and_skip(tmp_path):
    cache = phrases.PhraseCache(tmp_path / "phrases", KEY)
    first = next(iter(phrases.PHRASES))
    assert cache.next_missing() == (first, 0)
    cache.store(first, 0, b"\x01\x02")
    assert cache.next_missing() == (first, 1)
    cache.skip(first, 1)  # a failed render is not retried in a loop
    assert cache.next_missing()[0] != first
    assert not list((tmp_path / "phrases").glob("*.part"))


def test_match_whole_phrase_only_and_variants_take_turns(tmp_path):
    cache = phrases.PhraseCache(tmp_path, KEY)
    assert cache.match("Yes.") is None  # not rendered yet
    cache.store("Yes.", 0, b"Y0")
    assert cache.match("yes!") == b"Y0"
    cache.store("Yes.", 1, b"Y1")
    assert [cache.match("Yes.") for _ in range(3)] == [b"Y1", b"Y0", b"Y1"]
    cache.store("Really?", 0, b"R?")
    assert cache.match("Really?") == b"R?"
    assert cache.match("Really.") is None  # a statement is not the question
    assert cache.match("Yes, I agree.") is None


def test_store_failure_is_skipped(tmp_path):
    blocker = tmp_path / "phrases"
    blocker.write_text("not a directory")
    cache = phrases.PhraseCache(blocker, KEY)
    cache.store("Yes.", 0, b"Y0")
    assert ("Yes.", 0) in cache.skipped and cache.match("Yes.") is None
