# ingest/tagger.py
#
# Section 19 verbatim — six type-specific tagger prompts with real
# few-shot examples drawn from the 48 seed PYQs.
#
# FIX 2 applied in get_tagger_prompt — `sentences` is formatted as readable
# text, never as a dict.
# FIX 4 applied in VA_STRUCTURAL_TAGGER_PROMPT — note clarifies how
# correct_option and correct_order relate for wrong-one-out.

# ─────────────────────────────────────────────────────────────────────────────
# RC TAGGER
# ─────────────────────────────────────────────────────────────────────────────

RC_TAGGER_PROMPT = """Tag this RC question using ONLY the listed values.
Return valid JSON matching the schema below.

VALID SUBSKILLS (pick exactly one):
inference_basic | strengthen_weaken | main_idea_full_passage |
author_tone | specific_detail | vocab_in_context |
purpose_of_example | logical_structure

VALID TRAPS (1-3 items per question; never mix 'none' with others):
half_right_half_wrong | out_of_scope | too_extreme |
true_but_not_inferable | content_over_purpose | other | none

OPTION_TRAPS: for each wrong option assign one trap from the list above.
Correct option = null. Example:
{{"A": "out_of_scope", "B": null, "C": "too_extreme", "D": "true_but_not_inferable"}}

DIFFICULTY: easy | medium | hard (use hard for CAT-level multi-step reasoning)

ONE_LINE_TECHNIQUE: a single sentence (≤25 words) naming the cognitive move
that cracks this question. This is the embedding anchor — make it specific
and transferable, not a restatement of the question.

─── FEW-SHOT EXAMPLES ───

EXAMPLE 1 (main_idea_full_passage):
Passage topic: conservation biology — western barred bandicoot
Question: Which one of the following statements provides a gist of this passage?
A) The onslaught of animals brought in by the British led to the extinction of the western barred bandicoot.
B) Marsupials are going extinct due to the colonial era transformation of the ecosystem.
C) A type of bandicoots was nearly wiped out by invasive species but rescuers now pin hopes on a remnant island population.
D) The negligent attitude of the British colonists led to their annihilation.
Correct: C
Explanation: C captures both halves — near-wipeout by invasive species AND the present-day revival effort.

Expected tag output:
{{
  "subskill": "main_idea_full_passage",
  "traps_present": ["too_extreme", "out_of_scope", "half_right_half_wrong"],
  "option_traps": {{"A": "too_extreme", "B": "out_of_scope", "C": null, "D": "half_right_half_wrong"}},
  "difficulty": "medium",
  "one_line_technique": "Main idea must cover both halves of the passage — problem AND response; reject options that overstate or cover only one side."
}}

EXAMPLE 2 (purpose_of_example):
Passage topic: streaming services and digital art
Question: What is the purpose of the 'Netflix editing Stranger Things' example used in the passage?
A) To show that art in the digital age is no longer sacrosanct.
B) To show streaming services control access to the cultural commons.
C) To show unsubstantiated reports are increasing distrust of streaming services.
D) To show a practice that justifies fears that streaming services cannot be trusted as custodians of cultural artefacts.
Correct: D
Explanation: The example directly follows 'seemed like vindication to those who had long warned' — its rhetorical job is to evidence pre-existing distrust.

Expected tag output:
{{
  "subskill": "purpose_of_example",
  "traps_present": ["content_over_purpose", "out_of_scope", "half_right_half_wrong"],
  "option_traps": {{"A": "content_over_purpose", "B": "out_of_scope", "C": "half_right_half_wrong", "D": null}},
  "difficulty": "hard",
  "one_line_technique": "For purpose questions, ask what JOB this example does in the argument — not what it describes; content-over-purpose is the dominant trap."
}}

EXAMPLE 3 (inference_basic):
Passage topic: crafts and labour
Question: We can infer from the passage that medieval crafts guilds resembled mass production in that both
A) discouraged innovation by restricting entry through strict rules.
B) did not always employ egalitarian production processes.
C) did not necessarily promote creativity.
D) focused excessively on product quality.
Correct: C
Explanation: Mass production prioritises efficiency; guilds' hierarchy 'knocked the innovative spirit out'. Shared trait = failure to promote creativity.

Expected tag output:
{{
  "subskill": "inference_basic",
  "traps_present": ["half_right_half_wrong", "out_of_scope", "true_but_not_inferable"],
  "option_traps": {{"A": "half_right_half_wrong", "B": "true_but_not_inferable", "C": null, "D": "out_of_scope"}},
  "difficulty": "hard",
  "one_line_technique": "Comparison inference = find the trait true of BOTH items; eliminate options true of only one side."
}}

─── QUESTION TO TAG ───

Passage topic: {topic}
Question: {question_text}
A) {A}
B) {B}
C) {C}
D) {D}
Correct: {correct_option}
Explanation: {explanation}

Return JSON only."""


