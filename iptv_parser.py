import requests
import os
import re
import gzip
import json
import time
import hashlib
import xml.etree.ElementTree as ET
from io import BytesIO

SOURCES_FILE = "play.list"
OUTPUT_FILE = "playlist.m3u"
# Основной источник категорий — iptv-org API (channels.json содержит поле
# "categories" для каждого канала; id каналов совпадают с tvg-id в наших плейлистах).
# В epg.xml.gz от iptvx.one тег <category> отсутствует вовсе (проверено на живом файле),
# поэтому он остался только как дополнительный источник.
IPTV_ORG_CHANNELS_URLS = [
    "https://iptv-org.github.io/api/channels.json",
    "https://raw.githubusercontent.com/iptv-org/iptv.org/master/api/public/channels.json",
]
# Дополнительно: xmltv-категории из EPTG (iptvx.one) — если там когда-нибудь появятся <category>
EPG_URL = "https://iptvx.one/epg/epg.xml.gz"
# Кэш EPG между запусками (в CI можно ускорить дополнительно через actions/cache)
EPG_CACHE_DIR = ".cache"
EPG_CACHE_TTL_SECONDS = 6 * 3600  # 6 часов: плейлист пересобирается раз в сутки
# Максимум AUTO-источников, чтобы время сборки в CI не росло бесконечно
MAX_AUTO_SOURCES = int(os.environ.get("MAX_AUTO_SOURCES", "15"))

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
HEADERS = {"Authorization": f"token {GITHUB_TOKEN}"} if GITHUB_TOKEN else {}

# Маппинг категорий из EPG в наши
EPG_CATEGORY_MAP = {
    'movies': '🎬 Кино и Сериалы',
    'cinema': '🎬 Кино и Сериалы',
    'film': '🎬 Кино и Сериалы',
    'series': '🎬 Кино и Сериалы',
    'entertainment': '📺 Федеральные и Общие',
    'general': '📺 Федеральные и Общие',
    'sports': '⚽ Спорт',
    'sport': '⚽ Спорт',
    'news': '📰 Новости',
    'kids': '🧸 Детские',
    'children': '🧸 Детские',
    'music': '🎵 Музыка',
    'documentary': '🌍 Познавательные',
    'doc': '🌍 Познавательные',
    'lifestyle': '🌿 Природа, Охота, Рыбалка и Дача',
    'outdoor': '🌿 Природа, Охота, Рыбалка и Дача',
    'religion': '🕍 Религиозные',
    'auto': '🚗 Авто и Мото',
    'moto': '🚗 Авто и Мото',
    'food': '🍳 Еда и Кулинария',
    'cooking': '🍳 Еда и Кулинария',
    'shopping': '🛍️ Шопинг',
    'travel': '🌍 Познавательные',
    'science': '🌍 Познавательные',
    'history': '🌍 Познавательные',
    'nature': '🌿 Природа, Охота, Рыбалка и Дача',
    'hunting': '🌿 Природа, Охота, Рыбалка и Дача',
    'fishing': '🌿 Природа, Охота, Рыбалка и Дача',
    'health': '💊 Здоровье и Медицина',
    'medical': '💊 Здоровье и Медицина',
    'business': '📰 Новости',
    'finance': '📰 Новости',
    'regional': '🏛 Региональные каналы',
    'local': '🏛 Региональные каналы',
    # категории iptv-org database, отсутствовавшие в маппинге:
    'animation': '🧸 Детские',
    'kids': '🧸 Детские',
    'classic': '🎬 Кино и Сериалы',
    'comedy': '🎬 Кино и Сериалы',
    'adult': '🔞 18+',
    'relax': '🌿 Природа, Охота, Рыбалка и Дача',
    'education': '🌍 Познавательные',
    'educational': '🌍 Познавательные',
    'culture': '🌍 Познавательные',
    'art': '🌍 Познавательные',
    'family': '🧸 Детские',
    'weather': '📰 Новости',
    'religious': '🕍 Религиозные',
    'shop': '🛍️ Шопинг',
}

def _epg_cache_path():
    url_hash = hashlib.sha1(EPG_URL.encode("utf-8")).hexdigest()[:12]
    return os.path.join(EPG_CACHE_DIR, f"epg_{url_hash}.xml.gz")

