# Biology, Grades 8–10 — question-bank collection plan

Prepared 2026-10-05. A plan only: no question was collected, no candidate
dataset was created, nothing was imported or changed in the database.

Authority for scope: the three МОН programmes recorded in
`docs/reviews/biology_curriculum_structure_review.md` (amended texts of
15.11.2023) and the structure files under `data/subject_structures/biology/`.
Targets were set from each programme's „Учебно съдържание“ table — expected
results and new concepts — and its example laboratory topics, not from topic
titles. Physics banks were looked at for the pipeline only; no count was copied.

**Reading note.** In Grades 8 and 10 the programme lists expected results per
theme, not per sub-theme. Where a result names its organs or concepts, it is
assigned to the matching topic; results that apply to every sub-theme (for
example „изброява и описва по алгоритъм заболявания“) are applied to each. That
assignment is this plan's reading, not the programme's own layout.

## 1. Targets per topic

Scope class: narrow / normal / broad / exceptionally broad. "Raw" is what to
collect; "final" is the expected range after independent review. Both are
ranges of what the curriculum supports, not quotas: a collector who runs out of
distinct, in-scope material stops below the raw target and says so.
Difficulty intent is easy/medium/hard in percent. Diagram priority is for a
later image pass; every bank must stand on its own as text.

| Grade | Section | Topic | Slug | Scope class | Raw | Final | Difficulty E/M/H | Diagrams | Duplicate-risk notes |
|---|---|---|---|---|---|---|---|---|---|
| 8 | Структурна организация на човешкия организъм | Клетка и тъкани | `biology-g8-cell-and-tissues` | narrow | 40 | 30–35 | 40/50/10 | important | Four tissue types × (structure, function, location) invites the same fact three ways; cap at one item per tissue-property pair. Keep cell detail out (G9). |
| 8 | Обмяна на веществата | Храносмилателна система | `biology-g8-digestive-system` | broad | 68 | 50–60 | 35/50/15 | important | Organ → function pairs and “where is X digested” repeat easily; diet items drift into general health advice with no biology. |
| 8 | Обмяна на веществата | Дихателна система | `biology-g8-respiratory-system` | normal | 55 | 40–48 | 35/50/15 | useful | Airway order and “where does gas exchange happen” are the two facts most likely to be rewritten; smoking items repeat. |
| 8 | Обмяна на веществата | Отделителна система | `biology-g8-excretory-system` | normal | 48 | 35–42 | 35/50/15 | useful | Small organ set; urine-formation steps asked repeatedly. Do not go to nephron transport mechanisms beyond the school description. |
| 8 | Обмяна на веществата | Сърдечно-съдова система | `biology-g8-cardiovascular-system` | exceptionally broad | 88 | 65–75 | 30/50/20 | important | Highest in the grade: vessel types, circulation loops, blood cells and blood groups are four separate fact families, each easy to over-sample. Immunity/vaccines belong here only. |
| 8 | Движение и опора на тялото | Опорно-двигателна система – скелет и скелетни мускули | `biology-g8-musculoskeletal-system` | broad | 66 | 50–58 | 35/50/15 | important | Bone-naming items multiply without adding coverage; limit named bones to those the programme needs for regions and joints. |
| 8 | Размножаване, растеж и развитие | Полова система | `biology-g8-reproductive-system` | normal | 56 | 42–50 | 35/50/15 | important | Organ → function pairs for two systems; cycle-day arithmetic can repeat. STI items repeat the same prevention fact. |
| 8 | Размножаване, растеж и развитие | Зародишно и следзародишно развитие | `biology-g8-embryonic-and-postembryonic-development` | narrow | 44 | 32–38 | 40/50/10 | useful | Few stages, so stage-order items repeat. Gametogenesis and cleavage detail belong to G10. |
| 8 | Регулация и хомеостаза | Нервна система | `biology-g8-nervous-system` | broad | 74 | 55–65 | 30/50/20 | important | Reflex-arc element order and brain-part → function are over-sampled easily; psychoactive-substance items repeat the same warning. |
| 8 | Регулация и хомеостаза | Ендокринна система | `biology-g8-endocrine-system` | normal | 56 | 42–50 | 35/50/15 | useful | Gland → hormone → effect triples are the classic rewrite trap; one item per triple, then regulation and disorders. |
| 8 | Регулация и хомеостаза | Сетивни системи | `biology-g8-sensory-systems` | broad | 64 | 48–56 | 35/50/15 | important | Eye-part and ear-part → function lists; keep to one item per structure and spend the rest on the path of the signal and on disorders. |
| 8 | Регулация и хомеостаза | Кожа | `biology-g8-skin` | narrow | 40 | 30–35 | 40/50/10 | useful | Three layers and four functions; thermoregulation is the only process. First-aid items overlap with the musculoskeletal topic (trauma) — burns, frostbite, heat and sunstroke stay here. |
| 9 | Химичен състав на живата материя | Химичен състав на живата материя | `biology-g9-chemical-composition-of-living-matter` | broad | 78 | 58–68 | 30/50/20 | useful | Definition items for five compound groups plus “monomer of X” are easy to duplicate; comparison items must compare different pairs. |
| 9 | Надмолекулни комплекси | Надмолекулни комплекси | `biology-g9-supramolecular-complexes` | narrow | 48 | 35–42 | 35/50/15 | useful | Small concept set (definition, virus structure, reproduction stages); disease items repeat the same prevention fact across viruses. |
| 9 | Структура и процеси в клетката | Структура и процеси в клетката | `biology-g9-cell-structure-and-processes` | exceptionally broad | 95 | 70–80 | 30/50/20 | important | Largest bank. Organelle → function is the main trap; membrane transport, metabolism and the genetic processes are three separate process families and each needs its own budget. Bacteria diseases are here, viral ones are not. |
| 9 | Възпроизводство на клетката | Възпроизводство на клетката | `biology-g9-cell-reproduction` | normal | 60 | 45–52 | 30/50/20 | important | Phase-order items for mitosis and meiosis repeat; chromosome-count arithmetic repeats with changed numbers (the validator flags those as near duplicates). |
| 9 | Приложение на знанията за клетката | Приложение на знанията за клетката | `biology-g9-application-of-cell-knowledge` | narrow | 38 | 28–34 | 45/45/10 | not needed | RECALL-DOMINATED: the programme asks only to describe and illustrate applications. Field → example pairs will be rewritten unless every item is tied to a different cell process from themes 1–4. |
| 10 | Многоклетъчен организъм | Наследственост и изменчивост | `biology-g10-heredity-and-variation` | exceptionally broad | 96 | 70–80 | 25/50/25 | important | Cross problems with changed traits are the same exercise; budget them per cross type and per interaction type. Definitions (gene, allele, genotype, phenotype) must not be asked more than once each. |
| 10 | Многоклетъчен организъм | Размножаване, растеж и индивидуално развитие | `biology-g10-reproduction-growth-and-individual-development` | broad | 66 | 50–58 | 30/50/20 | important | Four explicit comparison pairs; each pair supports several distinct criteria but not ten. Human organs and the menstrual cycle are G8. |
| 10 | Биосфера | Популация, биоценоза, екосистема | `biology-g10-population-biocenosis-ecosystem` | broad | 74 | 55–65 | 30/50/20 | important | Definition items for five levels; food-chain and pyramid items repeat with swapped organisms. Factors and adaptations belong to the next topic. |
| 10 | Биосфера | Екологични фактори | `biology-g10-ecological-factors` | normal | 56 | 42–50 | 30/50/20 | useful | Factor → example classification is the trap; adaptation items need different organisms and different factors. |
| 10 | Биологична еволюция | Произход и развитие на живата материя | `biology-g10-origin-and-development-of-living-matter` | broad | 72 | 55–62 | 30/50/20 | useful | Darwinian vs modern factors, and micro- vs macroevolution, are the two comparisons that get rewritten. Evidence items belong to 3.3, human evolution to 3.2. |
| 10 | Биологична еволюция | Произход и еволюция на човека | `biology-g10-origin-and-evolution-of-humans` | narrow | 40 | 30–35 | 40/50/10 | useful | Stage-order items repeat. Sensitive content: follow the amended programme exactly (the term „раса“ as a new concept and the discussion of racial belonging were removed in 2023). |
| 10 | Биологична еволюция | Доказателства за биологичната еволюция | `biology-g10-evidence-for-biological-evolution` | normal | 48 | 36–44 | 35/50/15 | important | Homologous/analogous/rudimentary classification with a different organ each time is one item type; cap it and cover fossils, series and embryology. |

