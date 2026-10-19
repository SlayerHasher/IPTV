import requests
import os
import re
import json
import gzip
import math
import hashlib
import base64
import struct
import binascii
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from io import BytesIO

SOURCES_FILE = "play.list"
OUTPUT_FILE = "playlist.m3u"
EPG_URL = "https://iptvx.one/epg/epg.xml.gz"

# --- Статистика жизни трансляций (стабильные ссылки) ---
STABLE_DB_FILE = "stable_streams.json"      # база стабильных ссылок (создаётся автоматически)
STABLE_PLAYLIST_FILE = "stable.m3u"         # плейлист только из стабильных ссылок
STABLE_DAYS_MIN = 30                        # минимальный период жизни для отбора в stable.m3u
STABLE_MISSING_TOLERANCE = 7                # сколько дней подряд ссылка может отсутствовать,
                                            # прежде чем считаться умершей ("период жизни" фиксируется)

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
HEADERS = {"Authorization": f"token {GITHUB_TOKEN}"} if GITHUB_TOKEN else {}

# --- Внешний IPTV-хаб shirtv.uz (каталог готовых m3u-плейлистов) ---
# Сайт лежит на https (http редиректит 301). Запрос идёт напрямую; если сайт
# окажется недоступен (блокировки/таймауты в CI), включается запасной
# read-туннель r.jina.ai.
SHIRTV_INDEX_URL = "https://shirtv.uz/playlists.html"  # страница со списком плейлистов
SHIRTV_BASE_URL = "https://shirtv.uz/"                 # база для относительных ссылок
SHIRTV_TUNNEL_PREFIX = "https://r.jina.ai/"            # запасной read-прокси
SHIRTV_USE_TUNNEL = True                               # False — только прямые запросы
SHIRTV_MARKED_ONLY = True   # брать только плейлисты с бейджем "Свежий"/"Недавнее"
                            # (загруженные за последние ~7 дней); False — все 80+
SHIRTV_MAX_PLAYLISTS = 25   # жёсткий лимит количества плейлистов за прогон
SHIRTV_MARKER = "# --- AUTO SOURCES (shirtv.uz) ---"

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
    'religion': '✝️ Религиозные',
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
    'education': '🌍 Познавательные',
    'culture': '🌍 Познавательные',
    'family': '🧸 Детские',
    'adult': '🔞 18+',
    'erotic': '🔞 18+',
    'xxx': '🔞 18+',
    'weather': '📺 Федеральные и Общие',
    'military': '🌍 Познавательные',
    'war': '🌍 Познавательные',
    'comedy': '🎬 Кино и Сериалы',
    'animation': '🧸 Детские',
    'cartoon': '🧸 Детские',
    'regional': '🏛️ Региональные каналы',
    'local': '🏛️ Региональные каналы',
}

# Порядок вывода категорий в плейлисте (неизвестные — в конце, по алфавиту)
CATEGORY_ORDER = [
    '📺 Федеральные и Общие',
    '📰 Новости',
    '🎬 Кино и Сериалы',
    '⚽ Спорт',
    '🧸 Детские',
    '🎵 Музыка',
    '🌍 Познавательные',
    '🌿 Природа, Охота, Рыбалка и Дача',
    '🍳 Еда и Кулинария',
    '🚗 Авто и Мото',
    '📺 4K / Ultra HD',
    '🚂 Транспорт',
    '💊 Здоровье и Медицина',
    '✝️ Религиозные',
    '🛍️ Шопинг',
    '🔞 18+',
    '🏛️ Региональные каналы',
    '📦 Разное',
]

def load_epg_categories():
    """Скачиваем EPG и извлекаем категории каналов"""
    print("📥 Загрузка и парсинг EPG...")
    categories = {}
    
    try:
        response = requests.get(EPG_URL, timeout=60)
        response.raise_for_status()
        
        with gzip.GzipFile(fileobj=BytesIO(response.content)) as gz:
            xml_content = gz.read().decode('utf-8')
        
        root = ET.fromstring(xml_content)
        
        for channel in root.findall('.//channel'):
            channel_id = channel.get('id', '').lower().strip()
            
            display_name = ""
            category = ""
            
            name_elem = channel.find('display-name')
            if name_elem is not None and name_elem.text:
                display_name = name_elem.text.strip().lower()
            
            category_elem = channel.find('category')
            if category_elem is not None and category_elem.text:
                category = category_elem.text.strip().lower()
            
            if channel_id and category:
                categories[channel_id] = category
            if display_name and category:
                categories[display_name] = category
        
        print(f"✅ Извлечено {len(categories)} каналов с категориями из EPG")
        return categories
        
    except Exception as e:
        print(f"❌ Ошибка загрузки EPG: {e}")
        return {}

def map_category(epg_category):
    """Преобразуем категорию из EPG в наш формат"""
    if not epg_category:
        return None
    
    cat = epg_category.lower().strip()
    
    # Точное совпадение
    if cat in EPG_CATEGORY_MAP:
        return EPG_CATEGORY_MAP[cat]
    
    # Частичное совпадение
    for key, value in EPG_CATEGORY_MAP.items():
        if key in cat:
            return value
    
    # Если не нашли - возвращаем оригинал (без category-мусора вроде "Undefined")
    if cat in ('undefined', 'unknown', 'other', 'no_category'):
        return None
    return cat.capitalize()

def normalize_channel_name(name):
    name = name.lower().strip()
    name = re.sub(r'[^a-zа-я0-9]', '', name)
    return name

def is_russian_channel(extinf_line):
    line_lower = extinf_line.lower()
    if re.search(r'tvg-language="[^"]*russian[^"]*"', line_lower):
        return True
    if re.search(r'tvg-country="[^"]*(ru|rus)[^"]*"', line_lower):
        return True
    return False

def get_channel_name(extinf_line):
    match = re.search(r',(.*?)$', extinf_line)
    return match.group(1).strip() if match else "Unknown"

def get_tvg_id(extinf_line):
    match = re.search(r'tvg-id="([^"]*)"', extinf_line)
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
    clean_name = re.sub(r'\s*\(?\b(hd|fhd|sd|uhd|4k|2160p?|1080p?|720p?|576p?)\)?\s*', ' ', name_lower).strip()
    if clean_name in epg_categories:
        return map_category(epg_categories[clean_name])
    
    return None

