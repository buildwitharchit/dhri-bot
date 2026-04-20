# config.py

from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List, Dict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Telegram
    TELEGRAM_BOT_TOKEN: str
    WEBHOOK_SECRET: str

    # Database
    DATABASE_URL: str           # pooled — for app
    DATABASE_URL_DIRECT: str    # direct — for migrations only

    # Redis
    UPSTASH_REDIS_REST_URL: str
    UPSTASH_REDIS_REST_TOKEN: str

    # OpenRouter
    OPENROUTER_API_KEY: str
    OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"

    # Models
    MODEL_CHAT: str = "google/gemini-flash-1.5"
    MODEL_COMPLEX: str = "anthropic/claude-haiku-4-5"
    MODEL_TAGGER_STRUCTURED: str = "google/gemini-flash-1.5"
    MODEL_TAGGER_TECHNIQUE: str = "anthropic/claude-sonnet-4-5"
    MODEL_SUMMARIZER: str = "google/gemini-flash-1.5"
    MODEL_VERIFIER: str = "google/gemini-flash-1.5"
    MODEL_EMBEDDING: str = "openai/text-embedding-3-small"

    # Spend
    DAILY_LLM_SPEND_CAP_USD: float = 0.50

    # Admin
    ADMIN_REPORTS_SECRET: str

    # Railway
    RAILWAY_PUBLIC_DOMAIN: str
    PORT: int = 8000


settings = Settings()

# ─── TAXONOMY ──────────────────────────────────────────────────────────────

# Student-facing: 7 skills shown in UI, home screen, stats, weakest-skill prompt
STUDENT_SKILLS: List[str] = [
    "inference",
    "main_idea",
    "author_tone",
    "specific_detail",
    "purpose_and_structure",
    "para_jumbles",
    "vocab_grammar",
]

# Internal subskills: used for fine-grained retrieval, score tracking, tagger output
# Adding a subskill: add here, add to SUBSKILL_TO_TECHNIQUE_QUERY, done.
SUBSKILL_TO_SKILL: Dict[str, str] = {
    # inference group
    "inference_basic":          "inference",
    "strengthen_weaken":        "inference",
    # main_idea group
    "main_idea_full_passage":   "main_idea",
    "passage_summary":          "main_idea",
    # author_tone (no split)
    "author_tone":              "author_tone",
    # specific_detail group
    "specific_detail":          "specific_detail",
    "vocab_in_context":         "specific_detail",
    # purpose_and_structure group
    "purpose_of_example":       "purpose_and_structure",
    "logical_structure":        "purpose_and_structure",
    # para_jumbles group
    "structural_identification":"para_jumbles",
    "sequence_logic":           "para_jumbles",
    "pronoun_reference":        "para_jumbles",
    "example_principle_link":   "para_jumbles",
    # vocab_grammar group
    "grammar_rule":             "vocab_grammar",
    "vocabulary_meaning":       "vocab_grammar",
    "sentence_odd_one_out":     "vocab_grammar",
    "sentence_insertion":       "vocab_grammar",
    "paragraph_completion":     "vocab_grammar",
}

ALL_SUBSKILLS: List[str] = list(SUBSKILL_TO_SKILL.keys())

# Derived groupings — computed from SUBSKILL_TO_SKILL, not hardcoded
RC_SUBSKILLS = [s for s, sk in SUBSKILL_TO_SKILL.items()
                if sk in {"inference","main_idea","author_tone",
                          "specific_detail","purpose_and_structure"}
                and s != "passage_summary"]  # passage_summary uses va_summary type

PJ_SUBSKILLS = [s for s, sk in SUBSKILL_TO_SKILL.items() if sk == "para_jumbles"]

VA_SUBSKILLS = [s for s, sk in SUBSKILL_TO_SKILL.items()
                if sk == "vocab_grammar" or s == "passage_summary"]

# Which subskills are legal for each question type
# Enforced at ingest — any (type, subskill) not here is flagged needs_review
SKILL_TYPE_MATRIX: Dict[str, List[str]] = {
    "rc_question":            RC_SUBSKILLS,
    "pj":                     PJ_SUBSKILLS,
    "va_grammar":             ["grammar_rule"],
    "va_sentence_correction": ["grammar_rule"],
    "va_vocab":               ["vocabulary_meaning"],
    "va_fill_in_blank":       ["vocabulary_meaning", "paragraph_completion"],
    "va_wrong_one_out":       ["sentence_odd_one_out"],
    "va_sentence_insertion":  ["sentence_insertion"],
    "va_summary":             ["passage_summary"],
}

# ─── TRAPS ──────────────────────────────────────────────────────────────────

ALL_TRAPS: List[str] = [
    "half_right_half_wrong",    # dominant
    "out_of_scope",             # dominant
    "too_extreme",              # dominant
    "theme_break",              # dominant for VA-structural
    "true_but_not_inferable",
    "content_over_purpose",     # specific to purpose questions
    "other",                    # catchall for rare cases
    "none",                     # correct option
]

# ─── DISPLAY NAMES ──────────────────────────────────────────────────────────

SKILL_DISPLAY_NAMES: Dict[str, str] = {
    "inference":            "Inference",
    "main_idea":            "Main Idea",
    "author_tone":          "Author Tone",
    "specific_detail":      "Specific Detail",
    "purpose_and_structure":"Purpose & Structure",
    "para_jumbles":         "Para Jumbles",
    "vocab_grammar":        "Vocab & Grammar",
}

# ─── CONSTANTS ────────────────────────────────────────────────────────────

MIN_ATTEMPTS_FOR_WEAKEST_SKILL = 10