# ─────────────────────────────────────────────────────────────────────────────
# PJ TAGGER
# ─────────────────────────────────────────────────────────────────────────────

PJ_TAGGER_PROMPT = """Tag this PJ (para jumble) question using ONLY the listed values.

VALID SUBSKILLS (pick one):
structural_identification | sequence_logic | pronoun_reference | example_principle_link

Choose based on the dominant clue that solves the PJ:
- structural_identification: opener/closer locked by topic sentence or conclusion markers
- sequence_logic: transition words (therefore, however, so, despite) force order
- pronoun_reference: pronouns (it, they, this) force antecedent to come first
- example_principle_link: general claim → specific example mandatory order

VALID TRAPS: out_of_scope | other | none (PJs rarely have option-level traps)

PJ_CONNECTOR_MAP: for each sentence with a transition word/phrase, output:
{{"<sentence_label>": {{"connector": "<word>", "expected_position": <1-4 or 1-5>, "cannot_be_opening": <bool>}}}}
Empty {{}} if no clear connectors.

OPENING_CLUE: one sentence explaining why the first sentence cannot be anything else.

DIFFICULTY: easy | medium | hard

─── FEW-SHOT EXAMPLES ───

EXAMPLE 1 (sequence_logic):
Sentences:
1. Algorithms hosted on the internet are accessed by many, so biases in AI models have resulted in much larger impact.
2. Though 'algorithmic bias' is the popular term, the foundation of such bias is not in algorithms, but in the data.
3. Despite their widespread impact, it is relatively easier to fix AI biases than human-generated biases.
4. The impact of biased decisions made by humans is localised, but with the advent of AI, the impact is spread over a much wider scale.
Correct order: 4,1,2,3
Explanation: 4 opens the contrast (localised vs widespread). 1 extends via 'so'. 2 clarifies the TRUE source via 'though'. 3 concludes via 'despite'.

Expected tag output:
{{
  "subskill": "sequence_logic",
  "connector_type": "contrast_then_clarification",
  "opening_clue": "Sentence 4 stands alone with no backward reference; all others have connectors pointing back.",
  "pj_connector_map": {{
    "1": {{"connector": "so", "expected_position": 2, "cannot_be_opening": true}},
    "2": {{"connector": "though", "expected_position": 3, "cannot_be_opening": true}},
    "3": {{"connector": "despite", "expected_position": 4, "cannot_be_opening": true}}
  }},
  "traps_present": ["none"],
  "option_traps": {{}},
  "difficulty": "medium",
  "one_line_technique": "Lock the opener as the only sentence with no backward reference; then use connectors (though, despite, so) to fix the rest."
}}

EXAMPLE 2 (structural_identification):
Sentences:
1. What precisely are the 'unusual elements' that make a particular case so attractive to a certain kind of audience?
2. It might be a particularly savage level of depravity, very often related to the amount of mystery involved.
3. Unsolved, and perhaps unsolvable cases offer something that 'ordinary' murder doesn't.
4. Why are some crimes destined for perpetual re-examination and others locked into permanent obscurity?
Correct order: 4,1,2,3
Explanation: 4 opens with the broad question. 1 narrows. 2 answers. 3 concludes with the mystery-specific payoff.

Expected tag output:
{{
  "subskill": "structural_identification",
  "connector_type": "question_to_answer",
  "opening_clue": "Sentence 4 is a broad framing question; sentence 1 ('What precisely...') is a narrowing question that cannot precede the broad one.",
  "pj_connector_map": {{
    "1": {{"connector": "what precisely", "expected_position": 2, "cannot_be_opening": false}},
    "3": {{"connector": "unsolved", "expected_position": 4, "cannot_be_opening": true}}
  }},
  "traps_present": ["none"],
  "option_traps": {{}},
  "difficulty": "medium",
  "one_line_technique": "When two questions appear in a PJ, the broader (why X in general) comes before the narrower (what precisely is X)."
}}

─── QUESTION TO TAG ───

Sentences: {sentences}
Correct order: {correct_order}
Explanation: {explanation}

Return JSON only."""


