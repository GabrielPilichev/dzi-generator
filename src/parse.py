"""
ДЗИ Generator — PDF Parser
Извлича задачи 1-25 от PDF на ДЗИ изпит и ги записва в SQLite DB.

Използване:
    python parse.py path/to/exam.pdf path/to/answers.pdf
    
Където answers.pdf е същият или отделен файл, съдържащ ключа с отговорите.
В ДЗИ изпитите ключът обикновено е във втората половина на файла.
"""

import sys
import re
import sqlite3
import argparse
from pathlib import Path
import pdfplumber


# ============================================================
# Parser logic
# ============================================================

def extract_full_text(pdf_path: str) -> str:
    """Извлича целия текст от PDF като един стринг."""
    with pdfplumber.open(pdf_path) as pdf:
        pages_text = []
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                pages_text.append(text)
        return "\n".join(pages_text)


def split_questions_and_answers(full_text: str) -> tuple[str, str]:
    """
    Разделя текста на 'въпроси' и 'ключ с отговори'.
    Връща (questions_section, answers_section).
    """
    # Ключът с отговори започва с "Ключ с верните отговори"
    answer_marker = "Ключ с верните отговори"
    
    if answer_marker in full_text:
        idx = full_text.index(answer_marker)
        return full_text[:idx], full_text[idx:]
    
    return full_text, ""


def parse_multiple_choice(text: str) -> list[dict]:
    """
    Извлича задачи 1-15 (multiple choice).
    Връща списък от dict-ове с keys: number, prompt, options
    """
    questions = []
    
    # Pattern за multiple choice: започва с "N." и има А) Б) В) Г)
    # Spliт-ваме по номера на задача в началото на ред
    pattern = r"(?:^|\n)(\d+)\.\s+(.+?)(?=(?:\n\d+\.\s)|(?:\nОтговорите на задачите))"
    
    matches = re.finditer(pattern, text, re.DOTALL)
    
    for match in matches:
        num = int(match.group(1))
        if num > 15:
            break
        
        block = match.group(2).strip()
        
        # Намираме опциите А) Б) В) Г)
        # Pattern: "А) текст" до следващата опция или края
        option_pattern = r"([АБВГ])\)\s*(.+?)(?=\n[АБВГ]\)|\Z)"
        options = re.findall(option_pattern, block, re.DOTALL)
        
        if len(options) < 2:
            continue  # Не е валиден multiple choice
        
        # Promptа е всичко преди първата опция
        first_option_idx = block.find("А)")
        if first_option_idx == -1:
            continue
        prompt = block[:first_option_idx].strip()
        
        questions.append({
            "number": num,
            "type": "multiple_choice",
            "prompt": prompt,
            "options": [
                {"letter": letter, "text": text.strip()}
                for letter, text in options
            ]
        })
    
    return questions


def parse_fill_in(text: str) -> list[dict]:
    """
    Извлича задачи 16-25 (fill-in).
    Връща списък от dict-ове.
    """
    questions = []
    
    # Намираме секцията започваща с "Отговорите на задачите от 16."
    section_marker = "Отговорите на задачите от 16."
    if section_marker in text:
        text = text[text.index(section_marker):]
    
    # Split по номер на задача
    # Спираме на началото на Част 2 (задача 26) или края
    pattern = r"(?:^|\n)(1[6-9]|2[0-5])\.\s+(.+?)(?=(?:\n(?:1[6-9]|2[0-5])\.\s)|(?:ЧАСТ 2)|(?:Задача 26\.))"
    
    matches = re.finditer(pattern, text, re.DOTALL)
    
    for match in matches:
        num = int(match.group(1))
        if num < 16 or num > 25:
            continue
        
        block = match.group(2).strip()
        
        questions.append({
            "number": num,
            "type": "fill_in",
            "prompt": block,
        })
    
    return questions


def parse_answer_key(answers_text: str) -> dict:
    """
    Парсва ключа с отговори. Връща dict с key=question_number, value=answer.
    
    За multiple_choice (1-15): answer е една буква "А"/"Б"/"В"/"Г"
    За fill_in (16-25): answer е dict с key=subquestion_number, value=text
    """
    answers = {}
    
    # ===== Part 1: задачи 1-15 (multiple choice) =====
    # Pattern: "1. В 1" където В е верният отговор
    mc_pattern = r"(\d+)\.\s+([АБВГ])\s+\d"
    for match in re.finditer(mc_pattern, answers_text):
        num = int(match.group(1))
        if 1 <= num <= 15:
            answers[num] = match.group(2)
    
    # ===== Part 2: задачи 16-25 (fill-in) =====
    # Pattern: "16. – 3 точки\n(1) 10250\n(2) 10200\n(3) 10150"
    fi_pattern = r"(1[6-9]|2[0-5])\.\s*[–-]\s*\d+\s*точки\s*\n(.+?)(?=(?:\n(?:1[6-9]|2[0-5])\.\s*[–-])|(?:\n26\.\s*[–-])|\Z)"
    
    for match in re.finditer(fi_pattern, answers_text, re.DOTALL):
        num = int(match.group(1))
        body = match.group(2).strip()
        
        # Извличаме (1) (2) (3) отговори
        sub_answers = {}
        sub_pattern = r"\((\d+)\)\s+(.+?)(?=\(\d+\)|По\s+\d|За\s+верен|\Z)"
        for sub_match in re.finditer(sub_pattern, body, re.DOTALL):
            sub_num = int(sub_match.group(1))
            sub_answer = sub_match.group(2).strip()
            # Изчистваме trailing нюанси
            sub_answer = re.sub(r"\s+", " ", sub_answer).strip()
            sub_answers[sub_num] = sub_answer
        
        if sub_answers:
            answers[num] = sub_answers
    
    return answers


