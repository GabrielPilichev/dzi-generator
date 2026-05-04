"""
Sanity test за BgGPT setup.

3 теста върху твоя domain (Информационни технологии):

  Test 1: Просто познание ("какво е VLOOKUP")
  Test 2: Педагогически контекст ("как да обясня X на 9 клас")
  Test 3: Структуриран output (JSON отговор)

Целта: да видим дали BgGPT работи прилично за твоя use case.
Ако Test 3 (структуриран JSON) се проваля — це е блокер за topic_classifier.
Ако Test 2 (педагогика) дава тривиални отговори — модела не е добър за пораждане.

Time budget per test: 30s. Ако bggpt 9b-q8 не отговори в това време,
има problem с RAM / GPU offloading.

Употреба:
    python3 test_bggpt.py [--model MODEL] [--host HOST]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Path hack — позволи import-а на ./agents/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.ollama_client import (
    OllamaClient,
    OllamaError,
    DEFAULT_CHAT_MODEL,
    DEFAULT_HOST,
)


def divider():
    print("\n" + "=" * 60)


def test_1_basic_knowledge(client: OllamaClient, model: str) -> bool:
    divider()
    print("🧪 TEST 1: Базово познание (домейн: ИТ)")
    print("=" * 60)
    
    question = "Какво е VLOOKUP функция в Excel и кога се използва?"
    print(f"\nВъпрос: {question}\n")
    
    try:
        result = client.chat(
            messages=[{"role": "user", "content": question}],
            model=model,
        )
    except OllamaError as e:
        print(f"❌ FAIL: {e}")
        return False
    
    print(f"⏱  {result['elapsed_seconds']}s | "
          f"prompt={result['prompt_eval_count']}t | "
          f"output={result['eval_count']}t")
    print(f"\nОтговор:\n{result['content']}")
    
    # Heuristic checks
    content_lower = result["content"].lower()
    has_keywords = any(kw in content_lower for kw in [
        "vlookup", "търси", "таблица", "стойност", "ключ", "колона"
    ])
    
    if has_keywords and len(result["content"]) > 80:
        print(f"\n✅ PASS — отговорът покрива темата")
        return True
    else:
        print(f"\n⚠️  WEAK — отговорът е къс или не покрива темата")
        return False


def test_2_pedagogical(client: OllamaClient, model: str) -> bool:
    divider()
    print("🧪 TEST 2: Педагогически контекст")
    print("=" * 60)
    
    system = (
        "Ти си учител по Информационни технологии в българско средно училище. "
        "Обяснявай ясно и кратко, на разбираем език за конкретния клас. "
        "Използвай примери, релевантни на ученическата ежедневност."
    )
    question = (
        "Обясни SUMIF на ученик в 9 клас. Дай 1 примерна задача с числа, "
        "така че да може да я въведе в Excel и да се убеди, че работи."
    )
    print(f"\nSystem: {system[:80]}...")
    print(f"\nВъпрос: {question}\n")
    
    try:
        result = client.chat(
            messages=[{"role": "user", "content": question}],
            model=model,
            system=system,
        )
    except OllamaError as e:
        print(f"❌ FAIL: {e}")
        return False
    
    print(f"⏱  {result['elapsed_seconds']}s | output={result['eval_count']}t")
    print(f"\nОтговор:\n{result['content']}")
    
    content = result["content"].lower()
    has_sumif = "sumif" in content
    has_example = any(c.isdigit() for c in result["content"])
    
    if has_sumif and has_example and len(result["content"]) > 150:
        print(f"\n✅ PASS — има SUMIF, числа в примера, прилична дължина")
        return True
    else:
        print(f"\n⚠️  WEAK")
        if not has_sumif: print("    - не споменава SUMIF")
        if not has_example: print("    - няма числа в примера")
        if len(result["content"]) <= 150: print("    - твърде късо")
        return False


def test_3_structured_json(client: OllamaClient, model: str) -> bool:
    """
    КРИТИЧЕН: ако този не работи, topic_classifier ще трябва да парсва свободен текст.
    """
    divider()
    print("🧪 TEST 3: Структуриран JSON output (CRITICAL)")
    print("=" * 60)
    
    system = (
        "Ти класифицираш ИТ въпроси по тема. "
        "Отговаряй САМО с JSON обект, без коментари, без markdown. "
        "Форматът е: {\"slug\": \"<topic_slug>\", \"confidence\": <0..1>}."
    )
    
    question = """Класифицирай този въпрос в една от следните теми:
