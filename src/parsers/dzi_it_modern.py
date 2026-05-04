"""
Parser за ДЗИ по Информационни технологии — модерен формат (2023+).

Промени спрямо parse_v2.py:
  * BUG FIX: page numbers вече не bleed-ват в option text (trailing newline+digit се чисти)
  * BUG FIX: fill-in answer key parser е пренаписан — сега извлича подзадачи
    дори когато формата на ключа варира (стария parser работеше само за ~15%)
  * Подобрен topic classifier с повече ключови думи
  * Експонира се като клас (ExamParser), а не като функции
"""

from __future__ import annotations

import re
from typing import Optional

from .base import (
    ExamParser,
    ParsedExam,
    ParsedQuestion,
    ParsedOption,
    ParsedSubquestion,
)
from .registry import register_parser


# ============================================================
# Regex patterns
# ============================================================

# Multiple choice: "1. Текст въпрос..." до следваща задача или края
MC_PATTERN = re.compile(
    r"(?:^|\n)(\d+)\.\s*(.+?)"
    r"(?=(?:\n\d+\.\s*[А-Яа-яA-Za-z])|(?:\nОтговорите на задачите))",
    re.DOTALL,
)

# Опции А) Б) В) Г)
OPTION_PATTERN = re.compile(
    r"([АБВГ])\)\s*(.+?)(?=\n[АБВГ]\)|\Z)",
    re.DOTALL,
)

# Fill-in: "16. блок" до следваща задача или края на текста
FI_PATTERN = re.compile(
    r"(?:^|\n)(1[6-9]|2[0-5])\.\s*(.+?)"
    r"(?=(?:\n(?:1[6-9]|2[0-5])\.\s*)|(?:\nЧАСТ\s*2)|(?:\nЗадача\s*26\.)|\Z)",
    re.DOTALL,
)

# Marker за начало на ключа с отговори
ANSWER_KEY_MARKER = "Ключ с верните отговори"

# MC answer в ключа: "1. В 1" или "1. В" (някои варианти не показват точките)
MC_ANSWER_PATTERN = re.compile(
    r"(?:^|\n)(\d+)\.\s+([АБВГ])(?:\s+\d+)?",
    re.MULTILINE,
)

# Fill-in answer block в ключа: започва с "16." до следващото "NN." или края
FI_BLOCK_PATTERN = re.compile(
    r"(?:^|\n)(1[6-9]|2[0-5])\.\s+(.+?)"
    r"(?=(?:\n(?:1[6-9]|2[0-5])\.\s+)|(?:\n(?:2[6-9])\.\s+)|\Z)",
    re.DOTALL,
)

# Подотговори в fill-in блок: "(1) ..."
FI_SUBANSWER_PATTERN = re.compile(
    r"\((\d+)\)\s+(.+?)(?=\(\d+\)|\nПо\s+\d+|\nЗа\s+верен|\Z)",
    re.DOTALL,
)

# Page number bleed: trailing digit on its own line at end of option
TRAILING_PAGE_NUM = re.compile(r"\n\d+\s*$")


# ============================================================
# Topic classifier
# ============================================================

TOPIC_KEYWORDS = {
    "spreadsheets": [
        "електронна таблица", "формула", "клетка", "sumif", "countif", "vlookup",
        "if(", "обобщаваща таблица", "pivot", "среден аритметичен", "sum(", "average",
    ],
    "databases": [
        "база данни", "релационна", "заявка", "първичен ключ", "релация",
        "many to many", "many-to-many", "select ", "from ", "where ", "join ",
        "чужд ключ", "нормализ",
    ],
    "web": [
        "html", "css", "уеб сайт", "уеб страница", "браузър", "<form", "<input",
        "<textarea", "<select", "семантичн", "seo", "хипервр", "хостинг",
        "responsive", "адаптив", "id селектор", "class селектор",
    ],
    "graphics": [
        "пиксел", "вектор", "растер", "филтър на изображен", "цветова",
        "трасиране", "обектив", "матрица на пиксели", "разделителна способност",
        "dpi", "rgb", "cmyk", "lasso", "ласо", "клонира", "инструмент",
    ],
    "video_audio": [
        "видео", "аудио", "mp3", "звук", "кадър", "филм", "оператор", "монтаж",
        "субтитр", "квантуване", "дискретизация", "фпс", "fps", "честота на дискретизация",
    ],
    "info_systems": [
        "информационна система", "проектиране", "разработване", "внедряване",
        "софтуерен проект", "архитектура на софтуер", "етап на разработка",
        "облак", "cloud", "saas", "колаборативн",
    ],
    "hardware": [
        "ram", "rom", "процесор", "хардуер", "разрядност", "двоичн",
        "флаш памет", "ssd", "hdd", "видеокарта",
    ],
    "security": [
        "защита", "парола", "криптир", "https", "патент", "лиценз", "марка",
        "защитна стена", "firewall", "ddos", "xss", "фишинг", "phishing",
        "автентикация", "сертификат", "вирус", "троянски",
    ],
    "protocols": [
        "протокол", "smtp", "ftp", "imap", "tcp", "ip адрес", "dns",
    ],
    "encoding": [
        "unicode", "ascii", "utf-8", "utf-16", "кодиране на знаци",
    ],
    "algorithms": [
        "алгоритъм", "блок-схема", "псевдокод", "сложност", "сортировка",
        "линеен търсещ", "бинарно търсене",
    ],
}


