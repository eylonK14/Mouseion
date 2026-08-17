"""Rewrite the prompt golden file.

Run from `backend/` after an intentional prompt change:

    python -m tests.regenerate_golden

Then read the diff before committing — that diff is the point of the test.
"""

from __future__ import annotations

from tests.test_prompts import GOLDEN, build_golden_prompt


def main() -> None:
    GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN.write_text(build_golden_prompt(), encoding="utf-8")
    print(f"wrote {GOLDEN}")


if __name__ == "__main__":
    main()
