# retrieval/technique_queries.py

from config import ALL_SUBSKILLS

SUBSKILL_TO_TECHNIQUE_QUERY = {
    # ─── RC subskills ───────────────────────────────────────────────────────
    "inference_basic": (
        "derive unstated conclusion from passage premises — "
        "eliminate options true in world but unsupported by text — "
        "find minimum assumption passage must be making — "
        "distinguish what passage implies from what it explicitly states"
    ),
    "strengthen_weaken": (
        "identify core claim author is defending — "
        "determine which statement makes argument more or less valid — "
        "find assumption the argument depends on — "
        "strengthen adds evidence for claim, weaken removes support"
    ),
    "main_idea_full_passage": (
        "identify central argument encompassing entire passage — "
        "correct answer covers everything not just one section — "
        "reject options describing only part of the passage — "
        "the answer is the claim everything else supports"
    ),
    "author_tone": (
        "detect author evaluative stance from word choice not topic — "
        "locate stance words adjectives adverbs expressing attitude — "
        "distinguish sardonic from critical from cautious from appreciative — "
        "how the author says it, not what they say"
    ),
    "specific_detail": (
        "locate explicit information stated directly in passage — "
        "find which paragraph contains the stated fact — "
        "match question to exact location in text"
    ),
    "vocab_in_context": (
        "determine word meaning as used in passage not dictionary — "
        "read surrounding sentences for semantic constraints — "
        "non-standard or technical usage common in CAT passages"
    ),
    "purpose_of_example": (
        "identify why author included this paragraph or example — "
        "not what it says but what it DOES in the argument — "
        "purpose asks for rhetorical function not content"
    ),
    "logical_structure": (
        "identify how passage is organized argumentatively — "
        "determine relationship between passage sections — "
        "recognize claim-evidence, problem-solution, compare-contrast"
    ),
    # ─── PJ subskills ────────────────────────────────────────────────────────
    "structural_identification": (
        "identify mandatory first sentence: no pronouns no backward references — "
        "identify mandatory last sentence: conclusion markers no dangling refs — "
        "lock opening and closing before arranging the middle"
    ),
    "sequence_logic": (
        "therefore thus consequently must follow their cause — "
        "however but must follow statement they contrast — "
        "transition words create mandatory sequence constraints"
    ),
    "pronoun_reference": (
        "sentence with pronoun cannot precede its antecedent — "
        "it they this these must follow noun introducing what they refer to — "
        "map all pronouns before ordering"
    ),
    "example_principle_link": (
        "general principle followed by specific example — "
        "for example for instance must follow the principle they illustrate — "
        "identify claim sentence and evidence sentence"
    ),
    # ─── VA subskills ────────────────────────────────────────────────────────
    "grammar_rule": (
        "identify correct sentence by applying specific grammatical rule — "
        "subject verb agreement tense consistency parallelism modifier placement — "
        "diagnose which rule applies before evaluating options"
    ),
    "vocabulary_meaning": (
        "select word matching semantic and tonal register of context — "
        "formal academic vocabulary in dense non-fiction — "
        "synonym fill-in-blank contextual usage"
    ),
    "sentence_odd_one_out": (
        "four sentences share specific angle odd one shares topic different aspect — "
        "coherence is about perspective and sub-theme not just topic — "
        "the odd sentence breaks logical or thematic continuity"
    ),
    "sentence_insertion": (
        "place sentence in slot bridging its two neighbours — "
        "inserted sentence must flow from prior and lead into next — "
        "check both the sentence before AND the sentence after the blank — "
        "theme direction and pronoun references must all match"
    ),
    "paragraph_completion": (
        "choose sentence logically and tonally completing the paragraph — "
        "must be consistent with argument direction and author stance — "
        "cannot contradict or ignore what paragraph established"
    ),
    "passage_summary": (
        "capture central claim including its causal backbone — "
        "preserve both halves of a two-part argument — "
        "reject options stating observation without mechanism — "
        "reject options adding claims not present in passage"
    ),
}

# Validated at startup — see main.py
