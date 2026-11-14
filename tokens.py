"""Token counts. TOKENIZER_DIR selects the Llama 3.1 tokenizer.

Without that directory, or without the tokenizers package, counts fall back
to whitespace. The local harness uses the fallback. The door image does not.
"""

import os
import sys
import threading

_tokenizer = None
_loaded = False
_lock = threading.Lock()


def count_text_tokens(text):
    if not isinstance(text, str):
        return 0
    tokenizer = _llama()
    if tokenizer is None:
        return len(text.split()) if text else 0
    with _lock:
        return len(tokenizer.encode(text, add_special_tokens=False).ids)


def llama_active():
    return _llama() is not None


def _llama():
    global _tokenizer, _loaded
    if _loaded:
        return _tokenizer
    _loaded = True
    directory = os.environ.get("TOKENIZER_DIR", "").strip()
    if not directory:
        return None
    path = os.path.join(directory, "tokenizer.json")
    if not os.path.isfile(path):
        return None
    try:
        from tokenizers import Tokenizer
    except ModuleNotFoundError:
        return None
    try:
        _tokenizer = Tokenizer.from_file(path)
    except (OSError, ValueError, RuntimeError) as exc:
        sys.stderr.write("tokenizer did not load: %s\n" % exc)
        _tokenizer = None
    return _tokenizer