def _parse_epg_stream(stream):
    """Потоковый разбор XML EPG без загрузки всего текста в память
    (полный распакованный XML ~300+ МБ — ET.fromstring ловит MemoryError)."""
    categories = {}
    display_name = ""
    category = ""
    for event, elem in ET.iterparse(stream, events=("end",)):
        if elem.tag == "display-name" and not display_name and elem.text:
            display_name = elem.text.strip().lower()
        elif elem.tag == "category" and elem.text:
            category = elem.text.strip().lower()
        elif elem.tag == "channel":
            # id приводим к нижнему регистру: в EPG он может быть с заглавными
            cid = (elem.get("id") or "").strip().lower()
            if cid and category:
                categories[cid] = category
            if display_name and category:
                categories[display_name] = category
            display_name = category = ""
            elem.clear()  # освобождаем память разобранных узлов
    return categories

def load_epg_xml_bytes(session=None):
    """Возвращает СЫРОЙ (сжатый) gz-файл EPG, используя локальный кэш (TTL 6 часов),
    чтобы не скачивать ~50 МБ при каждом запуске."""
    cache_path = _epg_cache_path()
    try:
        if os.path.exists(cache_path) and \
           (time.time() - os.path.getmtime(cache_path)) < EPG_CACHE_TTL_SECONDS:
            print(f"📦 EPG из кэша: {cache_path}")
            with open(cache_path, "rb") as f:
                return f.read()
    except OSError as e:
        print(f"⚠️ Кэш EPG повреждён, скачиваю заново: {e}")

    getter = session or requests
    try:
        response = fetch_url(EPG_URL, session=getter, timeout=120)
    except Exception as e:
        # Свежий кэш не успел протухнуть — используем его даже если TTL истёк
        if os.path.exists(cache_path):
            print(f"⚠️ EPG недоступен ({type(e).__name__}), использую устаревший кэш")
            with open(cache_path, "rb") as f:
                return f.read()
        raise
    raw_gz = response.content
    # В кэш кладём исходный .gz — он компактнее распарсенных данных
    try:
        os.makedirs(EPG_CACHE_DIR, exist_ok=True)
        tmp_path = cache_path + ".tmp"
        with open(tmp_path, "wb") as f:
            f.write(raw_gz)
        os.replace(tmp_path, cache_path)
    except OSError as e:
        print(f"⚠️ Не удалось записать кэш EPG: {e}")
    return raw_gz

def load_iptv_org_categories(session=None):
    """Канал → категория из iptv-org API (channels.json, поле "categories").
    id каналов вида 'ChannelName.ru' совпадают с tvg-id в наших плейлистах.
    Кэшируется на диске как и EPG (сжатый JSON)."""
    print("📥 Загрузка категорий из iptv-org API...", flush=True)
    cache_path = None
    try:
        url_hash = hashlib.sha1(IPTV_ORG_CHANNELS_URLS[0].encode()).hexdigest()[:12]
        cache_path = os.path.join(EPG_CACHE_DIR, f"iptvorg_{url_hash}.json.gz")
        if os.path.exists(cache_path) and \
           (time.time() - os.path.getmtime(cache_path)) < EPG_CACHE_TTL_SECONDS:
            print(f"📦 iptv-org категории из кэша: {cache_path}")
            with gzip.GzipFile(cache_path, "rb") as f:
                channels = json.loads(f.read().decode("utf-8"))
            return _build_org_category_map(channels)
    except (OSError, ValueError) as e:
        print(f"⚠️ Кэш iptv-org повреждён, скачиваю заново: {e}")

    last_err = None
    for url in IPTV_ORG_CHANNELS_URLS:
        try:
            resp = fetch_url(url, session=session, timeout=60)
            channels = resp.json()
            if not isinstance(channels, list) or not channels:
                raise ValueError("некорректный формат channels.json")
            if cache_path:
                try:
                    os.makedirs(EPG_CACHE_DIR, exist_ok=True)
                    tmp_path = cache_path + ".tmp"
                    with gzip.GzipFile(tmp_path, "wb") as f:
                        f.write(resp.content)
                    os.replace(tmp_path, cache_path)
                except OSError as e:
                    print(f"⚠️ Не удалось записать кэш iptv-org: {e}")
            return _build_org_category_map(channels)
        except Exception as e:
            last_err = e
            print(f"⚠️ Не удалось получить {url}: {type(e).__name__}: {e}", flush=True)
    # Фолбэк: даже устаревший кэш лучше пустой базы
    if cache_path and os.path.exists(cache_path):
        print("⚠️ Использую устаревший кэш iptv-org")
        try:
            with gzip.GzipFile(cache_path, "rb") as f:
                channels = json.loads(f.read().decode("utf-8"))
            return _build_org_category_map(channels)
        except (OSError, ValueError):
            pass
    print(f"❌ Категории iptv-org недоступны: {last_err}")
    return {}

