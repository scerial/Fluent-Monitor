from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from fluentmon.transcript.assembler import MAX_PARTIAL, LineAssembler

KOREAN_TEXT = st.text(
    alphabet=st.sampled_from(list("abc 0123 가나다라 한글 경로\r\n\n")),
    max_size=300,
)


def feed_whole(data: bytes) -> list[str]:
    assembler = LineAssembler()
    return assembler.feed(data)


def feed_chunks(data: bytes, cuts: list[int]) -> list[str]:
    assembler = LineAssembler()
    lines: list[str] = []
    previous = 0
    for cut in sorted(set(c for c in cuts if 0 < c < len(data))) + [len(data)]:
        lines.extend(assembler.feed(data[previous:cut]))
        previous = cut
    return lines


@settings(max_examples=200, deadline=None)
@given(text=KOREAN_TEXT, cuts=st.lists(st.integers(min_value=0, max_value=2000), max_size=40))
def test_random_chunks_match_whole_feed(text: str, cuts: list[int]) -> None:
    data = text.encode("utf-8")
    assert feed_chunks(data, cuts) == feed_whole(data)


def test_multibyte_split_is_held_until_newline() -> None:
    data = "한글 경로\n".encode("utf-8")
    assembler = LineAssembler()
    assert assembler.feed(data[:1]) == []
    assert assembler.feed(data[1:4]) == []
    assert assembler.feed(data[4:-1]) == []
    assert assembler.feed(data[-1:]) == ["한글 경로"]


def test_cp949_bytes_fall_back_to_korean_text() -> None:
    text = "C:/작업/케이스.cas.h5\n"
    data = text.encode("cp949")
    assert feed_whole(data) == ["C:/작업/케이스.cas.h5"]


def test_partial_line_returned_only_after_newline() -> None:
    assembler = LineAssembler()
    assert assembler.feed(b"iter 1 2") == []
    assert assembler.feed(b"3") == []
    assert assembler.feed(b"\n") == ["iter 1 23"]


def test_empty_chunk_and_no_newline() -> None:
    assembler = LineAssembler()
    assert assembler.feed(b"") == []
    assert assembler.feed(b"abc") == []
    assert assembler.feed(b"") == []
    assert assembler.feed(b"\n") == ["abc"]


def test_empty_lines_and_crlf() -> None:
    assert feed_whole(b"\n\r\nx\r\n") == ["", "", "x"]


def test_reset_drops_partial_bytes() -> None:
    assembler = LineAssembler()
    assembler.feed(b"half")
    assembler.reset()
    assert assembler.feed(b"new\n") == ["new"]


def test_oversized_partial_line_is_returned_once() -> None:
    assembler = LineAssembler()
    big = b"a" * (MAX_PARTIAL + 10)
    assert assembler.feed(big) == ["a" * (MAX_PARTIAL + 10)]
    assert assembler.feed(b"b\n") == ["b"]