# ─────────────────────────────────────────────────────────────────────────────
# VA STRUCTURAL TAGGER (odd-one-out)
# ─────────────────────────────────────────────────────────────────────────────

VA_STRUCTURAL_TAGGER_PROMPT = """Tag this odd-one-out question.

NOTE: For wrong-one-out, correct_option is the letter of the odd
sentence (A-D), and correct_order is the remaining four sentences in
proper sequence.

VALID SUBSKILLS: sentence_odd_one_out
VALID TRAPS: theme_break | out_of_scope | other | none
OPTION_TRAPS: wrong option = trap, correct option = null.
DIFFICULTY: easy | medium | hard

─── FEW-SHOT EXAMPLES ───

EXAMPLE 1:
Sentences:
1. Animals have an interest in fulfilling their basic needs, but also in avoiding suffering.
2. Singer viewed himself as a utilitarian, presenting a direct moral theory concerning animal rights.
3. He argued for extending moral consideration to animals because animals have significant interests.
4. The event that publicly announced animal rights as a legitimate issue was Peter Singer's Animal Liberation text in 1975.
5. As such, we ought to view their interests alongside and equal to human interests.
Correct answer: Sentence 1 is the odd one
Correct order of other four: 4,2,3,5
Explanation: Sentences 4→2→3→5 chain Singer's utilitarian framework. Sentence 1 is a generic moral claim that doesn't reference Singer.

Expected tag output:
{{
  "subskill": "sentence_odd_one_out",
  "traps_present": ["theme_break"],
  "option_traps": {{"A": null, "B": "theme_break", "C": "theme_break", "D": "theme_break"}},
  "difficulty": "medium",
  "one_line_technique": "Odd-one-out = same topic, wrong angle; if four sentences argue FROM a specific framework and one argues WITHOUT referencing it, that one is odd."
}}

EXAMPLE 2:
Sentences:
1. Urbanites have more and better options for getting around: Uber, dockless bicycles, scooters.
2. When more people use buses or trains the service usually improves.
3. Worsening services, terrorist attacks and a rise in fares have been blamed for the trend.
4. Public transport is being squeezed structurally as people's need to travel is diminishing.
5. There has been a puzzling decline in the use of urban public transport in the west.
Correct answer: Sentence 2 is the odd one
Correct order of other four: 5,3,4,1
Explanation: 5 introduces puzzle. 3 gives proximate causes. 4 gives structural cause. 1 reinforces with alternatives. Sentence 2 claims the opposite dynamic.

Expected tag output:
{{
  "subskill": "sentence_odd_one_out",
  "traps_present": ["theme_break"],
  "option_traps": {{"A": "theme_break", "B": null, "C": "theme_break", "D": "theme_break"}},
  "difficulty": "medium",
  "one_line_technique": "Odd-one-out often breaks on DIRECTION not topic; same subject pointing the opposite way is the outlier."
}}

─── QUESTION TO TAG ───

Sentences: {sentences}
Correct answer: {correct_option}
Correct order: {correct_order}
Explanation: {explanation}

Return JSON only."""


# ─────────────────────────────────────────────────────────────────────────────
# VA INSERTION TAGGER
# ─────────────────────────────────────────────────────────────────────────────

