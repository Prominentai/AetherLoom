"""Word-prefix AND search over the existing frequency-ordered tag dictionary.

This module owns one reusable index shared by all prompt editors.
Build and search on a worker thread: cold indexing is
linear in dictionary size. Warm queries visit the smallest relevant posting
list, never the entire dictionary. Reload by replacing ``manager.suggestions``;
appending/removing entries also invalidates the index.
"""
from array import array
from bisect import bisect_left
import re
import threading
import weakref


_WORDS = re.compile(r"[^\W_]+", re.UNICODE)
_SPACES = re.compile(r"[\s_]+")
_PREFIX_LENGTH = 3
_MAX_ROWS = 1_000_000
_MAX_CHARACTERS = 32_000_000
_MAX_TAG_LENGTH = 4096
_MAX_BUCKETS = 250_000
_MAX_POSTINGS = 12_000_000
_CACHE_LOCK = threading.Lock()
_cache = None


def vocabulary_version(manager):
    """A reload/replacement invalidates pending results as well as the index."""
    source = getattr(manager, 'suggestions', None)
    return (id(source), len(source) if isinstance(source, (list, tuple)) else None)


def _terms(text):
    """Punctuation delimits words; symbol-only tags retain literal matching."""
    normalized = text.casefold()
    words = _WORDS.findall(normalized)
    if words:
        return words
    literal = _SPACES.sub(" ", normalized).strip()
    return [literal] if literal else []


def _word(entry):
    # Actual AutocompleteManager rows are (word, count), already sorted by
    # descending frequency. String rows make small standalone/mock lists useful.
    if isinstance(entry, str):
        return entry
    return entry[0] if isinstance(entry, (tuple, list)) and entry else None


class _Index:
    def __init__(self, source):
        if len(source) > _MAX_ROWS:
            raise ValueError("本地提示词库超过 100 万条索引限制")
        self.words = []
        self.postings = {}
        characters = postings_count = 0
        for entry in source:
            word = _word(entry)
            if not isinstance(word, str) or not word:
                continue
            characters += len(word)
            if len(word) > _MAX_TAG_LENGTH or characters > _MAX_CHARACTERS:
                raise ValueError("本地提示词库文本超过索引大小限制")
            rank = len(self.words)
            self.words.append(word)
            # A tag appears once per prefix, even when several of its words
            # share that prefix. IDs therefore stay sorted in frequency order.
            prefixes = {term[:length] for term in _terms(word)
                        for length in range(1, min(len(term), _PREFIX_LENGTH) + 1)}
            postings_count += len(prefixes)
            if postings_count > _MAX_POSTINGS:
                raise ValueError("本地提示词库超过索引容量限制")
            for prefix in prefixes:
                bucket = self.postings.get(prefix)
                if bucket is None:
                    if len(self.postings) >= _MAX_BUCKETS:
                        raise ValueError("本地提示词库超过索引前缀数量限制")
                    bucket = self.postings[prefix] = array('I')
                bucket.append(rank)

    def search(self, terms, limit):
        buckets = []
        for prefix in dict.fromkeys(term[:_PREFIX_LENGTH] for term in terms):
            bucket = self.postings.get(prefix)
            if bucket is None:
                return []
            buckets.append(bucket)
        buckets.sort(key=len)
        narrow, others = buckets[0], buckets[1:]
        positions = [0] * len(others)
        # Prefixes longer than the indexed first three characters are checked
        # only after their candidate IDs pass the other posting intersections.
        residual = [term for term in terms if len(term) > _PREFIX_LENGTH]
        result = []
        seen = set()
        for rank in narrow:
            match = True
            for index, bucket in enumerate(others):
                position = bisect_left(bucket, rank, positions[index])
                positions[index] = position
                if position == len(bucket):
                    return result
                if bucket[position] != rank:
                    match = False
                    break
            if not match:
                continue
            word = self.words[rank]
            if residual:
                words = _terms(word)
                if not all(any(word.startswith(prefix) for word in words) for prefix in residual):
                    continue
            display_key = _SPACES.sub(' ', word.casefold()).strip()
            if display_key in seen:
                continue
            seen.add(display_key)
            result.append(word)
            if len(result) >= limit:
                break
        return result


class _CacheEntry:
    def __init__(self, manager, source, index):
        self.manager_id = id(manager)
        try:
            self.manager_ref = weakref.ref(manager, _manager_gone)
        except TypeError:
            # Some lightweight mock managers cannot be weak-referenced. Keep
            # only their identity, not the manager itself, and just one index.
            self.manager_ref = None
        self.source = source
        self.length = len(source)
        self.index = index

    def matches(self, manager, source):
        return (self.manager_id == id(manager) and self.source is source
                and self.length == len(source)
                and (self.manager_ref is None or self.manager_ref() is manager))


def _manager_gone(reference):
    global _cache
    with _CACHE_LOCK:
        if _cache is not None and _cache.manager_ref is reference:
            _cache = None


def search(manager, query, limit=10):
    """Return original tag strings in the manager's descending-frequency order.

    Words are Unicode letters/digits; spaces, underscores and punctuation split
    words. Every query word must prefix a tag word, independently of word order.
    Exact/whole-tag prefixes receive no ranking bonus over more frequent tags.
    Only the current manager/list identity is cached; no manager is kept alive.
    """
    global _cache
    if manager is None or not isinstance(query, str) or len(query) > 512:
        return []
    try:
        limit = min(100, int(limit))
    except (ValueError, TypeError, OverflowError):
        return []
    if limit <= 0:
        return []
    terms = list(dict.fromkeys(_terms(query)))
    if not terms or len(terms) > 32:
        return []
    source = getattr(manager, 'suggestions', None)
    if not isinstance(source, (list, tuple)) or not source:
        # Unloading/replacing the dictionary with an empty list must also
        # release the old posting arrays, even though no new index is needed.
        with _CACHE_LOCK:
            if _cache is not None and _cache.manager_id == id(manager):
                _cache = None
        if isinstance(source, (list, tuple)):
            return []
        fallback = getattr(manager, 'get_matches', None)
        if callable(fallback):
            return list(fallback(_SPACES.sub('_', query.strip()), limit=limit))[:limit]
        return []
    with _CACHE_LOCK:
        if _cache is None or not _cache.matches(manager, source):
            _cache = _CacheEntry(manager, source, _Index(source))
        index = _cache.index
    return index.search(terms, limit)