### Totals

| Grade | Topics | Raw target | Expected final range | Collection runs |
|---|---|---|---|---|
| 8 | 12 | 699 | 519–612 | 23 |
| 9 | 5 | 319 | 236–276 | 10 |
| 10 | 7 | 452 | 338–394 | 14 |
| **Total** | **24** | **1470** | **1093–1282** | **47** |

Raw targets assume that roughly 15–25 % of candidates are deleted or merged in
review. Biology allows many surface variants of one fact, so the loss is
expected to be higher than the 5–9 % seen in the Physics banks.

### Why each topic has its target

- **8 · Клетка и тъкани** (narrow, raw 40): One sub-theme under one theme: levels of organisation in the human body and the kinds of tissue, recognised on images.
- **8 · Храносмилателна система** (broad, raw 68): Organs and functions, the digestion process, diseases by the cause–signs–prevention algorithm, food composition and eating regime, alcohol/energy drinks/diets/overeating, and two lab topics (menu, nutrient tests, BMI and energy use).
- **8 · Дихателна система** (normal, raw 55): Organs and functions, breathing and gas exchange, diseases, the significance of air composition, and a lab on breathing rate and vital capacity.
- **8 · Отделителна система** (normal, raw 48): Organs and functions, excretion as a process, diseases, and reading urine composition in norm and pathology.
- **8 · Сърдечно-съдова система** (exceptionally broad, raw 88): The sub-theme carries the heart, vessels and circulation, the formed elements of blood, blood groups, immunity, vaccines and immunisation, infection through blood, and blood-composition data, plus a pulse and blood-pressure lab.
- **8 · Опорно-двигателна система – скелет и скелетни мускули** (broad, raw 66): Skeleton and skeletal muscles, three kinds of bone connection, joints, comparison with mammals, injuries and diseases, risk factors, first aid for trauma, and tolerance toward people with disabilities.
- **8 · Полова система** (normal, raw 56): Male and female organs and functions, ovulation and the menstrual cycle, sexual and reproductive health, sexually transmitted diseases, risks of early sexual contact.
- **8 · Зародишно и следзародишно развитие** (narrow, raw 44): Main stages of embryonic and post-embryonic human development, named and recognised on an image; responsibility for offspring.
- **8 · Нервна система** (broad, raw 74): Structure and functions of the nervous system, reflex and reflex arc, unconditioned vs conditioned reflexes, nervous regulation and its link with hormonal regulation, diseases and injuries, alcohol and narcotic substances, mental-health prophylaxis.
- **8 · Ендокринна система** (normal, raw 56): Glands and hormones, hormonal regulation, examples of nervous–hormonal links, homeostasis, disorders, hormonal preparations.
- **8 · Сетивни системи** (broad, raw 64): Sense organs as parts of sensory systems: eye and vision, ear, hearing and balance, other senses; disorders and prevention; a lab on the visual system.
- **8 · Кожа** (narrow, raw 40): Structure and functions of the skin, thermoregulation, diseases and injuries, first aid for burns, frostbite, heat and sunstroke.
- **9 · Химичен състав на живата материя** (broad, raw 78): Groups of chemical elements, inorganic and organic compounds by the composition–properties–functions algorithm, carbohydrates, lipids, proteins, enzymes, nucleic acids, grouping by monomers, comparisons, living vs non-living matter, reading charts.
- **9 · Надмолекулни комплекси** (narrow, raw 48): Definition of supramolecular complexes, viruses, stages of virus reproduction, viral diseases by infection route – signs – prevention, prophylaxis.
- **9 · Структура и процеси в клетката** (exceptionally broad, raw 95): About a third of the programme: cell theory, prokaryotic and eukaryotic cells, membrane, nucleus, cytoplasm, organelles, bacterial diseases, membrane transport and cytoses, metabolism (anabolic and catabolic processes), genetic processes (replication, transcription, translation), linking processes to structures, and five of the nine lab topics.
- **9 · Възпроизводство на клетката** (normal, raw 60): Mitosis, meiosis, mitotic cycle and life cycle of the cell, kinds of division and their stages, following them on a diagram, significance of division and differentiation. Amitosis was removed in 2023.
- **9 · Приложение на знанията за клетката** (narrow, raw 38): Applications of cell knowledge in science, biotechnology, medicine, agriculture and environmental protection; project work.
- **10 · Наследственост и изменчивост** (exceptionally broad, raw 96): Eight definitions, Mendel's laws, mono- and dihybrid crosses, four allelic and four non-allelic interactions, kinds of variation, mutations and modifications, methods of human genetics, hereditary diseases, pedigrees, chromosomal sex determination, reading genetic schemes and karyograms.
- **10 · Размножаване, растеж и индивидуално развитие** (broad, raw 66): Phases of gametogenesis, stages of embryonic development in animals and humans, direct and indirect development, four comparisons, significance of gametogenesis, fertilisation and differentiation, factors of human development.
- **10 · Популация, биоценоза, екосистема** (broad, raw 74): Hierarchy of supra-organism levels, five definitions with examples, structure and composition of population, biocenosis, ecosystem and biosphere, development and productivity of ecosystems, cycles of matter, energy flow, ecological pyramid, and four field/lab topics.
- **10 · Екологични фактори** (normal, raw 56): Abiotic, biotic and anthropogenic factors with examples, adaptations and their relative nature, assessing the state of a population or ecosystem, predicting change under human impact, and two lab topics.
- **10 · Произход и развитие на живата материя** (broad, raw 72): Hypotheses for the origin of life, Darwin's theory and contribution, the modern theory, factors of evolution in both, criteria for species, speciation, micro- vs macroevolution, directions of the evolutionary process, paths of biological progress.
- **10 · Произход и еволюция на човека** (narrow, raw 40): Anthropogenesis, main stages of human evolution recognised in text or image, causes for the appearance of human races, one species.
- **10 · Доказателства за биологичната еволюция** (normal, raw 48): Four groups of evidence with examples, and seven defined terms: analogous, homologous and rudimentary organs, phylogenetic series, fossils, index fossils, transitional forms.

