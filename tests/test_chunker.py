from voicecode.engine.chunker import SentenceChunker


def feed_all(chunker: SentenceChunker, *deltas: str) -> list[str]:
    out: list[str] = []
    for delta in deltas:
        out.extend(chunker.feed(delta))
    return out


def test_first_chunk_at_first_sentence_boundary():
    chunker = SentenceChunker()
    out = feed_all(chunker, "Here is the first sentence. And now", " a second one arrives.")
    assert out[0] == "Here is the first sentence."


def test_boundary_split_across_deltas():
    chunker = SentenceChunker()
    assert chunker.feed("Sentence one ends here.") == []  # terminator at edge: wait
    assert chunker.feed(" More text") == ["Sentence one ends here."]


def test_tiny_fragments_merged():
    chunker = SentenceChunker(min_chars=20)
    out = feed_all(chunker, "Sure. Right. Okay then, let's do it properly. ")
    assert out == ["Sure. Right. Okay then, let's do it properly."]


def test_decimals_do_not_split():
    chunker = SentenceChunker()
    out = feed_all(chunker, "Pi is roughly 3.14 in that case. Next one.")
    assert out == ["Pi is roughly 3.14 in that case."]


def test_newline_is_a_boundary():
    chunker = SentenceChunker()
    out = feed_all(chunker, "First line without a period\nand more text follows here")
    assert out == ["First line without a period"]


def test_flush_returns_remainder_and_pending():
    chunker = SentenceChunker(min_chars=20)
    assert chunker.feed("Yes. And then some trailing words") == []
    assert chunker.flush() == "Yes. And then some trailing words"


def test_flush_empty():
    assert SentenceChunker().flush() is None


def test_question_and_exclamation_boundaries():
    chunker = SentenceChunker()
    out = feed_all(chunker, "Is it really done now? Absolutely, it is done! And so on.")
    assert out[:2] == ["Is it really done now?", "Absolutely, it is done!"]


def test_closing_quote_stays_with_sentence():
    chunker = SentenceChunker()
    out = feed_all(chunker, 'It said "all tests pass." Then it stopped right there.')
    assert out[0] == 'It said "all tests pass."'
