"""Startup evaluation application packages and command-line modules."""


def main() -> int:
    """Preserve the existing ``import main; main.main()`` CLI entry point."""
    from main.scripts.run_graph import main as run_graph
    return run_graph()
