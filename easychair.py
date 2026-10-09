"""HTTP и разбор HTML EasyChair; преобразования pandas находятся в notebook."""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

import pandas as pd
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_URL = "https://easychair.org"
AREA_INDEX_URL = f"{BASE_URL}/cfp/area"
RAW_COLUMNS = [
    "cfp_url", "acronym", "name", "location_raw", "submission_deadline_raw",
    "start_date_raw", "topics", "area_id", "area", "area_url", "fetched_at_utc",
]
EXPECTED_HEADERS = [
    "acronym", "name", "location", "submissiondeadline", "startdate", "topics",
]


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": "EasyChair-LR6-data-preparation/0.1"})
    retry = Retry(
        total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"], respect_retry_after_header=True,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def fetch_html(
    session: requests.Session,
    url: str,
    cache_dir: Path,
    key: str,
    *,
    refresh: bool = False,
    delay: float = 1.0,
) -> tuple[str, dict]:
    """Кэш сохраняет исходную дату получения. Ошибка не заменяется пустыми данными."""
    if not re.fullmatch(r"[a-z0-9_]+", key):
        raise ValueError("Недопустимое имя файла кэша")
    cache_dir.mkdir(parents=True, exist_ok=True)
    html_path = cache_dir / f"{key}.html"
    meta_path = cache_dir / f"{key}.json"
    if not refresh and html_path.exists() and meta_path.exists():
        html = html_path.read_text(encoding="utf-8")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        checksum = hashlib.sha256(html.encode("utf-8")).hexdigest()
        if meta.get("url") != url or meta.get("sha256") != checksum:
            raise ValueError(f"Кэш {key} не соответствует URL/контрольной сумме; включите REFRESH")
        return html, {**meta, "from_cache": True}

    time.sleep(delay)
    response = session.get(url, timeout=(15, 45))
    response.raise_for_status()
    if urlsplit(response.url).path.startswith("/account/"):
        raise RuntimeError(f"Вместо публичной таблицы получена страница входа: {url}")
    # Страницы EasyChair содержат UTF-8; кодировка по заголовку бывает не задана.
    response.encoding = "utf-8"
    html = response.text
    meta = {
        "url": url,
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sha256": hashlib.sha256(html.encode("utf-8")).hexdigest(),
    }
    html_temp = html_path.with_suffix(".html.tmp")
    html_temp.write_text(html, encoding="utf-8")
    html_temp.replace(html_path)
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return html, {**meta, "from_cache": False}


def discover_areas(html: str) -> pd.DataFrame:
    rows = []
    soup = BeautifulSoup(html, "lxml")
    for anchor in soup.select("a[href]"):
        url = urljoin(BASE_URL, anchor["href"])
        parsed = urlsplit(url)
        values = parse_qs(parsed.query).get("area", [])
        if parsed.netloc != "easychair.org" or parsed.path != "/cfp/area" or not values or not values[0].isdigit():
            continue
        name = anchor.get_text(" ", strip=True)
        if name:
            rows.append({"area_id": int(values[0]), "area": name, "area_url": url})
    if not rows:
        raise ValueError("Ссылки Areas не найдены: проверьте доступность сайта и структуру HTML")
    return (
        pd.DataFrame(rows).drop_duplicates("area_id")
        .sort_values("area", ignore_index=True)
    )


def canonical_cfp_url(href: str) -> str:
    parsed = urlsplit(urljoin(BASE_URL, href))
    if parsed.netloc != "easychair.org" or not parsed.path.startswith("/cfp/"):
        raise ValueError(f"Неожиданная ссылка CFP: {href}")
    return urlunsplit(("https", "easychair.org", parsed.path.rstrip("/"), "", ""))


def parse_cfps(html: str, area: dict, fetched_at_utc: str) -> tuple[pd.DataFrame, int | None]:
    """Разбирает все строки таблицы и сверяет число с контролем на странице."""
    soup = BeautifulSoup(html, "lxml")
    matches = []
    for candidate in soup.select("table.ct_table"):
        head = candidate.find("thead", recursive=False)
        if head is None:
            continue
        headers = [re.sub(r"\s+", "", th.get_text()).lower() for th in head.find_all("th")]
        if headers == EXPECTED_HEADERS:
            matches.append(candidate)
    if len(matches) != 1:
        raise ValueError(f"Таблица CFP не найдена однозначно для {area['area']}")
    table = matches[0]
    count_span = table.select_one('span[id$=":rows"]')
    if count_span is None and table.get("data-navigation") != "false":
        raise ValueError(f"Нет контрольного числа строк для {area['area']}")
    expected_count = None
    if count_span is not None:
        count_text = re.sub(r"[\s,]", "", count_span.get_text())
        if not count_text.isdigit():
            raise ValueError(f"Не удалось прочитать контрольное число строк: {count_text}")
        expected_count = int(count_text)
    body = table.find("tbody", recursive=False)
    if body is None:
        raise ValueError(f"У таблицы {area['area']} нет tbody")
    rows = []
    for tr in body.find_all("tr", recursive=False):
        cells = tr.find_all("td", recursive=False)
        if not cells:
            continue
        anchor = cells[0].find("a", href=True)
        # В пустых таблицах может быть строка No entries с colspan.
        empty_message = (
            len(cells) == 1 and cells[0].get("colspan") == "6"
            and re.fullmatch(r"no (entries|results|data|records)( found)?", cells[0].get_text(" ", strip=True), re.I)
        )
        if anchor is None and (expected_count == 0 or (expected_count is None and empty_message)):
            continue
        if len(cells) != 6 or anchor is None:
            raise ValueError(f"Неожиданная строка таблицы {area['area']}")
        rows.append({
            "cfp_url": canonical_cfp_url(anchor["href"]),
            "acronym": cells[0].get_text(" ", strip=True),
            "name": cells[1].get_text(" ", strip=True),
            "location_raw": cells[2].get_text(" ", strip=True),
            "submission_deadline_raw": cells[3].get_text(" ", strip=True),
            "start_date_raw": cells[4].get_text(" ", strip=True),
            "topics": [a.get_text(" ", strip=True) for a in cells[5].find_all("a")],
            **area,
            "fetched_at_utc": fetched_at_utc,
        })
    if expected_count is not None and len(rows) != expected_count:
        raise ValueError(
            f"{area['area']}: в HTML {len(rows)} строк, сайт сообщает {expected_count}. "
            "Возможно, изменилась пагинация; неполную таблицу экспортировать нельзя."
        )
    return pd.DataFrame(rows, columns=RAW_COLUMNS), expected_count
