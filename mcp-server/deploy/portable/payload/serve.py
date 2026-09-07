"""Лаунчер портабельної Windows-поставки MCP-сервера mcbp-ai.

Читає `server.env` поруч із собою, кладе значення в оточення процесу і піднімає
Streamable HTTP транспорт пакета `mcbp`.

Навіщо цей файл замість прямого виклику `mcbp-http`:
  * оточення задається з Python (`os.environ`), а не через `set` у .cmd — у PowerShell
    порожнє значення видаляє змінну, а `load_settings()` вимагає ПРИСУТНОСТІ
    `ONEC_PASSWORD` (порожній рядок при цьому легальний);
  * `mcbp_mcp_server.http.main()` кличе `uvicorn.run()` без параметрів TLS, тому https
    неможливо ввімкнути змінними оточення — коли в `server.env` задані
    `MCP_HTTP_SSL_CERTFILE` і `MCP_HTTP_SSL_KEYFILE`, uvicorn запускається звідси.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV_FILE = HERE / "server.env"

log = logging.getLogger("serve")


def read_env_file(path: Path) -> dict[str, str]:
    """`KEY=VALUE`, UTF-8. Порожній рядок і рядок, що починається з `#`, ігноруються.
    Значення може бути порожнім. Пробіли навколо ключа і значення обрізаються; щоб
    зберегти їх у значенні — візьміть його в лапки."""
    if not path.exists():
        raise FileNotFoundError(path)
    values: dict[str, str] = {}
    text = path.read_text(encoding="utf-8-sig")
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            log.warning("%s:%d: рядок без '=' пропущено: %r", path.name, lineno, raw)
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not key:
            log.warning("%s:%d: порожній ключ, рядок пропущено", path.name, lineno)
            continue
        values[key] = value
    return values


def _resolve(value: str) -> str:
    """Відносний шлях у server.env рахується від теки поставки, а не від поточного каталогу."""
    p = Path(value)
    return str(p if p.is_absolute() else (HERE / p))


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        os.environ.update(read_env_file(ENV_FILE))
    except FileNotFoundError:
        log.error("не знайдено файл конфігурації %s — покладіть його поруч із serve.py", ENV_FILE)
        return 1
    except OSError as e:
        log.error("не вдалося прочитати %s: %s", ENV_FILE, e)
        return 1

    try:
        from mcbp_mcp_server.http import create_app, load_http_settings
        from mcbp_mcp_server.http import main as http_main
        from mcbp_mcp_server.server import SettingsError, load_settings
    except ImportError as e:
        log.error(
            "пакет mcbp[http] не встановлений у вбудованому Python (%s). "
            "Запустіть update.cmd, щоб доставити його з TestPyPI.", e,
        )
        return 1

    try:
        onec = load_settings()
        http_settings = load_http_settings()
    except SettingsError as e:
        log.error("помилка в server.env: %s", e)
        log.error(
            "перевірте ONEC_BASE_URL, ONEC_USER і ONEC_PASSWORD — ключ ONEC_PASSWORD має бути "
            "присутній навіть із порожнім значенням.",
        )
        return 1

    certfile = os.environ.get("MCP_HTTP_SSL_CERTFILE", "").strip()
    keyfile = os.environ.get("MCP_HTTP_SSL_KEYFILE", "").strip()

    if not certfile and not keyfile:
        log.info("TLS не налаштований — запуск по http")
        http_main()
        return 0

    if not (certfile and keyfile):
        log.error(
            "для https потрібні ОБИДВА ключі: MCP_HTTP_SSL_CERTFILE і MCP_HTTP_SSL_KEYFILE",
        )
        return 1

    certfile, keyfile = _resolve(certfile), _resolve(keyfile)
    for label, path in (("сертифікат", certfile), ("приватний ключ", keyfile)):
        if not Path(path).is_file():
            log.error("%s не знайдено: %s — згенеруйте його через make-cert.ps1", label, path)
            return 1

    import uvicorn

    if not http_settings.allowed_hosts:
        log.warning(
            "MCP_HTTP_ALLOWED_HOSTS не заданий — захист від DNS-rebinding вимкнено. "
            "Задайте його публічним іменем перед виставленням назовні.",
        )
    log.info(
        "запуск mcbp-ai MCP over HTTPS на %s:%s%s (BAS %s, write=%s)",
        http_settings.host, http_settings.port, http_settings.path,
        onec.base_url, onec.allow_write,
    )
    uvicorn.run(
        create_app(http_settings),
        host=http_settings.host,
        port=http_settings.port,
        ssl_certfile=certfile,
        ssl_keyfile=keyfile,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
