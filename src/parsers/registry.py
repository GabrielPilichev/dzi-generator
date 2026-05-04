"""
Parser registry & auto-detection.

Всеки parser се регистрира в ALL_PARSERS чрез @register_parser декоратор.
detect_format(text) пуска текста през всички и връща най-уверения.
"""

from __future__ import annotations

from typing import Type, Optional

from .base import ExamParser

ALL_PARSERS: list = []


def register_parser(cls: Type[ExamParser]) -> Type[ExamParser]:
    """Декоратор: регистрира parser клас в глобалния списък."""
    if cls not in ALL_PARSERS:
        ALL_PARSERS.append(cls)
    return cls


def detect_format(text: str, min_confidence: float = 0.3) -> Optional[ExamParser]:
    """
    Пуска текста през всички регистрирани parsers и връща инстанцията
    с най-висок confidence (ако > min_confidence). Иначе None.
    """
    best: Optional[ExamParser] = None
    best_score = 0.0
    
    for cls in ALL_PARSERS:
        instance = cls()
        try:
            score = instance.detect(text)
        except Exception as e:
            print(f"   ⚠️  {cls.__name__}.detect() crash: {e}")
            continue
        if score > best_score:
            best_score = score
            best = instance
    
    if best and best_score >= min_confidence:
        return best
    return None


def get_parser_for(subject: str, level: str, format_version: str = "") -> Optional[ExamParser]:
    """
    Връща parser по точно име (без auto-detection).
    Полезно когато знаем формата от преди (напр. от scraper-а).
    """
    for cls in ALL_PARSERS:
        if cls.SUBJECT == subject and cls.LEVEL == level:
            if format_version and cls.FORMAT_VERSION != format_version:
                continue
            return cls()
    return None


# Import всички parsers за да се регистрират
# (трябва да са след дефиницията на register_parser за да работи декораторът)
from . import dzi_it_modern  # noqa: F401, E402
from . import dzi_it_legacy  # noqa: F401, E402