- sumif (функцията SUMIF в електронни таблици)
- vlookup (функция VLOOKUP)
- pivot-table (обобщаващи таблици)
- html-forms (HTML формуляри)
- primary-key (първичен ключ в БД)

Въпрос: "Какъв тип трябва да бъде колоната, която съхранява уникалния идентификатор на записа в таблица?"

Отговор (само JSON):"""
    
    print(f"\nSystem: {system[:80]}...")
    print(f"\nQuery format: класифицирай въпрос → JSON\n")
    
    try:
        result = client.chat(
            messages=[{"role": "user", "content": question}],
            model=model,
            system=system,
            options={"temperature": 0.0},  # за structured output
        )
    except OllamaError as e:
        print(f"❌ FAIL: {e}")
        return False
    
    print(f"⏱  {result['elapsed_seconds']}s | output={result['eval_count']}t")
    print(f"\nRaw отговор:\n{result['content']}")
    
    # Try parse JSON
    raw = result["content"]
    
    # Strip common prefixes/suffixes (markdown fencing, чужд текст)
    # Find first { and last }
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1:
        print(f"\n❌ FAIL — няма {{ }} в отговора")
        return False
    
    json_str = raw[start:end + 1]
    try:
        parsed = json.loads(json_str)
    except json.JSONDecodeError as e:
        print(f"\n❌ FAIL — JSON parse error: {e}")
        return False
    
    print(f"\nParsed JSON: {parsed}")
    
    if "slug" not in parsed:
        print(f"\n❌ FAIL — липсва ключ 'slug'")
        return False
    
    expected = "primary-key"
    actual = parsed["slug"]
    if actual == expected:
        print(f"\n✅ PASS — правилен slug ({expected})")
        return True
    else:
        print(f"\n⚠️  Wrong slug: '{actual}' (expected '{expected}')")
        print(f"    JSON структурата е валидна, но семантиката е грешна.")
        print(f"    За batch classification ще трябва да приемем по-нисък accuracy.")
        return False


# ============================================================
# Main
# ============================================================

def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--model", default=DEFAULT_CHAT_MODEL)
    p.add_argument("--host", default=DEFAULT_HOST)
    args = p.parse_args()
    
    print(f"🤖 BgGPT Sanity Test")
    print(f"   Model: {args.model}")
    print(f"   Host:  {args.host}")
    
    client = OllamaClient(host=args.host)
    
    if not client.is_alive():
        print(f"\n❌ Ollama не е достъпна на {args.host}")
        print(f"   Стартирай: ollama serve")
        sys.exit(1)
    
    models = client.list_models()
    model_names = [m.get("name", "") for m in models]
    if args.model not in model_names:
        print(f"\n❌ Модел '{args.model}' не е инсталиран.")
        print(f"   Pull-ни го: ollama pull {args.model}")
        print(f"   Инсталирани модели:")
        for n in model_names:
            print(f"      {n}")
        sys.exit(1)
    
    results = []
    results.append(("Basic knowledge", test_1_basic_knowledge(client, args.model)))
    results.append(("Pedagogical", test_2_pedagogical(client, args.model)))
    results.append(("Structured JSON", test_3_structured_json(client, args.model)))
    
    divider()
    print("📊 SUMMARY")
    print("=" * 60)
    for name, ok in results:
        print(f"   {'✅' if ok else '❌'}  {name}")
    
    passed = sum(1 for _, ok in results if ok)
    print(f"\n   {passed}/{len(results)} tests passed")
    
    if passed == len(results):
        print(f"\n🎉 BgGPT работи добре. Готови сме за topic_classifier.")
    elif passed >= 2:
        print(f"\n⚠️  BgGPT работи отчасти. Може да продължим с classifier,")
        print(f"   но трябва да очакваме грешки и да валидираме на batch.")
    else:
        print(f"\n❌ BgGPT не отговаря на изискванията. Трябва ескалация към")
        print(f"   Hermes/Gemini за production tasks.")


if __name__ == "__main__":
    main()
