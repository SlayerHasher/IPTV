import requests
import os
import re

# === НАСТРОЙКИ ===
SOURCES_FILE = "play.list"
OUTPUT_FILE = "playlist.m3u"
EPG_URL = "https://iptvx.one/epg/epg.xml.gz"

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
HEADERS = {"Authorization": f"token {GITHUB_TOKEN}"} if GITHUB_TOKEN else {}

def normalize_url(url):
    return url.strip().lower().rstrip('/')

def normalize_channel_name(name):
    """Приводит название канала к нормализованному виду для сравнения"""
    # Убираем пробелы, спецсимволы, приводим к нижнему регистру
    name = name.lower().strip()
    name = re.sub(r'[^a-zа-я0-9]', '', name)
    return name

def is_russian_channel(extinf_line):
    line_lower = extinf_line.lower()
    lang_match = re.search(r'tvg-language="([^"]*)"', line_lower)
    if lang_match:
        languages = [lang.strip() for lang in lang_match.group(1).split(';')]
        if any(lang in ['russian', 'rus', 'ru'] for lang in languages):
            return True
    country_match = re.search(r'tvg-country="([^"]*)"', line_lower)
    if country_match:
        countries = [c.strip() for c in country_match.group(1).split(';')]
        if any(c in ['ru', 'rus', 'by', 'kz', 'ua'] for c in countries):
            return True
    return False

def get_channel_name(extinf_line):
    match = re.search(r',(.*?)$', extinf_line)
    return match.group(1).strip() if match else "Unknown"

def normalize_category(name, existing_group=""):
    """Анализирует имя канала и возвращает строго стандартизированную категорию"""
    text = (name + " " + existing_group).lower()
    
    if any(w in text for w in ['4k', '2160p', 'ultra hd', 'uhd']): return "📺 4K / Ultra HD"
    elif any(w in text for w in ['авто', 'auto', 'мото', 'за рулем']): return "🚗 Авто и Мото"
    elif any(w in text for w in ['кухня', 'еда', 'food', 'кулинар', 'рецепт']): return "🍳 Еда и Кулинария"
    elif any(w in text for w in ['шопинг', 'магазин', 'tv shop', 'покупки', 'shopping']): return "🛍 Шопинг"
    elif any(w in text for w in ['охота', 'рыбалка', 'загородный', 'дача', 'усадьба', 'хобби']): return "🌿 Природа и Хобби"
    elif any(w in text for w in ['кино', 'movie', 'film', 'сериал', 'premiere', 'tv1000', 'кинопоказ', 'кинохит', 'ilove', 'indigo', 'кинокомедия']): return " Кино и Сериалы"
    elif any(w in text for w in ['спорт', 'sport', 'матч', 'match', 'футбол', 'бокс', 'кхл', 'ufc', 'боец', 'extreme']): return "⚽ Спорт"
    elif any(w in text for w in ['новости', 'news', '24', 'дождь', 'звезда', 'мир', 'vesti', 'euronews']): return "📰 Новости"
    elif any(w in text for w in ['детский', 'kids', 'карусель', 'мульт', 'nickelodeon', 'disney', 'gulli', 'tiiji', 'мультимузыка']): return "🧸 Детские"
    elif any(w in text for w in ['музыка', 'music', 'mtv', 'мюзик', 'bridge', 'ru.tv', 'телекафе', 'viva']): return "🎵 Музыка"
    elif any(w in text for w in ['юмор', 'comedy', 'индия', 'развлечен', 'entertainment', 'тнт4', 'суббота']): return "😂 Юмор и Развлечения"
    elif any(w in text for w in ['познавательный', 'doc', 'discovery', 'national', 'история', 'наука', 'travel', 'viasat']): return "🌍 Познавательные"
    elif any(w in text for w in ['религия', 'religion', 'спас', 'союз', 'вера', 'будем']): return "🙏 Религиозные"
    elif any(w in text for w in ['регион', 'спб', 'spb', 'кубань', 'катод', 'tv21', 'архыз', 'ямал', 'брянск', 'тула', 'самара']): return "🏛 Региональные"
    elif any(w in text for w in ['первый', 'россия', 'нтв', 'тнт', 'стс', 'рен', 'тв3', 'пятница', 'че', 'домашний', 'звезда', 'общественное', 'отр']): return "📺 Федеральные и Общие"
    else: return "📦 Разное"