VA_INSERTION_TAGGER_PROMPT = """Tag this sentence insertion question.

VALID SUBSKILLS: sentence_insertion
VALID TRAPS: theme_break | out_of_scope | half_right_half_wrong | other | none
OPTION_TRAPS: wrong option = trap, correct option = null.
DIFFICULTY: easy | medium | hard

─── FEW-SHOT EXAMPLES ───

EXAMPLE 1:
Source paragraph: "___(1)___. You can't just put things anywhere you want to. The evolved architecture of the brain is haphazard and disjointed. ___(2)___. Evolution doesn't design things... ___(3)___. The brain is more like a big, old house with piecemeal renovations..."
Missing sentence: "The brain isn't organized the way you might set up your home office or bathroom medicine cabinet."
Options: A) Option 4, B) Option 2, C) Option 1, D) Option 3
Correct: C
Explanation: Blank 1 introduces the contrast between intuitive organization (home office) and the brain's evolutionary complexity. The next sentence ('You can't just put things anywhere') directly builds on this.

Expected tag output:
{{
  "subskill": "sentence_insertion",
  "traps_present": ["theme_break"],
  "option_traps": {{"A": "theme_break", "B": "theme_break", "C": null, "D": "theme_break"}},
  "difficulty": "medium",
  "one_line_technique": "An inserted sentence that introduces a metaphor often goes FIRST; if the same idea is already developed downstream, the sentence is an opener."
}}

EXAMPLE 2:
Source paragraph: "The experience of reading philosophy is often disquieting. When reading philosophy, the values around which one has heretofore organised one's life may come to look provincial, flatly wrong, or even evil. ___(1)___. When beliefs previously held as truths are rendered implausible, new beliefs may be required. ___(2)___. What's worse, philosophers admonish each other to remain unsutured..."
Missing sentence: "This philosophical cut at one's core beliefs, values, and way of life is difficult enough."
Options: A) Blank A, B) Blank B, C) Blank C, D) Blank D
Correct: B
Explanation: Blank B (Option 2) follows the description of values appearing 'provincial, flatly wrong'. The sentence summarises that cut and leads into 'what's worse'.

Expected tag output:
{{
  "subskill": "sentence_insertion",
  "traps_present": ["theme_break"],
  "option_traps": {{"A": "theme_break", "B": null, "C": "theme_break", "D": "theme_break"}},
  "difficulty": "medium",
  "one_line_technique": "'This X is difficult enough' patterns follow a description of X; identify what X is and place the sentence after it, not before."
}}

EXAMPLE 3:
Source paragraph: (Renaissance music paragraph) "...This music boom lasted for thirty years... ___(2)___. The rebirth in both literature and music originated in Italy... Renaissance music was mostly polyphonic in texture. ___(3)___. Extreme contrasts in dynamics, rhythm, and tone colour do not occur..."
Missing sentence: "Comprehending a wide range of emotions, Renaissance music nevertheless portrayed all emotions in a balanced and moderate fashion."
Options: A) Option 3, B) Option 4, C) Option 1, D) Option 2
Correct: A
Explanation: Position 3 follows 'Renaissance music was mostly polyphonic' and sets up 'Extreme contrasts... do not occur'. The emotional-balance claim bridges polyphony and the lack-of-contrasts naturally.

Expected tag output:
{{
  "subskill": "sentence_insertion",
  "traps_present": ["theme_break"],
  "option_traps": {{"A": null, "B": "theme_break", "C": "theme_break", "D": "theme_break"}},
  "difficulty": "medium",
  "one_line_technique": "A general claim goes BEFORE specific instances that illustrate it; find the slot where the sentence bridges general→specific."
}}

─── QUESTION TO TAG ───

Source paragraph: {source_text}
Question: {question_text}
A) {A}
B) {B}
C) {C}
D) {D}
Correct: {correct_option}
Explanation: {explanation}

Return JSON only."""


# ─────────────────────────────────────────────────────────────────────────────
# VA SUMMARY TAGGER
# ─────────────────────────────────────────────────────────────────────────────

