import os
import sys


def selfcheck() -> int:
    """Проверка после обновления: все модули импортируются. Печатает версию."""
    import importlib
    from . import __version__
    names = ["i18n", "util", "knowledge", "online", "checks", "startup", "processes", "junk", "network", "sysinfo",
             "lhm", "macapps", "macstorage", "journal", "passwords", "optimize", "reinstall", "settings", "net", "online", "updater", "cli",
             "conflicts", "diskhealth", "power", "bigfiles", "watch", "ed25519", "license"]
    try:
        import PySide6.QtWidgets  # noqa: F401 — без Qt (сервер, только --cli) окно не проверяем
        names += ["gui", "license_ui"]
    except ImportError:
        pass
    for name in names:
        importlib.import_module(f"{__package__}.{name}")
    print(__version__)
    out = os.environ.get("WHYSLOW_SELFCHECK_OUT")  # у оконного WhySlow.exe нет stdout, версию читают из файла
    if out:
        with open(out, "w", encoding="utf-8") as f:
            f.write(__version__)
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
    if "--optimize" in sys.argv:
        from .cli import main as cli_main
        return cli_main(fix=True)
    if "--storage" in sys.argv:
        from .cli import storage
        return storage()
    if "--passwords" in sys.argv:  # --passwords выгрузка.csv: проверка паролей по базе утечек
        from .cli import check_passwords
        i = sys.argv.index("--passwords")
        if i + 1 >= len(sys.argv):
            print("--passwords FILE.csv")
            return 2
        return check_passwords(sys.argv[i + 1])
    if "--license" in sys.argv:  # --license [КЛЮЧ]: состояние подписки или ввод ключа без окна
        from .cli import license_cmd
        i = sys.argv.index("--license")
        return license_cmd(sys.argv[i + 1] if i + 1 < len(sys.argv) else "")
    if "--cli" in sys.argv:
        from .cli import main as cli_main
        return cli_main()
    from .gui import main as gui_main
    return gui_main()


if __name__ == "__main__":
    sys.exit(main())
