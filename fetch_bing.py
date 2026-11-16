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
PREFERRED_MARKETS = ("en-US", "en-GB")  # порядок = приоритет
API_URL = "https://www.bing.com/HPImageArchive.aspx"
DATA_FILE = Path("data.json")
INDEX_FILE = Path("index.html")
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
# Суффикс рынка Bing всегда вида _XX-XX1234567890 или _ROW1234567890.
# Требуем подчёркивание и цифры — регекс не «съедает» окончания имён и идемпотентен.
MARKET_SUFFIX = re.compile(r"_[A-Z]{2,3}(?:-[A-Z]{2,3})?\d+$")
PRELOAD_RE = re.compile(r"(<!--LATEST:PRELOAD:START-->).*?(<!--LATEST:PRELOAD:END-->)", re.DOTALL)
IMG_RE = re.compile(r"(<!--LATEST:IMG:START-->).*?(<!--LATEST:IMG:END-->)", re.DOTALL)


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
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
            if attempt < max_retries:
                time.sleep(2 ** attempt)
                continue
            raise
    raise RuntimeError("Не удалось получить JSON после всех попыток")


def normalize_url(url: str) -> str:
    return (url or "").strip().replace("/th?id=", "/th/id/")


def clean_raw_id(raw_id: str) -> str:
    return MARKET_SUFFIX.sub("", raw_id.strip())


def get_image_id(urlbase: str, start_date: str) -> str:
    raw_id = urlbase.split("?id=OHR.", 1)[1] if "?id=OHR." in urlbase else urlbase.rsplit("/", 1)[-1]
    raw_id = raw_id.split("&", 1)[0]
    return clean_raw_id(raw_id) or f"bing-{start_date}"


def market_of(urlbase: str) -> str:
    match = re.search(r"_([A-Z]{2}-[A-Z]{2}|ROW)\d+$", urlbase)
    return match.group(1) if match else "ROW"


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
        if not isinstance(entry, dict):
            continue
        stored_id = str(entry.get("img_id", old_id)).strip()
        # Сохранённые id уже очищены — доверяем им (идемпотентность).
        clean_id = stored_id or clean_raw_id(str(old_id))
        sort_key = str(entry.get("sort_key", "")).strip()
        if not sort_key:
            legacy_date = str(entry.get("date", "")).replace("-", "")
            sort_key = f"{legacy_date}_{clean_id}"
        if clean_id not in cleaned_db:
            # Сохраняем все поля записи (title, copyright, markets и будущие).
            cleaned_db[clean_id] = {
                **entry,
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


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f"{path.name}.", suffix=".tmp", delete=False,
        ) as tmp:
            tmp.write(text)
            temporary_path = Path(tmp.name)
        os.replace(temporary_path, path)
    except OSError as error:
        print(f"Ошибка записи файла {path}: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    finally:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink(missing_ok=True)


def write_database(data: dict) -> None:
    atomic_write(DATA_FILE, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def update_index_latest(database: dict) -> bool:
    """Вшивает свежий кадр дня в index.html: LCP-изображение становится
    обнаруживаемым в HTML и всегда актуальным. Возвращает True при изменении."""
    if not INDEX_FILE.exists() or not database:
        return False
    entry = next(iter(database.values()))
    preview = entry.get("preview", "")
    if not preview:
        return False
    hero_1366 = re.sub(r"_\d+x\d+\.jpg$", "_1366x768.jpg", preview)
    title = str(entry.get("title") or entry.get("img_id", "Bing")).strip()
    sort_key = str(entry.get("sort_key", ""))
    date_label = sort_key[:8]
    alt = f"{title} — обои Bing ({date_label})"
    preload = (
        f'\n<link rel="preload" as="image" fetchpriority="high" href="{hero_1366}" '
        f'imagesrcset="{hero_1366} 1366w, {preview} 1920w" imagesizes="100vw">\n'
    )
    img = (
        f'\n  <img class="hero-img" id="heroImg" alt="{alt}" width="1366" height="768" '
        f'fetchpriority="high" loading="eager" decoding="async" referrerpolicy="no-referrer" '
        f'sizes="100vw" srcset="{hero_1366} 1366w, {preview} 1920w" src="{hero_1366}">\n  '
    )
    html = INDEX_FILE.read_text(encoding="utf-8")
    new_html = PRELOAD_RE.sub(lambda m: m.group(1) + preload + m.group(2), html, count=1)
    new_html = IMG_RE.sub(lambda m: m.group(1) + img + m.group(2), new_html, count=1)
    if new_html == html:
        return False
    atomic_write(INDEX_FILE, new_html)
    return True


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
    # Обрабатываем рынки в порядке приоритета: en-US, en-GB, затем остальные.
    ordered = list(PREFERRED_MARKETS) + [m for m in MARKETS if m not in PREFERRED_MARKETS]
    for market in ordered:
        for image in market_results.get(market, []):
            urlbase = image.get("urlbase", "")
            start_date = image.get("startdate", "")
            if not urlbase or len(start_date) != 8 or not start_date.isdigit():
                continue
            clean_id = get_image_id(urlbase, start_date)
            if not clean_id:
                continue
            base_path = normalize_url(f"https://www.bing.com{urlbase}")
            code = market_of(urlbase)
            title = str(image.get("title", "")).strip()
            copyright_text = str(image.get("copyright", "")).strip()
            if clean_id not in database:
                database[clean_id] = {
                    "sort_key": f"{start_date}_{clean_id}",
                    "img_id": clean_id,
                    "url": f"{base_path}_UHD.jpg",
                    "preview": f"{base_path}_1920x1080.jpg",
                    "title": title,
                    "copyright": copyright_text,
                    "market": code,
                    "markets": [code],
                }
            else:
                entry = database[clean_id]
                markets = entry.get("markets") or [entry.get("market", code)]
                if code not in markets:
                    markets.append(code)
                entry["markets"] = markets
                # Самая ранняя дата появления среди рынков.
                old_date = str(entry.get("sort_key", ""))[:8]
                if start_date and (not old_date or start_date < old_date):
                    entry["sort_key"] = f"{start_date}_{clean_id}"
                if market in PREFERRED_MARKETS:
                    entry["url"] = f"{base_path}_UHD.jpg"
                    entry["preview"] = f"{base_path}_1920x1080.jpg"
                    entry["market"] = code
                    if title:
                        entry["title"] = title
                    if copyright_text:
                        entry["copyright"] = copyright_text
    sorted_database = dict(sorted(
        database.items(),
        key=lambda item: str(item[1].get("sort_key", "")),
        reverse=True,
    ))
    write_database(sorted_database)
    index_changed = update_index_latest(sorted_database)
    print(f"Архив обновлён. Успешных рынков: {len(market_results)}/{len(MARKETS)}. "
          f"Всего записей: {len(sorted_database)}. index.html обновлён: {index_changed}.")


if __name__ == "__main__":
    fetch_wallpapers()