## 2. Coverage matrix

Each topic lists its assessable dimensions, taken from the programme. A topic
collected in several runs splits its dimensions between the runs; a run writes
questions only for its own dimensions.

### Grade 8

**Клетка и тъкани** — `biology-g8-cell-and-tissues`, raw 40 in 1 run
- run-1 (max 40): D1 Levels of organisation: cell → tissue → organ → system → organism, and their order; D2 Kinds of tissue on a structural and functional basis; D3 Tissue ↔ function ↔ where it is found; D4 Recognising a tissue from a description (image-dependent items deferred); D5 Assigning an organ to a system by function or structure

**Храносмилателна система** — `biology-g8-digestive-system`, raw 68 in 2 runs
- run-1 (max 34): D1 Organs of the tract and glands: order and structure; D2 Function of each organ; mechanical vs chemical processing; D3 Digestion and absorption as a sequence; D4 Nutrients and food composition; healthy regime
- run-2 (max 34): D5 Diseases: cause – signs – prevention; D6 Alcohol, energy drinks, diets, overeating: effect on the organism; D7 Applied data: menu, BMI, basal metabolism, nutrient tests

**Дихателна система** — `biology-g8-respiratory-system`, raw 55 in 2 runs
- run-1 (max 28): D1 Airways and lungs: order and structure; D2 Breathing movements and gas exchange; D3 Structure–function links (alveoli, cilia, etc.)
- run-2 (max 27): D4 Air composition and its effect; D5 Diseases: cause – signs – prevention; D6 Breathing rate and vital capacity: reading measurements