def get_category_fallback(name):
    """Только для каналов, которых нет в EPG"""
    name_lower = name.lower()
    
    # Точные соответствия известным русскоязычным каналам (исправляют ошибки
    # ключевых слов, например «Победа» ≠ кулинария, «Еда» ≠ «Разное»)
    exact_map = {
        'победа': '🌍 Познавательные',
        'звезда': '📺 Федеральные и Общие',
        'красная звезда': '🌍 Познавательные',
        'военное дело': '🌍 Познавательные',
        'совершенно секретно': '🌍 Познавательные',
        'сов. секретно': '🌍 Познавательные',
        'спас': '✝️ Религиозные',
        'царьград': '📰 Новости',
        'родной канал': '📺 Федеральные и Общие',
        'охотник и рыболов': '🌿 Природа, Охота, Рыбалка и Дача',
        'первый метео': '🌍 Познавательные',
        'юмор чп': '📺 Федеральные и Общие',
        'блокбастер тв': '🎬 Кино и Сериалы',
        'про блокбастеры': '🎬 Кино и Сериалы',
        'умняша': '🧸 Детские',
        'тяни-толки': '🧸 Детские',
        'еда': '🍳 Еда и Кулинария',
        'еда .hd': '🍳 Еда и Кулинария',
        'заканывай дачу': '🌿 Природа, Охота, Рыбалка и Дача',
    }
    key = name_lower.strip()
    if key in exact_map:
        return exact_map[key]
    base = re.sub(r'\s*(hd|fhd|sd|uhd|4k|2160p?|1080p?|720p?|576p?)\s*$', '', key).strip()
    if base in exact_map:
        return exact_map[base]
    
    if any(x in name_lower for x in ['4k', '2160', 'uhd']):
        return "📺 4K / Ultra HD"
    if any(x in name_lower for x in ['кино', 'cinema', 'movie', 'film', 'премьера', 'premiere', 'tv1000', 'блокбастер', 'комедия', 'драма', 'триллер', 'series', 'сериал']):
        return "🎬 Кино и Сериалы"
    if any(x in name_lower for x in ['спорт', 'sport', 'матч', 'match', 'футбол', 'хоккей', 'биатлон', 'теннис', 'бойцовск']):
        return "⚽ Спорт"
    if any(x in name_lower for x in ['новости', 'news', 'дождь', 'vesti', 'избиратель', 'рБК', 'rtvi']):
        return "📰 Новости"
    if any(x in name_lower for x in ['детский', 'kids', 'карусель', 'мульт', 'disney', 'cartoon', 'анимац', 'тиля', 'tom and jerry', 'nick', 'джелтмуж']):
        return "🧸 Детские"
    if any(x in name_lower for x in ['музыка', 'music', 'mtv', 'муз', 'bridge', 'шансон', 'рок', 'jazz']):
        return "🎵 Музыка"
    if any(x in name_lower for x in ['discovery', 'national', 'история', 'history', 'наука', 'travel', 'познател', 'документал', 'geographic', 'планета', 'космос', 'вооруж', 'армия', 'военн', 'секретно', 'метеоро', 'погода', 'meteorolog', 'техно', 'машин', 'astral', 'knowledge']):
        return "🌍 Познавательные"
    if any(x in name_lower for x in ['охота', 'рыбалка', 'дача', 'усадьба', 'природа', 'лес', 'садов', 'ферма', 'fish', 'hunt']):
        return "🌿 Природа, Охота, Рыбалка и Дача"
    if any(x in name_lower for x in ['кухня', 'culinary', 'кулинар', 'food network', 'фуд тв', '24grid', 'еда тв', 'вкусно', 'рецепт', 'шеф', 'chef', 'vegans', 'vegan', '素食']):
        return "🍳 Еда и Кулинария"
    if any(x in name_lower for x in ['авто', 'auto', 'мото', 'moto', 'driving', 'дорожное', 'шоссе']):
        return "🚗 Авто и Мото"
    if any(x in name_lower for x in ['ржд', 'транспорт', 'автотрек', 'поезд']):
        return "🚂 Транспорт"
    if any(x in name_lower for x in ['спб', 'регион', 'область', 'кубань', 'сургут', 'самара', 'крим', 'казань', 'урал', 'сибирь', 'приморье', 'ханты', 'ялтин', 'белгород', 'липецк', 'воронеж', 'тул']):
        return "🏛️ Региональные каналы"
    if any(x in name_lower for x in ['первый', 'россия', 'нтв', 'тнт', 'стс', 'петербург', 'четверт', 'пятый', 'домашн', 'пиатница', 'пятниц', 'звезда', 'мир', 'культура', 'только', 'orange']):
        return "📺 Федеральные и Общие"
    if any(x in name_lower for x in ['религ', 'православ', 'вера', 'церковь', 'spas', 'грязь']):
        return "✝️ Религиозные"
    if any(x in name_lower for x in ['shop', 'покупка', 'товар', 'галерея', 'telemall', 'quality']):
        return "🛍️ Шопинг"
    if any(x in name_lower for x in ['здоров', 'медицин', 'врач', 'доктор', 'health', 'medic']):
        return "💊 Здоровье и Медицина"
    
    return "📦 Разное"

def fix_extinf(extinf_line, channel_name, epg_categories):
    safe_id = re.sub(r'[^a-zа-я0-9]', '', channel_name.lower())
    tvg_id = get_tvg_id(extinf_line)
    
    # Сначала ищем в EPG
    category = get_category_from_epg(channel_name, tvg_id, epg_categories)
    
    # Если не нашли - fallback
    if not category:
        category = get_category_fallback(channel_name)
    
    # Извлекаем часть до запятой из строки "#EXTINF:<длительность> <атрибуты>,<имя>"
    m = re.match(r'#EXTINF:\s*([^,]*),(.*)$', extinf_line, re.DOTALL)
    if not m:
        return extinf_line
    
    head = m.group(1).strip()
    
    # Определяем длительность: если явно указана в начале — берём её, иначе -1
    dur_m = re.match(r'^(-?\d+(?:\.\d+)?)', head)
    if dur_m:
        duration = dur_m.group(1)
        attrs_part = head[dur_m.end():].strip()
    else:
        duration = "-1"
        attrs_part = head
    
    # Разбираем атрибуты корректно; при этом чиним повреждённые строки вида
    # tvg-name="..."-1 (длительность "прилипла" к значению атрибута)
    ATTR_RE = r'([A-Za-z0-9_-]+)=(?:"((?:[^"\\]|\\.)*)"|(\S*))'
    
    consumed = []
    for am in re.finditer(ATTR_RE, attrs_part):
        if am.group(2) is not None:
            tail_m = re.match(r'-{1,}\d*(?=\s|\Z)', attrs_part[am.end():])
            if tail_m:
                consumed.append((am.end(), am.end() + tail_m.end()))
    
    # Вырезаем налипшие хвосты длительности из строки атрибутов
    for s, e in sorted(consumed, reverse=True):
        attrs_part = attrs_part[:s] + attrs_part[e:]
    
    # Пересобираем список атрибутов (очищаем значения от налипших "-1"/"--1")
    tokens = []
    for am in re.finditer(ATTR_RE, attrs_part):
        key = am.group(1)
        if am.group(2) is not None:
            value = re.sub(r'-{1,}\d*$', '', am.group(2)).strip()
            tokens.append(f'{key}="{value}"')
        else:
            tokens.append(f'{key}={am.group(3)}')
    
    attrs_part = " ".join(tokens)
    
    # Добавляем tvg-id
    if 'tvg-id=' not in attrs_part.lower():
        attrs_part = f'tvg-id="{safe_id}" {attrs_part}'.strip()
    
    # Добавляем tvg-name
    if 'tvg-name=' not in attrs_part.lower():
        attrs_part = f'tvg-name="{channel_name}" {attrs_part}'.strip()
    
    # Заменяем/добавляем group-title
    if 'group-title=' in attrs_part.lower():
        attrs_part = re.sub(r'group-title="[^"]*"', f'group-title="{category}"', attrs_part)
    else:
        attrs_part = f'{attrs_part} group-title="{category}"'.strip()
    
    return f'#EXTINF:{duration} {attrs_part},{channel_name}'

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

