from datetime import date
import re

from agri_monitor import app
from agri_monitor.config import read_sources
from agri_monitor.models import Article


def test_pesticide_title_allowlist_includes_only_requested_categories():
    sources = read_sources("sources.yml")
    pesticide = next(
        source for source in sources if source.name == "農藥與法規修正彙整表"
    )
    patterns = pesticide.include_title_patterns

    included = [
        "公告修正「亞克瑞」農藥使用方法及其範圍如附件",
        "公告「扶比胺」農藥使用方法及其範圍如附件",
        "預先通知公告「派滅芬」農藥使用方法及其範圍",
        "農藥許可證資料更新通知",
    ]
    excluded = [
        "全台各縣市代噴人員登錄名冊",
        "全台各縣市空中施作代噴人員登錄名冊",
        "一般最新消息",
    ]

    assert all(any(re.search(pattern, title) for pattern in patterns) for title in included)
    assert all(not any(re.search(pattern, title) for pattern in patterns) for title in excluded)


def test_fda_source_tracks_only_latest_pesticide_mrl_standard():
    sources = read_sources("sources.yml")
    fda = next(source for source in sources if source.name == "衛福部食藥署")
    assert fda.url == "https://www.fda.gov.tw/TC/news.aspx?cid=3"
    assert fda.query_keyword == "農藥殘留容許量標準"
    assert fda.parser == "fda_news_table"
    assert fda.latest_only is True
    assert fda.show_no_update is True
    assert any(
        re.search(pattern, "修正「農藥殘留容許量標準」第三條附表一")
        for pattern in fda.include_title_patterns
    )


def _filtered_titles(source_name: str, titles: list[str]) -> list[str]:
    source = next(source for source in read_sources("sources.yml") if source.name == source_name)
    articles = [
        Article(title, f"https://example.test/{index}", date(2026, 6, index))
        for index, title in enumerate(titles, start=1)
    ]
    return [
        article.title
        for article in app._apply_source_title_filters(source, articles)
    ]


def test_hualien_brewing_training_title_is_excluded():
    titles = [
        "酒麴與釀造班結訓 傳承原鄉風味培育釀造尖兵",
        "豪雨後花蓮農改場籲請農友加強田間管理",
    ]
    assert _filtered_titles("花蓮農改場－本場新聞", titles) == [
        "豪雨後花蓮農改場籲請農友加強田間管理"
    ]


def test_southern_dares_title_rules_keep_requested_examples_only():
    kh_titles = [
        "本場出缺技工1名，意者請洽本場秘書室",
        "排水、清園、防病害，雨後農田復育三關鍵",
        "轉知文化部辦理「第三屆文化部社區營造獎參選」活動",
        "鋒面報到天氣不穩 高雄農改場籲農友加強防汛措施",
        "抗風耐旱白色奇蹟！澎湖鐵炮百合春季魅力綻放",
        "恆春地區玉荷包荔枝穩產技術觀摩會圓滿成功",
        "汛期來臨雨水漸多，留意病害來攪局",
    ]
    assert _filtered_titles("高雄農改場－公告資訊", kh_titles) == [
        "排水、清園、防病害，雨後農田復育三關鍵",
        "鋒面報到天氣不穩 高雄農改場籲農友加強防汛措施",
        "恆春地區玉荷包荔枝穩產技術觀摩會圓滿成功",
        "汛期來臨雨水漸多，留意病害來攪局",
    ]

    tainan_titles = [
        "豪雨過後加強復原管理 臺南區農改場籲農友儘速排除積水及防治病害",
        "「雲嘉南地區農業淨零排放知識觀念推廣及碳足跡數位工具輔導」(報名場次)",
        "畢業季首選國產火鶴花 臺南場研發「專用保鮮劑」助攻",
        "西瓜技術諮詢講習暨產銷班座談會",
        "大蒜技術諮詢講習暨產銷班座談會",
        "文旦修剪枝條現地循環利用及土壤增匯講習會",
        "農務e把抓系統說明及實際操作練習(報名場次)",
        "臺南場推蘆筍栽培一貫化機械作業 兼顧產量與嫩莖品質",
        "勞動部「第56屆全國技能競賽」資料",
        "變天下雨倒數！臺南農改場籲：落實田間排水、防範作物病害",
    ]
    assert _filtered_titles("台南農改場－本場快訊", tainan_titles) == [
        "豪雨過後加強復原管理 臺南區農改場籲農友儘速排除積水及防治病害",
        "畢業季首選國產火鶴花 臺南場研發「專用保鮮劑」助攻",
        "西瓜技術諮詢講習暨產銷班座談會",
        "大蒜技術諮詢講習暨產銷班座談會",
        "文旦修剪枝條現地循環利用及土壤增匯講習會",
        "臺南場推蘆筍栽培一貫化機械作業 兼顧產量與嫩莖品質",
        "變天下雨倒數！臺南農改場籲：落實田間排水、防範作物病害",
    ]

    taitung_titles = [
        "兼顧產量與環境！臺東農改場辦理梅子友善栽培管理技術講習",
        "把農田搬進教室！臺東農改場研發「小米卡牌」 食農課程輕鬆學",
        "臺東農改場於達仁鄉辦理芒果栽培講習 強化果園整合管理技術",
        "臺東農改場辦理友善環境肥培管理班 強化學員土壤健康及永續利用觀念",
        "維護水稻生產環境 減少溫室氣體排放  臺東農改場舉辦水稻田間歇灌溉示範觀摩會",
        "禾蛛緣椿象剋星! 臺東農改場辦理有機水稻禾蛛緣椿象防治及肥培管理技術觀摩會",
        "禾蛛緣椿象剋星!白將軍(白殭菌)守護有機水稻",
        "落實有機及友善環境耕作  臺東農改場傳授有機肥培管理與小米、洛神葵病蟲害整合防治策略",
    ]
    assert _filtered_titles("台東農改場－新聞", taitung_titles) == [
        "兼顧產量與環境！臺東農改場辦理梅子友善栽培管理技術講習",
        "臺東農改場於達仁鄉辦理芒果栽培講習 強化果園整合管理技術",
        "維護水稻生產環境 減少溫室氣體排放  臺東農改場舉辦水稻田間歇灌溉示範觀摩會",
        "禾蛛緣椿象剋星! 臺東農改場辦理有機水稻禾蛛緣椿象防治及肥培管理技術觀摩會",
        "禾蛛緣椿象剋星!白將軍(白殭菌)守護有機水稻",
        "落實有機及友善環境耕作  臺東農改場傳授有機肥培管理與小米、洛神葵病蟲害整合防治策略",
    ]


def test_unrequested_taitung_subsources_are_disabled():
    names = {source.name for source in read_sources("sources.yml")}
    assert "台東農改場－活動" not in names
    assert "台東農改場－公告" not in names
    assert "台東農改場－警報" not in names
