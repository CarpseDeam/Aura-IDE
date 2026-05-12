"""Pure-Python BM25 scorer with a smart tokenizer. Zero dependencies beyond stdlib.

The tokenizer handles camelCase, snake_case, and punctuation boundaries so that
code search queries like "authentication handler" find "AuthHandler", etc.
"""

from __future__ import annotations

import math
import re


def tokenize(text: str) -> list[str]:
    """Tokenize *text* for BM25 indexing/search.

    Applies, in order:
    1. camelCase boundary splitting
    2. Lowercasing
    3. Punctuation/whitespace/snake_case split
    4. Empty token filtering
    5. Minimum token length of 2

    Args:
        text: Raw string content (source code, query, etc.).

    Returns:
        A list of normalized tokens.
    """
    return _tokenize_impl(text)


def _tokenize_impl(text: str) -> list[str]:
    """Internal tokenizer implementation."""
    if not text:
        return []

    # Step 1: Insert boundaries for camelCase.
    # Insert a null byte (\x00) before any uppercase letter that is:
    #   - preceded by a lowercase letter (e.g. "getData" -> "get\x00Data")
    #   - followed by a lowercase letter and preceded by an uppercase
    #     (e.g. "HTTPSConnection" -> "HTTPS\x00Connection")
    # We operate on the original string so we can detect case.
    result = []
    i = 0
    chars = list(text)
    while i < len(chars):
        ch = chars[i]
        if ch.isupper() and i > 0:
            prev = chars[i - 1]
            # Case 1: preceded by lowercase -> split
            if prev.islower():
                result.append("\x00")
            # Case 2: preceded by uppercase AND (followed by lowercase or at end)
            elif prev.isupper() and i + 1 < len(chars) and chars[i + 1].islower():
                result.append("\x00")
        result.append(ch)
        i += 1

    text = "".join(result)

    # Step 2: Lowercase
    text = text.lower()

    # Step 3: Split on punctuation/whitespace/underscore
    # Use a more complete set of delimiters including underscore for snake_case
    tokens = re.split(r"[^a-zA-Z0-9]+", text)

    # Step 4: Filter
    return [t for t in tokens if len(t) >= 2]


