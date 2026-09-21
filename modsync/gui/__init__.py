"""Qt Widgets desktop frontend for ModSync."""


def main() -> int:
    from .app import main as run

    return run()


__all__ = ["main"]
