import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from tempfile import NamedTemporaryFile

MARKETS = (
    "en-US", "en-GB", "en-CA", "en-AU", "en-NZ",
    "de-DE", "fr-FR", "es-ES", "it-IT", "nl-NL",
    "pl-PL", "pt-BR", "pt-PT", "ru-RU", "ja-JP",
    "zh-CN", "ko-KR",
)

PREFERRED_MARKETS = {"en-US", "en-GB"}
API_URL = "https://www.bing.com/HPImageArchive.aspx"
DATA_FILE = Path("data.json")
REQUEST_TIMEOUT = 20

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Encoding": "gzip",
}

_MARKET_SUFFIX = re.compile(r"_(?:[A-Z]{2,3}(?:-[A-Z]{2,3})?)\d*$", re.IGNORECASE)


def fetch_json_with_retry(params: dict, max_retries: int = 3) -> dict:
    url = f"{API_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers=HEADERS)
    retry_codes = {429, 500, 502, 503, 504}

    for attempt in range(max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as response:
                body = response.read()
                if "gzip" in (response.info().get("Content-Encoding") or "").lower():
                    body = gzip.decompress(body)
                return json.loads(body.decode("utf-8"))
        except urllib.error.HTTPError as error:
            error.close()
            if error.code in retry_codes and attempt < max_retries:
                time.sleep(2 ** attempt)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt < max_retries:
                time.sleep(2 ** attempt)
                continue
            raise
    return {}


def normalize_url(url: str) -> str:
    return (url or "").replace("/th?id=", "/th/id/")


def get_image_id(urlbase: str, start_date: str) -> str:
    raw_id = urlbase.split("?id=OHR.", 1)[1] if "?id=OHR." in urlbase else urlbase.rsplit("/", 1)[-1]
    raw_id = raw_id.split("&", 1)[0]
    return _MARKET_SUFFIX.sub("", raw_id) or f"bing-{start_date}"


def load_database() -> dict[str, dict]:
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
        clean_id = _MARKET_SUFFIX.sub("", entry.get("img_id", old_id))
        sort_key = _MARKET_SUFFIX.sub("", entry.get("sort_key", f"{entry.get('date', '').replace('-', '')}_{clean_id}"))
        if clean_id not in cleaned_db:
            cleaned_db[clean_id] = {
                "sort_key": sort_key,
                "img_id": clean_id,
                "url": normalize_url(entry.get("url", "")),
                "preview": normalize_url(entry.get("preview", "")),
            }
    return cleaned_db


def fetch_market(market: str) -> tuple[str, list[dict]]:
    images = []
    seen = set()
    for page_idx in (0, 7):
        params = {"format": "js", "idx": page_idx, "n": 8, "mkt": market}
        payload = fetch_json_with_retry(params)
        page = payload.get("images", [])
        if not isinstance(page, list):
            raise ValueError("Поле images имеет неправильный формат")
        for img in page:
            if isinstance(img, dict):
                key = (img.get("urlbase"), img.get("startdate"))
                if key not in seen:
                    seen.add(key)
                    images.append(img)
    return market, images


def write_database(data: dict) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=DATA_FILE.parent,
            prefix=f"{DATA_FILE.name}.", suffix=".tmp", delete=False,
        ) as tmp:
            json.dump(data, tmp, ensure_ascii=False, indent=2)
            tmp.write("\n")
            temporary_path = Path(tmp.name)
        os.replace(temporary_path, DATA_FILE)
    except OSError as error:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink(missing_ok=True)
        print(f"Ошибка записи файла {DATA_FILE}: {error}", file=sys.stderr)
        raise SystemExit(1) from error


def fetch_wallpapers() -> None:
    database = load_database()
    market_results = {}

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(fetch_market, m): m for m in MARKETS}
        for future in as_completed(futures):
            market = futures[future]
            try:
                _, images = future.result()
                market_results[market] = images
                print(f"{market}: уникальных изображений — {len(images)}")
            except Exception as error:
                print(f"Ошибка для {market}: {error}", file=sys.stderr)

    if not market_results:
        print("Ошибка: не удалось получить данные ни для одного рынка.", file=sys.stderr)
        raise SystemExit(1)

    for market in MARKETS:
        for image in market_results.get(market, []):
            urlbase = image.get("urlbase", "")
            start_date = image.get("startdate", "")
            if not urlbase or len(start_date) != 8 or not start_date.isdigit():
                continue

            clean_id = get_image_id(urlbase, start_date)
            if not clean_id:
                continue

            base_path = normalize_url(f"https://www.bing.com{urlbase}")
            if clean_id not in database:
                database[clean_id] = {
                    "sort_key": f"{start_date}_{clean_id}",
                    "img_id": clean_id,
                    "url": f"{base_path}_UHD.jpg",
                    "preview": f"{base_path}_1920x1080.jpg",
                }
            elif market in PREFERRED_MARKETS:
                database[clean_id]["url"] = f"{base_path}_UHD.jpg"
                database[clean_id]["preview"] = f"{base_path}_1920x1080.jpg"

    sorted_database = dict(sorted(
        database.items(),
        key=lambda item: str(item[1].get("sort_key", "")),
        reverse=True,
    ))
    write_database(sorted_database)
    print(f"Архив обновлён. Успешных рынков: {len(market_results)}/{len(MARKETS)}. "
          f"Всего записей: {len(sorted_database)}.")


if __name__ == "__main__":
    fetch_wallpapers()