def fix_extinf(extinf_line, channel_name):
    """Гарантирует наличие tvg-id, tvg-name и ПРАВИЛЬНОГО group-title"""
    safe_id = re.sub(r'[^a-zа-я0-9]', '', channel_name.lower())
    
    group_match = re.search(r'group-title="([^"]*)"', extinf_line)
    current_group = group_match.group(1) if group_match else ""
    new_category = normalize_category(channel_name, current_group)
    
    if 'tvg-id=' not in extinf_line.lower():
        extinf_line = extinf_line.replace('#EXTINF:', f'#EXTINF: tvg-id="{safe_id}"', 1)
        
    if 'tvg-name=' not in extinf_line.lower():
        extinf_line = extinf_line.replace('#EXTINF:', f'#EXTINF: tvg-name="{channel_name}"', 1)
        
    if group_match:
        extinf_line = re.sub(r'group-title="[^"]*"', f'group-title="{new_category}"', extinf_line)
    else:
        extinf_line = re.sub(r'(#EXTINF:[^,]*,)', f'\\1 group-title="{new_category}" ', extinf_line)
        if 'group-title=' not in extinf_line:
            extinf_line = extinf_line.replace('#EXTINF:', f'#EXTINF: group-title="{new_category}" ', 1)
            
    return extinf_line

def get_existing_urls():
    urls = set()
    if not os.path.exists(SOURCES_FILE): return urls
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
        if repo.get("fork", False) or repo.get("stargazers_count", 0) < 5: continue
        repo_name = repo["full_name"]
        default_branch = repo.get("default_branch", "main")
        tree_url = f"https://api.github.com/repos/{repo_name}/git/trees/{default_branch}?recursive=1"
        try:
            tree_resp = requests.get(tree_url, headers=HEADERS, timeout=15)
            if tree_resp.status_code != 200: continue
            tree_data = tree_resp.json()
        except Exception: continue
        if "tree" not in tree_data: continue
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
    if not os.path.exists(filepath): return sources
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

def parse_m3u(url, filter_russian=False):
    print(f"📥 Загрузка: {url} {'(с фильтрацией)' if filter_russian else '(без фильтрации)'}")
    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
    except requests.RequestException as e:
        print(f"❌ Ошибка загрузки {url}: {e}")
        return []
    channels = []
    lines = response.text.splitlines()
    current_extinf = ""
    for line in lines:
        line = line.strip()
        if not line: continue
        if line.startswith("#EXTINF:"):
            current_extinf = line
        elif line.startswith("http"):
            if not filter_russian or is_russian_channel(current_extinf):
                name = get_channel_name(current_extinf)
                fixed_extinf = fix_extinf(current_extinf, name)
                channels.append({"extinf": fixed_extinf, "url": line})
            current_extinf = ""
    print(f"   ✅ Найдено каналов: {len(channels)}")
    return channels

def get_category_for_sort(extinf_line):
    match = re.search(r'group-title="([^"]*)"', extinf_line)
    return match.group(1) if match else "📦 Разное"

def main():
    gh_urls = find_github_playlists()
    update_sources_file(gh_urls)
    sources = load_sources(SOURCES_FILE)
    if not sources:
        print("🛑 Нет источников для обработки. Завершаю работу.")
        return
    print(f"\n📋 Найдено {len(sources)} источников в {SOURCES_FILE}\n")
    all_channels = []
    for src in sources:
        channels = parse_m3u(src["url"], filter_russian=src["filter_russian"])
        all_channels.extend(channels)
    print(f"\n📊 Всего каналов до обработки: {len(all_channels)}")
    
    # ДЕДУПЛИКАЦИЯ ПО ИМЕНИ КАНАЛА
    seen_names = set()
    unique_channels = []
    
    for ch in all_channels:
        name = get_channel_name(ch['extinf'])
        norm_name = normalize_channel_name(name)
        
        # Если канал с таким именем еще не встречался, добавляем его
        if norm_name not in seen_names:
            seen_names.add(norm_name)
            unique_channels.append(ch)
            
    # Сортировка: сначала по категории, потом по имени канала
    unique_channels.sort(key=lambda x: (get_category_for_sort(x['extinf']), get_channel_name(x['extinf']).lower()))
    
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(f'#EXTM3U url-tvg="{EPG_URL}"\n')
        for ch in unique_channels:
            f.write(f"{ch['extinf']}\n")
            f.write(f"{ch['url']}\n")
            
    removed_count = len(all_channels) - len(unique_channels)
    print(f"\n✅ Успешно сохранено {len(unique_channels)} уникальных каналов в {OUTPUT_FILE}")
    print(f" Удалено дубликатов (по имени): {removed_count}")

if __name__ == "__main__":
    main()