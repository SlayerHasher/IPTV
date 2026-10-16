"""Юнит-тесты iptv_parser (запуск: python -m unittest discover tests)."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import iptv_parser as p


class TestChannelName(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(
            p.get_channel_name('#EXTINF:-1 tvg-id="a",Первый канал'),
            "Первый канал")

    def test_comma_in_attributes(self):
        # запятая внутри кавычек — не разделитель
        line = '#EXTINF:-1 group-title="Спорт, HD",Setanta Sports'
        self.assertEqual(p.get_channel_name(line), "Setanta Sports")

    def test_broken_quotes_fallback(self):
        line = '#EXTINF: tvg-name="Unknown"-1 group-title="X",Нормальное имя'
        self.assertEqual(p.get_channel_name(line), "Нормальное имя")

    def test_no_comma(self):
        self.assertEqual(p.get_channel_name("#EXTINF:-1 без запятой"), "Unknown")


class TestFixExtinf(unittest.TestCase):
    def test_valid_format(self):
        src = '#EXTINF:-1 tvg-id="p" tvg-logo="http://x/l.png",Первый'
        out = p.fix_extinf(src, "Первый", {})
        self.assertTrue(out.startswith("#EXTINF:-1 "), out)
        self.assertTrue(out.endswith(",Первый"), out)
        self.assertIn('tvg-logo="http://x/l.png"', out)
        self.assertIn('group-title=', out)

    def test_idempotent(self):
        src = '#EXTINF:-1 tvg-id="p",Первый'
        once = p.fix_extinf(src, "Первый", {})
        twice = p.fix_extinf(once, p.get_channel_name(once), {})
        self.assertEqual(once, twice)

    def test_bad_source_order_fixed(self):
        # источник отдаёт 'атрибуты,-1,имя' — чиним в '-1 атрибуты,имя'
        src = '#EXTINF:tvg-id="x",-1 Some Channel'
        out = p.fix_extinf(src, "Some Channel", {})
        self.assertTrue(out.startswith("#EXTINF:-1 "), out)


class TestRussianFilter(unittest.TestCase):
    def test_case_insensitive_attrs(self):
        self.assertTrue(p.is_russian_channel('#EXTINF:-1 TVG-Language="Russian",X'))
        self.assertTrue(p.is_russian_channel('#EXTINF:-1 tvg-country="ru",X'))
        self.assertTrue(p.is_russian_channel('#EXTINF:-1 tvg-Country="RUS",X'))

    def test_non_russian(self):
        self.assertFalse(p.is_russian_channel('#EXTINF:-1 tvg-country="DE",X'))
        self.assertFalse(p.is_russian_channel('#EXTINF:-1 tvg-country="BY",X'))


class TestCategories(unittest.TestCase):
    def test_map_exact_and_partial(self):
        self.assertEqual(p.map_category("movies"), "🎬 Кино и Сериалы")
        self.assertEqual(p.map_category("Documentary & Science"), "🌍 Познавательные")

    def test_longer_key_wins(self):
        # 'documentary' не должен перекрываться ключом 'doc' в другую сторону
        self.assertEqual(p.map_category("documentary"), p.map_category("doc"))

    def test_fallback_has_emoji(self):
        cat = p.get_category_fallback("Какой-то Музыкальный Канал")
        self.assertEqual(cat, "🎵 Музыка")
        self.assertFalse(cat.startswith(" "))

    def test_all_categories_normalized(self):
        # ни одна категория не должна начинаться с пробела (потерянный эмодзи)
        for v in p.EPG_CATEGORY_MAP.values():
            self.assertFalse(v.startswith(" "), v)
        names = ["кино", "спорт", "новости", "детский", "муз", "охота",
                 "кухня", "авто", "ржд", "спб", "первый"]
        for n in names:
            self.assertFalse(p.get_category_fallback(n).startswith(" "), n)


class TestTokenFilter(unittest.TestCase):
    def test_expired_tokens_detected(self):
        bad = [
            "http://h/iptv/ch438.php?wmsAuthSign=c2VydmVyX3RpbWU9MTAv",
            "https://h/playlist/x/type.m3u8?token=abc123",
            "http://h/locken_2.php?id=141863080&key=0d5c94b483d00d2f67c5fa8759134cf5",
            "http://h/index-russia.html?md5=YKxLyq7PQFYsdyCjGbw&expires=1760606984",
        ]
        for u in bad:
            self.assertTrue(p.is_expired_token_url(u), u)

    def test_clean_urls_pass(self):
        good = [
            "https://cdn-live.idmedia.work/online/100321/playlist.m3u8",
            "http://ad-hls-rentri.cdnvideo.com/rt1_online/avto_fm/playlist.m3u8",
            "http://79.142.16.43:8080/dvr-mobilka-1231/index.m3u8",
            "http://live-otac-prod.cdn.arcolink.host/66339/live.m3u8",
        ]
        for u in good:
            self.assertFalse(p.is_expired_token_url(u), u)


class TestSourcesFile(unittest.TestCase):
    AUTO = "# --- AUTO SOURCES (GitHub Search) ---"

    def _write(self, lines):
        fd, path = tempfile.mkstemp(text=True)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        self.addCleanup(os.unlink, path)
        return path

    def test_load_splits_manual_and_auto(self):
        path = self._write(["# comment", "https://manual/a.m3u",
                            self.AUTO, "https://auto/b.m3u"])
        srcs = p.load_sources(path)
        self.assertEqual(srcs[0], {"url": "https://manual/a.m3u", "filter_russian": False})
        self.assertEqual(srcs[1], {"url": "https://auto/b.m3u", "filter_russian": True})

    def test_max_auto_limit(self):
        old = p.MAX_AUTO_SOURCES
        p.MAX_AUTO_SOURCES = 2
        self.addCleanup(setattr, p, "MAX_AUTO_SOURCES", old)
        path = self._write([self.AUTO] + [f"https://auto/{i}.m3u" for i in range(5)])
        srcs = p.load_sources(path)
        self.assertEqual(len(srcs), 2)

    def test_marker_not_glued_after_reload(self):
        """update_sources_file + повторное чтение не должны склеивать маркер с URL."""
        path = self._write(["# manual", "https://manual/a.m3u"])
        old_sources = p.SOURCES_FILE
        p.SOURCES_FILE = path
        self.addCleanup(setattr, p, "SOURCES_FILE", old_sources)
        p.update_sources_file({"https://auto/new.m3u"})
        with open(path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn(self.AUTO + "\n", content)
        self.assertNotIn(self.AUTO + "http", content)
        srcs = p.load_sources(path)
        urls = {s["url"] for s in srcs}
        self.assertIn("https://auto/new.m3u", urls)
        self.assertIn("https://manual/a.m3u", urls)


class TestEpgStreamParser(unittest.TestCase):
    def test_iterparse_extraction(self):
        import io
        xml = ('<?xml version="1.0"?><tv>'
               '<channel id="First.Channel.1@ru">'
               '<display-name>Первый Канал</display-name>'
               '<category>Movies</category></channel>'
               '<channel id="Sport.TV.2@ru">'
               '<display-name>Sport TV</display-name>'
               '</channel>'
               '<channel id="No.Cat@ru"><display-name>Bez Kategorii</display-name>'
               '<category>   </category></channel></tv>').encode('utf-8')
        cats = p._parse_epg_stream(io.BytesIO(xml))
        self.assertEqual(cats["first.channel.1@ru"], "movies")
        self.assertEqual(cats["первый канал"], "movies")
        self.assertNotIn("sport.tv.2@ru", cats)      # нет category — не добавляем
        self.assertNotIn("bez kategorii", cats)       # пустая category — игнор


class TestStubUrls(unittest.TestCase):
    def test_stubs(self):
        self.assertTrue(p.is_stub_url("acestream://abcdef"))
        self.assertTrue(p.is_stub_url("http://localhost:8080/x.m3u8"))
        self.assertFalse(p.is_stub_url("https://some.host/x.m3u8"))


class TestIptvOrgCategories(unittest.TestCase):
    """Канал-категории из iptv-org API (channels.json) — основной источник категорий"""

    SAMPLE = [
        {"id": "PerviyKanal.ru", "name": "Первый Канал", "categories": ["general"]},
        {"id": "MatchFootball.ru", "name": "Матч! Футбол", "categories": ["sports", "animation"]},
        {"id": "NoCat.ru", "name": "NoCat", "categories": []},
        {"id": "RelaxCh.ru", "name": "Relax Channel", "categories": ["relax"]},
    ]

    def test_build_map_by_id_and_name(self):
        m = p._build_org_category_map(self.SAMPLE)
        self.assertEqual(m["perviykanal.ru"], "general")   # id в нижнем регистре
        self.assertEqual(m["первый канал"], "general")     # и по имени
        self.assertEqual(m["matchfootball.ru"], "sports")  # берётся первая категория
        self.assertNotIn("nocat.ru", m)                    # пустые categories пропускаются

    def test_mapped_to_russian_groups(self):
        m = p._build_org_category_map(self.SAMPLE)
        self.assertEqual(p.map_category(m["perviykanal.ru"]), "📺 Федеральные и Общие")
        self.assertEqual(p.map_category(m["matchfootball.ru"]), "⚽ Спорт")
        self.assertEqual(p.map_category(m["relaxch.ru"]), "🌿 Природа, Охота, Рыбалка и Дача")

    def test_all_known_iptvorg_categories_mapped(self):
        # ни одна реальная категория iptv-org не должна утекать в «латинские» группы
        known = ["auto", "kids", "news", "music", "movies", "series", "sports",
                 "documentary", "lifestyle", "religion", "religious", "shop",
                 "shopping", "travel", "history", "science", "culture", "art",
                 "education", "educational", "entertainment", "general", "family",
                 "weather", "adult", "classic", "comedy", "animation", "relax",
                 "outdoor", "food", "cooking", "health", "medical", "business",
                 "finance", "regional", "local", "moto", "hunting", "fishing",
                 "cinema", "film", "children"]
        for cat in known:
            mapped = p.map_category(cat)
            self.assertTrue(mapped and mapped[0] not in "abcdefghijklmnopqrstuvwxyz",
                            f"категория '{cat}' не замаплена: {mapped}")


if __name__ == "__main__":
    unittest.main()