def parse_playlists_html(html, base_url):
    """Универсальный разбор HTML-каталога плейлистов.

    Понимает карточки в стиле shirtv.uz (<div class="playlist-card"> с
    playlist-name / last-update-badge / href на .m3u), а также обычные
    списки ссылок <a href="...m3u">. Возвращает dict {url: info}, где
    info — dict {"name": str, "fresh": bool}.
    """
    found = {}

    # 1) Карточное представление (shirtv.uz и подобные хабы): режем HTML на
    #    блоки <div class="playlist-card"...> и внутри каждого ищем имя,
    #    бейдж свежести и ссылку на .m3u.
    for cm in re.finditer(r'playlist-card\b[\s\S]*?(?=playlist-card\b|\Z)', html):
        block = cm.group(0)
        um = re.search(r'(?:data-url|href)="([^"]+\.m3u8?)"', block)
        if not um:
            continue
        url = um.group(1).strip()
        if url.startswith("//"):
            url = "http:" + url
        elif url.startswith("/"):
            url = base_url.rstrip("/") + url
        elif not re.match(r'^https?://', url):
            url = base_url + url
        nm = re.search(r'playlist-name">([^<]+)<', block)
        fresh = bool(re.search(r'last-update-badge[^>]*update-(recent|fresh)', block))
        prev = found.get(url)
        found.setdefault(url, {"name": "", "fresh": False})
        if nm and not found[url]["name"]:
            found[url]["name"] = nm.group(1).strip()
        found[url]["fresh"] = found[url]["fresh"] or fresh

    # 2) Любые ссылки вида <a href="....m3u"> (простые каталоги без карточек)
    for m in re.finditer(r'<a[^>]+href="([^"]+\.m3u8?)"', html, re.I):
        href = m.group(1).strip()
        if href.startswith("//"):
            href = "http:" + href
        elif href.startswith("/"):
            href = base_url.rstrip("/") + href
        elif not re.match(r'^https?://', href):
            href = base_url + href
        name = ""
        tag = re.search(r'<a[^>]*>(.*?)</a>', m.group(0), re.S | re.I)
        if tag:
            name = re.sub(r'<[^>]+>', '', tag.group(1)).strip()
        found.setdefault(href, {"name": name, "fresh": False})

    return found


