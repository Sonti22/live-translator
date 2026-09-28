"""
Stock short answers rendered in my voice ahead of time.

When a translated clause is exactly one of them ("Yes.", "Thank you.", "Could you repeat the question?")
the voice plays the cached audio at once instead of waiting a TTS round trip. The voice renders the
missing ones in the background while I am silent; the cache lives on disk, one file per render.
"""
import hashlib
import re
from pathlib import Path

# phrase -> renders kept: frequent answers get two that take turns, so they don't sound canned
PHRASES = {
    "Yes.": 2, "No.": 2, "Sure.": 2, "Okay.": 2, "Right.": 2, "Exactly.": 2, "Of course.": 2,
    "Thank you.": 2, "Good question.": 2, "Yes, of course.": 1, "Thanks.": 1, "Absolutely.": 1,
    "Definitely.": 1, "Great.": 1, "I agree.": 1, "I see.": 1, "Got it.": 1, "That's right.": 1,
    "Not really.": 1, "I understand.": 1, "No problem.": 1, "Sounds good.": 1, "Thank you very much.": 1,
    "You're welcome.": 1, "Nice to meet you.": 1, "Hello.": 1, "Hi.": 1, "Really?": 1,
    "Let me think.": 1, "Can you hear me?": 1, "Could you repeat the question?": 1,
    "Sorry, could you repeat that?": 1,
}


def normalize(text):
    """(words, is a question): "Yes!" matches "Yes.", but "Really?" does not match "Really."."""
    text = text.strip().lower().replace("’", "'")
    return " ".join(re.findall(r"[a-z0-9']+", text)), text.rstrip("\"')»” ").endswith("?")


class PhraseCache:
    """Rendered stock phrases of one voice. key = "provider|model|voice|language|speed": any change
    to how the voice sounds is a different set of files."""

    def __init__(self, cache_dir, key):
        self.dir, self.key = Path(cache_dir), key
        self.index = {normalize(text): text for text in PHRASES}
        self.turns = {}
        self.skipped = set()  # renders that failed this session: not retried in a loop
        self.loaded = {}

    def path(self, text, variant=0):
        name = f"{self.key}|{text}" + (f"|{variant}" if variant else "")
        return self.dir / f"{hashlib.sha1(name.encode()).hexdigest()}.pcm"

    def match(self, text):
        """Cached audio when `text` is a whole stock phrase (its renders take turns), else None."""
        phrase = self.index.get(normalize(text))
        if phrase is None:
            return None
        ready = [v for v in range(PHRASES[phrase]) if self.path(phrase, v).exists()]
        if not ready:
            return None
        turn = self.turns.get(phrase, 0)
        self.turns[phrase] = turn + 1
        return self._load(phrase, ready[turn % len(ready)])

    def _load(self, text, variant):
        if (text, variant) not in self.loaded:
            try:
                self.loaded[text, variant] = self.path(text, variant).read_bytes()
            except OSError:
                return None
        return self.loaded[text, variant]

    def next_missing(self):
        """(phrase, variant) to render next, or None when all are on disk."""
        for text, count in PHRASES.items():
            for variant in range(count):
                if (text, variant) not in self.skipped and not self.path(text, variant).exists():
                    return text, variant
        return None

    def store(self, text, variant, pcm):
        path = self.path(text, variant)
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            part = path.with_suffix(".part")
            part.write_bytes(pcm)
            part.replace(path)  # a half-written file never looks like a finished render
        except OSError:
            self.skipped.add((text, variant))

    def skip(self, text, variant):
        self.skipped.add((text, variant))