**Отделителна система** — `biology-g8-excretory-system`, raw 48 in 2 runs
- run-1 (max 24): D1 Organs of the system and their order; D2 Kidney structure at school level and its function; D3 Formation and removal of urine as a sequence
- run-2 (max 24): D4 Urine composition: norm vs pathology (table data); D5 Diseases: cause – signs – prevention; D6 Link with the blood and water balance

**Сърдечно-съдова система** — `biology-g8-cardiovascular-system`, raw 88 in 3 runs
- run-1 (max 30): D1 Heart: structure and work; D2 Vessels: kinds, structure–function; D3 Circulation: the two loops as a sequence
- run-2 (max 29): D4 Blood: plasma and formed elements, functions; D5 Blood groups and transfusion compatibility; D6 Immunity: kinds, vaccines, vaccination, immunisation
- run-3 (max 29): D7 Diseases and blood-borne infection: cause – signs – prevention, personal responsibility; D8 Data: blood composition in norm and pathology; pulse and arterial pressure at rest and under load

**Опорно-двигателна система – скелет и скелетни мускули** — `biology-g8-musculoskeletal-system`, raw 66 in 2 runs
- run-1 (max 33): D1 Parts of the skeleton and their functions; D2 Bone connections: fixed, semi-movable, movable; joint structure; D3 Skeletal muscles: structure, work, fatigue; D4 Bone and muscle as a lever system: structure–function
- run-2 (max 33): D5 Comparison with the mammal skeleton; D6 Injuries and diseases: cause – signs – prevention; posture; D7 First aid for trauma

**Полова система** — `biology-g8-reproductive-system`, raw 56 in 2 runs
- run-1 (max 28): D1 Male reproductive organs: structure and function; D2 Female reproductive organs: structure and function; D3 Gametes; ovulation and the menstrual cycle as a sequence
- run-2 (max 28): D4 Sexual and reproductive health; D5 Sexually transmitted diseases: cause – signs – prevention; D6 Responsible behaviour: applying the rules to a situation

**Зародишно и следзародишно развитие** — `biology-g8-embryonic-and-postembryonic-development`, raw 44 in 2 runs
- run-1 (max 22): D1 Fertilisation and the start of development; D2 Stages of embryonic development in order; D3 Placenta and umbilical cord: role
- run-2 (max 22): D4 Birth; D5 Post-embryonic periods and their features; D6 Factors that put development at risk

**Нервна система** — `biology-g8-nervous-system`, raw 74 in 2 runs
- run-1 (max 37): D1 Neuron and nervous tissue in the system; D2 Central nervous system: parts and functions; D3 Peripheral nervous system; D4 Reflex and reflex arc as a sequence
- run-2 (max 37): D5 Unconditioned vs conditioned reflexes: comparison; D6 Nervous regulation and homeostasis; link with hormonal regulation; D7 Diseases and injuries: cause – signs – prevention; D8 Alcohol, narcotic and psychoactive substances: effect on person and society

**Ендокринна система** — `biology-g8-endocrine-system`, raw 56 in 2 runs
- run-1 (max 28): D1 Endocrine glands and where they are; D2 Hormones and their effects; D3 Hormonal regulation as feedback; D4 Nervous vs hormonal regulation: comparison and links
- run-2 (max 28): D5 Homeostasis: the organism as a whole; D6 Disorders of too much or too little hormone; D7 Hormonal preparations: discussion-level facts

**Сетивни системи** — `biology-g8-sensory-systems`, raw 64 in 2 runs
- run-1 (max 32): D1 Sensory system: receptor – pathway – brain centre; D2 Eye: structure and function of parts; D3 Vision: path of light and of the signal; common defects; D4 Ear: structure; hearing and balance
- run-2 (max 32): D5 Taste, smell, skin senses; D6 Disorders and injuries: cause – signs – prevention; D7 Observations on the visual system

**Кожа** — `biology-g8-skin`, raw 40 in 1 run
- run-1 (max 40): D1 Layers of the skin and what they contain; D2 Functions of the skin; D3 Thermoregulation as a mechanism; D4 Diseases and injuries; hygiene; D5 First aid: burns, frostbite, heat and sunstroke

### Grade 9

