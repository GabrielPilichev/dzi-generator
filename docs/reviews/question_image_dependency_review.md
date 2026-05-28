# Question Image Dependency Review

Manual review worksheet generated from `src/audit_question_image_dependencies.py`.
This file is for review only. Do not treat it as an import manifest.

## Summary

- DB: `data/questions.db`
- Questions inspected: 832
- Likely image-dependent candidates: 28
- Tool prefilled suggestions in this file: 16
- Covered by existing usable visual: 0
- Needs mapping or file fix: 28

## Category Counts

| Category | Count |
|---|---:|
| chart/table | 4 |
| diagram | 12 |
| screenshot/UI | 3 |
| unknown visual dependency | 9 |

## Instructions

For each candidate, fill the manual fields before any asset import or DB update:

- `needs_image`: `yes`, `no`, or `unclear`.
- `asset_source`: `official_exam_pdf`, existing resource ZIP path, or other reviewed source.
- `expected_asset_filename`: deterministic filename, normally `<source_slug>_q<source_number>.png`.
- `notes`: reviewer context, uncertainty, or reason for rejection.
- `suggested_by_tool`: `yes` means blank fields were pre-filled by this script and still need human approval.
  After verification, change it to `no` or leave it blank so validation can treat the row as human-reviewed.

## Candidates

### 1. Q182 — aug_2022_v2 #2

- question_id: `182`
- source/exam: `aug_2022_v2`
- source_number: `2`
- question_type: `multiple_choice`
- topic: `access-select-queries` — Заявки за избиране в база данни
- section(s): 11. клас: Проектиране и създаване на бази от данни и на информационни системи (grade11-m1-databases-and-information-systems); 11. клас: Обобщение — Обработка и анализ на данни (grade11-m1-summary); 11. клас: Обобщение — Мултимедия (grade11-m2-summary); 12. клас: Подготовка за матура (grade12-dzi-preparation)
- detected_category: `screenshot/UI`
- confidence: `high`
- asset_status: `needs_mapping`
- visual_asset_count: 0
- missing_visual_asset_count: 0
- reason: screenshot/UI: 'показана заявка' (shown query reference, high); unknown visual dependency: 'изображението' (image reference, high); unknown visual dependency: 'на изображението' (image reference, high)

Question text snippet:

> На изображението по-долу е показана заявка. Отбележете типа на заявката:

Review fields:

- needs_image: yes
- asset_source: official_exam_pdf
- expected_asset_filename: aug_2022_v2_q2.png
- notes: 
- suggested_by_tool: yes

---

### 2. Q200 — aug_2022_v2 #23

- question_id: `200`
- source/exam: `aug_2022_v2`
- source_number: `23`
- question_type: `fill_in`
- topic: `relational-model` — Релационен модел
- section(s): 11. клас: Проектиране и създаване на бази от данни и на информационни системи (grade11-m1-databases-and-information-systems); 11. клас: Обобщение — Обработка и анализ на данни (grade11-m1-summary); 11. клас: Обобщение — Мултимедия (grade11-m2-summary); 12. клас: Подготовка за матура (grade12-dzi-preparation)
- detected_category: `unknown visual dependency`
- confidence: `high`
- asset_status: `needs_mapping`
- visual_asset_count: 0
- missing_visual_asset_count: 0
- reason: unknown visual dependency: 'изображението' (image reference, high)

Question text snippet:

> База от данни съдържа две таблици - едната с информация за автомобилни части в магазин за авточасти, а другата - с данн…

Review fields:

- needs_image: yes
- asset_source: official_exam_pdf
- expected_asset_filename: aug_2022_v2_q23.png
- notes: 
- suggested_by_tool: yes

---

### 3. Q201 — aug_2022_v2 #24

