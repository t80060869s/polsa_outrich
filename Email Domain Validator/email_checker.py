#!/usr/bin/env python3
"""
Email Domain Validator
----------------------
Скрипт проверяет наличие MX-записей для списка email-адресов.
Поддерживает CLI (файлы) и REST API.
Использует асинхронность, кэширование DNS-ответов и обработку IDNA.

Зависимости:
    pip install aiohttp dnspython
"""

import argparse
import asyncio
import json
import logging
import re
import sys
from dataclasses import dataclass
from typing import Dict, List

# Попытка импорта внешних зависимостей с понятной ошибкой
try:
    import dns.asyncresolver
    import dns.exception
    import dns.resolver
    from aiohttp import web
except ImportError as e:
    print(f"Ошибка: Не установлена необходимая библиотека. ({e.name})")
    print("Выполните: pip install aiohttp dnspython")
    sys.exit(1)

# --- CONFIGURATION ---
CONCURRENCY_LIMIT = 50  # Ограничение одновременных DNS-запросов
DNS_TIMEOUT = 3.0  # Таймаут ожидания ответа от DNS

# --- CONSTANTS (OUTPUT MESSAGES) ---
MSG_VALID = "домен валиден"
MSG_NO_DOMAIN = "домен отсутствует"
MSG_NO_MX = "MX-записи отсутствуют или некорректны"
MSG_INVALID_FORMAT = "некорректный формат email"
MSG_TIMEOUT = "таймаут (проблемы с сетью)"

# --- LOGGING ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


@dataclass
class CheckResult:
    email: str
    status: str


class EmailVerifier:
    def __init__(self):
        self._resolver = dns.asyncresolver.Resolver()
        self._resolver.timeout = DNS_TIMEOUT
        self._resolver.lifetime = DNS_TIMEOUT
        # Кэш результатов проверки доменов: {domain: status_message}
        self._domain_cache: Dict[str, str] = {}
        self._cache_lock = asyncio.Lock()
        # Regex для базовой проверки (RFC 5322 слишком сложен для простых задач, берем практичный)
        self._email_regex = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

    def _extract_domain(self, email: str) -> str:
        """Извлекает домен из email."""
        try:
            return email.split("@")[1].lower()
        except IndexError:
            return ""

    async def _check_dns_mx(self, domain: str) -> str:
        """
        Проверяет MX записи для домена.
        Возвращает одну из статусных строк.
        """
        # Обработка IDNA (кириллические домены и т.д.)
        try:
            encoded_domain = domain.encode("idna").decode("ascii")
        except UnicodeError:
            return MSG_NO_DOMAIN

        try:
            # Запрос MX записей
            answers = await self._resolver.resolve(encoded_domain, "MX")
            if answers:
                return MSG_VALID
            else:
                return MSG_NO_MX
        except dns.resolver.NXDOMAIN:
            return MSG_NO_DOMAIN
        except (dns.resolver.NoAnswer, dns.resolver.NoNameservers):
            return MSG_NO_MX
        except dns.exception.Timeout:
            # Таймаут можно трактовать по-разному, но в рамках задачи это скорее проблема связи/сервера
            logger.warning(f"Timeout for domain: {domain}")
            return MSG_TIMEOUT
        except Exception as e:
            logger.error(f"Unexpected error for {domain}: {e}")
            return MSG_NO_MX

    async def check_email(self, email: str) -> CheckResult:
        """Основной метод проверки одного email."""
        email = email.strip()

        # 1. Проверка формата
        if not self._email_regex.match(email):
            return CheckResult(email, MSG_INVALID_FORMAT)

        domain = self._extract_domain(email)

        # 2. Проверка кэша (Deduplication)
        async with self._cache_lock:
            if domain in self._domain_cache:
                return CheckResult(email, self._domain_cache[domain])

        # 3. DNS запрос
        status = await self._check_dns_mx(domain)

        # 4. Сохранение в кэш
        async with self._cache_lock:
            self._domain_cache[domain] = status

        return CheckResult(email, status)

    async def check_list(self, emails: List[str]) -> List[CheckResult]:
        """Параллельная проверка списка с ограничением конкурентности."""
        semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)

        async def sem_task(email):
            async with semaphore:
                return await self.check_email(email)

        tasks = [sem_task(email) for email in emails if email]
        return await asyncio.gather(*tasks)


# --- API HANDLERS ---


async def handle_check(request):
    try:
        data = await request.json()
        if not isinstance(data, list):
            return web.json_response({"error": "Expected a JSON list of strings"}, status=400)

        verifier = request.app["verifier"]
        results = await verifier.check_list(data)

        response_data = {res.email: res.status for res in results}
        return web.json_response(response_data, dumps=lambda x: json.dumps(x, ensure_ascii=False))
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    except Exception as e:
        logger.exception("API Error")
        return web.json_response({"error": str(e)}, status=500)


async def start_api(port: int):
    app = web.Application()
    app["verifier"] = EmailVerifier()
    app.add_routes([web.post("/check", handle_check)])

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    print(f"Server started at http://0.0.0.0:{port}")
    print(f"Test via curl: curl -X POST -d '[\"test@gmail.com\"]' http://localhost:{port}/check")
    await site.start()

    # Бесконечный цикл для API режима
    await asyncio.Event().wait()


# --- CLI HANDLERS ---


async def run_cli(input_file: str, output_file: str | None = None):
    verifier = EmailVerifier()

    try:
        with open(input_file, "r", encoding="utf-8") as f:
            raw_emails = [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        print(f"Файл {input_file} не найден.")
        sys.exit(1)

    print(f"Загружено {len(raw_emails)} адресов. Начинаем проверку...")
    results = await verifier.check_list(raw_emails)

    # Вывод результатов
    output_lines = []
    for res in results:
        line = f"{res.email}: {res.status}"
        print(line)
        output_lines.append(line)

    if output_file:
        with open(output_file, "w", encoding="utf-8") as f:
            f.write("\n".join(output_lines) + "\n")
        print(f"Результаты сохранены в {output_file}")


# --- MAIN ---


def main():
    parser = argparse.ArgumentParser(description="Email MX Validator")
    subparsers = parser.add_subparsers(dest="mode", required=True, help="Режим работы")

    # CLI Mode
    parser_cli = subparsers.add_parser("cli", help="Запуск проверки из файла")
    parser_cli.add_argument("input_file", help="Путь к файлу с email-адресами (один на строку)")
    parser_cli.add_argument("--out", "-o", help="Путь к файлу для сохранения результата")

    # API Mode
    parser_api = subparsers.add_parser("api", help="Запуск REST API сервера")
    parser_api.add_argument("--port", "-p", type=int, default=8080, help="Порт сервера (default: 8080)")

    args = parser.parse_args()

    try:
        if args.mode == "cli":
            asyncio.run(run_cli(args.input_file, args.out))
        elif args.mode == "api":
            asyncio.run(start_api(args.port))
    except KeyboardInterrupt:
        print("\nОстановка...")


if __name__ == "__main__":
    main()
