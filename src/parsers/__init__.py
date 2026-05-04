"""ДЗИ Generator — parsers package."""
from .base import ExamParser, ParsedExam, ParsedQuestion, ParsedOption, ParsedSubquestion
from .registry import detect_format, get_parser_for, register_parser, ALL_PARSERS

__all__ = [
    "ExamParser",
    "ParsedExam",
    "ParsedQuestion",
    "ParsedOption",
    "ParsedSubquestion",
    "detect_format",
    "get_parser_for",
    "register_parser",
    "ALL_PARSERS",
]
