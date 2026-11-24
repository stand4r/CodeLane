"""Local intent routing: verification is not permission to implement changes."""

import re


def verification_only(goal):
    text = goal.casefold()
    if re.search(r"тр[её]хэтап|три этапа|команд[ау]\s+из|three.stage", text):
        return False
    text = re.sub(r"\bне\s+(?:исправ\w*|добав\w*|измен\w*|реализ\w*|fix|modify)\b", "", text)
    mutation = re.search(
        r"\b(?:исправ\w*|доработ\w*|добав\w*|реализ\w*|внедр\w*|оптимизир\w*|почини\w*|измени\w*|optimize|fix|implement|add|modify|refactor)\b",
        text,
    )
    verification = re.search(
        r"\b(?:провер\w*|тест\w*|самотест\w*|протест\w*|повтор\w*|измер\w*|засек\w*|benchmark|test|verify|check)\b",
        text,
    )
    return bool(verification and not mutation)