**Химичен състав на живата материя** — `biology-g9-chemical-composition-of-living-matter`, raw 78 in 2 runs
- run-1 (max 39): D1 Chemical elements by share; examples of significance; D2 Water and mineral salts; D3 Carbohydrates: composition – properties – functions; D4 Lipids: composition – properties – functions; D5 Proteins: structure levels at school depth, functions
- run-2 (max 39): D6 Enzymes: nature, specificity, conditions; D7 Nucleic acids: DNA and RNA, comparison; D8 Grouping by monomers; comparing biopolymer groups; D9 Living vs non-living matter; reading composition charts

**Надмолекулни комплекси** — `biology-g9-supramolecular-complexes`, raw 48 in 2 runs
- run-1 (max 24): D1 Supramolecular complexes: definition and examples; D2 Viruses: composition and structure; why they are not cells; D3 Reproduction of a virus as a sequence of stages
- run-2 (max 24): D4 Viral diseases: route of infection – signs – prevention; D5 Prophylaxis: vaccines and behaviour, applied to a situation

**Структура и процеси в клетката** — `biology-g9-cell-structure-and-processes`, raw 95 in 3 runs
- run-1 (max 32): D1 Cell theory: main statements; D2 Prokaryotic vs eukaryotic cell: comparison; D3 Cell membrane: structure and properties; D4 Nucleus and cytoplasm
- run-2 (max 32): D5 Organelles: structure ↔ function; plant vs animal cell; D6 Bacteria and bacterial diseases: route – signs – prevention; D7 Membrane transport: passive vs active; osmosis; D8 Endocytosis and exocytosis
- run-3 (max 31): D9 Metabolism: anabolic vs catabolic; photosynthesis and respiration at programme depth; D10 Replication, transcription, translation: what, where, in what order; D11 Process ↔ structure that provides it; D12 Observations: microscope preparations, osmotic behaviour, pigments

**Възпроизводство на клетката** — `biology-g9-cell-reproduction`, raw 60 in 2 runs
- run-1 (max 30): D1 Life cycle and mitotic cycle of the cell; interphase; D2 Mitosis: phases and events in order; D3 Meiosis: two divisions, what changes
- run-2 (max 30): D4 Mitosis vs meiosis: comparison; D5 Chromosome number before and after division; D6 Significance of division; cell differentiation

**Приложение на знанията за клетката** — `biology-g9-application-of-cell-knowledge`, raw 38 in 1 run — **recall-dominated, see section 3**
- run-1 (max 38): D1 Biotechnological production: which cell process is used; D2 Medicine: diagnostics, antibiotics, vaccines as applications; D3 Agriculture; D4 Environmental protection; D5 Matching an application to the knowledge it rests on

### Grade 10

**Наследственост и изменчивост** — `biology-g10-heredity-and-variation`, raw 96 in 3 runs
- run-1 (max 32): D1 Core terms: heredity, variation, gene, allele, genotype, phenotype; D2 Mendel's laws; monohybrid cross; D3 Dihybrid cross; D4 Allelic interactions: complete and incomplete dominance, codominance, lethal
- run-2 (max 32): D5 Non-allelic interactions: complementary, epistatic, polymeric; pleiotropy; D6 Kinds of variation; modifications vs mutations; D7 Methods of human genetics; hereditary diseases; D8 Pedigree and sex determination in humans
- run-3 (max 32): D9 Reading genetic schemes, karyograms, tables; D10 Factors causing modifications and mutations

**Размножаване, растеж и индивидуално развитие** — `biology-g10-reproduction-growth-and-individual-development`, raw 66 in 2 runs
- run-1 (max 33): D1 Asexual vs sexual reproduction; D2 Gametogenesis: phases; spermatogenesis vs oogenesis; D3 Fertilisation: external vs internal; D4 Embryonic development: stages in order
- run-2 (max 33): D5 Post-embryonic development: direct vs indirect; D6 Growth and cell differentiation; D7 External and internal factors of human development

**Популация, биоценоза, екосистема** — `biology-g10-population-biocenosis-ecosystem`, raw 74 in 2 runs
- run-1 (max 37): D1 Supra-organism levels in order; D2 Population: structure and characteristics; spatial distribution; D3 Biocenosis: composition and relations; ecological niche; D4 Ecosystem: components, food chains and webs
- run-2 (max 37): D5 Energy flow and ecological pyramids; D6 Cycles of matter; D7 Development (succession) and productivity; D8 Biosphere

**Екологични фактори** — `biology-g10-ecological-factors`, raw 56 in 2 runs
- run-1 (max 28): D1 Classifying factors: abiotic, biotic, anthropogenic; D2 Abiotic factors and organism responses; limits of tolerance at programme depth; D3 Biotic relations as factors
- run-2 (max 28): D4 Adaptations ↔ factors; relative adaptation; D5 Anthropogenic impact: assessing a described state; D6 Predicting change from given data

**Произход и развитие на живата материя** — `biology-g10-origin-and-development-of-living-matter`, raw 72 in 2 runs
- run-1 (max 36): D1 Hypotheses for the origin of life; D2 Darwin: struggle for existence, natural and artificial selection; D3 Modern theory: population as the unit; factors of evolution; D4 Darwinian vs modern factors: comparison
- run-2 (max 36): D5 Biological species and its criteria; D6 Speciation; microevolution; D7 Macroevolution; directions and paths of biological progress; D8 Applying a factor to a described case