def _build_org_category_map(channels):
    """[{id, name, categories:[...]}] -> {id/имя: первая категория} (в нижнем регистре)"""
    result = {}
    for ch in channels:
        cats = ch.get("categories") or []
        if not cats:
            continue
        cat = str(cats[0]).strip().lower()
        cid = str(ch.get("id") or "").strip().lower()
        name = str(ch.get("name") or "").strip().lower()
        if cid:
            result.setdefault(cid, cat)
        if name:
            result.setdefault(name, cat)
    return result

def load_epg_categories(session=None):
    """Объединённая база «канал → категория»: iptv-org API (основа) + xmltv EPG (дополнение)"""
    merged = load_iptv_org_categories(session=session)
    epg_count = 0
    try:
        raw_gz = load_epg_xml_bytes(session=session)
        with gzip.GzipFile(fileobj=BytesIO(raw_gz)) as gz:
            epg_cats = _parse_epg_stream(gz)
        del raw_gz
        epg_count = len(epg_cats)
        # данные EPG не перекрывают iptv-org, только дополняют
        for k, v in epg_cats.items():
            merged.setdefault(k, v)
    except Exception as e:
        print(f"❌ Ошибка загрузки EPG: {type(e).__name__}: {e}", flush=True)
    print(f"✅ База категорий: {len(merged)} ключей "
          f"(из них {epg_count} из xmltv EPG)", flush=True)
    return merged

def map_category(epg_category):
    """Преобразуем категорию из EPG в наш формат"""
    if not epg_category:
        return None
    
    cat = epg_category.lower().strip()
    
    # Точное совпадение
    if cat in EPG_CATEGORY_MAP:
        return EPG_CATEGORY_MAP[cat]
    
    # Частичное совпадение (длинные ключи раньше, чтобы 'documentary'
    # не перекрывался коротким 'doc')
    for key in sorted(EPG_CATEGORY_MAP, key=len, reverse=True):
        if key in cat:
            return EPG_CATEGORY_MAP[key]
    
    # Если не нашли - возвращаем оригинал
    return cat.capitalize()

def normalize_channel_name(name):
    name = name.lower().strip()
    name = re.sub(r'[^a-zа-я0-9]', '', name)
    return name

def is_russian_channel(extinf_line):
    # Поиск без учёта регистра: в источниках атрибуты бывают TVG-Language=...
    if re.search(r'tvg-language="[^"]*russian[^"]*"', extinf_line, re.IGNORECASE):
        return True
    if re.search(r'tvg-country="[^"]*(?:\bru\b|rus)[^"]*"', extinf_line, re.IGNORECASE):
        return True
    return False

def get_channel_name(extinf_line):
    """Имя канала — часть строки #EXTINF после разделителя-запятой.

    Запятые могут встречаться внутри значений атрибутов (group-title="a,b"),
    поэтому сканируем строку с учётом кавычек: разделитель — первая запятая
    ВНЕ кавычек. Если парные кавычки не закрылись (битые строки вида
    'tvg-name="X"-1 ...'), делаем откат на последний сегмент после запятой.
    """
    in_quotes = False
    pos = -1
    for i, ch in enumerate(extinf_line):
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == ',' and not in_quotes:
            pos = i
            break
    if pos == -1:
        return "Unknown"  # нет разделителя — имя не извлечь
    name = extinf_line[pos + 1:].strip()
    if in_quotes or not name:
        # Кавычки не закрылись или имя пустое — некорректная строка:
        # берём текст после последней запятой
        tail = extinf_line.rsplit(',', 1)[-1].strip().strip('"')
        name = tail if tail else "Unknown"
    return name

def get_tvg_id(extinf_line):
    match = re.search(r'tvg-id="([^"]*)"', extinf_line, re.IGNORECASE)
    return match.group(1).lower().strip() if match else ""

def get_category_from_epg(channel_name, tvg_id, epg_categories):
    """Ищем категорию в EPG базе"""
    
    # 1. По tvg-id
    if tvg_id and tvg_id in epg_categories:
        return map_category(epg_categories[tvg_id])
    
    # 2. По имени канала
    name_lower = channel_name.lower().strip()
    if name_lower in epg_categories:
        return map_category(epg_categories[name_lower])
    
    # 3. Частичное совпадение (без HD, FHD и т.д.)
    clean_name = re.sub(r'\s*(hd|fhd|sd|uhd|4k|2160|1080|720)\s*', '', name_lower).strip()
    if clean_name in epg_categories:
        return map_category(epg_categories[clean_name])
    
    return None