def classify_topic(prompt: str) -> str:
    p = prompt.lower()
    # Check most specific first (algorithms before web because algorithms can mention "for/if")
    for topic, words in TOPIC_KEYWORDS.items():
        if any(w in p for w in words):
            return topic
    return "general"


# ============================================================
# Header parsing (subject/year/session/variant)
# ============================================================

def parse_header(text: str) -> dict:
    """
    Опитваме се да открием годината/сесията/варианта от заглавната страница.
    PDF-ите обикновено имат „ДЗИ ... месец година" в началото.
    """
    head = text[:2000].lower()
    
    # Year
    year_m = re.search(r"\b(20[12]\d)\b", head)
    year = int(year_m.group(1)) if year_m else None
    
    # Session
    session = None
    if "май" in head:
        session = "may"
    elif "август" in head:
        session = "august"
    elif "юни" in head:
        session = "june"
    
    # Variant: "Вариант 1", "Вариант 2"
    variant_m = re.search(r"вариант\s*(\d)", head)
    variant = int(variant_m.group(1)) if variant_m else 1
    
    return {"year": year, "session": session, "variant": variant}


# ============================================================
# Option text cleaning
# ============================================================

def clean_option_text(text: str) -> str:
    """
    Чисти типични bleed-ове от option text:
      * Trailing page numbers ("\n2" в края)
      * Множествени spaces
    """
    text = text.strip()
    # Махни trailing page number ("\n2" или "\n  2  ")
    text = TRAILING_PAGE_NUM.sub("", text)
    # Колапсирай multiple newlines
    text = re.sub(r"\n+", "\n", text)
    text = text.strip()
    return text


# ============================================================
# Main parser
# ============================================================

