"""
ДЗИ Generator — PDF Parser (v2)
Извлича задачи 1-25 от PDF на ДЗИ изпит и ги записва в SQLite DB.

Промени в v2:
- Поправен regex който приема "4.Текст" (без интервал)
- По-агресивно почистване на дублирани опции
- По-добро handling на edge cases
"""

import sys
import re
import sqlite3
import argparse
from pathlib import Path
import pdfplumber


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
    """Разделя текста на 'въпроси' и 'ключ с отговори'."""
    answer_marker = "Ключ с верните отговори"
    if answer_marker in full_text:
        idx = full_text.index(answer_marker)
        return full_text[:idx], full_text[idx:]
    return full_text, ""


def parse_multiple_choice(text: str) -> list[dict]:
    """
    Извлича задачи 1-15 (multiple choice).
    v2: regex приема "4.Текст" (без интервал след точката).
    """
    questions = []
    
    # КЛЮЧОВА ПРОМЯНА: \s* вместо \s+ — приема "4.Изискванията" (0 или повече whitespace)
    pattern = r"(?:^|\n)(\d+)\.\s*(.+?)(?=(?:\n\d+\.\s*[А-Яа-я])|(?:\nОтговорите на задачите))"
    
    matches = re.finditer(pattern, text, re.DOTALL)
    
    for match in matches:
        num = int(match.group(1))
        if num > 15:
            break
        
        block = match.group(2).strip()
        
        # Намираме опциите А) Б) В) Г)
        option_pattern = r"([АБВГ])\)\s*(.+?)(?=\n[АБВГ]\)|\Z)"
        options_raw = re.findall(option_pattern, block, re.DOTALL)
        
        if len(options_raw) < 2:
            continue
        
        # ВАЖНО: dedup-ваме опции с еднаква буква (бъг от parsing)
        # Ако има дубликат, взимаме само първия
        seen_letters = set()
        options = []
        for letter, text_opt in options_raw:
            if letter not in seen_letters:
                options.append({"letter": letter, "text": text_opt.strip()})
                seen_letters.add(letter)
        
        # Изисква 4 уникални опции
        if len(options) != 4:
            continue
        
        # Promptа е всичко преди първата опция
        first_option_idx = block.find("А)")
        if first_option_idx == -1:
            continue
        prompt = block[:first_option_idx].strip()
        
        # Skip ако prompt-ът е твърде къс (вероятно бъг)
        if len(prompt) < 10:
            continue
        
        questions.append({
            "number": num,
            "type": "multiple_choice",
            "prompt": prompt,
            "options": options
        })
    
    # Dedup по номер (ако regex е намерил един въпрос два пъти)
    seen_nums = set()
    unique_questions = []
    for q in questions:
        if q["number"] not in seen_nums:
            unique_questions.append(q)
            seen_nums.add(q["number"])
    
    return unique_questions


def parse_fill_in(text: str) -> list[dict]:
    """Извлича задачи 16-25 (fill-in)."""
    questions = []
    
    section_marker = "Отговорите на задачите от 16."
    if section_marker in text:
        text = text[text.index(section_marker):]
    
    # \s* вместо \s+
    pattern = r"(?:^|\n)(1[6-9]|2[0-5])\.\s*(.+?)(?=(?:\n(?:1[6-9]|2[0-5])\.\s*)|(?:ЧАСТ 2)|(?:Задача 26\.))"
    
    matches = re.finditer(pattern, text, re.DOTALL)
    
    seen_nums = set()
    for match in matches:
        num = int(match.group(1))
        if num < 16 or num > 25 or num in seen_nums:
            continue
        seen_nums.add(num)
        
        block = match.group(2).strip()
        if len(block) < 20:
            continue
        
        questions.append({
            "number": num,
            "type": "fill_in",
            "prompt": block,
        })
    
    return questions