def get_category_fallback(name):
    """Только для каналов, которых нет в EPG"""
    name_lower = name.lower()
    
    if any(x in name_lower for x in ['4k', '2160', 'uhd']):
        return "📺 4K / Ultra HD"
    if any(x in name_lower for x in ['кино', 'cinema', 'movie', 'film', 'премьера', 'premiere', 'tv1000']):
        return "🎬 Кино и Сериалы"
    if any(x in name_lower for x in ['спорт', 'sport', 'матч', 'match', 'футбол', 'хоккей']):
        return "⚽ Спорт"
    if any(x in name_lower for x in ['новости', 'news', '24', 'дождь', 'vesti']):
        return "📰 Новости"
    if any(x in name_lower for x in ['детский', 'kids', 'карусель', 'мульт', 'disney']):
        return "🧸 Детские"
    if any(x in name_lower for x in ['музыка', 'music', 'mtv', 'муз', 'bridge']):
        return "🎵 Музыка"
    if any(x in name_lower for x in ['discovery', 'national', 'история', 'history', 'наука', 'travel']):
        return "🌍 Познавательные"
    if any(x in name_lower for x in ['охота', 'рыбалка', 'дача', 'усадьба']):
        return "🌿 Природа, Охота, Рыбалка и Дача"
    if any(x in name_lower for x in ['кухня', 'еда', 'food', 'кулинар']):
        return "🍳 Еда и Кулинария"
    if any(x in name_lower for x in ['авто', 'auto', 'мото']):
        return "🚗 Авто и Мото"
    if any(x in name_lower for x in ['ржд', 'транспорт']):
        return "🚂 Транспорт"
    if any(x in name_lower for x in ['спб', 'регион', 'область', 'кубань', 'сургут', 'самара']):
        return "🏛 Региональные каналы"
    if any(x in name_lower for x in ['первый', 'россия', 'нтв', 'тнт', 'стс']):
        return "📺 Федеральные и Общие"
    
    return "📦 Разное"

def fix_extinf(extinf_line, channel_name, epg_categories):
    """Пересобирает строку #EXTINF корректно: сначала атрибуты, затем
    имя канала после запятой. Чинит исходный порядок 'атрибуты,-1,имя'."""
    safe_id = re.sub(r'[^a-zа-я0-9]', '', channel_name.lower())
    tvg_id = get_tvg_id(extinf_line)

    # Сначала ищем в EPG
    category = get_category_from_epg(channel_name, tvg_id, epg_categories)

    # Если не нашли - fallback
    if not category:
        category = get_category_fallback(channel_name)

    # Извлекаем уже существующие атрибуты (в кавычках — чтобы не захватывать мусор)
    attrs = {}
    for key in ("tvg-id", "tvg-name", "tvg-logo", "tvg-language",
                "tvg-country", "tvg-shift", "group-title"):
        m = re.search(key + r'="([^"]*)"', extinf_line, re.IGNORECASE)
        if m and m.group(1):
            attrs[key] = m.group(1)

    attrs.setdefault("tvg-id", tvg_id or safe_id)
    attrs["tvg-name"] = channel_name
    attrs["group-title"] = category

    attr_str = " ".join(f'{k}="{v}"' for k, v in attrs.items() if v)
    return f"#EXTINF:-1 {attr_str},{channel_name}"

