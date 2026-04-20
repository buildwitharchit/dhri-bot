# bot/keyboards.py
#
# Inline keyboard builders used across handlers.

from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from config import SKILL_DISPLAY_NAMES


def home_quick_keyboard(profile: Optional[dict] = None) -> InlineKeyboardMarkup:
    """Main home menu. Adds 'Work on X' row only when weakest_skill is set."""
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton("Reading Comp", callback_data="mode_rc"),
            InlineKeyboardButton("Para Jumbles", callback_data="mode_pj"),
        ],
        [
            InlineKeyboardButton("Vocab & Grammar", callback_data="mode_va"),
            InlineKeyboardButton("Stats", callback_data="stats_full"),
        ],
    ]
    weakest = (profile or {}).get("weakest_skill")
    if weakest:
        label = SKILL_DISPLAY_NAMES.get(weakest, weakest.title())
        rows.insert(
            0,
            [InlineKeyboardButton(f"Work on {label} ⚡", callback_data="drill_weakest")],
        )
    return InlineKeyboardMarkup(rows)


def answer_keyboard() -> InlineKeyboardMarkup:
    """Four-option MCQ answer buttons (A/B/C/D)."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("A", callback_data="answer_A"),
            InlineKeyboardButton("B", callback_data="answer_B"),
            InlineKeyboardButton("C", callback_data="answer_C"),
            InlineKeyboardButton("D", callback_data="answer_D"),
        ]
    ])


def onboarding_year_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("2025", callback_data="onboard_year_2025"),
            InlineKeyboardButton("2026", callback_data="onboard_year_2026"),
            InlineKeyboardButton("2027", callback_data="onboard_year_2027"),
        ]
    ])


def onboarding_level_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Beginner", callback_data="onboard_level_beginner"),
            InlineKeyboardButton("Intermediate", callback_data="onboard_level_intermediate"),
        ],
        [
            InlineKeyboardButton("Advanced", callback_data="onboard_level_advanced"),
        ],
    ])


def va_type_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Grammar", callback_data="va_type_va_grammar"),
            InlineKeyboardButton("Vocabulary", callback_data="va_type_va_vocab"),
        ],
        [
            InlineKeyboardButton(
                "Sentence Correction", callback_data="va_type_va_sentence_correction"
            ),
            InlineKeyboardButton(
                "Fill in Blank", callback_data="va_type_va_fill_in_blank"
            ),
        ],
        [
            InlineKeyboardButton(
                "Odd One Out", callback_data="va_type_va_wrong_one_out"
            ),
            InlineKeyboardButton(
                "Sentence Insertion", callback_data="va_type_va_sentence_insertion"
            ),
        ],
        [
            InlineKeyboardButton(
                "Passage Summary", callback_data="va_type_va_summary"
            ),
        ],
    ])
