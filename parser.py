import requests
import os
import re
import json
import gzip
import hashlib
import xml.etree.ElementTree as ET
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
    except requests.RequestException as e:
        print(f"❌ Ошибка загрузки {url}: {e}")
        return []
    
    if epg_categories is None:
        epg_categories = {}
        
    channels = []
    lines = response.text.splitlines()
    current_extinf = ""
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
            if not filter_russian or is_russian_channel(current_extinf):
                name = get_channel_name(current_extinf)
                fixed_extinf = fix_extinf(current_extinf, name, epg_categories)
                channels.append({"extinf": fixed_extinf, "url": line})
            current_extinf = ""
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
    # Загружаем категории из EPG
    print("\n📚 Загрузка категорий из EPG...")
    epg_categories = load_epg_categories()
    
    gh_urls = find_github_playlists()
    update_sources_file(gh_urls)
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
        except OSError:
            pass

if __name__ == "__main__":
    main()