def get_existing_urls():
    urls = set()
    if not os.path.exists(SOURCES_FILE):
        return urls
    with open(SOURCES_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                urls.add(line)
    return urls

def find_github_playlists():
    print("🔍 Поиск новых русских источников на GitHub...")
    new_urls = set()
    search_url = "https://api.github.com/search/repositories"
    params = {"q": "iptv russian OR iptv ru OR iptv россия in:name,description,topics", "sort": "stars", "order": "desc", "per_page": 20}
    try:
        resp = requests.get(search_url, headers=HEADERS, params=params, timeout=15)
        resp.raise_for_status()
        repos = resp.json().get("items", [])
    except Exception as e:
        print(f"❌ Ошибка поиска репозиториев: {e}")
        return new_urls

    for repo in repos:
        if repo.get("fork", False) or repo.get("stargazers_count", 0) < 5:
            continue
        repo_name = repo["full_name"]
        default_branch = repo.get("default_branch", "main")
        tree_url = f"https://api.github.com/repos/{repo_name}/git/trees/{default_branch}?recursive=1"
        try:
            tree_resp = requests.get(tree_url, headers=HEADERS, timeout=15)
            if tree_resp.status_code != 200:
                continue
            tree_data = tree_resp.json()
        except Exception:
            continue
        if "tree" not in tree_data:
            continue
        for item in tree_data["tree"]:
            if item["type"] == "blob":
                path = item["path"]
                if path.lower().endswith(".m3u") or path.lower().endswith(".m3u8"):
                    raw_url = f"https://raw.githubusercontent.com/{repo_name}/{default_branch}/{path}"
                    new_urls.add(raw_url)
    print(f"   ✅ Найдено {len(new_urls)} потенциальных ссылок на GitHub.")
    return new_urls

def update_sources_file(new_urls):
    existing_urls = get_existing_urls()
    urls_to_add = new_urls - existing_urls
    if not urls_to_add:
        print("🔄 Новых источников на GitHub не найдено.")
        return
    print(f"📝 Добавление {len(urls_to_add)} новых источников в {SOURCES_FILE}...")
    manual_lines = []
    auto_urls = set()
    auto_marker = "# --- AUTO SOURCES (GitHub Search) ---"
    if os.path.exists(SOURCES_FILE):
        with open(SOURCES_FILE, "r", encoding="utf-8") as f:
            in_auto_section = False
            for line in f:
                if auto_marker in line:
                    in_auto_section = True
                    continue
                if in_auto_section:
                    stripped = line.strip()
                    if stripped and not stripped.startswith("#"):
                        auto_urls.add(stripped)
                else:
                    manual_lines.append(line)
    auto_urls.update(urls_to_add)
    with open(SOURCES_FILE, "w", encoding="utf-8") as f:
        f.writelines(manual_lines)
        f.write("\n")
        f.write(auto_marker + "\n")
        for url in sorted(list(auto_urls)):
            f.write(url + "\n")
    print(f"✅ Файл {SOURCES_FILE} успешно обновлен.")

def load_sources(filepath):
    sources = []
    if not os.path.exists(filepath):
        return sources
    in_auto_section = False
    auto_marker = "# --- AUTO SOURCES (GitHub Search) ---"
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if auto_marker in line:
                in_auto_section = True
                continue
            if line and not line.startswith("#"):
                sources.append({"url": line, "filter_russian": in_auto_section})
    # Ограничиваем число AUTO-источников, чтобы время сборки в CI не росло
    # бесконечно с каждым новым найденным репозиторием.
    manual = [s for s in sources if not s["filter_russian"]]
    auto = [s for s in sources if s["filter_russian"]]
    if len(auto) > MAX_AUTO_SOURCES:
        print(f"ℹ️ AUTO-источников {len(auto)}, используется только первые "
              f"{MAX_AUTO_SOURCES} (задаётся переменной MAX_AUTO_SOURCES)")
        auto = auto[:MAX_AUTO_SOURCES]
    return manual + auto

def fetch_url(url, session=None, timeout=15):
    """Загрузка URL с повторными попытками и таймаутом на чтение."""
    getter = session or requests
    last_error = None
    for attempt in range(3):
        try:
            response = getter.get(url, timeout=(5, timeout))
            response.raise_for_status()
            # Явно указываем кодировку, если сервер её не задал
            if response.encoding == 'ISO-8859-1':
                response.encoding = response.apparent_encoding or 'utf-8'
            return response
        except requests.RequestException as e:
            last_error = e
            if attempt < 2:
                time.sleep(2 ** attempt)  # 1с, 2с — не долбим зависающий хост
    raise last_error

def is_stub_url(url):
    """Проверка на заведомо нерабочие заглушки (только явные случаи)"""
    url_lower = url.lower()
    # Только абсолютно явные заглушки, которые НЕ могут быть рабочими
    stub_patterns = [
        '127.0.0.1',       # localhost IP
        'localhost',       # localhost имя
        'example.com',     # резервное доменное имя
        '0.0.0.0',         # все интерфейсы (невалидно для клиента)
        'acestream://',    # Ace Stream требует отдельного клиента (не HTTP)
    ]
    return any(pattern in url_lower for pattern in stub_patterns)

# Протухшие/временные токены доступа: такие ссылки работают несколько часов
# и у большинства зрителей будут отдавать 403 — в публичном плейлисте бесполезны
EXPIRING_TOKEN_RE = re.compile(
    r'(?:[?&](?:token|wmsauthsign|sessionid|session_id|hash|key|k|sign|md5|usertoken)=)'
    r'|(?:/[a-f0-9]{24,}/?)$',
    re.IGNORECASE,
)

def is_expired_token_url(url):
    """Ссылки с временными токенами (wmsAuthSign, token=...) протухают через часы."""
    return bool(EXPIRING_TOKEN_RE.search(url))

def parse_m3u(url, filter_russian=False, epg_categories=None, session=None):
    print(f" Загрузка: {url} {'(с фильтрацией)' if filter_russian else '(без фильтрации)'}")
    try:
        response = fetch_url(url, session=session)
    except requests.RequestException as e:
        print(f"❌ Ошибка загрузки {url}: {e}")
        return []
    
    if epg_categories is None:
        epg_categories = {}
        
    channels = []
    skipped_tokens = 0
    lines = response.text.splitlines()
    current_extinf = ""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF:"):
            current_extinf = line
        elif line.startswith("http"):
            # Пропускаем заглушки и ссылки с протухающими токенами
            if is_stub_url(line) or is_expired_token_url(line):
                if is_expired_token_url(line):
                    skipped_tokens += 1
                continue
            if not filter_russian or is_russian_channel(current_extinf):
                name = get_channel_name(current_extinf)
                if not name or name == "Unknown":
                    current_extinf = ""
                    continue
                fixed_extinf = fix_extinf(current_extinf, name, epg_categories)
                channels.append({"extinf": fixed_extinf, "url": line})
            current_extinf = ""
    suffix = f" (пропущено сессий с протухающими токенами: {skipped_tokens})" if skipped_tokens else ""
    print(f"   ✅ Найдено каналов: {len(channels)}{suffix}", flush=True)
    return channels

def get_category_for_sort(extinf_line):
    match = re.search(r'group-title="([^"]*)"', extinf_line)
    return match.group(1) if match else "📦 Разное"

def main():
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; iptv-parser)"})

    # Загружаем категории из EPG (с локальным кэшем)
    print("\n📚 Загрузка категорий из EPG...")
    epg_categories = load_epg_categories(session=session)

    gh_urls = find_github_playlists()
    update_sources_file(gh_urls)
    sources = load_sources(SOURCES_FILE)
    if not sources:
        print("🛑 Нет источников для обработки. Завершаю работу.")
        return
    print(f"\n📋 Найдено {len(sources)} источников в {SOURCES_FILE}\n")
    
    all_channels = []
    seen_urls = set()  # защита от одинаковых ссылок в разных источниках
    started = time.time()
    for idx, src in enumerate(sources, 1):
        if src["url"] in seen_urls:
            continue
        seen_urls.add(src["url"])
        print(f"[{idx}/{len(sources)}] elapsed {time.time()-started:.0f}s", flush=True)
        channels = parse_m3u(src["url"], filter_russian=src["filter_russian"],
                             epg_categories=epg_categories, session=session)
        all_channels.extend(channels)
    print(f"\n Всего каналов до обработки: {len(all_channels)}")
    
    # ДЕДУПЛИКАЦИЯ ПО ИМЕНИ КАНАЛА
    seen_names = set()
    unique_channels = []
    
    for ch in all_channels:
        name = get_channel_name(ch['extinf'])
        norm_name = normalize_channel_name(name)
        
        if norm_name and norm_name not in seen_names:
            seen_names.add(norm_name)
            unique_channels.append(ch)
    
    # Сортировка
    unique_channels.sort(key=lambda x: (get_category_for_sort(x['extinf']), get_channel_name(x['extinf']).lower()))
    
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(f'#EXTM3U url-tvg="{EPG_URL}"\n')
        for ch in unique_channels:
            f.write(f"{ch['extinf']}\n")
            f.write(f"{ch['url']}\n")
    
    removed_count = len(all_channels) - len(unique_channels)
    print(f"\n✅ Успешно сохранено {len(unique_channels)} уникальных каналов в {OUTPUT_FILE}")
    print(f"🗑 Удалено дубликатов (по имени): {removed_count}")
    
    # Статистика
    categories_count = {}
    for ch in unique_channels:
        cat = get_category_for_sort(ch['extinf'])
        categories_count[cat] = categories_count.get(cat, 0) + 1
    
    print("\n Категории:")
    for cat, count in sorted(categories_count.items(), key=lambda x: x[1], reverse=True):
        print(f"  {cat}: {count}")

if __name__ == "__main__":
    main()