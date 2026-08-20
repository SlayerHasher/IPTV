import requests
import os
import re
import json
import gzip
import xml.etree.ElementTree as ET
from io import BytesIO

SOURCES_FILE = "play.list"
OUTPUT_FILE = "playlist.m3u"
EPG_URL = "https://iptvx.one/epg/epg.xml.gz"

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
HEADERS = {"Authorization": f"token {GITHUB_TOKEN}"} if GITHUB_TOKEN else {}

# Маппинг категорий из EPG в наши
EPC_CATEGORY_MAP = {
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
    'religion': ' Религиозные',
    'auto': '🚗 Авто и Мото',
    'moto': '🚗 Авто и Мото',
    'food': '🍳 Еда и Кулинария',
    'cooking': '🍳 Еда и Кулинария',
    'shopping': '🛍 Шопинг',
    'travel': '🌍 Познавательные',
    'science': '🌍 Познавательные',
    'history': '🌍 Познавательные',
    'nature': '🌿 Природа, Охота, Рыбалка и Дача',
    'hunting': ' Природа, Охота, Рыбалка и Дача',
    'fishing': ' Природа, Охота, Рыбалка и Дача',
    'health': ' Здоровье и Медицина',
    'medical': '💊 Здоровье и Медицина',
    'business': '📰 Новости',
    'finance': '📰 Новости',
    'regional': '🏛 Региональные каналы',
    'local': '🏛 Региональные каналы',
}

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
    if cat in EPC_CATEGORY_MAP:
        return EPC_CATEGORY_MAP[cat]
    
    # Частичное совпадение
    for key, value in EPC_CATEGORY_MAP.items():
        if key in cat:
            return value
    
    # Если не нашли - возвращаем оригинал
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
        return " Музыка"
    if any(x in name_lower for x in ['discovery', 'national', 'история', 'history', 'наука', 'travel']):
        return "🌍 Познавательные"
    if any(x in name_lower for x in ['охота', 'рыбалка', 'дача', 'усадьба']):
        return "🌿 Природа, Охота, Рыбалка и Дача"
    if any(x in name_lower for x in ['кухня', 'еда', 'food', 'кулинар']):
        return " Еда и Кулинария"
    if any(x in name_lower for x in ['авто', 'auto', 'мото']):
        return " Авто и Мото"
    if any(x in name_lower for x in ['ржд', 'транспорт']):
        return "🚂 Транспорт"
    if any(x in name_lower for x in ['спб', 'регион', 'область', 'кубань', 'сургут', 'самара']):
        return "🏛 Региональные каналы"
    if any(x in name_lower for x in ['первый', 'россия', 'нтв', 'тнт', 'стс']):
        return " Федеральные и Общие"
    
    return "📦 Разное"

def fix_extinf(extinf_line, channel_name, epg_categories):
    safe_id = re.sub(r'[^a-zа-я0-9]', '', channel_name.lower())
    tvg_id = get_tvg_id(extinf_line)
    
    # Сначала ищем в EPG
    category = get_category_from_epg(channel_name, tvg_id, epg_categories)
    
    # Если не нашли - fallback
    if not category:
        category = get_category_fallback(channel_name)
    
    # Добавляем tvg-id
    if 'tvg-id=' not in extinf_line.lower():
        extinf_line = extinf_line.replace('#EXTINF:', f'#EXTINF: tvg-id="{safe_id}"', 1)
    
    # Добавляем tvg-name
    if 'tvg-name=' not in extinf_line.lower():
        extinf_line = extinf_line.replace('#EXTINF:', f'#EXTINF: tvg-name="{channel_name}"', 1)
    
    # Заменяем group-title
    if 'group-title=' in extinf_line.lower():
        extinf_line = re.sub(r'group-title="[^"]*"', f'group-title="{category}"', extinf_line)
    else:
        extinf_line = re.sub(r'(#EXTINF:[^,]*,)', f'\\1 group-title="{category}" ', extinf_line)
        if 'group-title=' not in extinf_line:
            extinf_line = extinf_line.replace('#EXTINF:', f'#EXTINF: group-title="{category}" ', 1)
    
    return extinf_line

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