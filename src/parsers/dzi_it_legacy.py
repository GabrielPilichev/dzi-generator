"""
Parser за ДЗИ ИТ — стар формат (преди 2023).

Особености:
  * Май/Авг 2022 имат 10 multiple choice + 4 fill-in задачи (НЕ 15+10)
  * Формулировките са малко по-различни ("точки" може да отсъства)
  * Опитваме се да обхванем и пробните ДЗИ от 2007-2009

Това е stub-имплементация — основното е detect() който различава от modern формата.
Детайлите на parse() са по-консервативни (ако много неща не съвпадат, връщаме малко
въпроси с warnings, вместо да крашваме).
"""

from __future__ import annotations

import re

from .base import (
    ExamParser,
    ParsedExam,
    ParsedQuestion,
    ParsedOption,
    ParsedSubquestion,
)
from .registry import register_parser
from .dzi_it_modern import (
    OPTION_PATTERN,
    ANSWER_KEY_MARKER,
    classify_topic,
    clean_option_text,
    parse_header,
)


# Legacy MC: задачи 1-10 (вместо 1-15)
LEGACY_MC_PATTERN = re.compile(
    r"(?:^|\n)([1-9]|10)\.\s*(.+?)"
    r"(?=(?:\n(?:[1-9]|1[0-4])\.\s*[А-Яа-яA-Za-z])|(?:\nОтговорите))",
    re.DOTALL,
)

# Legacy fill-in: задачи 11-14 (4 fill-in вместо 10)
LEGACY_FI_PATTERN = re.compile(
    r"(?:^|\n)(1[1-4])\.\s*(.+?)"
    r"(?=(?:\n(?:1[1-5])\.\s*)|(?:\nЗадача\s*1[5-9]\.)|(?:\nЧАСТ\s*2)|\Z)",
    re.DOTALL,
)


@register_parser
class DziItLegacyParser(ExamParser):
    """ДЗИ ИТ — стар формат (преди 2023). 10 MC + 4 fill-in."""
    
    SUBJECT = "informatika_it"
    LEVEL = "DZI"
    FORMAT_VERSION = "legacy_pre2023"
    SUPPORTED_RANGE = "2007-2022 (предимно Май/Август 2022)"
    
    def detect(self, text: str) -> float:
        """
        Detection signals:
          * „зрелостен" / „матур" в шапката → +0.2
          * Точно 10 MC (не повече) → +0.3
          * Задачи 11-14 присъстват → +0.2
          * НЕ присъстват задачи 15+ MC → +0.2
          * „Информационни технологии" → +0.1
        """
        head = text[:3000].lower()
        score = 0.0
        
        if "зрелостен" in head or "матур" in head:
            score += 0.2
        if "информационни технологии" in head:
            score += 0.1
        
        # Брой MC задачи (1-10 трябва да съществуват)
        mc_count = 0
        for m in LEGACY_MC_PATTERN.finditer(text):
            num = int(m.group(1))
            if 1 <= num <= 10:
                mc_count += 1
        
        if mc_count >= 9:
            score += 0.3
        elif mc_count >= 6:
            score += 0.15
        
        # Fill-in 11-14
        fi_count = sum(1 for _ in LEGACY_FI_PATTERN.finditer(text))
        if fi_count >= 3:
            score += 0.2
        
        # АНТИ-сигнал: ако има задачи 11-15 като MC (не като fill-in), значи е modern
        # Намираме „11. ... А) Б)" pattern
        if re.search(r"\n11\.\s.+?А\)", text, re.DOTALL):
            score -= 0.3
        if re.search(r"\n15\.\s.+?А\)", text, re.DOTALL):
            score -= 0.2
        
        return max(0.0, min(score, 1.0))
    
    def parse(self, text: str) -> ParsedExam:
        header = parse_header(text)
        
        if ANSWER_KEY_MARKER in text:
            split_idx = text.index(ANSWER_KEY_MARKER)
            questions_text = text[:split_idx]
            answers_text = text[split_idx:]
        else:
            questions_text = text
            answers_text = ""
        
        mc_questions = self._parse_mc(questions_text)
        fi_questions = self._parse_fi(questions_text)
        
        # Answer key
        mc_answers = self._parse_mc_answers(answers_text)
        for q in mc_questions:
            correct = mc_answers.get(q.number)
            if correct:
                for opt in q.options:
                    if opt.letter == correct:
                        opt.is_correct = True
                        break
        
        for q in mc_questions + fi_questions:
            q.topic = classify_topic(q.prompt)
        
        return ParsedExam(
            subject=self.SUBJECT,
            level=self.LEVEL,
            year=header["year"],
            session=header["session"],
            variant=header["variant"],
            format_version=self.FORMAT_VERSION,
            questions=mc_questions + fi_questions,
            parser_used=self.__class__.__name__,
            raw_text_length=len(text),
            confidence=self.detect(text),
            notes=["Legacy parser: fill-in отговорите не са добре поддържани, "
                   "защото структурата на ключа варира между години."],
        )
    
    @staticmethod
    def _parse_mc(text: str) -> list:
        out: list = []
        seen = set()
        for m in LEGACY_MC_PATTERN.finditer(text):
            num = int(m.group(1))
            if num > 10 or num in seen:
                continue
            seen.add(num)
            
            block = m.group(2).strip()
            options_raw = OPTION_PATTERN.findall(block)
            if len(options_raw) < 2:
                continue
            
            seen_letters = set()
            options = []
            for letter, raw_text in options_raw:
                if letter in seen_letters:
                    continue
                seen_letters.add(letter)
                options.append(ParsedOption(
                    letter=letter,
                    text=clean_option_text(raw_text),
                ))
            
            if len(options) != 4:
                continue
            
            first_opt = block.find("А)")
            if first_opt == -1:
                continue
            prompt = block[:first_opt].strip()
            if len(prompt) < 10:
                continue
            
            out.append(ParsedQuestion(
                number=num,
                question_type="multiple_choice",
                prompt=prompt,
                points=1,
                options=options,
            ))
        return out
    
    @staticmethod
    def _parse_fi(text: str) -> list:
        out: list = []
        seen = set()
        for m in LEGACY_FI_PATTERN.finditer(text):
            num = int(m.group(1))
            if num < 11 or num > 14 or num in seen:
                continue
            seen.add(num)
            
            block = m.group(2).strip()
            if len(block) < 20:
                continue
            
            out.append(ParsedQuestion(
                number=num,
                question_type="fill_in",
                prompt=block,
                points=3,
                warnings=["legacy_format"],
                quality_score=0.7,
            ))
        return out
    
    @staticmethod
    def _parse_mc_answers(answers_text: str) -> dict:
        out = {}
        for m in re.finditer(r"(?:^|\n)([1-9]|10)\.\s+([АБВГ])", answers_text, re.MULTILINE):
            out[int(m.group(1))] = m.group(2)
        return out