def classify_topic(prompt: str) -> str:
    """Прост topic classifier на базата на ключови думи."""
    prompt_lower = prompt.lower()
    
    keywords = {
        "spreadsheets": ["електронна таблица", "формула", "клетка", "sumif", "countif", "if(", "обобщаваща таблица", "pivot"],
        "databases": ["база данни", "релационна", "таблица", "заявка", "първичен ключ", "релация", "books", "many to many"],
        "web": ["html", "css", "уеб", "сайт", "браузър", "форма", "textarea", "сем", "seo", "хипервр"],
        "graphics": ["графика", "пиксел", "вектор", "растер", "филтър", "цвят", "шрифт"],
        "video_audio": ["видео", "аудио", "mp3", "звук", "кадър", "филм", "оператор"],
        "info_systems": ["информационна система", "етап", "проектиране", "разработване", "внедряване", "софтуерен проект"],
        "hardware": ["ram", "rom", "процесор", "хардуер", "диск", "карта"],
        "security": ["защита", "парола", "криптир", "https", "патент", "лиценз", "марка", "защитна стена", "firewall"],
        "protocols": ["протокол", "http", "smtp", "ftp", "ip"],
        "encoding": ["unicode", "кодиране", "ascii", "utf"],
    }
    
    for topic, words in keywords.items():
        if any(w in prompt_lower for w in words):
            return topic
    
    return "general"


# ============================================================
# Database operations
# ============================================================

def insert_questions_into_db(
    db_path: str,
    source_exam: str,
    multiple_choice: list[dict],
    fill_in: list[dict],
    answers: dict,
):
    """Записва извлечените въпроси в DB."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    inserted_mc = 0
    inserted_fi = 0
    
    # ===== Multiple choice =====
    for q in multiple_choice:
        topic = classify_topic(q["prompt"])
        correct_letter = answers.get(q["number"])
        
        cursor.execute("""
            INSERT INTO questions (source_exam, source_number, question_type, topic, points, prompt)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (source_exam, q["number"], "multiple_choice", topic, 1, q["prompt"]))
        
        question_id = cursor.lastrowid
        
        for opt in q["options"]:
            is_correct = 1 if opt["letter"] == correct_letter else 0
            cursor.execute("""
                INSERT INTO multiple_choice_options (question_id, option_letter, option_text, is_correct)
                VALUES (?, ?, ?, ?)
            """, (question_id, opt["letter"], opt["text"], is_correct))
        
        inserted_mc += 1
    
    # ===== Fill-in =====
    for q in fill_in:
        topic = classify_topic(q["prompt"])
        sub_answers = answers.get(q["number"], {})
        
        cursor.execute("""
            INSERT INTO questions (source_exam, source_number, question_type, topic, points, prompt)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (source_exam, q["number"], "fill_in", topic, 3, q["prompt"]))
        
        question_id = cursor.lastrowid
        
        for sub_num, sub_answer in sub_answers.items():
            cursor.execute("""
                INSERT INTO fill_in_subquestions (question_id, subquestion_number, correct_answer, points)
                VALUES (?, ?, ?, ?)
            """, (question_id, sub_num, sub_answer, 1))
        
        inserted_fi += 1
    
    conn.commit()
    conn.close()
    
    return inserted_mc, inserted_fi


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="Парсва ДЗИ PDF в DB")
    parser.add_argument("pdf", help="Път до PDF на изпита (включва ключа с отговори)")
    parser.add_argument("--db", default="data/questions.db", help="Път до SQLite DB")
    parser.add_argument("--source", default=None, help="Идентификатор на изпита (напр. 'may_2025_v2')")
    args = parser.parse_args()
    
    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        print(f"❌ Не намирам файл: {pdf_path}")
        sys.exit(1)
    
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"❌ Не намирам DB: {db_path}")
        print("   Първо създай DB: python -c \"import sqlite3; conn=sqlite3.connect('data/questions.db'); conn.executescript(open('src/schema.sql').read())\"")
        sys.exit(1)
    
    source_exam = args.source or pdf_path.stem
    
    print(f"📄 Чета PDF: {pdf_path}")
    full_text = extract_full_text(str(pdf_path))
    
    print(f"✂️  Разделям въпроси/отговори...")
    questions_text, answers_text = split_questions_and_answers(full_text)
    
    print(f"🔍 Парсвам multiple choice (1-15)...")
    mc = parse_multiple_choice(questions_text)
    print(f"   Намерени: {len(mc)} задачи")
    
    print(f"🔍 Парсвам fill-in (16-25)...")
    fi = parse_fill_in(questions_text)
    print(f"   Намерени: {len(fi)} задачи")
    
    print(f"🔑 Парсвам ключа с отговорите...")
    answers = parse_answer_key(answers_text)
    print(f"   Намерени отговори за {len(answers)} задачи")
    
    print(f"💾 Записвам в DB: {db_path}")
    inserted_mc, inserted_fi = insert_questions_into_db(
        str(db_path), source_exam, mc, fi, answers
    )
    
    print(f"\n✅ Готово!")
    print(f"   Multiple choice: {inserted_mc} въпроса")
    print(f"   Fill-in: {inserted_fi} въпроса")
    print(f"   Source: {source_exam}")


if __name__ == "__main__":
    main()
