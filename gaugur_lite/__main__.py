"""支持 `python -m gaugur_lite`。"""

from .cli import app


if __name__ == "__main__":
    app(prog_name="python -m gaugur_lite")

