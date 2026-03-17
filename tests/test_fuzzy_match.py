"""Unit tests for fuzzy text matching in annotations."""

import fitz
import pytest

from zotero_mcp.annotations import _fuzzy_find_in_page, _normalize_text


def _make_words(text_list):
    """Create fake PyMuPDF word tuples: (x0, y0, x1, y1, word, block, line, word_idx)."""
    words = []
    x = 0
    for i, w in enumerate(text_list):
        words.append((x, 0, x + 50, 12, w, 0, 0, i))
        x += 55
    return words


class TestNormalizeText:
    def test_ligatures(self):
        assert _normalize_text("e\ufb00ect") == "effect"
        assert _normalize_text("\ufb01nd") == "find"
        assert _normalize_text("\ufb02ow") == "flow"

    def test_quotes(self):
        assert _normalize_text("\u201chello\u201d") == '"hello"'
        assert _normalize_text("\u2018hi\u2019") == "'hi'"

    def test_dashes(self):
        assert _normalize_text("a\u2013b") == "a-b"
        assert _normalize_text("a\u2014b") == "a-b"

    def test_whitespace(self):
        assert _normalize_text("  hello   world  ") == "hello world"


class TestFuzzyFindInPage:
    def test_exact_match(self):
        text = "the quick brown fox jumps over the lazy dog".split()
        words = _make_words(text)
        word_texts = [_normalize_text(w[4]) for w in words]
        search = "quick brown fox"

        rects, matched, dist = _fuzzy_find_in_page(words, word_texts, search)

        assert rects is not None
        assert dist == 0
        assert matched == "quick brown fox"

    def test_minor_ocr_error(self):
        # Simulate OCR reading "brwon" instead of "brown"
        text = "the quick brwon fox jumps over the lazy dog".split()
        words = _make_words(text)
        word_texts = [_normalize_text(w[4]) for w in words]
        search = "quick brown fox"

        rects, matched, dist = _fuzzy_find_in_page(words, word_texts, search)

        assert rects is not None
        assert dist is not None and dist > 0
        assert "brwon" in matched  # Returns what's actually in the PDF

    def test_hyphenated_word(self):
        # PDF extracted "trans-" "formation" as separate words
        text = "the trans- formation of data is complete".split()
        words = _make_words(text)
        word_texts = [_normalize_text(w[4]) for w in words]
        search = "transformation of data"

        rects, matched, dist = _fuzzy_find_in_page(words, word_texts, search)

        assert rects is not None
        assert dist is not None

    def test_completely_different_text_rejected(self):
        text = "the quick brown fox jumps over the lazy dog".split()
        words = _make_words(text)
        word_texts = [_normalize_text(w[4]) for w in words]
        search = "machine learning neural network"

        rects, matched, dist = _fuzzy_find_in_page(words, word_texts, search)

        assert rects is None

    def test_empty_inputs(self):
        rects, matched, dist = _fuzzy_find_in_page([], [], "hello world")
        assert rects is None

        words = _make_words(["hello"])
        word_texts = ["hello"]
        rects, matched, dist = _fuzzy_find_in_page(words, word_texts, "")
        assert rects is None

    def test_correct_rects_returned(self):
        text = "aaa bbb ccc ddd eee".split()
        words = _make_words(text)
        word_texts = [_normalize_text(w[4]) for w in words]
        search = "bbb ccc ddd"

        rects, matched, dist = _fuzzy_find_in_page(words, word_texts, search)

        assert rects is not None
        assert len(rects) == 3
        # Verify rects correspond to words at indices 1, 2, 3
        for i, rect in enumerate(rects):
            assert rect.x0 == words[i + 1][0]

    def test_ligature_mismatch(self):
        # PDF has ligature, search has ascii
        text = ["the", "e\ufb00ect", "of", "the", "treatment"]
        words = _make_words(text)
        word_texts = [_normalize_text(w[4]) for w in words]
        search = "the effect of the treatment"

        rects, matched, dist = _fuzzy_find_in_page(words, word_texts, search)

        assert rects is not None

    def test_custom_max_l_dist(self):
        # With very strict matching (dist=0), OCR error should fail
        text = "the quick brwon fox jumps".split()
        words = _make_words(text)
        word_texts = [_normalize_text(w[4]) for w in words]
        search = "quick brown fox"

        rects, matched, dist = _fuzzy_find_in_page(
            words, word_texts, search, max_l_dist=0
        )
        assert rects is None

        # With generous matching, it should succeed
        rects, matched, dist = _fuzzy_find_in_page(
            words, word_texts, search, max_l_dist=5
        )
        assert rects is not None

    def test_multiple_char_errors(self):
        # Multiple OCR errors in a longer passage
        text = "recgnition of hndwritten chracters using deep larning".split()
        words = _make_words(text)
        word_texts = [_normalize_text(w[4]) for w in words]
        search = "recognition of handwritten characters using deep learning"

        rects, matched, dist = _fuzzy_find_in_page(words, word_texts, search)

        assert rects is not None
