import requests
import os
import re

# === НАСТРОЙКИ ===
SOURCES_FILE = "play.list"
OUTPUT_FILE = "playlist.m3u"

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
HEADERS = {"Authorization": f"token {GITHUB_TOKEN}"} if GITHUB_TOKEN else {}

def normalize_url(url):
    return url.strip().lower().rstrip('/')

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
        if any(c in ['ru', 'rus'] for c in countries):
            return True
    return False

def get_channel_name(extinf_line):
    match = re.search(r',(.*?)$', extinf_line)
    return match.group(1).strip().lower() if match else ""

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
    params = {
        "q": "iptv russian OR iptv ru OR iptv россия in:name,description,topics",
        "sort": "stars", "order": "desc", "per_page": 20 
    }
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
                channels.append({"extinf": current_extinf, "url": line})
            current_extinf = ""
    print(f"   ✅ Найдено каналов: {len(channels)}")
    return channels

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
    seen_urls = set()
    unique_channels = []
    for ch in all_channels:
        norm_url = normalize_url(ch['url'])
        if norm_url not in seen_urls:
            seen_urls.add(norm_url)
            unique_channels.append(ch)
    unique_channels.sort(key=lambda x: get_channel_name(x['extinf']))
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write('#EXTM3U url-tvg="https://iptv-org.github.io/epg/languages/rus.epg.xml"\n')
        for ch in unique_channels:
            f.write(f"{ch['extinf']}\n")
            f.write(f"{ch['url']}\n")
    removed_count = len(all_channels) - len(unique_channels)
    print(f"\n✅ Успешно сохранено {len(unique_channels)} уникальных каналов в {OUTPUT_FILE}")
    print(f"🗑 Удалено дубликатов (по URL): {removed_count}")

if __name__ == "__main__":
    main()