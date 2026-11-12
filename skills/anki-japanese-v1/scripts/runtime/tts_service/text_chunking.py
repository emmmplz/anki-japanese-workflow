"""Chunk by input characters, without treating punctuation as a boundary."""


def split_by_length(text: str, max_chars: int = 150) -> list[str]:
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or not 1 <= max_chars <= 510:
        raise ValueError('max_segment_chars must be an integer between 1 and 510')
    # Upstream treats newlines as sentence boundaries, so make them ordinary
    # spaces before counting. All other characters, including punctuation, stay.
    text = text.replace('\r\n', ' ').replace('\r', ' ').replace('\n', ' ')
    return [text[start:start + max_chars] for start in range(0, len(text), max_chars)]
