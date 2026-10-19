import sys


def selfcheck() -> int:
    """Проверка после обновления: все модули импортируются. Печатает версию."""
    import importlib
    from . import __version__
    names = ["i18n", "util", "knowledge", "online", "checks", "startup", "processes", "junk", "network", "sysinfo",
             "lhm", "journal", "settings", "net", "online", "updater", "cli"]
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
    if "--lang" in sys.argv:  # --lang en / --lang ru: язык только на этот запуск
        from . import i18n
        i = sys.argv.index("--lang")
        code = sys.argv[i + 1] if i + 1 < len(sys.argv) else ""
        if code in i18n.LANGS:
            i18n.set_lang(code, save=False)
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
