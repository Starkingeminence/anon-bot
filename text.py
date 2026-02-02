import re
import unicodedata

# -----------------------------
# Normalize text
# -----------------------------
def normalize_text(text: str) -> str:
    """
    Normalize text by:
    - Removing accents
    - Converting to lowercase
    - Stripping leading/trailing spaces
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("utf-8")
    text = text.lower().strip()
    return text


# -----------------------------
# Remove emojis
# -----------------------------
def remove_emojis(text: str) -> str:
    """
    Remove emojis from a string.
    """
    emoji_pattern = re.compile(
        "["
        "\U0001F600-\U0001F64F"  # emoticons
        "\U0001F300-\U0001F5FF"  # symbols & pictographs
        "\U0001F680-\U0001F6FF"  # transport & map symbols
        "\U0001F1E0-\U0001F1FF"  # flags
        "\U00002700-\U000027BF"
        "\U000024C2-\U0001F251"
        "]+",
        flags=re.UNICODE,
    )
    return emoji_pattern.sub(r"", text)


# -----------------------------
# Remove links from text
# -----------------------------
def remove_links(text: str) -> str:
    """
    Remove URLs from a string.
    """
    url_pattern = re.compile(r"https?://\S+|www\.\S+")
    return url_pattern.sub(r"", text)


# -----------------------------
# Check for offensive words
# -----------------------------
def contains_word(text: str, word: str) -> bool:
    """
    Check if a word exists in text, ignoring case and punctuation.
    """
    normalized_text = normalize_text(text)
    normalized_word = normalize_text(word)
    # Word boundaries to match exact word
    pattern = r"\b" + re.escape(normalized_word) + r"\b"
    return bool(re.search(pattern, normalized_text))


# -----------------------------
# Shorten text for display
# -----------------------------
def shorten_text(text: str, max_length: int = 50) -> str:
    """
    Shorten text with ellipsis if longer than max_length.
    """
    if len(text) <= max_length:
        return text
    return text[: max_length - 3] + "..."
