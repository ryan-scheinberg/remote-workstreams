from remote_workstreams.audio.chunker import SentenceChunker


def test_first_chunk_at_first_sentence_boundary():
    chunker = SentenceChunker()
    out: list[str] = []
    for delta in ("Here is the first sentence. And now", " a second one arrives."):
        out.extend(chunker.feed(delta))
    assert out[0] == "Here is the first sentence."


def test_tiny_fragments_merged():
    chunker = SentenceChunker(min_chars=20)
    out = chunker.feed("Sure. Right. Okay then, let's do it properly. ")
    assert out == ["Sure. Right. Okay then, let's do it properly."]


def test_decimals_do_not_split():
    chunker = SentenceChunker()
    assert chunker.feed("Pi is roughly 3.14 in that case. Next one.") == [
        "Pi is roughly 3.14 in that case."
    ]


def test_flush_returns_remainder_and_pending():
    chunker = SentenceChunker(min_chars=20)
    assert chunker.feed("Yes. And then some trailing words") == []
    assert chunker.flush() == "Yes. And then some trailing words"