**Произход и еволюция на човека** — `biology-g10-origin-and-evolution-of-humans`, raw 40 in 1 run
- run-1 (max 40): D1 Anthropogenesis: definition and driving factors; D2 Stages of human evolution in order and their features; D3 Human vs other primates: what changed; D4 Causes for the appearance of human populations/races; D5 All humans as one species: the argument

**Доказателства за биологичната еволюция** — `biology-g10-evidence-for-biological-evolution`, raw 48 in 2 runs
- run-1 (max 24): D1 Groups of evidence and what each shows; D2 Homologous vs analogous organs; D3 Rudimentary organs; D4 Comparative embryology and physiology
- run-2 (max 24): D5 Fossils, index fossils, transitional forms; D6 Phylogenetic series; D7 Choosing evidence that supports a stated claim

## 3. Coverage principles

**Mixture, not vocabulary.** Across an ordinary topic the bank should hold all
of these where the programme supports them:

| Kind | What it tests | Typical share |
|---|---|---|
| A | Terms and recognition | at most a quarter |
| B | Understanding and explanation | a fifth to a quarter |
| C | Comparison and classification | about a fifth |
| D | Process, sequence and mechanism | about a fifth |
| E | Application to a described situation (health, first aid, an organism, a case) | a tenth to a fifth |
| F | Reading a table, graph or described experiment | up to a tenth, where the programme has data or lab work |

No share is forced where a topic lacks the material. The programmes ask for
„сравнява“, „проследява“, „обосновава“, „аргументира“, „анализира данни“ far
more often than „дефинира“, so a recall-heavy bank misrepresents them.

**Flagged as recall-dominated:** Grade 9 „Приложение на знанията за клетката“.
The programme only asks to describe and illustrate applications. Its target is
the lowest, and every item must tie an application to a cell process from the
other four themes. Also watch Grade 8 „Клетка и тъкани“ and „Кожа“, and Grade
10 „Произход и еволюция на човека“: small concept sets where kind A can crowd
out the rest.

**Difficulty** uses the existing model.

- easy: recognise or recall one fact, or apply it in one obvious step;
- medium: explain, compare on a named criterion, order a process, classify an
  unfamiliar example, read one value from data;
- hard: combine two ideas from the topic, predict the result of a change,
  interpret data and draw a conclusion, solve a cross with a non-trivial ratio.

Baseline 30–40 / 45–55 / 10–20. Topics with real multi-step reasoning carry
more hard items (heredity 25 %, cardiovascular, nervous, cell processes, cell
division, ecology and evolution 20 %); small descriptive topics carry 10 %.
Hard never means obscure: no named enzyme lists, no rare syndromes, no figures
to memorise, nothing from profiled XI–XII or university courses.

**Distractors.** Each wrong option should be a real confusion: a neighbouring
structure, a reversed cause and effect, a swapped process step, the other
member of a compared pair, a wrong classification, a similar term (митоза /
мейоза, транскрипция / транслация, хомоложни / аналогни, артерия / вена). Four
options of similar length and grammar. No „всички изброени“, no „нито едно от
изброените“, no joke options, no negative stems unless the negation is the
point and is capitalised.

**Duplicate guards.** The validator finds only exact and near-identical
wording, inside one file and against stored questions; it does not see two
files of the same topic and does not see a reworded fact.

1. One item per fact. A second item on the same structure, step or definition
   is allowed only if it tests a different kind (A–F) of knowledge.
2. Every candidate records `research.concept` as a short, specific fact label
   (for example „алвеоли – газова обмяна“), so reviewers can sort by concept.
3. A later run of a topic reads the earlier run's file first and lists the
   concepts already covered in its notes; it never edits that file.
4. Comparison items must use different pairs or different criteria.
5. Calculation and cross items differ in structure, not only in numbers or
   trait names.
6. The reviewer compares all files of a grade together (section 6).

## 4. Cross-grade and cross-topic boundaries

| Area | Where it belongs | What must not leak |
|---|---|---|
| Cell | G8 „Клетка и тъкани“: the cell only as the unit of tissues. G9: composition, structure, processes, division. | No organelles, membrane transport or division in G8. |
| Reproduction and development | G8: human organs, cycle, health, human stages. G10: gametogenesis phases, comparisons across organisms, direct and indirect development. | No gametogenesis or cleavage detail in G8; no human anatomy or STI items in G10. |
| Meiosis | G9 „Възпроизводство на клетката“. G10 uses it as known. | G10 items do not re-test phases of meiosis; G9 items do not test inheritance. |
| DNA and genetic processes | G9 „Структура и процеси в клетката“ (replication, transcription, translation). G10 heredity works with genes and alleles. | No genetic code tables, operons or mutation mechanisms at molecular level. |
| Viruses, bacteria, immunity | Viruses: G9 theme 2. Bacteria: G9 theme 3. Immunity and vaccines: G8 cardiovascular. | Vaccine items in G9 stay at prophylaxis level; G8 does not test virus reproduction. |
| Diseases | Each system's diseases stay with that system. First aid: trauma with the musculoskeletal topic, burns/frostbite/heat with skin, bleeding with cardiovascular. | No clinical detail beyond cause – signs – prevention. |
| Regulation | Nervous vs hormonal comparison is tested once, in the endocrine topic; the nervous topic covers the link from its side only. | |
| Ecology vs evolution | Adaptation as a response to factors: G10 ecological factors. Adaptation as a result of selection: G10 3.1. | |
| Evolution topics | Mechanisms 3.1, humans 3.2, evidence 3.3. | Evidence items are not placed in 3.1; human fossils are 3.2. |