def shirtv_fetch(url):
    """Загрузка страницы каталога shirtv.uz с запасным read-туннелем.

    Сначала пробуем прямой запрос (сайт обычно отвечает нормально), при
    сетевой ошибке или пустом ответе — туннель r.jina.ai. Возвращает
    текст страницы или None.
    """
    candidates = [(url, {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"})]
    if SHIRTV_USE_TUNNEL:
        candidates.append((SHIRTV_TUNNEL_PREFIX + url, {"User-Agent": "Mozilla/5.0"}))
    for target, hdrs in candidates:
        try:
            resp = requests.get(target, timeout=30, allow_redirects=True, headers=hdrs)
            resp.raise_for_status()
            text = resp.text
            # туннель может вернуть заглушку/ошибку без ссылок на .m3u
            if ".m3u" in text or target == url:
                via = "напрямую" if target == url else "через туннель r.jina.ai"
                print(f"   📡 Каталог загружен {via} ({len(text)} симв.)")
                return text
            print("   ⚠️  Туннель вернул пустой ответ без ссылок .m3u...")
        except requests.RequestException as e:
            print(f"   ⚠️  Загрузка не удалась ({target[:60]}...): {e}")
    return None


def find_shirtv_playlists():
    """Парсит каталог shirtv.uz/playlists.html и возвращает
    множество прямых ссылок на m3u-плейлисты."""
    print(f"\n🌐 Поиск внешних плейлистов на {SHIRTV_INDEX_URL}...")
    html = shirtv_fetch(SHIRTV_INDEX_URL)
    if html is None:
        print("⚠️  Не удалось загрузить каталог shirtv.uz ни напрямую, ни через туннель.")
        return set()

    playlists = parse_playlists_html(html, SHIRTV_BASE_URL)
    if not playlists:
        print("⚠️  На странице каталога ссылок на .m3u не найдено.")
        return set()

    # Исключаем саму страницу каталога и служебные ссылки
    urls = {u for u, info in playlists.items() if "playlists.html" not in u.lower()}

    if SHIRTV_MARKED_ONLY:
        # берём только плейлисты со статусом свежести ("Свежий"/"Недавнее")
        fresh = {u for u, info in playlists.items() if info.get("fresh")}
        if fresh:
            urls &= fresh
        else:
            print("   ⚠️  Бейджи свежести не распознаны — беру все найденные плейлисты.")

    urls = {u.replace("http://", "https://", 1) if u.startswith("http://shirtv.uz") else u
            for u in urls}

    result = sorted(urls)[:SHIRTV_MAX_PLAYLISTS]
    print(f"   🎯 Найдено плейлистов: {len(urls)}, берём: {len(result)} (лимит {SHIRTV_MAX_PLAYLISTS})")
    return set(result)

def update_sources_file(new_urls, shirtv_urls=None):
    """Пересобирает play.list из трёх секций: ручные URL, авто-GitHub и shirtv.uz."""
    shirtv_urls = shirtv_urls or set()
    gh_marker = "# --- AUTO SOURCES (GitHub Search) ---"

    manual_lines, gh_urls, sh_urls = [], set(), set()
    if os.path.exists(SOURCES_FILE):
        section = None
        with open(SOURCES_FILE, "r", encoding="utf-8") as f:
            for line in f:
                if gh_marker in line:
                    section = "gh"; continue
                if SHIRTV_MARKER in line:
                    section = "sh"; continue
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and section:
                    (gh_urls if section == "gh" else sh_urls).add(stripped)
                    continue
                if not section:
                    manual_lines.append(line)

    urls_to_add = (new_urls | shirtv_urls) - get_existing_urls()
    if not urls_to_add and (gh_urls | sh_urls):
        print("🔄 Новых источников не найдено (GitHub + shirtv.uz).")
        return

    gh_new = (gh_urls | (new_urls - shirtv_urls))
    sh_new = sh_urls | shirtv_urls
    print(f"📝 Обновление {SOURCES_FILE}: GitHub={len(gh_new)}, shirtv.uz={len(sh_new)}...")

    while manual_lines and not manual_lines[-1].strip():
        manual_lines.pop()
    with open(SOURCES_FILE, "w", encoding="utf-8") as f:
        f.writelines(manual_lines)
        f.write("\n")
        f.write(gh_marker + "\n")
        for url in sorted(gh_new):
            f.write(url + "\n")
        f.write("\n")
        f.write(SHIRTV_MARKER + "\n")
        for url in sorted(sh_new):
            f.write(url + "\n")
    print(f"✅ Файл {SOURCES_FILE} успешно обновлен.")

def load_sources(filepath):
    sources = []
    if not os.path.exists(filepath):
        return sources
    section = None  # None -> ручные, "gh" -> GitHub, "sh" -> shirtv.uz
    gh_marker = "# --- AUTO SOURCES (GitHub Search) ---"
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if gh_marker in line:
                section = "gh"; continue
            if SHIRTV_MARKER in line:
                section = "sh"; continue
            if line and not line.startswith("#"):
                # фильтруем русскоязычные только для случайных GitHub-источников;
                # shirtv.uz отдаёт готовые русские плейлисты — фильтр не нужен
                sources.append({"url": line, "filter_russian": section == "gh"})
    return sources

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

# --- Живая проверка потока «подписочных» ссылок ---------------------------------
# Некоторые сервисы вставляют платную заглушку ПОД ОБЫЧНЫМ именем канала:
# в плейлисте всё чисто, а при открытии потока плеер показывает картинку
# «Требуется подписка». Такое невозможно увидеть в тексте M3U — только по
# содержимому самого потока. Поэтому ссылки с подозрительных хостов
# проверяются HEAD/GET-запросом: если сервер отдаёт HTML-страницу вместо
# видеопотока или прямо сообщает о подписке/403 — канал исключается.
SUBSCRIPTION_URL_CHECK = True          # главный выключатель живой проверки
SUBSCRIPTION_URL_CONCURRENCY = 8       # сколько потоков проверять параллельно
SUBSCRIPTION_URL_TIMEOUT = 6           # таймаут запроса, сек (CI-бюджет!)
SUBSCRIPTION_URL_MAX_BODY = 65536      # сколько байт ответа читать для анализа
# Кэш вердиктов между прогонами: хост/URL, отдавший заглушку, больше не
# проверяется заново (экономит время CI). Файл коммитится вместе с плейлистами.
STREAM_CHECK_CACHE_FILE = "stream_check_cache.json"
STREAM_CHECK_CACHE_TTL_DAYS = 14       # «ок»-вердикты протухают через N дней
                                       # (заглушка может появиться позже)

# Признаки «не видео» в HTTP-ответе
SUBS_HTTP_CODES = {402, 403, 451}      # Payment Required / Forbidden / Legal
SUBS_BODY_RE = re.compile(
    r'нужна\s+подписка|требуется\s+подписка|нет\s+подписки|без\s+подписки|'
    r'подключите\s+подписк|оформите\s+подписк|доступ\s+по\s+подписке|'
    r'subscription\s+(?:required|needed|expired)|subscribe\s+to\s+watch|'
    r'premium\s+(?:only|required)',
    re.IGNORECASE)
# MIME-типы, которые НЕ могут быть живым видеопотоком
NON_VIDEO_TYPES = ('text/html', 'text/plain', 'application/json',
                   'image/jpeg', 'image/png', 'image/gif', 'image/webp')
# Если хост отдал «не видео» столько раз подряд — заносим его в чёрный список
# и больше к нему не ходим (экономит время CI при массовых подписках).
BAD_HOST_THRESHOLD = 3
_bad_host_counter = {}

# --- Детектор видеозаглушек «нет доступа / нужна подписка» ----------------------
# Главный случай: сервер отдаёт ПОЛНОЦЕННЫЙ видеопоток (200 OK, video/*), но
# вместо трансляции в нём — заставка «Нет сигнала», «Требуется подписка»,
# бегущая строка или тестовая таблица. По HTTP-коду и тексту M3U это не
# выловить вообще. Поэтому содержимое потока анализируется на уровне кадров:
#   1) MP4/MOV — парсятся боксы stts/stss/stsc/stco, извлекаются смещения
#      ключевых кадров, JPEG/PNG-сниппеты декодируются в пиксели;
#   2) MPEG-TS — сканируются RTP-пакеты, собираются IDR-кадры H.264
#      (NAL-тип 5) с эмуляцией start-code (0x03), декодируются через ffmpeg.
# Дальше по кадрам считаются «синтетические» признаки:
#   • монохромность (насыщенность ~0) — текст/графика на чёрном или сером;
#   • низкая сложность (энтропия < порога) — мало деталей, флэт-графика;
#   • статичность — кадры почти идентичны (реальная сцена так не ведёт себя).
# Поток с 2+ кадрами, где заглушечные кадры ≥80% и есть статичность ИЛИ
# полностью монохромный набор — помечается как «заставка» и исключается.
STUB_VIDEO_CHECK = True                # выключатель анализа кадров
STUB_SAMPLE_BYTES = 1_500_000          # сколько байт потока скачивать (~1.5 МБ)
STUB_MAX_FRAMES = 8                    # сколько сэмплов-кадров декодировать
STUB_ENTROPY_TH = 4.3                  # бит/пиксель ниже этого = примитивная картинка
STUB_STATIC_DIFF = 7                   # средний побайтовый diff кадров ниже этого = статика
STUB_FRAME_RATIO = 0.8                 # доля «заглушечных» кадров для вердикта

# Опциональные тяжёлые зависимости: без них детектор деградирует до HTTP-уровня
try:
    from PIL import Image
except ImportError:
    Image = None
import shutil as _shutil
import subprocess as _subprocess
_FFMPEG_BIN = _shutil.which('ffmpeg')


def _decode_image_to_gray(data):
    """Декодировать JPEG/PNG в (gray_bytes, width, height).
    Сначала быстрый путь — Pillow; при неудаче — ffmpeg из PATH (RGB → L)."""
    if not data or len(data) < 100:
        return None
    if Image is not None:
        try:
            img = Image.open(BytesIO(data))
            img.load()
            g = img.convert('L')
            return list(g.getdata()), g.width, g.height
        except Exception:
            pass
    if _FFMPEG_BIN is None:
        return None
    try:
        p = _subprocess.run(
            [_FFMPEG_BIN, '-hide_banner', '-loglevel', 'error',
             '-i', 'pipe:0', '-frames:v', '1', '-pix_fmt', 'gray',
             '-f', 'rawvideo', 'pipe:1'],
            input=data, capture_output=True, timeout=10)
        raw = p.stdout
        m = re.search(rb'(\d{2,5})x(\d{2,5})', p.stderr)
        if m and len(raw) >= int(m.group(1)) * int(m.group(2)):
            w, h = int(m.group(1)), int(m.group(2))
            return list(raw[:w * h]), w, h
    except Exception:
        pass
    return None


def _frame_stats(gray, w, h):
    """(средняя яркость downscaled-кадра, энтропия в бит/пиксель)."""
    step = max(1, int((w * h / 4096) ** 0.5))
    px = gray[::step]
    n = len(px)
    if n < 64:
        return 255.0, 8.0
    hist = [0] * 256
    for v in px:
        hist[v] += 1
    ent = -sum((c / n) * math.log2(c / n) for c in hist if c)
    mean = sum(px) / n
    return mean, ent


def _is_stub_frame(mean, ent):
    # примитивная картинка: почти чёрный/почти белый фон ИЛИ низкая энтропия
    return ent < STUB_ENTROPY_TH or mean < 16 or mean > 240


def _stub_verdict(frames):
    """По списку gray-кадров вынести вердикт: строка-причина или None."""
    stats = [_frame_stats(*g) for g in frames]
    if len(stats) == 1:
        mean, ent = stats[0]
        if (ent < STUB_ENTROPY_TH * 0.7) and (mean < 16 or mean > 240):
            return "видеозаставка в потоке (однотонный кадр)"
        return None
    stubs = sum(1 for m, e in stats if _is_stub_frame(m, e))
    diffs = [sum(abs(a - b) for a, b in zip(g1[0], g2[0])) / len(g1[0])
             for g1, g2 in zip(frames, frames[1:]) if len(g1[0]) == len(g2[0])]
    static = bool(diffs) and (sum(diffs) / len(diffs)) < STUB_STATIC_DIFF
    if stubs >= len(stats) * STUB_FRAME_RATIO and static:
        return (f"видеозаставка «нет доступа/подписка» "
                f"(статичный поток, {stubs}/{len(stats)} примитивных кадров)")
    return None


def _find_box(buf, name, start=0, end=None):
    """Найти MP4-бокс `name` (без вложенности) — вернуть (payload_start, payload_end)."""
    end = len(buf) if end is None else end
    i = start
    while i + 8 <= end:
        size = struct.unpack('>I', buf[i:i + 4])[0]
        typ = buf[i + 4:i + 8]
        if size < 8 or i + size > end:
            break
        if typ == name:
            return i + 8, i + size
        i += size
    return None


def _find_moov_in_sample(buf):
    """Есть ли полный moov-бокс внутри скачанного куска MP4."""
    top = 0
    n = len(buf)
    while top + 8 <= n:
        size = struct.unpack('>I', buf[top:top + 4])[0]
        typ = buf[top + 4:top + 8]
        if size < 8:
            break
        if typ == b'moov' and top + size <= n:
            return True
        top += size
    return False


def _ffmpeg_gray_frames(data, fmt=None, tail_data=b''):
    """Декодировать контейнер/ES-поток через ffmpeg -> [(gray, w, h), ...].
    Кадры берутся с шагом ~1 секунда (fps=1): заставка статична на всём
    протяжении, а живое видео между такими сэмплами заметно меняется.
    tail_data — докачанный хвост файла (для MP4 с moov в конце)."""
    if _FFMPEG_BIN is None or not data:
        return []
    blob = data + tail_data if tail_data else data
    cmd = [_FFMPEG_BIN, '-hide_banner', '-loglevel', 'info']
    if fmt:
        cmd += ['-f', fmt]
    cmd += ['-i', 'pipe:0', '-vf', 'fps=1', '-frames:v', str(STUB_MAX_FRAMES),
            '-pix_fmt', 'gray', '-f', 'rawvideo', 'pipe:1']
    try:
        p = _subprocess.run(cmd, input=blob, capture_output=True, timeout=25)
    except Exception:
        return []
    raw = p.stdout
    ms = re.findall(rb'(\d{2,5})x(\d{2,5})', p.stderr)
    if not ms or not raw:
        return []
    w, h = int(ms[-1][0]), int(ms[-1][1])
    fs = w * h
    if fs <= 0:
        return []
    frames = []
    for i in range(0, min(len(raw) // fs, n)):
        frames.append((raw[i * fs:(i + 1) * fs], w, h))
    return frames


def _jpeg_dims(buf):
    """Размеры JPEG по SOF-маркеру (без полной декодировки)."""
    i = 2
    while i + 9 < len(buf):
        if buf[i] != 0xFF:
            i += 1
            continue
        marker = buf[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg_len = struct.unpack('>H', buf[i + 2:i + 4])[0]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            hgt, wid = struct.unpack('>HH', buf[i + 5:i + 9])
            return wid, hgt
        i += 2 + seg_len
    return None


def _extract_embedded_jpegs(buf):
    """Запасной путь без ffmpeg: найти целые JPEG внутри дампа потока."""
    images = []
    for m in list(re.finditer(rb'\xff\xd8\xff[\xdb\xe0-\xef]', buf))[:STUB_MAX_FRAMES]:
        eoi = buf.find(b'\xff\xd9', m.start())
        if eoi > m.start() and eoi - m.start() > 500:
            images.append(buf[m.start():eoi + 2])
    return images


def detect_video_stub(url):
    """Скачать кусок потока и вынести вердикт по кадрам: строка-причина или None.
    Работает для MP4/MOV, MPEG-TS и «сырых» ES-потоков; без ffmpeg/Pillow
    деградирует до поиска встроенных JPEG + HTTP-уровня."""
    try:
        g = requests.get(url, timeout=SUBSCRIPTION_URL_TIMEOUT + 8, stream=True,
                         headers={"User-Agent": "Mozilla/5.0 (IPTVChecker/1.0)"},
                         allow_redirects=True)
        try:
            body = g.raw.read(STUB_SAMPLE_BYTES, decode_content=True)
        finally:
            g.close()
    except requests.RequestException:
        return None
    if len(body) < 20_000:
        return None
    frames = []
    is_ts = body[:1] == b'\x47' and len(body) > 188 * 40 and body[188:189] == b'\x47'
    if is_ts:
        frames = _ffmpeg_gray_frames(body, fmt='mpegts')
    elif body[4:8] in (b'ftyp', b'moov', b'stol', b'free'):
        frames = _ffmpeg_gray_frames(body, fmt='mp4')
        if not frames:
            for im in _extract_embedded_jpegs(body):
                fr = _decode_image_to_gray(im)
                if fr:
                    frames.append(fr)
    else:
        # неизвестный контейнер: пробуем как ES h264, затем встроенные JPEG
        frames = _ffmpeg_gray_frames(body, fmt='h264')
        if not frames:
            for im in _extract_embedded_jpegs(body):
                fr = _decode_image_to_gray(im)
                if fr:
                    frames.append(fr)
    if not frames:
        return None
    return _stub_verdict(frames)


def load_stream_check_cache():
    try:
        with open(STREAM_CHECK_CACHE_FILE, encoding='utf-8') as f:
            cache = json.load(f)
        if isinstance(cache, dict) and 'bad' in cache and 'ok' in cache:
            return cache
    except Exception:
        pass
    return {"bad": {}, "ok": {}}


def save_stream_check_cache(cache):
    tmp = STREAM_CHECK_CACHE_FILE + ".tmp"
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STREAM_CHECK_CACHE_FILE)

# --- Фильтр «подписочных» каналов -------------------------------------------------
# Многие IPTV-сервисы вставляют в плейлисты платные каналы-заглушки: вместо потока
# плеер показывает картинку/видео «Требуется подписка», «Нет доступа» и т.п.
# Такие каналы распознаются по имени и отфильтровываются ещё на этапе парсинга.
SUBSCRIPTION_PATTERNS = [
    r'нужна\s+подписка',
    r'необходима\s+подписка',
    r'требуется\s+подписка',
    r'нет\s+подписки',
    r'без\s+подписки',
    r'подключите\s+подписку',
    r'оформите\s+подписку',
    r'приобретите\s+подписку',
    r'купить\s+подписк',
    r'для\s+просмотра\s+нужна',
    r'доступ\s+по\s+подписке',
    r'подписка\s+(?:не\s+)?(?:истекла|закончилась|окончена)',
    r'trial\s+(?:version|channel|only)',
    r'subscription\s+(?:required|needed|only|expired)',
    r'requires?\s+(?:a\s+)?subscription',
    r'subscribe\s+to\s+watch',
    r'no\s+subscription',
    r'premium\s+(?:only|required|channel)',
    r'paid\s+(?:channel|access)',
]
SUBSCRIPTION_RE = re.compile('|'.join(SUBSCRIPTION_PATTERNS), re.IGNORECASE)

def is_subscription_channel(extinf_line):
    """Канал-заглушка «нужна подписка»? Проверяется имя канала и атрибуты tvg-*."""
    name = get_channel_name(extinf_line)
    if SUBSCRIPTION_RE.search(name):
        return True
    # Двойная страховка: подпись может прятаться в tvg-name/tvg-shift
    for attr in ('tvg-name', 'tvg-shift', 'tvg-subtitle'):
        m = re.search(attr + r'="([^"]*)"', extinf_line, re.IGNORECASE)
        if m and SUBSCRIPTION_RE.search(m.group(1)):
            return True
    return False

def purge_subscription_entries(path):
    """Разовая чистка готового M3U-файла: удаляет записи каналов-заглушек
    «нужна подписка» (строку #EXTINF и следующий за ней URL)."""
    try:
        with open(path, encoding='utf-8') as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return 0
    out, removed = [], 0
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith('#EXTINF:') and is_subscription_channel(line):
            removed += 1
            i += 1
            # удаляем непосредственно предшествующие служебные комментарии
            # (# LIVE-DAYS и т.п.), если они приклеены к этой строке EXTINF
            while out and out[-1].startswith('#') and not out[-1].startswith('#EXTM3U'):
                out.pop()
            # пропускаем вложенные комментарии после EXTINF и сам URL
            while i < len(lines) and (lines[i].startswith('#') or lines[i].startswith('http')):
                if lines[i].startswith('http'):
                    i += 1
                    break
                i += 1
            continue
        out.append(line)
        i += 1
    if removed:
        with open(path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(out) + '\n')
    return removed

def check_stream_http(url):
    """Дешёвый HTTP-уровень проверки: вердикт-строка или None."""
    from urllib.parse import urlparse
    host = urlparse(url).netloc.lower()
    if _bad_host_counter.get(host, 0) >= BAD_HOST_THRESHOLD:
        return f"хост {host} в чёрном списке (массовые заглушки)"
    hdrs = {"User-Agent": "Mozilla/5.0 (IPTVChecker/1.0)"}
    try:
        # сначала дешёвый HEAD — многие серверы сразу выдают код/тип контента
        r = requests.head(url, timeout=SUBSCRIPTION_URL_TIMEOUT,
                          headers=hdrs, allow_redirects=True)
        if r.status_code in SUBS_HTTP_CODES:
            _bad_host_counter[host] = _bad_host_counter.get(host, 0) + 1
            return f"HTTP {r.status_code} (доступ только по подписке)"
        ctype = (r.headers.get('Content-Type') or '').lower()
        if ctype.startswith(NON_VIDEO_TYPES):
            _bad_host_counter[host] = _bad_host_counter.get(host, 0) + 1
            return f"сервер отдаёт {ctype.split(';')[0]} вместо видеопотока"
        # HEAD не дал ответа (405/пустой тип) — уточняем коротким GET
        if r.status_code >= 400 or not ctype:
            g = requests.get(url, timeout=SUBSCRIPTION_URL_TIMEOUT,
                             headers=hdrs, stream=True, allow_redirects=True)
            try:
                if g.status_code in SUBS_HTTP_CODES:
                    _bad_host_counter[host] = _bad_host_counter.get(host, 0) + 1
                    return f"HTTP {g.status_code} (доступ только по подписке)"
                body = g.raw.read(SUBSCRIPTION_URL_MAX_BODY, decode_content=True)
            finally:
                g.close()
            head_text = body.decode('utf-8', 'ignore')
            if b'<html' in body[:2048].lower() and len(body) < SUBSCRIPTION_URL_MAX_BODY // 2:
                _bad_host_counter[host] = _bad_host_counter.get(host, 0) + 1
                return "HTML-страница вместо видеопотока"
            if SUBS_BODY_RE.search(head_text):
                _bad_host_counter[host] = _bad_host_counter.get(host, 0) + 1
                return "в ответе потока есть текст о подписке"
        else:
            _bad_host_counter.pop(host, None)  # хост отдал видео — сбрасываем счётчик
    except requests.RequestException:
        return None  # таймаут/сеть ≠ подписка: оставляем канал в покое
    return None


# Сколько потоков за прогон проверять «дорогим» кадровым анализом (CI-бюджет).
STUB_FRAME_CHECK_BUDGET = int(os.environ.get("STUB_FRAME_CHECK_BUDGET", "300"))
# Кадровый анализ пропускается для заведомо «живых» HLS-плейлистов (.m3u8):
# там контент — текстовые сегменты, а не видео; заставка видна только в .ts.
STUB_SKIP_EXTS = ('.m3u8', '.mpd')


def _needs_frame_check(url):
    u = url.lower().split('?', 1)[0]
    return not any(u.endswith(ext) for ext in STUB_SKIP_EXTS)


def check_stream_url_full(url, cache, budget_left):
    """Полная проверка одного URL с учётом кэша между прогонами.
    Возвращает (verdict_or_None, checked_bool)."""
    key = hashlib.md5(url.encode()).hexdigest()
    now = datetime.now(timezone.utc)
    today = now.strftime('%Y-%m-%d')
    cached_bad = cache["bad"].get(key)
    if cached_bad:
        return cached_bad.get("reason", "ранее помечен как заглушка"), False
    ok_ts = cache["ok"].get(key)
    fresh_ok = False
    if ok_ts:
        try:
            age = (now - datetime.strptime(ok_ts, '%Y-%m-%d')).days
            fresh_ok = age < STREAM_CHECK_CACHE_TTL_DAYS
        except ValueError:
            fresh_ok = False
    # 1) дешёвый HTTP-уровень
    verdict = check_stream_http(url)
    if verdict:
        cache["bad"][key] = {"url": url, "reason": verdict, "since": today}
        return verdict, True
    # 2) дорогой кадровый уровень — только если бюджет позволяет и URL похож на поток
    if STUB_VIDEO_CHECK and budget_left > 0 and _needs_frame_check(url):
        if not fresh_ok:
            verdict = detect_video_stub(url)
            if verdict:
                cache["bad"][key] = {"url": url, "reason": verdict, "since": today}
                return verdict, True
    cache["ok"][key] = today
    return None, True


def filter_subscription_streams(channels):
    """Параллельная живая проверка потоков: удаляет каналы, у которых вместо
    трансляции сервер отдаёт страницу/заставку «нужна подписка».
    Два уровня: HTTP-код/контент + анализ кадров самого видео."""
    if not SUBSCRIPTION_URL_CHECK or not channels:
        return channels
    cache = load_stream_check_cache()
    urls = sorted({ch["url"] for ch in channels})
    # URL уже известные «плохими» из кэша — без сети
    reasons = {}
    to_check = []
    for u in urls:
        key = hashlib.md5(u.encode()).hexdigest()
        if key in cache["bad"]:
            reasons[u] = cache["bad"][key].get("reason", "заглушка (кэш)")
        else:
            to_check.append(u)
    print(f"   🩺 Проверка потоков: {len(urls)} уникальных ссылок "
          f"(из кэша известно «заглушек»: {len(reasons)}, проверяем: {len(to_check)})...")
    budget = STUB_FRAME_CHECK_BUDGET
    checked = 0
    with ThreadPoolExecutor(max_workers=SUBSCRIPTION_URL_CONCURRENCY) as pool:
        futures = {}
        for u in to_check:
            take = budget > 0
            if take:
                budget -= 1
            futures[pool.submit(check_stream_url_full, u, cache, take)] = u
        for fut in as_completed(futures):
            verdict, was_checked = fut.result()
            if was_checked:
                checked += 1
            if verdict:
                reasons[futures[fut]] = verdict
    save_stream_check_cache(cache)
    if not reasons:
        print(f"   ✅ Все проверенные потоки отдают видео ({checked} проверено)")
        return channels
    kept = []
    shown = 0
    for ch in channels:
        if ch["url"] in reasons:
            if shown < 40:
                name = get_channel_name(ch["extinf"])[:40]
                print(f"   🚫 {name:40s} — {reasons[ch['url']]}")
                shown += 1
        else:
            kept.append(ch)
    bad_urls = {ch["url"] for ch in channels if ch["url"] in reasons}
    print(f"   💳 Удалено каналов с платной заглушкой/заставкой: {len(reasons)} "
          f"({len(bad_urls)} уникальных ссылок)")
    return kept

# ============================================================================
# СТАТИСТИКА ЖИЗНИ ТРАНСЛЯЦИЙ
# Каждому уникальному URL присваивается стабильный ID (md5). В JSON-базе
# хранится история наблюдений: когда ссылка появилась, исчезала, вернулась.
# "Период жизни" — это длительность текущей непрерывной жизни ссылки
# (от последней точки появления до сегодняшнего дня). Ссылка считается
# умершей, если отсутствует более STABLE_MISSING_TOLERANCE дней подряд;
# тогда её период жизни фиксируется в архиве историй.
# ============================================================================

def stream_id(url):
    """Стабильный идентификатор трансляции по URL"""
    return hashlib.md5(url.encode("utf-8")).hexdigest()

def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def _days_between(d1, d2):
    a = datetime.strptime(d1, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    b = datetime.strptime(d2, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return (b - a).days

def load_stable_db():
    if os.path.exists(STABLE_DB_FILE):
        try:
            with open(STABLE_DB_FILE, "r", encoding="utf-8") as f:
                db = json.load(f)
            if isinstance(db, dict) and isinstance(db.get("streams"), dict):
                return db
        except (json.JSONDecodeError, OSError) as e:
            print(f"⚠️ Не удалось прочитать {STABLE_DB_FILE} ({e}), начинаю новую базу")
    return {"updated": "", "streams": {}}

def update_stable_db(db, channels):
    """Обновляет базу по списку каналов, актуальных на сегодня.

    channels: список словарей {"url": ..., "extinf": ...}
    Возвращает сводку для отчёта.
    """
    today = _today()
    streams = db["streams"]
    seen_ids = set()
    new_count = resurrected_count = 0

    for ch in channels:
        url = ch["url"]
        sid = stream_id(url)
        seen_ids.add(sid)
        rec = streams.get(sid)
        name = get_channel_name(ch["extinf"])
        category = get_category_for_sort(ch["extinf"])

        if rec is None:
            # Новая трансляция: жизнь началась сегодня
            streams[sid] = {
                "url": url,
                "name": name,
                "category": category,
                "first_seen": today,
                "current_birth": today,     # начало текущего непрерывного периода жизни
                "last_seen": today,
                "last_miss_start": today,   # потенциальное начало перерыва (если завтра пропадёт)
                "alive": True,
                "appearances": 1,           # сколько раз ссылка появлялась (с учётом возвращений)
                "histories": [],            # завершённые периоды жизни: [{start, end, days}]
            }
            new_count += 1
        else:
            rec["name"] = name
            rec["category"] = category
            if not rec.get("alive", False):
                # Ссылка вернулась после смерти — начинается новый период жизни
                rec["alive"] = True
                rec["current_birth"] = today
                rec["appearances"] = rec.get("appearances", 0) + 1
                resurrected_count += 1
            # Короткие перерывы (в пределах допуска) не прерывают период жизни,
            # поэтому current_birth при возврате в рамках допуска НЕ сбрасывается
            rec["last_seen"] = today
            rec["last_miss_start"] = today

    # Обработка отсутствующих сегодня ссылок
    dead_now = []
    for sid, rec in streams.items():
        if sid in seen_ids or not rec.get("alive", False):
            continue
        last_visible = rec.get("last_miss_start") or rec.get("last_seen", today)
        gap_days = _days_between(last_visible, today)
        if gap_days > STABLE_MISSING_TOLERANCE:
            # Пропадание дольше допуска — трансляция мертва, период жизни фиксируется
            lifetime = _days_between(rec["current_birth"], rec["last_seen"]) + 1
            rec["histories"].append({
                "start": rec["current_birth"],
                "end": rec["last_seen"],
                "days": lifetime,
            })
            rec["alive"] = False
            dead_now.append((rec.get("name", "?"), lifetime))

    db["updated"] = today
    alive_now = [r for r in streams.values() if r.get("alive")]
    return {
        "total": len(streams),
        "new": new_count,
        "dead_now": dead_now,
        "resurrected": resurrected_count,
        "alive": len(alive_now),
    }

def current_lifespan_days(rec):
    """Текущий период жизни активной ссылки (в днях)"""
    return _days_between(rec["current_birth"], _today()) + 1

def save_stable_db(db):
    with open(STABLE_DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    print(f"💾 Статистика жизни трансляций сохранена в {STABLE_DB_FILE}")

def write_stable_playlist(db, channels_by_url):
    """Формирует stable.m3u: только ссылки, живущие без перерывов >= STABLE_DAYS_MIN дней.

    Период жизни указывается обязательно — в tvg-lifetime (в днях) и в комментарии
    к каждой записи.
    """
    entries = []
    for rec in db["streams"].values():
        if not rec.get("alive"):
            continue
        lifespan = current_lifespan_days(rec)
        if lifespan < STABLE_DAYS_MIN:
            continue
        extinf = channels_by_url.get(rec["url"])
        if not extinf:
            # Ссылка жива, но сегодня не получена из источников (в пределах допуска) —
            # собираем строку EXTINF из сохранённых данных
            safe_id = re.sub(r'[^a-zа-я0-9]', '', rec["name"].lower())
            extinf = (f'#EXTINF:-1 tvg-id="{safe_id}" '
                      f'tvg-name="{rec["name"]}" group-title="{rec["category"]}",{rec["name"]}')
        entries.append((lifespan, rec, extinf))

    entries.sort(key=lambda x: (-x[0], x[1]["name"].lower()))

    today = _today()
    with open(STABLE_PLAYLIST_FILE, "w", encoding="utf-8") as f:
        f.write(f'#EXTM3U url-tvg="{EPG_URL}"\n')
        f.write(f"# Стабильные трансляции: период жизни (tvg-lifetime) не менее "
                f"{STABLE_DAYS_MIN} дней без перерывов.\n")
        f.write(f"# Обновлено: {today}. Источник данных: {STABLE_DB_FILE}\n")
        for lifespan, rec, extinf in entries:
            f.write(f"# LIVE-DAYS: {lifespan} | SINCE: {rec['current_birth']} | "
                    f"APPEARANCES: {rec.get('appearances', 1)}\n")
            # Добавляем атрибут tvg-lifetime в строку EXTINF
            if 'tvg-lifetime=' in extinf.lower():
                extinf = re.sub(r'tvg-lifetime="[^"]*"', f'tvg-lifetime="{lifespan}"', extinf)
            else:
                # Вставляем перед первым незаключённым в кавычки запятым (разделитель attrs/name)
                m = re.search(r'#EXTINF:[^,]*,', extinf)
                if m:
                    pos = m.end() - 1
                    extinf = extinf[:pos] + f' tvg-lifetime="{lifespan}"' + extinf[pos:]
            f.write(extinf + "\n")
            f.write(rec["url"] + "\n")
    print(f"📄 Сохранено {len(entries)} стабильных трансляций "
          f"(жизнь >= {STABLE_DAYS_MIN} дней) в {STABLE_PLAYLIST_FILE}")

def print_stability_report(db):
    """Печатает сводную статистику о жизни трансляций"""
    alive = [r for r in db["streams"].values() if r.get("alive")]
    dead = [r for r in db["streams"].values() if not r.get("alive")]
    if not alive and not dead:
        return
    lifespans = sorted((_days_between(r["current_birth"], r["last_seen"]) + 1 for r in alive), reverse=True)
    print("\n 📊 Статистика жизни трансляций:")
    print(f"   Активных ссылок в базе: {len(alive)}, мёртвых (в архиве): {len(dead)}")
    if lifespans:
        median = lifespans[len(lifespans) // 2]
        buckets = [(30, ">= 30 дней"), (90, ">= 90 дней"), (180, ">= 180 дней"), (365, ">= 365 дней")]
        for days_min, label in buckets:
            n = sum(1 for l in lifespans if l >= days_min)
            print(f"   Период жизни {label}: {n}")
        print(f"   Медианный период жизни: {median} дн., максимальный: {lifespans[0]} дн.")
    if alive:
        top = sorted(alive, key=current_lifespan_days, reverse=True)[:5]
        print("   Топ-5 самых долгоживущих ссылок:")
        for r in top:
            print(f"     {current_lifespan_days(r):4d} дн. | {r['name'][:40]:40s} | с {r['current_birth']}")

def parse_m3u(url, filter_russian=False, epg_categories=None):
    print(f" Загрузка: {url} {'(с фильтрацией)' if filter_russian else '(без фильтрации)'}")
    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        # Надёжная декодировка: серверы отдают .m3u с content-type audio/mpegurl,
        # для которого requests не знает кодировки и берёт latin-1 -> кракозябры.
        # Стандарт IPTV-плейлистов — UTF-8, поэтому сначала пробуем utf-8 строго,
        # при ошибке — apparent_encoding, в самом крайнем случае utf-8 с заменой.
        if not response.encoding or response.encoding.lower() in ("iso-8859-1", "latin-1"):
            for enc in ("utf-8", (response.apparent_encoding or "utf-8"), "utf-8"):
                try:
                    response.content.decode(enc)
                    response.encoding = enc
                    break
                except (UnicodeDecodeError, LookupError):
                    continue
            else:
                response.encoding = "utf-8"
                response._content = response.content  # text использует errors='replace'
    except requests.RequestException as e:
        print(f"❌ Ошибка загрузки {url}: {e}")
        return []
    
    if epg_categories is None:
        epg_categories = {}
        
    channels = []
    lines = response.text.splitlines()
    current_extinf = ""
    skipped_subs = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF:"):
            current_extinf = line
        elif line.startswith("http"):
            # Пропускаем заглушки
            if is_stub_url(line):
                continue
            # Пропускаем каналы-заглушки «нужна подписка»
            if current_extinf and is_subscription_channel(current_extinf):
                skipped_subs += 1
                current_extinf = ""
                continue
            if not filter_russian or is_russian_channel(current_extinf):
                name = get_channel_name(current_extinf)
                fixed_extinf = fix_extinf(current_extinf, name, epg_categories)
                channels.append({"extinf": fixed_extinf, "url": line})
            current_extinf = ""
    if skipped_subs:
        print(f"   🚫 Отфильтровано каналов «нужна подписка»: {skipped_subs}")
    print(f"   ✅ Найдено каналов: {len(channels)}")
    return channels

def get_category_for_sort(extinf_line):
    match = re.search(r'group-title="([^"]*)"', extinf_line)
    return match.group(1) if match else "📦 Разное"

def category_sort_key(category):
    """Сортировка категорий по заданному порядку; неизвестные — в конец, по алфавиту"""
    try:
        return (0, CATEGORY_ORDER.index(category), "")
    except ValueError:
        return (1, 0, category)

def main():
    # Разовая чистка уже существующих плейлистов от «подписочных» заглушек
    for pf in (OUTPUT_FILE, STABLE_PLAYLIST_FILE):
        n = purge_subscription_entries(pf)
        if n:
            print(f"🧹 {pf}: удалено записей «нужна подписка»: {n}")

    # Загружаем категории из EPG
    print("\n📚 Загрузка категорий из EPG...")
    epg_categories = load_epg_categories()
    
    gh_urls = find_github_playlists()
    shirtv_urls = find_shirtv_playlists()
    update_sources_file(gh_urls, shirtv_urls)
    sources = load_sources(SOURCES_FILE)
    if not sources:
        print("🛑 Нет источников для обработки. Завершаю работу.")
        return
    print(f"\n📋 Найдено {len(sources)} источников в {SOURCES_FILE}\n")
    
    all_channels = []
    for src in sources:
        channels = parse_m3u(src["url"], filter_russian=src["filter_russian"], epg_categories=epg_categories)
        all_channels.extend(channels)
    print(f"\n Всего каналов до обработки: {len(all_channels)}")
    
    # Живая проверка потоков: каналы, у которых вместо трансляции сервер
    # отдаёт страницу «нужна подписка» (под обычным именем канала такое не
    # вычислить по тексту M3U — только по содержимому HTTP-потока).
    all_channels = filter_subscription_streams(all_channels)

    # ДЕДУПЛИКАЦИЯ ПО ИМЕНИ КАНАЛА
    seen_names = set()
    unique_channels = []
    
    for ch in all_channels:
        name = get_channel_name(ch['extinf'])
        norm_name = normalize_channel_name(name)
        
        if norm_name not in seen_names:
            seen_names.add(norm_name)
            unique_channels.append(ch)
    
    # Сортировка
    unique_channels.sort(key=lambda x: (category_sort_key(get_category_for_sort(x['extinf'])), get_channel_name(x['extinf']).lower()))
    
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

    # ===== Статистика жизни трансляций =====
    print("\n📈 Обновление статистики жизни трансляций...")
    db = load_stable_db()
    summary = update_stable_db(db, all_channels)
    save_stable_db(db)

    channels_by_url = {}
    for ch in all_channels:
        channels_by_url.setdefault(ch["url"], ch["extinf"])
    write_stable_playlist(db, channels_by_url)
    print_stability_report(db)

    if summary["dead_now"]:
        print(f"\n💀 Зафиксирована смерть {len(summary['dead_now'])} ссылок "
              f"(отсутствуют > {STABLE_MISSING_TOLERANCE} дней):")
        for name, lifetime in summary["dead_now"][:10]:
            print(f"   {name[:45]:45s} | период жизни: {lifetime} дн.")
    if summary["new"]:
        print(f"🆕 Новых трансляций сегодня: {summary['new']}")
    if summary["resurrected"]:
        print(f"♻️ Вернувшихся после смерти: {summary['resurrected']}")

    # Сводка для лога GitHub Actions
    gh_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if gh_summary:
        alive = [r for r in db["streams"].values() if r.get("alive")]
        stable_n = sum(1 for r in alive if current_lifespan_days(r) >= STABLE_DAYS_MIN)
        try:
            with open(gh_summary, "a", encoding="utf-8") as f:
                f.write("## 📈 Статистика жизни трансляций\n\n")
                f.write("| Показатель | Значение |\n|---|---|\n")
                f.write(f"| Ссылок в базе (всего) | {summary['total']} |\n")
                f.write(f"| Активных | {summary['alive']} |\n")
                f.write(f"| Стабильных (жизнь ≥ {STABLE_DAYS_MIN} дн.) | {stable_n} |\n")
                f.write(f"| Новых сегодня | {summary['new']} |\n")
                f.write(f"| Умерших сегодня | {len(summary['dead_now'])} |\n")
                f.write(f"| Вернувшихся после смерти | {summary['resurrected']} |\n")
                if alive:
                    best = max(alive, key=current_lifespan_days)
                    f.write(f"| Рекорд периода жизни | {current_lifespan_days(best)} дн. "
                            f"({best['name']}) |\n")
                f.write(f"\nСтабильные ссылки: `{STABLE_PLAYLIST_FILE}` · "
                        f"Полная база: `{STABLE_DB_FILE}`\n")
                f.write("\n🎛️ **Управление всеми списками** — интерактивный дашборд: "
                        "<https://raw.githubusercontent.com/"
                        f"{os.environ.get('GITHUB_REPOSITORY', 'OWNER/REPO')}/main/dashboard.html>`__\n")
        except OSError:
            pass

if __name__ == "__main__":
    main()