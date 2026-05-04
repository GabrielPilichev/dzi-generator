"""
Base класове за parsers.

Всеки конкретен parser (за определен формат изпит) наследява ExamParser
и имплементира:
  - METADATA: subject, level, format_version (class attributes)
  - detect(text) -> float: confidence score 0.0-1.0
  - parse(text) -> ParsedExam: парсва текста в структуриран формат

Auto-detection: registry-то пуска текста през всички регистрирани parsers
и взима този с най-висок confidence score.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


# ============================================================
# Структурирани данни (резултат от parsing)
# ============================================================

@dataclass
class ParsedOption:
    """Опция за multiple_choice въпрос."""
    letter: str          # 'А', 'Б', 'В', 'Г' (или 'A','B','C','D' за други формати)
    text: str
    is_correct: bool = False


@dataclass
class ParsedSubquestion:
    """Подзадача за fill_in въпрос."""
    number: int          # 1, 2, 3
    correct_answer: str
    text: str = ""       # контекст преди празното поле (ако има)
    alternatives: list = field(default_factory=list)


@dataclass
class ParsedQuestion:
    """Един въпрос (всякакъв тип)."""
    number: int
    question_type: str   # 'multiple_choice' | 'fill_in' | 'true_false' | ...
    prompt: str
    points: int = 1
    topic: str = "general"
    options: list = field(default_factory=list)        # ParsedOption
    subquestions: list = field(default_factory=list)   # ParsedSubquestion
    has_image: bool = False
    image_path: Optional[str] = None
    
    # Quality info: parser може да маркира въпроси които изглеждат подозрителни
    quality_score: float = 1.0
    warnings: list = field(default_factory=list)


@dataclass
class ParsedExam:
    """Изпит като цяло — резултат от parser.parse()."""
    subject: str         # 'informatika_it', 'matematika', ...
    level: str           # 'DZI', 'NVO_7', 'NVO_10'
    year: Optional[int] = None
    session: Optional[str] = None
    variant: int = 1
    format_version: str = ""
    
    questions: list = field(default_factory=list)      # ParsedQuestion
    
    # Метаданни за debug
    parser_used: str = ""
    raw_text_length: int = 0
    confidence: float = 1.0
    notes: list = field(default_factory=list)


# ============================================================
# Базов parser
# ============================================================

class ExamParser(ABC):
    """
    Абстрактен parser. Всеки конкретен формат наследява и попълва
    METADATA + имплементира detect() и parse().
    """
    
    # Class attributes — попълват се в наследниците
    SUBJECT: str = ""           # 'informatika_it'
    LEVEL: str = ""             # 'DZI'
    FORMAT_VERSION: str = ""    # 'modern_2023'
    
    # Списък с години/сесии, които парсърът поддържа (за debug)
    SUPPORTED_RANGE: str = ""
    
    @abstractmethod
    def detect(self, text: str) -> float:
        """
        Връща confidence score 0.0-1.0 че този parser е подходящ за дадения текст.
        0.0 = със сигурност не е този формат.
        1.0 = със сигурност е този формат.
        ~0.5+ = вероятно е, но има неяснота.
        """
        raise NotImplementedError
    
    @abstractmethod
    def parse(self, text: str) -> ParsedExam:
        """Парсва текста и връща ParsedExam."""
        raise NotImplementedError
    
    # Optional: AI fallback hook (за бъдещата AI integration)
    def parse_with_ai_fallback(self, text: str, ai_client=None) -> ParsedExam:
        """
        Default implementation: викнове parse(). Може да бъде override-нато
        за да използва AI fallback когато regex parser-ът върне малко въпроси.
        """
        result = self.parse(text)
        # Hook за AI: ако confidence е ниско или има малко въпроси, може да се извика AI
        # Това ще се имплементира по-късно
        return result
    
    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} {self.SUBJECT}/{self.LEVEL}/{self.FORMAT_VERSION}>"