- question_id: `201`
- source/exam: `aug_2022_v2`
- source_number: `24`
- question_type: `fill_in`
- topic: `raster-vs-vector` — Растер vs вектор
- section(s): 11. клас: Обобщение — Обработка и анализ на данни (grade11-m1-summary); 11. клас: Обработка на растерни изображения (grade11-m2-raster-images); 11. клас: Създаване и обработка на векторни изображения (grade11-m2-vector-images); 11. клас: Обобщение — Мултимедия (grade11-m2-summary); 12. клас: Подготовка за матура (grade12-dzi-preparation)
- detected_category: `unknown visual dependency`
- confidence: `high`
- asset_status: `needs_mapping`
- visual_asset_count: 0
- missing_visual_asset_count: 0
- reason: unknown visual dependency: 'изображението' (image reference, high)

Question text snippet:

> Разгледайте изображението по-долу. Напишете в листа за отговори как се нарича полиномна параметрична крива (набор от ди…

Review fields:

- needs_image: yes
- asset_source: official_exam_pdf
- expected_asset_filename: aug_2022_v2_q24.png
- notes: 
- suggested_by_tool: yes

---

### 4. Q84 — aug_2023 #19

- question_id: `84`
- source/exam: `aug_2023`
- source_number: `19`
- question_type: `fill_in`
- topic: `raster-vs-vector` — Растер vs вектор
- section(s): 11. клас: Обобщение — Обработка и анализ на данни (grade11-m1-summary); 11. клас: Обработка на растерни изображения (grade11-m2-raster-images); 11. клас: Създаване и обработка на векторни изображения (grade11-m2-vector-images); 11. клас: Обобщение — Мултимедия (grade11-m2-summary); 12. клас: Подготовка за матура (grade12-dzi-preparation)
- detected_category: `unknown visual dependency`
- confidence: `high`
- asset_status: `needs_mapping`
- visual_asset_count: 0
- missing_visual_asset_count: 0
- reason: unknown visual dependency: 'изображението' (image reference, high); unknown visual dependency: 'на изображението' (image reference, high)

Question text snippet:

> Запишете термините, допълващи правилно следните изречения: (В листа за отговори напишете (1), (2) и (3) и срещу тях зап…

Review fields:

- needs_image: yes
- asset_source: official_exam_pdf
- expected_asset_filename: aug_2023_q19.png
- notes: 
- suggested_by_tool: yes

---

### 5. Q88 — aug_2023 #23

- question_id: `88`
- source/exam: `aug_2023`
- source_number: `23`
- question_type: `fill_in`
- topic: `pivot-table` — Pivot Table и Pivot Chart
- section(s): 11. клас: Приложение на електронни таблици за обработка на големи обеми от данни (grade11-m1-spreadsheets-big-data); 11. клас: Обобщение — Обработка и анализ на данни (grade11-m1-summary); 11. клас: Обобщение — Мултимедия (grade11-m2-summary); 12. клас: Подготовка за матура (grade12-dzi-preparation)
- detected_category: `diagram`
- confidence: `medium`
- asset_status: `needs_mapping`
- visual_asset_count: 0
- missing_visual_asset_count: 0
- reason: diagram: 'диаграмата' (diagram reference, medium)

Question text snippet:

> В листа за отговори напишете (1), (2) и (3) и срещу тях съответно наименованието на диаграмата. (1) (2) (3)

Review fields:

- needs_image: 
- asset_source: 
- expected_asset_filename: 
- notes: 
- suggested_by_tool: 

---

### 6. Q89 — aug_2023 #24

- question_id: `89`
- source/exam: `aug_2023`
- source_number: `24`
- question_type: `fill_in`
- topic: `countif` — COUNTIF
- section(s): 11. клас: Приложение на електронни таблици за обработка на големи обеми от данни (grade11-m1-spreadsheets-big-data); 11. клас: Обобщение — Обработка и анализ на данни (grade11-m1-summary); 11. клас: Обобщение — Мултимедия (grade11-m2-summary); 12. клас: Подготовка за матура (grade12-dzi-preparation)
- detected_category: `chart/table`
- confidence: `high`
- asset_status: `needs_mapping`
- visual_asset_count: 0
- missing_visual_asset_count: 0
- reason: chart/table: 'разгледайте графиката' (shown chart/graph reference, high); chart/table: 'графиката' (chart/graph reference, medium)

Question text snippet:

> Разгледайте графиката на получените резултати от проведено анкетно проучване. Като знаете броя на анкетираните, които с…

Review fields:

- needs_image: yes
- asset_source: official_exam_pdf
- expected_asset_filename: aug_2023_q24.png
- notes: 
- suggested_by_tool: yes

---

### 7. Q239 — aug_2023_v2 #19

- question_id: `239`
- source/exam: `aug_2023_v2`
- source_number: `19`
- question_type: `fill_in`
- topic: `raster-vs-vector` — Растер vs вектор
- section(s): 11. клас: Обобщение — Обработка и анализ на данни (grade11-m1-summary); 11. клас: Обработка на растерни изображения (grade11-m2-raster-images); 11. клас: Създаване и обработка на векторни изображения (grade11-m2-vector-images); 11. клас: Обобщение — Мултимедия (grade11-m2-summary); 12. клас: Подготовка за матура (grade12-dzi-preparation)
- detected_category: `unknown visual dependency`
- confidence: `high`
- asset_status: `needs_mapping`
- visual_asset_count: 0
- missing_visual_asset_count: 0
- reason: unknown visual dependency: 'изображението' (image reference, high); unknown visual dependency: 'на изображението' (image reference, high)

Question text snippet:

> Запишете термините, допълващи правилно следните изречения: (В листа за отговори напишете (1), (2) и (3) и срещу тях зап…

Review fields:

- needs_image: yes
- asset_source: official_exam_pdf
- expected_asset_filename: aug_2023_v2_q19.png
- notes: 
- suggested_by_tool: yes

---

### 8. Q243 — aug_2023_v2 #23

- question_id: `243`
- source/exam: `aug_2023_v2`
- source_number: `23`
- question_type: `fill_in`
- topic: `pivot-table` — Pivot Table и Pivot Chart
- section(s): 11. клас: Приложение на електронни таблици за обработка на големи обеми от данни (grade11-m1-spreadsheets-big-data); 11. клас: Обобщение — Обработка и анализ на данни (grade11-m1-summary); 11. клас: Обобщение — Мултимедия (grade11-m2-summary); 12. клас: Подготовка за матура (grade12-dzi-preparation)
- detected_category: `diagram`
- confidence: `medium`
- asset_status: `needs_mapping`
- visual_asset_count: 0
- missing_visual_asset_count: 0
- reason: diagram: 'диаграмата' (diagram reference, medium)

Question text snippet:

> В листа за отговори напишете (1), (2) и (3) и срещу тях съответно наименованието на диаграмата. (1) (2) (3)

Review fields:

- needs_image: 
- asset_source: 
- expected_asset_filename: 
- notes: 
- suggested_by_tool: 

---

### 9. Q244 — aug_2023_v2 #24

- question_id: `244`
- source/exam: `aug_2023_v2`
- source_number: `24`
- question_type: `fill_in`
- topic: `countif` — COUNTIF
- section(s): 11. клас: Приложение на електронни таблици за обработка на големи обеми от данни (grade11-m1-spreadsheets-big-data); 11. клас: Обобщение — Обработка и анализ на данни (grade11-m1-summary); 11. клас: Обобщение — Мултимедия (grade11-m2-summary); 12. клас: Подготовка за матура (grade12-dzi-preparation)
- detected_category: `chart/table`
- confidence: `high`
- asset_status: `needs_mapping`
- visual_asset_count: 0
- missing_visual_asset_count: 0
- reason: chart/table: 'разгледайте графиката' (shown chart/graph reference, high); chart/table: 'графиката' (chart/graph reference, medium)

Question text snippet:

> Разгледайте графиката на получените резултати от проведено анкетно проучване. Като знаете броя на анкетираните, които с…

Review fields:

- needs_image: yes
- asset_source: official_exam_pdf
- expected_asset_filename: aug_2023_v2_q24.png
- notes: 
- suggested_by_tool: yes

---

### 10. Q124 — aug_2024_v2 #9

- question_id: `124`
- source/exam: `aug_2024_v2`
- source_number: `9`
- question_type: `multiple_choice`
- topic: `wireframe-mockup` — Wireframe и mockup на уеб страница
- section(s): 12. клас: Проектиране и графичен дизайн на уеб сайт (grade12-m3-web-design-and-graphic-design); 12. клас: Обобщение — Уеб дизайн (grade12-m3-summary); 12. клас: Подготовка за матура (grade12-dzi-preparation)
- detected_category: `unknown visual dependency`
- confidence: `high`
- asset_status: `needs_mapping`
- visual_asset_count: 0
- missing_visual_asset_count: 0
- reason: unknown visual dependency: 'даденото изображение' (given image, high)

Question text snippet:

> Даденото изображение представлява визуално представяне на уеб страница. Как се нарича?

Review fields:

- needs_image: yes
- asset_source: official_exam_pdf
- expected_asset_filename: aug_2024_v2_q9.png
- notes: 
- suggested_by_tool: yes

---

### 11. Q132 — aug_2024_v2 #17

- question_id: `132`
- source/exam: `aug_2024_v2`
- source_number: `17`
- question_type: `fill_in`
- topic: `—` — —
- section(s): —
- detected_category: `diagram`
- confidence: `medium`
- asset_status: `needs_mapping`
- visual_asset_count: 0
- missing_visual_asset_count: 0
- reason: diagram: 'диаграмата' (diagram reference, medium)

Question text snippet:

> В диаграмата е представена извадка от резултатите и явилите се ученици на ДЗИ ИТ профилирана подготовка. В полето за от…

Review fields:

- needs_image: 
- asset_source: 
- expected_asset_filename: 
- notes: 
- suggested_by_tool: 

---

### 12. Q390 — classroom_tests_8_12_2026 id:390

- question_id: `390`
- source/exam: `classroom_tests_8_12_2026`
- source_number: `—`
- question_type: `multiple_choice`
- topic: `pseudocode` — Псевдокод
- section(s): 10. клас: Създаване на съдържание (grade10-creating-content); 10. клас: Обобщение — Какво научихте в 10. клас (grade10-summary); 12. клас: Подготовка за матура (grade12-dzi-preparation)
- detected_category: `diagram`
- confidence: `high`
- asset_status: `needs_mapping`
- visual_asset_count: 0
- missing_visual_asset_count: 0
- reason: diagram: 'коя от фигурите' (visual answer choices, high)

Question text snippet:

> Коя от фигурите се използва за изобразяване на безусловно действие в блок- схема на алгоритъм:

Review fields:

- needs_image: 
- asset_source: 
- expected_asset_filename: 
- notes: 
- suggested_by_tool: 

---

### 13. Q391 — classroom_tests_8_12_2026 id:391

- question_id: `391`
- source/exam: `classroom_tests_8_12_2026`
- source_number: `—`
- question_type: `multiple_choice`
- topic: `pseudocode` — Псевдокод
- section(s): 10. клас: Създаване на съдържание (grade10-creating-content); 10. клас: Обобщение — Какво научихте в 10. клас (grade10-summary); 12. клас: Подготовка за матура (grade12-dzi-preparation)
- detected_category: `diagram`
- confidence: `high`
- asset_status: `needs_mapping`
- visual_asset_count: 0
- missing_visual_asset_count: 0
- reason: diagram: 'коя от фигурите' (visual answer choices, high)

Question text snippet:

> Коя от фигурите се използва за въвеждане на данни в блок-схема на алгоритъм:

Review fields:

- needs_image: 
- asset_source: 
- expected_asset_filename: 
- notes: 
- suggested_by_tool: 

---

### 14. Q678 — classroom_tests_alt_2026 id:678

- question_id: `678`
- source/exam: `classroom_tests_alt_2026`
- source_number: `—`
- question_type: `multiple_choice`
- topic: `pseudocode` — Псевдокод
- section(s): 10. клас: Създаване на съдържание (grade10-creating-content); 10. клас: Обобщение — Какво научихте в 10. клас (grade10-summary); 12. клас: Подготовка за матура (grade12-dzi-preparation)
- detected_category: `diagram`
- confidence: `high`
- asset_status: `needs_mapping`
- visual_asset_count: 0
- missing_visual_asset_count: 0
- reason: diagram: 'коя от фигурите' (visual answer choices, high)

Question text snippet:

> Изберете верния отговор: коя от фигурите се използва за изобразяване на безусловно действие в блок- схема на алгоритъм?

Review fields:

- needs_image: 
- asset_source: 
- expected_asset_filename: 
- notes: 
- suggested_by_tool: 

---

### 15. Q679 — classroom_tests_alt_2026 id:679

- question_id: `679`
- source/exam: `classroom_tests_alt_2026`
- source_number: `—`
- question_type: `multiple_choice`
- topic: `pseudocode` — Псевдокод
- section(s): 10. клас: Създаване на съдържание (grade10-creating-content); 10. клас: Обобщение — Какво научихте в 10. клас (grade10-summary); 12. клас: Подготовка за матура (grade12-dzi-preparation)
- detected_category: `diagram`
- confidence: `high`
- asset_status: `needs_mapping`
- visual_asset_count: 0
- missing_visual_asset_count: 0
- reason: diagram: 'коя от фигурите' (visual answer choices, high)

Question text snippet:

> Изберете верния отговор: коя от фигурите се използва за въвеждане на данни в блок-схема на алгоритъм?

Review fields:

- needs_image: 
- asset_source: 
- expected_asset_filename: 
- notes: 
- suggested_by_tool: 

---

### 16. Q163 — jun_2022 #5

- question_id: `163`
- source/exam: `jun_2022`
- source_number: `5`
- question_type: `multiple_choice`
- topic: `image-filters` — Филтри за изображения
- section(s): 11. клас: Обобщение — Обработка и анализ на данни (grade11-m1-summary); 11. клас: Обработка на растерни изображения (grade11-m2-raster-images); 11. клас: Мултимедия — мултимедиен продукт (grade11-m2-multimedia-product); 11. клас: Обобщение — Мултимедия (grade11-m2-summary); 12. клас: Подготовка за матура (grade12-dzi-preparation)
- detected_category: `unknown visual dependency`
- confidence: `high`
- asset_status: `needs_mapping`
- visual_asset_count: 0
- missing_visual_asset_count: 0
- reason: unknown visual dependency: 'следното изображение' (shown image, high)

Question text snippet:

> Кой инструмент е използван за създаване на следното изображение?

Review fields:

- needs_image: yes
- asset_source: official_exam_pdf
- expected_asset_filename: jun_2022_q5.png
- notes: 
- suggested_by_tool: yes

---

### 17. Q175 — jun_2022 #20

- question_id: `175`
- source/exam: `jun_2022`
- source_number: `20`
- question_type: `fill_in`
- topic: `pivot-table` — Pivot Table и Pivot Chart
- section(s): 11. клас: Приложение на електронни таблици за обработка на големи обеми от данни (grade11-m1-spreadsheets-big-data); 11. клас: Обобщение — Обработка и анализ на данни (grade11-m1-summary); 11. клас: Обобщение — Мултимедия (grade11-m2-summary); 12. клас: Подготовка за матура (grade12-dzi-preparation)
- detected_category: `diagram`
- confidence: `medium`
- asset_status: `needs_mapping`
- visual_asset_count: 0
- missing_visual_asset_count: 0
- reason: diagram: 'диаграмата' (diagram reference, medium)

Question text snippet:

> На диаграмата са представени графично приходите от продажби в четири магазина, разпределени по групи продукти. Запишете…

Review fields:

- needs_image: 
- asset_source: 
- expected_asset_filename: 
- notes: 
- suggested_by_tool: 

---

### 18. Q62 — may_2022_v1 #19

- question_id: `62`
- source/exam: `may_2022_v1`
- source_number: `19`
- question_type: `fill_in`
- topic: `spreadsheet-function-basics` — Функции в електронни таблици
- section(s): 9. клас: Приложни програми (grade9-application-programs); 9. клас: Работа по проекти (grade9-project-work); 9. клас: Обобщение — Какво научихте в 9. клас (grade9-summary); 10. клас: Преговор (grade10-review); 12. клас: Подготовка за матура (grade12-dzi-preparation)
- detected_category: `chart/table`
- confidence: `high`
- asset_status: `needs_mapping`
- visual_asset_count: 0
- missing_visual_asset_count: 0
- reason: chart/table: 'даден е фрагмент от електронна таблица' (spreadsheet fragment, high); diagram: 'диаграмата' (diagram reference, medium)

Question text snippet:

> Даден е фрагмент от електронна таблица и диаграма. Стойностите, по които е построена диаграмата, са пропорционални на д…

Review fields:

- needs_image: yes
- asset_source: official_exam_pdf
- expected_asset_filename: may_2022_v1_q19.png
- notes: 
- suggested_by_tool: yes

---

### 19. Q36 — may_2023 #15

- question_id: `36`
- source/exam: `may_2023`
- source_number: `15`
- question_type: `multiple_choice`
- topic: `gantt-chart` — Диаграма на Гант
- section(s): 12. клас: Обобщение — Уеб дизайн (grade12-m3-summary); 12. клас: Управление на проекти (grade12-m4-project-management); 12. клас: Обобщение: Решаване на проблеми с ИКТ (grade12-m4-summary); 12. клас: Подготовка за матура (grade12-dzi-preparation)
- detected_category: `unknown visual dependency`
- confidence: `high`
- asset_status: `needs_mapping`
- visual_asset_count: 0
- missing_visual_asset_count: 0
- reason: unknown visual dependency: 'изображението' (image reference, high); unknown visual dependency: 'на изображението' (image reference, high)

Question text snippet:

> Какъв вид диаграма е представена на изображението?

Review fields:

- needs_image: yes
- asset_source: official_exam_pdf
- expected_asset_filename: may_2023_q15.png
- notes: 
- suggested_by_tool: yes

---

### 20. Q40 — may_2023 #19

- question_id: `40`
- source/exam: `may_2023`
- source_number: `19`
- question_type: `fill_in`
- topic: `sql-select` — SQL SELECT
- section(s): 11. клас: Проектиране и създаване на бази от данни и на информационни системи (grade11-m1-databases-and-information-systems); 11. клас: Обобщение — Обработка и анализ на данни (grade11-m1-summary); 11. клас: Обобщение — Мултимедия (grade11-m2-summary); 12. клас: Подготовка за матура (grade12-dzi-preparation)
- detected_category: `screenshot/UI`
- confidence: `high`
- asset_status: `needs_mapping`
- visual_asset_count: 0
- missing_visual_asset_count: 0
- reason: screenshot/UI: 'показаната заявка' (shown query reference, high)

Question text snippet:

> В таблиците по-долу е въведена информация за ученици и за тези от тях, които са участници в различни олимпиади. В листа…

Review fields:

- needs_image: yes
- asset_source: official_exam_pdf
- expected_asset_filename: may_2023_q19.png
- notes: 
- suggested_by_tool: yes

---

### 21. Q830 — may_2023_v2 #12

- question_id: `830`
- source/exam: `may_2023_v2`
- source_number: `12`
- question_type: `fill_in`
- topic: `dataset` — Набор от данни (dataset)
- section(s): 10. клас: Създаване на съдържание (grade10-creating-content)
- detected_category: `diagram`
- confidence: `medium`
- asset_status: `needs_mapping`
- visual_asset_count: 0
- missing_visual_asset_count: 0
- reason: diagram: 'диаграмата' (diagram reference, medium)

Question text snippet:

> На диаграмата по-долу е показано процентното разпределение на родените деца през 2021 г. в България по райони. Ако разд…

Review fields:

- needs_image: 
- asset_source: 
- expected_asset_filename: 
- notes: 
- suggested_by_tool: 

---

### 22. Q213 — may_2023_v2 #15

- question_id: `213`
- source/exam: `may_2023_v2`
- source_number: `15`
- question_type: `multiple_choice`
- topic: `gantt-chart` — Диаграма на Гант
- section(s): 12. клас: Обобщение — Уеб дизайн (grade12-m3-summary); 12. клас: Управление на проекти (grade12-m4-project-management); 12. клас: Обобщение: Решаване на проблеми с ИКТ (grade12-m4-summary); 12. клас: Подготовка за матура (grade12-dzi-preparation)
- detected_category: `unknown visual dependency`
- confidence: `high`
- asset_status: `needs_mapping`
- visual_asset_count: 0
- missing_visual_asset_count: 0
- reason: unknown visual dependency: 'изображението' (image reference, high); unknown visual dependency: 'на изображението' (image reference, high)

Question text snippet:

> Какъв вид диаграма е представена на изображението?

Review fields:

- needs_image: yes
- asset_source: official_exam_pdf
- expected_asset_filename: may_2023_v2_q15.png
- notes: 
- suggested_by_tool: yes

---

### 23. Q217 — may_2023_v2 #19

- question_id: `217`
- source/exam: `may_2023_v2`
- source_number: `19`
- question_type: `fill_in`
- topic: `sql-select` — SQL SELECT
- section(s): 11. клас: Проектиране и създаване на бази от данни и на информационни системи (grade11-m1-databases-and-information-systems); 11. клас: Обобщение — Обработка и анализ на данни (grade11-m1-summary); 11. клас: Обобщение — Мултимедия (grade11-m2-summary); 12. клас: Подготовка за матура (grade12-dzi-preparation)
- detected_category: `screenshot/UI`
- confidence: `high`
- asset_status: `needs_mapping`
- visual_asset_count: 0
- missing_visual_asset_count: 0
- reason: screenshot/UI: 'показаната заявка' (shown query reference, high)

Question text snippet:

> В таблиците по-долу е въведена информация за ученици и за тези от тях, които са участници в различни олимпиади. В листа…

Review fields:

- needs_image: yes
- asset_source: official_exam_pdf
- expected_asset_filename: may_2023_v2_q19.png
- notes: 
- suggested_by_tool: yes

---

### 24. Q91 — may_2024_v1 #1

- question_id: `91`
- source/exam: `may_2024_v1`
- source_number: `1`
- question_type: `multiple_choice`
- topic: `spreadsheet-function-basics` — Функции в електронни таблици
- section(s): 9. клас: Приложни програми (grade9-application-programs); 9. клас: Работа по проекти (grade9-project-work); 9. клас: Обобщение — Какво научихте в 9. клас (grade9-summary); 10. клас: Преговор (grade10-review); 12. клас: Подготовка за матура (grade12-dzi-preparation)
- detected_category: `diagram`
- confidence: `medium`
- asset_status: `needs_mapping`
- visual_asset_count: 0
- missing_visual_asset_count: 0
- reason: diagram: 'диаграмата' (diagram reference, medium)

Question text snippet:

> В електронна таблица са записани резултатите от проведено писмено изпитване по информационни технологии. На резултатите…

Review fields:

- needs_image: 
- asset_source: 
- expected_asset_filename: 
- notes: 
- suggested_by_tool: 

---

### 25. Q106 — may_2024_v1 #16

- question_id: `106`
- source/exam: `may_2024_v1`
- source_number: `16`
- question_type: `fill_in`
- topic: `spreadsheet-function-basics` — Функции в електронни таблици
- section(s): 9. клас: Приложни програми (grade9-application-programs); 9. клас: Работа по проекти (grade9-project-work); 9. клас: Обобщение — Какво научихте в 9. клас (grade9-summary); 10. клас: Преговор (grade10-review); 12. клас: Подготовка за матура (grade12-dzi-preparation)
- detected_category: `diagram`
- confidence: `medium`
- asset_status: `needs_mapping`
- visual_asset_count: 0
- missing_visual_asset_count: 0
- reason: diagram: 'диаграмата' (diagram reference, medium)

Question text snippet:

> В диаграмата са представени данни от Евростат за потреблението на интернет в домакинствата в балкански държави за после…

Review fields:

- needs_image: 
- asset_source: 
- expected_asset_filename: 
- notes: 
- suggested_by_tool: 

---

### 26. Q108 — may_2024_v1 #18

- question_id: `108`
- source/exam: `may_2024_v1`
- source_number: `18`
- question_type: `fill_in`
- topic: `pivot-table` — Pivot Table и Pivot Chart
- section(s): 11. клас: Приложение на електронни таблици за обработка на големи обеми от данни (grade11-m1-spreadsheets-big-data); 11. клас: Обобщение — Обработка и анализ на данни (grade11-m1-summary); 11. клас: Обобщение — Мултимедия (grade11-m2-summary); 12. клас: Подготовка за матура (grade12-dzi-preparation)
- detected_category: `chart/table`
- confidence: `high`
- asset_status: `needs_mapping`
- visual_asset_count: 0
- missing_visual_asset_count: 0
- reason: chart/table: 'таблицата по-долу' (table shown outside text flow, high)

Question text snippet:

> Дадена е таблица с данни за класовете от 8 до 12 клас и техните извинени и неизвинени отсъствия. В полетата за отговори…

Review fields:

- needs_image: yes
- asset_source: official_exam_pdf
- expected_asset_filename: may_2024_v1_q18.png
- notes: 
- suggested_by_tool: yes

---

### 27. Q111 — may_2024_v1 #21

- question_id: `111`
- source/exam: `may_2024_v1`
- source_number: `21`
- question_type: `fill_in`
- topic: `raster-vs-vector` — Растер vs вектор
- section(s): 11. клас: Обобщение — Обработка и анализ на данни (grade11-m1-summary); 11. клас: Обработка на растерни изображения (grade11-m2-raster-images); 11. клас: Създаване и обработка на векторни изображения (grade11-m2-vector-images); 11. клас: Обобщение — Мултимедия (grade11-m2-summary); 12. клас: Подготовка за матура (grade12-dzi-preparation)
- detected_category: `unknown visual dependency`
- confidence: `high`
- asset_status: `needs_mapping`
- visual_asset_count: 0
- missing_visual_asset_count: 0
- reason: unknown visual dependency: 'изображението' (image reference, high); unknown visual dependency: 'на изображението' (image reference, high)

Question text snippet:

> В полето за отговори срещу (1), (2) и (3) напишете коя конструктивна операция е използвана за създаването на обекти, ви…

Review fields:

- needs_image: yes
- asset_source: official_exam_pdf
- expected_asset_filename: may_2024_v1_q21.png
- notes: 
- suggested_by_tool: yes

---

### 28. Q16 — may_2025_v2 #16

- question_id: `16`
- source/exam: `may_2025_v2`
- source_number: `16`
- question_type: `fill_in`
- topic: `pivot-table` — Pivot Table и Pivot Chart
- section(s): 11. клас: Приложение на електронни таблици за обработка на големи обеми от данни (grade11-m1-spreadsheets-big-data); 11. клас: Обобщение — Обработка и анализ на данни (grade11-m1-summary); 11. клас: Обобщение — Мултимедия (grade11-m2-summary); 12. клас: Подготовка за матура (grade12-dzi-preparation)
- detected_category: `diagram`
- confidence: `medium`
- asset_status: `needs_mapping`
- visual_asset_count: 0
- missing_visual_asset_count: 0
- reason: diagram: 'диаграмата' (diagram reference, medium)

Question text snippet:

> На диаграмата са представени суми от продажби на стоки в няколко области от страната за месец юни. В полетата за отгово…

Review fields:

- needs_image: 
- asset_source: 
- expected_asset_filename: 
- notes: 
- suggested_by_tool: 

---
