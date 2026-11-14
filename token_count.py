"""Print one Llama token count and exit. The door image uses this as a one-shot."""

import os
import sys

os.environ.setdefault("TOKENIZER_DIR", "/app/tokenizer")

import tokens


def main():
    if not tokens.llama_active():
        raise SystemExit("Llama tokenizer did not load")
    text = sys.argv[1] if len(sys.argv) > 1 else ""
    print(tokens.count_text_tokens(text))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