VA_SUMMARY_TAGGER_PROMPT = """Tag this passage summary question.

VALID SUBSKILLS: passage_summary
VALID TRAPS: out_of_scope | too_extreme | half_right_half_wrong | other | none
OPTION_TRAPS: wrong option = trap, correct option = null.
DIFFICULTY: easy | medium | hard

─── FEW-SHOT EXAMPLES ───

EXAMPLE 1:
Source paragraph: "Scientific research shows that many animals are very intelligent... Many animals also display wide-ranging emotions, including joy, happiness, empathy, compassion, grief... It's not surprising that animals share many emotions with us because we also share brain structures, located in the limbic system, that are the seat of our emotions."
Question: Which option best captures the essence of the passage?
A) The advanced sensory and motor abilities of animals is the reason why they can display wide-ranging emotions.
B) The similarity in brain structure explains why animals show emotions typically associated with humans.
C) Animals can show emotions which are typically associated with humans.
D) Animals are more intelligent than us in sensing danger and detecting diseases.
Correct: B
Explanation: B preserves the causal backbone — shared limbic structures explain shared emotions. C drops the WHY.

Expected tag output:
{{
  "subskill": "passage_summary",
  "traps_present": ["half_right_half_wrong", "out_of_scope"],
  "option_traps": {{"A": "half_right_half_wrong", "B": null, "C": "half_right_half_wrong", "D": "out_of_scope"}},
  "difficulty": "medium",
  "one_line_technique": "A summary must preserve the passage's causal backbone; options stating only the observation (without mechanism) are incomplete."
}}

EXAMPLE 2:
Source paragraph: "Colonialism is not a modern phenomenon... In the sixteenth century, colonialism changed decisively because of technological developments in navigation... The modern European colonial project emerged when it became possible to move large numbers of people across the ocean..."
Question: Which option best captures the essence of the passage?
A) Colonialism surged in the 16th century due to advancements in navigation, enabling British settlements.
B) As a result of developments in navigation, European colonialism led to displacement and political changes in the 16th century.
C) Colonialism, conceptualized in the 16th century, allowed colonizers to expand.
D) Technological advancements in navigation in the 16th century transformed colonialism, enabling Europeans to establish settlements and exert political dominance over distant regions.
Correct: D
Explanation: D preserves continuity-vs-change: colonialism was TRANSFORMED, not invented. C wrongly says 'conceptualized in the 16th century'.

Expected tag output:
{{
  "subskill": "passage_summary",
  "traps_present": ["half_right_half_wrong", "too_extreme"],
  "option_traps": {{"A": "too_extreme", "B": "half_right_half_wrong", "C": "half_right_half_wrong", "D": null}},
  "difficulty": "medium",
  "one_line_technique": "Summary must preserve continuity-vs-change distinction; when passage says X 'changed decisively', don't pick an option saying X was 'conceptualized'."
}}

EXAMPLE 3:
Source paragraph: "Certain codes may be so widely distributed... that they appear not to be constructed but 'naturally' given... However, this does not mean that no codes have intervened; rather, that the codes have been profoundly naturalized. The operation of naturalized codes reveals... the depth and near-universality of the codes in use. This has the (ideological) effect of concealing the practices of coding."
Question: Which option best captures the essence of the passage?
A) All codes have a natural origin but some are so widespread that they become universal.
B) Not all codes are natural but certain codes are naturalized. Ideology aims to hide the mechanism of coding behind signs.
C) Language and visual signs are codes. However, some codes are so widespread that they seem naturally given and also hide the mechanism of coding behind the signs.
D) Learning signs at an early age makes all such codes appear natural. This naturalization is the effect of ideology.
Correct: C
Explanation: C captures both moves — codes SEEM natural despite being constructed AND this appearance conceals the coding mechanism. B attributes to ideology an 'aim' to hide; passage calls it an effect.

Expected tag output:
{{
  "subskill": "passage_summary",
  "traps_present": ["too_extreme", "out_of_scope", "half_right_half_wrong"],
  "option_traps": {{"A": "too_extreme", "B": "out_of_scope", "C": null, "D": "half_right_half_wrong"}},
  "difficulty": "hard",
  "one_line_technique": "Summary options must not add claims the passage doesn't make — watch for added intent claims ('aims to') or absolute claims ('all codes')."
}}

─── QUESTION TO TAG ───

Source paragraph: {source_text}
Question: {question_text}
A) {A}
B) {B}
C) {C}
D) {D}
Correct: {correct_option}
Explanation: {explanation}

Return JSON only."""


# ─────────────────────────────────────────────────────────────────────────────
# VA SEMANTIC TAGGER (grammar / vocab / fill-in-blank)
# ─────────────────────────────────────────────────────────────────────────────

