def main() -> None:
    """Console-script entry point. Imported lazily so that small helper processes
    (like the run-copy child) don't pay for loading the whole UI."""
    from .app import main as run

    run()


__all__ = ["main"]
