import asyncio
import logging
import os
from typing import List, Optional

# Сторонние библиотеки (нужно установить: pip install aiogram aiofiles chardet python-dotenv)
import aiofiles
import chardet
from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import BufferedInputFile
from dotenv import load_dotenv

# --- 1. CONFIGURATION LAYER ---

# Загружаем переменные окружения.
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
FILE_PATH = os.getenv("FILE_PATH", "data.txt")

# Константы Telegram
MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB в байтах максимальный размер файла
MAX_TEXT_LENGTH = 20000  # Если файл больше этого значения, то отправляем как документ
MAX_MESSAGE_LENGTH = 4096  # Максимальный объём одного сообщения
API_DELAY = 0.5  # Задержка между сообщениями во избежание флуда

# Настройка логирования вместо print()
logging.basicConfig(level=logging.INFO, format="%(asctime)s - [%(levelname)s] - %(message)s")
logger = logging.getLogger(__name__)


# --- 2. LOGIC LAYER ---


class TextSplitter:
    """
    Класс, отвечающий за корректное разделение текста на части.
    Использует подход 'Smart Split', стараясь не разрывать строки.
    """

    @staticmethod
    def split(text: str, chunk_size: int = MAX_MESSAGE_LENGTH) -> List[str]:
        if len(text) <= chunk_size:
            return [text]

        chunks = []
        while text:
            if len(text) <= chunk_size:
                chunks.append(text)
                break

            # Берем кусок максимальной длины
            chunk = text[:chunk_size]

            # Пытаемся найти последний перенос строки, чтобы не резать на полуслове
            last_newline = chunk.rfind("\n")

            if last_newline != -1:
                # Обрезаем по переносу строки
                limit = last_newline + 1
            else:
                # Если одна сплошная строка без переносов - ищем пробел
                last_space = chunk.rfind(" ")
                if last_space != -1:
                    limit = last_space + 1
                else:
                    # Если вообще нет разделителей (например, длинный хэш), режем жестко
                    limit = chunk_size

            chunks.append(text[:limit])
            text = text[limit:]

        return chunks


class TelegramSender:
    """
    Класс-фасад для взаимодействия с Telegram API.
    Инкапсулирует логику ретраев и выбора метода отправки.
    """

    def __init__(self, token: str):
        self.bot = Bot(token=token)

    async def close(self):
        await self.bot.session.close()

    async def send_text_safe(self, chat_id: int | str, text: str):
        """
        Умная отправка текста. Если длинный — бьет на части.
        """
        chunks = TextSplitter.split(text)
        total_chunks = len(chunks)

        logger.info(f"Текст разбит на {total_chunks} сообщений.")

        for i, chunk in enumerate(chunks, 1):
            try:
                await self.bot.send_message(chat_id=chat_id, text=chunk, parse_mode=None)

                # Rate Limiting: небольшая пауза между отправками частей
                if i < total_chunks:
                    await asyncio.sleep(API_DELAY)

            except TelegramRetryAfter as e:
                logger.warning(f"Flood control: Ждем {e.retry_after} секунд.")
                await asyncio.sleep(e.retry_after)
                # Повторная попытка рекурсивно (можно улучшить через цикл while)
                await self.bot.send_message(chat_id=chat_id, text=chunk)
            except Exception as e:
                logger.error(f"Ошибка при отправке части {i}: {e}")

    async def send_document(self, chat_id: int | str, content: bytes, filename: str, caption: str | None = None):
        """
        Отправка содержимого как файла.
        """
        input_file = BufferedInputFile(content, filename=filename)
        try:
            await self.bot.send_document(chat_id=chat_id, document=input_file, caption=caption)
            logger.info("Документ успешно отправлен.")
        except Exception as e:
            logger.error(f"Ошибка отправки документа: {e}")


# --- 3. APPLICATION LAYER ---


async def read_file_content(path: str) -> Optional[str]:
    """
    Асинхронное чтение файла с обработкой отсутствия и ошибок кодировки.
    """
    if not os.path.exists(path):
        logger.error(f"Файл {path} не найден.")
        return None

    # --- ПРОВЕРКА РАЗМЕРА (Fast Fail) ---
    file_size = os.path.getsize(path)

    if file_size > MAX_FILE_SIZE_BYTES:
        logger.error(f"Файл слишком большой ({file_size / 1024 / 1024:.2f} MB). Лимит Telegram: 50 MB.")
        return None

    if file_size == 0:
        logger.warning("Файл пуст.")
        return None

    try:
        # Шаг 1: Читаем файл в бинарном режиме, чтобы chardet мог его проанализировать
        async with aiofiles.open(path, mode="rb") as f:
            raw_data = await f.read()

            # Шаг 2: Определяем кодировку с помощью chardet
            detection = chardet.detect(raw_data)
            detected_encoding = detection["encoding"]
            if not detected_encoding:
                logger.warning("Ошибка определения кодировки. Попробуйте сохранить файл в UTF-8.")
                return None
            logger.info(f"Обнаруженная кодировка: {detected_encoding}")

            # Шаг 3: Декодируем бинарные данные в строку, используя найденную кодировку
            content = raw_data.decode(detected_encoding)
            return content
    except UnicodeDecodeError:
        logger.error("Ошибка кодировки. Попробуйте сохранить файл в UTF-8.")
        return None
    except Exception as e:
        logger.error(f"Ошибка чтения файла: {e}")
        return None


async def main():
    # Валидация конфигурации
    if not BOT_TOKEN or not CHAT_ID:
        logger.critical("Не задан BOT_TOKEN или CHAT_ID в .env файле.")
        return

    sender = TelegramSender(BOT_TOKEN)

    try:
        logger.info(f"Начинаем обработку файла: {FILE_PATH}")
        content = await read_file_content(FILE_PATH)

        if content:
            # СТРАТЕГИЯ ОТПРАВКИ:
            # 1. Если текст очень большой (> 20 000 символов), лучше слать файлом, чтобы не спамить.
            # 2. Иначе шлем текстом с разбивкой.

            char_count = len(content)
            logger.info(f"Прочитано символов: {char_count}")

            if char_count > MAX_TEXT_LENGTH:
                logger.info("Текст слишком большой для сообщений. Отправляем как документ.")
                # Конвертируем строку обратно в байты для отправки
                await sender.send_document(
                    CHAT_ID,
                    content.encode("utf-8"),
                    filename=os.path.basename(FILE_PATH),
                    caption="Файл был слишком велик для отправки текстом.",
                )
            else:
                await sender.send_text_safe(CHAT_ID, content)

    except Exception as e:
        logger.exception(f"ОКритическая ошибка выполнения: {e}")
    finally:
        # Корректное закрытие сессии бота
        await sender.close()
        logger.info("Работа завершена.")


if __name__ == "__main__":
    # Запуск Event Loop
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Скрипт остановлен пользователем.")
