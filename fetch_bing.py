import json
import os
import sys
import time
import re
import gzip
from pathlib import Path
from tempfile import NamedTemporaryFile
import urllib.request
import urllib.parse
import urllib.error

MARKETS = [
    "en-US", "en-AU", "en-CA", "zh-CN", "de-DE", "es-ES", "fr-FR",
    "it-IT", "ja-JP", "en-NZ", "en-GB", "nl-NL", "pl-PL", "pt-BR",
    "pt-PT", "ko-KR", "ru-RU",
]

DATA_FILE = Path("data.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/127.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Encoding": "gzip",
}

REQUEST_TIMEOUT = 30

_MARKET_SUFFIX = re.compile(r"_(?:[A-Z]{2,3}(?:-[A-Z]{2,3})?)\d*$", re.IGNORECASE)


def fetch_json_with_retry(api_url, params, headers, max_retries=3):
    query_string = urllib.parse.urlencode(params)
    url = f"{api_url}?{query_string}"
    req = urllib.request.Request(url, headers=headers)
    retry_status_codes = {429, 500, 502, 503, 504}

    for attempt in range(max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as response:
                body = response.read()
                encoding = (response.info().get("Content-Encoding") or "").lower()
                if "gzip" in encoding:
                    body = gzip.decompress(body)
                return json.loads(body.decode("utf-8"))
        except urllib.error.HTTPError as error:
            error.close()
            if error.code in retry_status_codes and attempt < max_retries:
                time.sleep(1 * (2 ** attempt))
                continue
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            if attempt < max_retries:
                time.sleep(1 * (2 ** attempt))
                continue
            raise


def load_database():
    if not DATA_FILE.exists():
        return {}
    try:
        with DATA_FILE.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except Exception as error:
        print(f"Ошибка чтения файла {DATA_FILE}: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    if not isinstance(data, dict):
        print(f"Ошибка: файл {DATA_FILE} должен содержать JSON-объект.", file=sys.stderr)
        raise SystemExit(1)

    cleaned_db = {}
    for old_id, entry in data.items():
        fresh_id = _MARKET_SUFFIX.sub("", entry.get("img_id", old_id))
        if entry.get("sort_key"):
            entry["sort_key"] = _MARKET_SUFFIX.sub("", entry["sort_key"])
        if fresh_id not in cleaned_db:
            entry["img_id"] = fresh_id
            cleaned_db[fresh_id] = entry
        else:
            for m in entry.get("markets", []):
                if m not in cleaned_db[fresh_id].setdefault("markets", []):
                    cleaned_db[fresh_id]["markets"].append(m)
            if not cleaned_db[fresh_id].get("description") and entry.get("description"):
                cleaned_db[fresh_id]["description"] = entry["description"]
                cleaned_db[fresh_id]["copyright"] = entry.get("copyright")
    return cleaned_db


def get_image_id(urlbase, start_date):
    if "?id=OHR." in urlbase:
        raw_id = urlbase.split("?id=OHR.", 1)[1]
    else:
        raw_id = urlbase.rsplit("/", 1)[-1]
    raw_id = raw_id.split("&", 1)[0]
    raw_id = _MARKET_SUFFIX.sub("", raw_id)
    return raw_id or f"bing-{start_date}"


def build_entry(image, clean_id):
    urlbase = image.get("urlbase") or ""
    start_date = image.get("startdate") or ""
    copyright_text = (image.get("copyright") or "").strip()
    title = image.get("title") or clean_id
    if title == "Info":
        title = clean_id
    return {
        "sort_key": f"{start_date}_{clean_id}",
        "date": f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}",
        "url": f"https://www.bing.com{urlbase}_UHD.jpg",
        "preview": f"https://www.bing.com{urlbase}_1920x1080.jpg",
        "img_id": clean_id,
        "title": title,
        "description": copyright_text,
        "copyright": copyright_text,
        "markets": [],
    }


# Предпочтительные рынки для метаданных: их текст перезаписывает CJK-версии.
PREFERRED_MARKETS = ("en-US", "en-GB")
_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")


def _is_latin(text):
    return not _CJK.search(text or "")


def update_entry(entry, image, market):
    copyright_text = (image.get("copyright") or "").strip()
    markets = entry.setdefault("markets", [])
    if market not in markets:
        markets.append(market)
    if not entry.get("description") and copyright_text:
        entry["description"] = copyright_text
    if not entry.get("copyright") and copyright_text:
        entry["copyright"] = copyright_text
    if not entry.get("title") or entry["title"] == "Info":
        new_title = image.get("title") or entry["img_id"]
        if new_title != "Info":
            entry["title"] = new_title
    # en-US/en-GB перепривязывают запись, если сохранён CJK/Info-текст
    if (market in PREFERRED_MARKETS and copyright_text
            and (not _is_latin(entry.get("description"))
                 or not _is_latin(entry.get("title")))):
        entry["description"] = copyright_text
        entry["copyright"] = copyright_text
        entry["title"] = image.get("title") or entry["title"]
    # Preferred-рынки всегда перезаписывают url/preview для консистентности
    if market in PREFERRED_MARKETS:
        urlbase = image.get("urlbase") or ""
        if urlbase:
            entry["url"] = f"https://www.bing.com{urlbase}_UHD.jpg"
            entry["preview"] = f"https://www.bing.com{urlbase}_1920x1080.jpg"


def fetch_wallpapers():
    database = load_database()
    successful_markets = 0
    received_images = 0

    for market in MARKETS:
        api_url = "https://www.bing.com/HPImageArchive.aspx"

        # Bing отдаёт максимум 8 картинок за запрос. Страницы idx > 0
        # НЕ документированы и работают со скользящим окном: idx=1 почти
        # повторяет idx=0 (пересечение 7 из 8). Окно idx=0..2 (~10 дней)
        # покрывает расхождение дат между рынками + пару пропущенных
        # запусков. Больше 3 страниц не гоняем — нечего рисковать.
        pages = (0, 1, 2)
        seen_on_market = set()

        try:
            images = []
            for page_idx in pages:
                params = {
                    "format": "js",
                    "idx": page_idx,
                    "n": 8,          # <-- ВОТ ОТВЕТ НА ФЛАГ №1: не откатывалась
                    "mkt": market,
                }
                payload = fetch_json_with_retry(api_url, params, HEADERS)
                page = payload.get("images", [])
                if not isinstance(page, list):
                    raise ValueError("Поле images имеет неправильный формат")
                images.extend(page)   # пустая страница = +0, не падает

            successful_markets += 1
            received_images += len(images)

            for image in images:
                if not isinstance(image, dict):
                    continue
                # защита от пересечения скользящих окон ВНУТРИ рынка
                dedup_key = (image.get("urlbase"), image.get("startdate"))
                if dedup_key in seen_on_market:
                    continue
                seen_on_market.add(dedup_key)

                urlbase = image.get("urlbase", "")
                start_date = image.get("startdate", "")
                if not urlbase or len(start_date) != 8 or not start_date.isdigit():
                    continue
                clean_id = get_image_id(urlbase, start_date)
                if not clean_id:
                    continue
                if clean_id not in database:
                    database[clean_id] = build_entry(image, clean_id)
                update_entry(database[clean_id], image, market)

            print(f"{market}: получено изображений — {len(images)} "
                  f"(уникальных {len(seen_on_market)})")

        except (urllib.error.URLError, TimeoutError, OSError) as error:
            print(f"Ошибка запроса для {market}: {error}", file=sys.stderr)
        except (ValueError, TypeError, KeyError) as error:
            print(f"Ошибка обработки данных для {market}: {error}", file=sys.stderr)

    if successful_markets == 0:
        print("Ошибка: не удалось получить данные ни для одного рынка.", file=sys.stderr)
        raise SystemExit(1)

    sorted_database = dict(sorted(
        database.items(),
        key=lambda item: str(item[1].get("sort_key", "")),
        reverse=True,
    ))
    write_database(sorted_database)
    print(f"Архив обновлён. Успешных рынков: {successful_markets}/{len(MARKETS)}. "
          f"Получено изображений: {received_images}. "
          f"Всего уникальных записей: {len(sorted_database)}.")


def write_database(data):
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=DATA_FILE.parent,
            prefix=f"{DATA_FILE.name}.", suffix=".tmp", delete=False,
        ) as temporary_file:
            json.dump(data, temporary_file, ensure_ascii=False, indent=4)
            temporary_file.write("\n")
            temporary_path = Path(temporary_file.name)
        os.replace(temporary_path, DATA_FILE)
    except OSError as error:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink(missing_ok=True)
        print(f"Ошибка записи файла {DATA_FILE}: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    fetch_wallpapers()
