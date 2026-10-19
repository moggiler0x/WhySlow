import sys


def selfcheck() -> int:
    """Проверка после обновления: все модули импортируются. Печатает версию."""
    import importlib
    from . import __version__
    names = ["util", "knowledge", "checks", "startup", "processes", "junk", "network", "sysinfo",
             "lhm", "journal", "settings", "updater", "cli"]
    try:
        import PySide6.QtWidgets  # noqa: F401 — без Qt (сервер, только --cli) окно не проверяем
        names.append("gui")
    except ImportError:
        pass
    for name in names:
        importlib.import_module(f"{__package__}.{name}")
    print(__version__)
    return 0


def main() -> int:
    if "--version" in sys.argv:
        from . import __version__
        print(__version__)
        return 0
    if "--selfcheck" in sys.argv:
        return selfcheck()
    if "--update" in sys.argv:
        from .cli import update
        return update()
    if "--cli" in sys.argv:
        from .cli import main as cli_main
        return cli_main()
    from .gui import main as gui_main
    return gui_main()


if __name__ == "__main__":
    sys.exit(main())
