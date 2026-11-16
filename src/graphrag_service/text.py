"""Text helpers: keyword-search tokenisation that works for Chinese, and a token estimate."""
from __future__ import annotations

import re

_CJK = "㐀-䶿一-鿿豈-﫿぀-ヿ가-힯"
RE_CJK_RUN = re.compile(f"[{_CJK}]+")
RE_CJK = re.compile(f"[{_CJK}]")
RE_WORD = re.compile(r"\w+")

STOPWORDS = frozenset("""
a an and are as at be been but by can could did do does for from had has have how i if in into is it its
me my of on or our should so than that the their them then there these they this to was we were what when
where which who whom whose why will with would you your about after also any now not no still
""".split())


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip()).casefold()


def _bigrams(run: str) -> str:
    if len(run) == 1:
        return f" {run} "
    return " " + " ".join(run[i:i + 2] for i in range(len(run) - 1)) + " "


def fts_text(s: str) -> str:
    """Text as stored in FTS5.

    FTS5's unicode61 tokenizer keeps a whole run of Chinese characters as one token and its
    trigram tokenizer cannot match two-character words, so each run is rewritten as
    overlapping bigrams. Everything else is left to unicode61.
    """
    return RE_CJK_RUN.sub(lambda m: _bigrams(m.group(0)), s or "")


def fts_query(q: str, limit: int = 40) -> str | None:
    """An OR query over the content words of `q`, or None when nothing is searchable."""
    seen, out = set(), []
    for tok in RE_WORD.findall(fts_text(q).casefold()):
        if tok in seen or (tok in STOPWORDS and not RE_CJK.search(tok)):
            continue
        if len(tok) == 1 and not RE_CJK.search(tok) and not tok.isdigit():
            continue
        seen.add(tok)
        out.append('"' + tok.replace('"', "") + '"')
        if len(out) >= limit:
            break
    return " OR ".join(out) if out else None


def approx_tokens(s: str) -> int:
    """Rough token count: ~1.3 per Latin word, 1 per CJK character. Good enough for budgets."""
    if not s:
        return 0
    cjk = len(RE_CJK.findall(s))
    words = len(RE_WORD.findall(RE_CJK.sub(" ", s)))
    return int(words * 1.3 + cjk) + 1