Out of scope everywhere: profiled XI–XII modules, plant and animal systematics
from earlier grades, amitosis, „висша нервна дейност“, psychosexual development
stages and the removed race-related results (all taken out in 2023).

## 5. Images and diagrams

Priority per topic is in the main table. **Important** means the programme
itself says „означава върху схема, изображение, модел“ or „разчита“ for the
core of the topic (tissues, organ systems, the cell, division phases, genetic
schemes and karyograms, pyramids and food webs, homologous organs). **Useful**
means diagrams would add items but text covers the topic. Nothing is collected
now: a question that needs a picture is not written, and a described table or
described experiment is used instead where that works.

## 6. Pipeline limits and collection runs

| Limit | Ceiling | Plan |
|---|---|---|
| Subject and grade per run | 1 and 1 | 1 and 1 |
| `max_topics` | 10 | 1 |
| `max_candidates` | 250 | at most 40 |
| `max_candidates_per_source` | 50 | equal to the run's `max_candidates` (at most 40) |
| `max_pages` | 100 | 8 |
| `max_runtime_minutes` | 60 | 60 |

The binding limits are runtime and the per-source cap. Physics runs produced
about 23 candidates in 12–30 minutes, so 40 is the most one 60-minute run
should attempt; most Biology candidates will cite one official source, so a run
could not exceed 50 in any case. Topics above 40 are therefore collected in two
or three runs. No limit is changed.

- Dataset: `data/question_candidates/biology/grade-<n>/<topic slug>-run-<k>.json`
  (ignored by Git), run id `<topic slug>-run-<k>`, started with
  `src.start_question_collection` against the grade's structure file.
- One run = one topic = one file = one session. No two sessions share a file.
- Run k of a topic starts only after run k−1 is finished.

**47 runs in 12 waves of up to 4 simultaneous sessions.** Waves are balanced by candidates, not topics.

| Wave | Runs (topic slug without `biology-`, run, max candidates) | Candidates |
|---|---|---|
| 1 | `g8-cell-and-tissues` run-1 (40); `g8-skin` run-1 (40); `g10-origin-and-evolution-of-humans` run-1 (40); `g9-chemical-composition-of-living-matter` run-1 (39) | 159 |
| 2 | `g9-application-of-cell-knowledge` run-1 (38); `g8-nervous-system` run-1 (37); `g10-population-biocenosis-ecosystem` run-1 (37); `g10-origin-and-development-of-living-matter` run-1 (36) | 148 |
| 3 | `g8-digestive-system` run-1 (34); `g8-musculoskeletal-system` run-1 (33); `g10-reproduction-growth-and-individual-development` run-1 (33); `g8-sensory-systems` run-1 (32) | 132 |
| 4 | `g9-cell-structure-and-processes` run-1 (32); `g10-heredity-and-variation` run-1 (32); `g8-cardiovascular-system` run-1 (30); `g9-cell-reproduction` run-1 (30) | 124 |
| 5 | `g8-respiratory-system` run-1 (28); `g8-reproductive-system` run-1 (28); `g8-endocrine-system` run-1 (28); `g10-ecological-factors` run-1 (28) | 112 |
| 6 | `g8-excretory-system` run-1 (24); `g9-supramolecular-complexes` run-1 (24); `g10-evidence-for-biological-evolution` run-1 (24); `g8-embryonic-and-postembryonic-development` run-1 (22) | 94 |
| 7 | `g9-chemical-composition-of-living-matter` run-2 (39); `g8-nervous-system` run-2 (37); `g10-population-biocenosis-ecosystem` run-2 (37); `g10-origin-and-development-of-living-matter` run-2 (36) | 149 |
| 8 | `g8-digestive-system` run-2 (34); `g8-musculoskeletal-system` run-2 (33); `g10-reproduction-growth-and-individual-development` run-2 (33); `g8-sensory-systems` run-2 (32) | 132 |
| 9 | `g9-cell-structure-and-processes` run-2 (32); `g10-heredity-and-variation` run-2 (32); `g9-cell-reproduction` run-2 (30); `g8-cardiovascular-system` run-2 (29) | 123 |
| 10 | `g8-reproductive-system` run-2 (28); `g8-endocrine-system` run-2 (28); `g10-ecological-factors` run-2 (28); `g8-respiratory-system` run-2 (27) | 111 |
| 11 | `g8-excretory-system` run-2 (24); `g9-supramolecular-complexes` run-2 (24); `g10-evidence-for-biological-evolution` run-2 (24); `g8-embryonic-and-postembryonic-development` run-2 (22) | 94 |
| 12 | `g10-heredity-and-variation` run-3 (32); `g9-cell-structure-and-processes` run-3 (31); `g8-cardiovascular-system` run-3 (29) | 92 |