VA_SEMANTIC_TAGGER_PROMPT = """Tag this grammar/vocabulary question.

VALID SUBSKILLS (pick one — must match question type):
- For va_grammar or va_sentence_correction: grammar_rule
- For va_vocab: vocabulary_meaning
- For va_fill_in_blank: vocabulary_meaning or paragraph_completion

VALID TRAPS: out_of_scope | half_right_half_wrong | other | none
OPTION_TRAPS: wrong option = trap, correct option = null.
DIFFICULTY: easy | medium | hard

─── FEW-SHOT EXAMPLES ───

EXAMPLE 1 (va_grammar — subject-verb agreement):
Question: Which of the following sentences is grammatically correct?
A) Neither the manager nor the employees was aware of the change.
B) Neither the manager nor the employees were aware of the change.
C) Neither the manager nor the employees is aware of the change.
D) Neither the manager nor the employees has been aware of the change.
Correct: B
Explanation: With 'neither... nor', the verb agrees with the subject closest to it. 'Employees' is plural → 'were'.

Expected tag output:
{{
  "subskill": "grammar_rule",
  "traps_present": ["half_right_half_wrong"],
  "option_traps": {{"A": "half_right_half_wrong", "B": null, "C": "half_right_half_wrong", "D": "half_right_half_wrong"}},
  "difficulty": "medium",
  "one_line_technique": "With neither/nor or either/or, verb agrees with the subject CLOSER to it, not the first subject."
}}

EXAMPLE 2 (va_vocab — contextual meaning):
Question: Choose the word that best fits the blank: "The professor's _____ remarks during the lecture surprised the students, as he was usually reserved."
A) loquacious
B) taciturn
C) reticent
D) circumspect
Correct: A
Explanation: The contrast 'usually reserved' signals the blank needs a word meaning the opposite — talkative. Only 'loquacious' fits.

Expected tag output:
{{
  "subskill": "vocabulary_meaning",
  "traps_present": ["half_right_half_wrong"],
  "option_traps": {{"A": null, "B": "half_right_half_wrong", "C": "half_right_half_wrong", "D": "out_of_scope"}},
  "difficulty": "medium",
  "one_line_technique": "Contextual vocabulary — look for a contrast or similarity marker in the surrounding sentence that constrains the blank's meaning."
}}

EXAMPLE 3 (va_sentence_correction — modifier placement):
Question: Which sentence is correctly constructed?
A) Walking through the garden, the flowers bloomed beautifully.
B) Walking through the garden, she saw flowers blooming beautifully.
C) The flowers, walking through the garden, bloomed beautifully.
D) Beautifully blooming, she walked through the garden of flowers.
Correct: B
Explanation: The participle 'walking' must modify a human subject, not 'flowers'. B is the only option where 'walking' correctly modifies 'she'.

Expected tag output:
{{
  "subskill": "grammar_rule",
  "traps_present": ["half_right_half_wrong"],
  "option_traps": {{"A": "half_right_half_wrong", "B": null, "C": "half_right_half_wrong", "D": "half_right_half_wrong"}},
  "difficulty": "medium",
  "one_line_technique": "Dangling modifier test — the introductory participial phrase must modify the subject of the main clause; 'walking' needs a person, not an object."
}}

─── QUESTION TO TAG ───

Question type: {type}
Question: {question_text}
A) {A}
B) {B}
C) {C}
D) {D}
Correct: {correct_option}
Explanation: {explanation}

Return JSON only."""


# ─────────────────────────────────────────────────────────────────────────────
# DISPATCHER
# ─────────────────────────────────────────────────────────────────────────────

def get_tagger_prompt(q_type: str, question: dict) -> str:
    """Select prompt based on question type and format it with question fields."""
    mapping = {
        'rc_question':            RC_TAGGER_PROMPT,
        'pj':                     PJ_TAGGER_PROMPT,
        'va_wrong_one_out':       VA_STRUCTURAL_TAGGER_PROMPT,
        'va_sentence_insertion':  VA_INSERTION_TAGGER_PROMPT,
        'va_summary':             VA_SUMMARY_TAGGER_PROMPT,
        'va_grammar':             VA_SEMANTIC_TAGGER_PROMPT,
        'va_sentence_correction': VA_SEMANTIC_TAGGER_PROMPT,
        'va_vocab':               VA_SEMANTIC_TAGGER_PROMPT,
        'va_fill_in_blank':       VA_SEMANTIC_TAGGER_PROMPT,
    }
    template = mapping.get(q_type, RC_TAGGER_PROMPT)

    # Build format kwargs with safe defaults
    fmt_kwargs = {
        "type": q_type,
        "topic": question.get("topic") or question.get("passage_topic") or "general",
        "question_text": question.get("question_text", ""),
        "correct_option": question.get("correct_option") or "",
        "correct_order": question.get("correct_order") or "",
        "explanation": question.get("explanation", ""),
        "source_text": question.get("source_text") or question.get("passage_text") or "",
    }

    # FIX 2 — sentences must be readable text, not a dict repr.
    if question.get("sentences"):
        fmt_kwargs["sentences"] = "\n".join(
            f"{k}: {v}" for k, v in question["sentences"].items()
        )
    else:
        fmt_kwargs["sentences"] = ""

    opts = question.get("options") or {}
    fmt_kwargs["A"] = opts.get("A", "")
    fmt_kwargs["B"] = opts.get("B", "")
    fmt_kwargs["C"] = opts.get("C", "")
    fmt_kwargs["D"] = opts.get("D", "")

    return template.format(**fmt_kwargs)