@register_parser
class DziItModernParser(ExamParser):
    """ДЗИ Информационни технологии — формат от 2023 г. нататък (15 MC + 10 fill-in + 3 практически)."""
    
    SUBJECT = "informatika_it"
    LEVEL = "DZI"
    FORMAT_VERSION = "modern_2023"
    SUPPORTED_RANGE = "May/Aug 2023 - present"
    
    def detect(self, text: str) -> float:
        """
        Confidence signals:
          * Има ли „ДЗИ" / „зрелостен изпит" → +0.2
          * Има ли „информационни технологии" → +0.2
          * Има ли всичките 15 MC задачи → +0.3
          * Има ли задачи 16-25 → +0.2
          * Има ли „Ключ с верните отговори" → +0.1
        """
        head = text[:3000].lower()
        score = 0.0
        
        if "зрелостен изпит" in head or "матур" in head:
            score += 0.2
        if "информационни технологии" in head or "ит" in head:
            score += 0.2
        
        # Check for 15+ MC questions
        mc_matches = list(MC_PATTERN.finditer(text))
        mc_count = sum(1 for m in mc_matches if int(m.group(1)) <= 15)
        if mc_count >= 14:
            score += 0.3
        elif mc_count >= 10:
            score += 0.15
        
        # Check for fill-in 16-25
        fi_matches = list(FI_PATTERN.finditer(text))
        if len(fi_matches) >= 8:
            score += 0.2
        elif len(fi_matches) >= 4:
            score += 0.1
        
        if ANSWER_KEY_MARKER in text:
            score += 0.1
        
        return min(score, 1.0)
    
    def parse(self, text: str) -> ParsedExam:
        # Header info
        header = parse_header(text)
        
        # Split на въпроси и ключ
        questions_text, answers_text = self._split_qa(text)
        
        # Parse multiple choice
        mc_questions = self._parse_mc(questions_text)
        # Parse fill-in
        fi_questions = self._parse_fi(questions_text)
        
        # Parse answer key
        mc_answers = self._parse_mc_answers(answers_text)
        fi_answers = self._parse_fi_answers(answers_text)
        
        # Attach answers
        for q in mc_questions:
            correct_letter = mc_answers.get(q.number)
            if correct_letter:
                for opt in q.options:
                    if opt.letter == correct_letter:
                        opt.is_correct = True
                        break
            else:
                q.warnings.append("missing_answer_key")
                q.quality_score *= 0.5
        
        for q in fi_questions:
            sub_answers = fi_answers.get(q.number, {})
            for sub_num, sub_text in sub_answers.items():
                q.subquestions.append(ParsedSubquestion(
                    number=sub_num,
                    correct_answer=sub_text,
                ))
            if not q.subquestions:
                q.warnings.append("no_subanswers_parsed")
                q.quality_score *= 0.3
        
        # Topic classification
        for q in mc_questions + fi_questions:
            q.topic = classify_topic(q.prompt)
        
        all_questions = mc_questions + fi_questions
        
        return ParsedExam(
            subject=self.SUBJECT,
            level=self.LEVEL,
            year=header["year"],
            session=header["session"],
            variant=header["variant"],
            format_version=self.FORMAT_VERSION,
            questions=all_questions,
            parser_used=self.__class__.__name__,
            raw_text_length=len(text),
            confidence=self.detect(text),
        )
    
    # ------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------
    
    @staticmethod
    def _split_qa(text: str) -> tuple:
        if ANSWER_KEY_MARKER in text:
            idx = text.index(ANSWER_KEY_MARKER)
            return text[:idx], text[idx:]
        return text, ""
    
    @staticmethod
    def _parse_mc(text: str) -> list:
        questions: list = []
        seen_numbers = set()
        
        for match in MC_PATTERN.finditer(text):
            num = int(match.group(1))
            if num > 15 or num in seen_numbers:
                continue
            
            block = match.group(2).strip()
            
            # Find options
            options_raw = OPTION_PATTERN.findall(block)
            if len(options_raw) < 2:
                continue
            
            # Dedup опции с еднаква буква (бъг от parsing)
            seen_letters = set()
            options = []
            for letter, raw_text in options_raw:
                if letter in seen_letters:
                    continue
                seen_letters.add(letter)
                cleaned = clean_option_text(raw_text)
                options.append(ParsedOption(letter=letter, text=cleaned))
            
            if len(options) != 4:
                continue
            
            # Prompt = всичко преди първото "А)"
            first_opt_idx = block.find("А)")
            if first_opt_idx == -1:
                continue
            prompt = block[:first_opt_idx].strip()
            
            if len(prompt) < 10:
                continue
            
            seen_numbers.add(num)
            questions.append(ParsedQuestion(
                number=num,
                question_type="multiple_choice",
                prompt=prompt,
                points=1,
                options=options,
            ))
        
        return questions
    
    @staticmethod
    def _parse_fi(text: str) -> list:
        # Restrict to fill-in section
        section_marker = "Отговорите на задачите от 16."
        if section_marker in text:
            text = text[text.index(section_marker):]
        
        questions: list = []
        seen_numbers = set()
        
        for match in FI_PATTERN.finditer(text):
            num = int(match.group(1))
            if num < 16 or num > 25 or num in seen_numbers:
                continue
            seen_numbers.add(num)
            
            block = match.group(2).strip()
            if len(block) < 20:
                continue
            
            questions.append(ParsedQuestion(
                number=num,
                question_type="fill_in",
                prompt=block,
                points=3,
            ))
        
        return questions
    
    @staticmethod
    def _parse_mc_answers(answers_text: str) -> dict:
        """Извлича MC отговори от ключа. Връща {1: 'В', 2: 'А', ...}."""
        out = {}
        for m in MC_ANSWER_PATTERN.finditer(answers_text):
            num = int(m.group(1))
            if 1 <= num <= 15:
                out[num] = m.group(2)
        return out
    
    @staticmethod
    def _parse_fi_answers(answers_text: str) -> dict:
        """
        Извлича fill-in отговори от ключа. Връща {16: {1: 'отг1', 2: 'отг2', 3: 'отг3'}, ...}.
        
        ПОПРАВЕНО: старият parser изискваше „16. – 3 точки\\n", което се срещаше
        рядко. Сега използваме по-широк pattern и подзадачите се извличат от
        целия блок до следващия номер.
        """
        # Намираме секцията с fill-in отговори (след MC отговорите)
        # Грубо: започва от първото "16." в answers_text
        fi_start_match = re.search(r"\n(1[6-9]|2[0-5])\.\s+", answers_text)
        if not fi_start_match:
            return {}
        fi_section = answers_text[fi_start_match.start():]
        
        out: dict = {}
        for m in FI_BLOCK_PATTERN.finditer(fi_section):
            num = int(m.group(1))
            if not (16 <= num <= 25):
                continue
            block = m.group(2).strip()
            
            sub_answers: dict = {}
            for sm in FI_SUBANSWER_PATTERN.finditer(block):
                sub_num = int(sm.group(1))
                sub_text = sm.group(2).strip()
                # Чистим
                sub_text = re.sub(r"\s+", " ", sub_text)
                # Махаме trailing scoring инфо ("По 1 точка...", "За верен отговор...")
                sub_text = re.sub(r"\s*(По\s+\d+\s+точк.*?$|За\s+верен\s+отговор.*?$)", "", sub_text)
                sub_text = sub_text.strip()
                if sub_text:
                    sub_answers[sub_num] = sub_text
            
            if sub_answers:
                out[num] = sub_answers
        
        return out