Four simultaneous sessions is the recommendation: each is independent, and four
keeps usage and the review backlog manageable. Six is possible in waves 1–6
(all first runs); more than that adds nothing because later runs wait on
earlier ones. **Run wave 1 as a pilot** and review one of its files before
starting wave 2, so that wording, depth and the concept labels are corrected
once instead of in 47 files.

Collectors do not approve, export or import; do not touch tracked application
files, the structure files, the database or the vault; do not stage or commit.

### After collection

1. Validate every file (`src.validate_question_candidates`, with `--db`, never
   `--annotate` onto the original).
2. Independent review per grade, in section-sized batches, plus one
   cross-file duplicate pass per grade sorted by `research.concept`:
   Grade 8 in five batches (one per section), Grade 9 in three (theme 3 alone),
   Grade 10 in three. Output: one correction specification per grade.
3. Deterministic correction from the specification.
4. Post-correction verification, then approval by a separate reviewer identity,
   export and import with the existing tools.

## 7. Model and effort

| Phase | Model and effort | Why |
|---|---|---|
| Planning (this document) | Opus 5.5 High | Scope judgement. |
| Collection, most topics | Sonnet 5.5 Medium | Bounded writing against a stated dimension list. |
| Collection, six topics | Sonnet 5.5 High | Accuracy-critical or reasoning-heavy: G9 „Структура и процеси в клетката“, „Възпроизводство на клетката“, „Химичен състав на живата материя“; G10 „Наследственост и изменчивост“, „Произход и развитие на живата материя“; G8 „Сърдечно-съдова система“. |
| Independent review | Opus 5.5 High | Scientific errors, scope and semantic duplicates are the expensive failures. |
| Deterministic correction | Sonnet 5.5 Medium | Applies an exact specification. |
| Post-correction verification | Sonnet 5.5 Medium or High | Mechanical comparison against the specification. |
| Validation, export, import | No reasoning model unless a step fails | Existing scripts. |

No collection on Opus. If the pilot shows Sonnet Medium writing mostly recall
items, raise those topics to High before buying more review.

## 8. Scientific verification priorities

Verification is for facts, not for scope: an authoritative source that goes
deeper does not widen the bank.

| Priority | Topics | Typical risks | Suitable sources |
|---|---|---|---|
| Highest | G10 heredity and variation | Ratios for interaction types, definitions of codominance vs incomplete dominance, epistasis and polymery examples, sex-linked inheritance, pedigree logic | OpenStax *Biology 2e* and *Concepts of Biology*; NCBI Bookshelf; NIH MedlinePlus Genetics and NHGRI for hereditary diseases |
| Highest | G9 cell structure and processes; cell reproduction | Organelle functions, passive vs active transport, where each genetic process happens, phase events, chromosome numbers through meiosis | OpenStax *Biology 2e*; NCBI Bookshelf (*Molecular Biology of the Cell* for checking only); Nature Scitable |
| High | G9 chemical composition; supramolecular complexes | Monomers and bonds named correctly, enzyme properties, virus life cycle, transmission routes | OpenStax; NCBI Bookshelf; WHO fact sheets for viral diseases |
| High | G8 cardiovascular (blood groups, immunity, vaccines), nervous, endocrine, sensory, reproductive | Transfusion compatibility, hormone–gland pairs, cycle timing, STI facts, substance effects | OpenStax *Anatomy and Physiology 2e*; WHO; NIH/MedlinePlus; Bulgarian Ministry of Health material for the national immunisation calendar if it is mentioned at all |
| High | G10 evolution topics | Darwinian vs modern factors, species criteria, hominin stages and dates, homologous vs analogous examples | Understanding Evolution (UC Berkeley); Smithsonian Human Origins; OpenStax |
| Medium | G10 ecology topics | Pyramid types, energy transfer, succession, niche vs habitat | OpenStax; Nature Scitable; EEA or IPBES only for anthropogenic examples |
| Medium | Remaining G8 systems; G8 first aid | First-aid steps change over time | OpenStax *Anatomy and Physiology 2e*; Red Cross / Bulgarian Red Cross first-aid guidance |

Bulgarian terminology follows the programme's wording; where a textbook term
differs from the programme, the programme wins. Third-party pages are research
only: questions are newly written (`original_from_research`), never copied.

## 9. Open points

1. Raw targets are estimates. The pilot wave should be used to recalibrate
   them, especially for the two largest banks.
2. The split of dimensions between runs is contiguous and approximate; a
   collector may report that a dimension is exhausted and stop early.
3. The Grade 9 structure has one topic per section, so its five banks are also
   section banks; „Структура и процеси в клетката“ will be a 70–80 question
   section and may deserve a later decision about how tests draw from it.
4. Image-based questions are deferred; topics marked "important" will be
   under-covered on recognition until an image pass exists.