def parse_answer_key(answers_text: str) -> dict:
    """Парсва ключа с отговори."""
    answers = {}
    
    # Multiple choice answers: "1. В 1"
    mc_pattern = r"(\d+)\.\s+([АБВГ])\s+\d"
    for match in re.finditer(mc_pattern, answers_text):
        num = int(match.group(1))
        if 1 <= num <= 15:
            answers[num] = match.group(2)
    
    # Fill-in answers
    fi_pattern = r"(1[6-9]|2[0-5])\.\s*[–-]\s*\d+\s*точки\s*\n(.+?)(?=(?:\n(?:1[6-9]|2[0-5])\.\s*[–-])|(?:\n26\.\s*[–-])|\Z)"
    
    for match in re.finditer(fi_pattern, answers_text, re.DOTALL):
        num = int(match.group(1))
        body = match.group(2).strip()
        
        sub_answers = {}
        sub_pattern = r"\((\d+)\)\s+(.+?)(?=\(\d+\)|По\s+\d|За\s+верен|\Z)"
        for sub_match in re.finditer(sub_pattern, body, re.DOTALL):
            sub_num = int(sub_match.group(1))
            sub_answer = sub_match.group(2).strip()
            sub_answer = re.sub(r"\s+", " ", sub_answer).strip()
            sub_answers[sub_num] = sub_answer
        
        if sub_answers:
            answers[num] = sub_answers
    
    return answers


def classify_topic(prompt: str) -> str:
    """Прост topic classifier."""
    prompt_lower = prompt.lower()
    keywords = {
        "spreadsheets": ["електронна таблица", "формула", "клетка", "sumif", "countif", "if(", "обобщаваща таблица", "pivot"],
        "databases": ["база данни", "релационна", "заявка", "първичен ключ", "релация", "books", "many to many"],
        "web": ["html", "css", "уеб", "сайт", "браузър", "форма", "textarea", "сем", "seo", "хипервр", "хостинг"],
        "graphics": ["графика", "пиксел", "вектор", "растер", "филтър", "цвят", "шрифт", "трасиране", "обектив", "матрица"],
        "video_audio": ["видео", "аудио", "mp3", "звук", "кадър", "филм", "оператор", "монтаж", "субтитр", "квантуване", "дискретизация"],
        "info_systems": ["информационна система", "етап", "проектиране", "разработване", "внедряване", "софтуерен проект", "архитектура"],
        "hardware": ["ram", "rom", "процесор", "хардуер", "диск", "карта", "разрядност"],
        "security": ["защита", "парола", "криптир", "https", "патент", "лиценз", "марка", "защитна стена", "firewall", "ddos", "xss"],
        "protocols": ["протокол", "http", "smtp", "ftp", "ip"],
        "encoding": ["unicode", "кодиране", "ascii", "utf"],
    }
    for topic, words in keywords.items():
        if any(w in prompt_lower for w in words):
            return topic
    return "general"


def insert_questions_into_db(db_path, source_exam, multiple_choice, fill_in, answers):
    """Записва извлечените въпроси в DB."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    inserted_mc = 0
    inserted_fi = 0
    skipped = 0
    
    for q in multiple_choice:
        topic = classify_topic(q["prompt"])
        correct_letter = answers.get(q["number"])
        
        try:
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
        except sqlite3.IntegrityError as e:
            print(f"   ⚠️  Skip MC #{q['number']}: {e}")
            skipped += 1
            conn.rollback()
            continue
    
    for q in fill_in:
        topic = classify_topic(q["prompt"])
        sub_answers = answers.get(q["number"], {})
        
        try:
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
        except sqlite3.IntegrityError as e:
            print(f"   ⚠️  Skip FI #{q['number']}: {e}")
            skipped += 1
            conn.rollback()
            continue
    
    conn.commit()
    conn.close()
    
    return inserted_mc, inserted_fi, skipped


def main():
    parser = argparse.ArgumentParser(description="Парсва ДЗИ PDF в DB")
    parser.add_argument("pdf", help="Път до PDF на изпита")
    parser.add_argument("--db", default="data/questions.db")
    parser.add_argument("--source", default=None)
    args = parser.parse_args()
    
    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        print(f"❌ Не намирам файл: {pdf_path}")
        sys.exit(1)
    
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"❌ Не намирам DB: {db_path}")
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
    inserted_mc, inserted_fi, skipped = insert_questions_into_db(
        str(db_path), source_exam, mc, fi, answers
    )
    
    print(f"\n✅ Готово!")
    print(f"   Multiple choice: {inserted_mc} въпроса")
    print(f"   Fill-in: {inserted_fi} въпроса")
    if skipped > 0:
        print(f"   Skipped: {skipped} (вече съществуващи или дубликати)")
    print(f"   Source: {source_exam}")


if __name__ == "__main__":
    main()