class BM25Scorer:
    """BM25 scoring engine with an in-memory inverted index.

    Supports incremental add/remove of documents and fast top-k search.
    Supports ``to_dict()`` / ``from_dict()`` for disk serialization.

    Test cases:
    - Two documents, one shared term: query that term, expect both ranked by BM25.
    - Empty index: search returns [].
    - Re-index a doc ID: the old version is fully replaced.
    - ``to_dict()`` then ``from_dict()`` yields an identical scorer.
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        """Initialise BM25 scorer.

        Args:
            k1: Term frequency saturation parameter (default 1.5).
            b: Length normalization parameter (default 0.75).
        """
        self._k1 = k1
        self._b = b
        # Inverted index: term -> {doc_id -> term_frequency}
        self._index: dict[str, dict[str, int]] = {}
        # Document lengths: doc_id -> total token count
        self._doc_lengths: dict[str, int] = {}
        # Number of documents
        self._N: int = 0
        # Average document length (cached)
        self._avgdl: float = 0.0

    def to_dict(self) -> dict:
        """Serialize scorer state for disk caching.

        Returns:
            A JSON-serializable dict with all mutable state.
        """
        return {
            "k1": self._k1,
            "b": self._b,
            "index": self._index,
            "doc_lengths": self._doc_lengths,
            "N": self._N,
            "avgdl": self._avgdl,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "BM25Scorer":
        """Reconstruct a BM25Scorer from a dict produced by :meth:`to_dict`.

        Args:
            data: Dict with keys ``k1``, ``b``, ``index``, ``doc_lengths``,
                  ``N``, ``avgdl``.

        Returns:
            A fully restored BM25Scorer instance.
        """
        scorer = cls(k1=data["k1"], b=data["b"])
        scorer._index = data["index"]
        scorer._doc_lengths = data["doc_lengths"]
        scorer._N = data["N"]
        scorer._avgdl = data["avgdl"]
        return scorer

    def add_document(self, doc_id: str, tokens: list[str]) -> None:
        """Index or re-index a document.

        If *doc_id* already exists, the old entry is removed first.

        Args:
            doc_id: Unique identifier for the document (e.g. relative file path).
            tokens: List of tokens from :func:`tokenize`.
        """
        # Remove existing first to support re-indexing
        if doc_id in self._doc_lengths:
            self.remove_document(doc_id)

        for token in tokens:
            self._index.setdefault(token, {}).setdefault(doc_id, 0)
            self._index[token][doc_id] += 1

        self._doc_lengths[doc_id] = len(tokens)
        self._N += 1
        self._avgdl = sum(self._doc_lengths.values()) / self._N if self._N > 0 else 0.0

    def remove_document(self, doc_id: str) -> None:
        """Remove a document from the index.

        Args:
            doc_id: The document identifier to remove.
        """
        if doc_id not in self._doc_lengths:
            return

        # Decrement term frequencies
        for term, docs in self._index.items():
            if doc_id in docs:
                del docs[doc_id]
                if not docs:
                    # Remove term entry if no docs remain
                    pass  # can't delete while iterating, do it in a second pass

        # Clean up empty term entries
        empty_terms = [t for t, d in self._index.items() if not d]
        for t in empty_terms:
            del self._index[t]

        # Remove from doc lengths
        del self._doc_lengths[doc_id]
        self._N -= 1
        self._avgdl = sum(self._doc_lengths.values()) / self._N if self._N > 0 else 0.0

    def _idf(self, term: str) -> float:
        """Compute Inverse Document Frequency for *term*.

        Args:
            term: The query term.

        Returns:
            IDF score smoothed with 0.5 add-one smoothing.
        """
        df = len(self._index.get(term, {}))
        if df == 0 or self._N == 0:
            return 0.0
        return math.log((self._N - df + 0.5) / (df + 0.5) + 1.0)

    def score(self, query_tokens: list[str], doc_id: str) -> float:
        """Compute BM25 score for a single document against query tokens.

        Args:
            query_tokens: Tokenized query.
            doc_id: Document identifier to score.

        Returns:
            BM25 relevance score.
        """
        dl = self._doc_lengths.get(doc_id, 0)
        if dl == 0 or self._avgdl == 0:
            return 0.0

        total = 0.0
        for term in set(query_tokens):  # unique terms only
            tf = self._index.get(term, {}).get(doc_id, 0)
            if tf == 0:
                continue
            idf = self._idf(term)
            numerator = tf * (self._k1 + 1)
            denominator = tf + self._k1 * (1 - self._b + self._b * (dl / self._avgdl))
            total += idf * numerator / denominator
        return total

    def search(self, query_tokens: list[str], top_k: int = 5) -> list[tuple[str, float]]:
        """Search the index for the top-k most relevant documents.

        Args:
            query_tokens: Tokenized query.
            top_k: Maximum number of results to return.

        Returns:
            List of ``(doc_id, score)`` tuples, sorted descending by score.
        """
        # Collect candidate doc_ids that match at least one query token
        candidates: set[str] = set()
        for term in query_tokens:
            docs = self._index.get(term, {})
            candidates.update(docs.keys())

        # Score each candidate
        scored: list[tuple[str, float]] = []
        for doc_id in candidates:
            s = self.score(query_tokens, doc_id)
            if s > 0:
                scored.append((doc_id, s))

        # Sort descending by score
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    @property
    def doc_count(self) -> int:
        """Return the number of indexed documents."""
        return self._N

    @property
    def term_count(self) -> int:
        """Return the number of unique terms in the index."""
        return len(self._index)
