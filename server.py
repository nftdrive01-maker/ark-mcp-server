"""
Fetch MCP サーバー
ウェブサイトとファイルの検索・取得機能を提供
"""

import json
import logging
import os
import re
import urllib.request
import urllib.parse
import html
from html.parser import HTMLParser
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

# .env を自動読み込み（同名の実環境変数は上書きしない）
load_dotenv(Path(__file__).resolve().parent / ".env")

# FastMCP インスタンスを作成
mcp = FastMCP("fetch-server", host="0.0.0.0", port=8000)

_LOGGER = logging.getLogger("fetch_server")


# ─── 言語検出とフィルタリング ────────────────────────────────────────

def _detect_page_language(html: str) -> str | None:
    """HTMLメタデータから言語を検出（例：ja, en, zh）"""
    # <html lang="ja"> パターン
    lang_match = re.search(r'<html[^>]+lang=["\']?([a-z]{2}(?:-[a-zA-Z]{2})?)["\']?', html, re.IGNORECASE)
    if lang_match:
        lang = lang_match.group(1).lower()
        return lang.split('-')[0]  # ja-JP → ja
    
    # <meta http-equiv="Content-Language"> パターン
    meta_match = re.search(r'<meta\s+http-equiv=["\']?Content-Language["\']?[^>]*content=["\']([a-z]{2})', html, re.IGNORECASE)
    if meta_match:
        return meta_match.group(1).lower()
    
    return None


def _is_multilingual_path(path: str) -> bool:
    """多言語リンク（/en/, /zh/, /fr/ など）をフィルタ"""
    # 言語パスプレフィックスをチェック
    lang_patterns = [
        r'^/(?:en|english|ja|jp|japanese|zh|chinese|cn|zhcn|zhtw|fr|french|de|german|es|spanish|ko|korean)(?:/|$)',
        r'^/(?:en|ja|zh|fr|de|es|ko|cn)$',
        r'/(?:en|english|ja|jp|japanese|zh|chinese|cn|zhcn|zhtw|fr|french|de|german|es|spanish|ko|korean)/',  # パス内部
    ]
    
    for pattern in lang_patterns:
        if re.search(pattern, path, re.IGNORECASE):
            return True
    
    return False


def _normalize_page_title(title: str, site_name: str = "") -> str:
    """ページタイトルを正規化（冗長な情報を除去）"""
    if not title:
        return ""
    
    normalized = title.strip()
    
    # サイト名を除去（サイト名があれば）
    if site_name:
        site_patterns = [
            re.escape(site_name),
            re.escape(site_name.replace("　", " "))
        ]
        for pattern in site_patterns:
            normalized = re.sub(f'\\s*{pattern}\\s*', ' ', normalized, flags=re.IGNORECASE)
    
    # 汎用サフィックス・テンプレ文言を除去
    normalized = re.sub(
        r'\s*(?:最新情報一覧|新着情報|お知らせ一覧|ニュース一覧|一覧ページ|一覧|トピックス|ニュース|news|topics|list|archive)\s*',
        ' ',
        normalized,
        flags=re.IGNORECASE,
    )
    
    # 区切り記号の正規化
    normalized = re.sub(r'\s*[|/\\\-・｜]\s*', ' ', normalized)
    
    # 連続スペースを整理
    normalized = re.sub(r'\s+', ' ', normalized).strip()
    
    return normalized if normalized else title


def _is_special_content_page(url: str, title: str, body: str) -> bool:
    """特設サイト・キャンペーン・記念コンテンツを判定"""
    source = f"{url} {title} {body}".lower()
    
    patterns = [
        r'(?:100周年|50周年|75周年|開校|創立|記念|anniversary|commemoration)',
        r'キャンペーン|campaign|campaign\s*page',
        r'特設|特別企画|特集|special\s*edition',
        r'(?:体験|ツアー|イベント)\s*(?:予約|受付|申込)',
        r'(?:文化祭|学園祭|体育祭|sports\s*day)',
    ]
    
    for pattern in patterns:
        if re.search(pattern, source, re.IGNORECASE):
            return True
    
    return False


def _is_question_list_page(title: str, body: str) -> bool:
    """質問文だけが並ぶページを判定（FAQ等）"""
    if not body or len(body.strip()) < 50:
        return False
    
    lines = body.split('\n')
    question_lines = 0
    total_lines = 0
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        total_lines += 1
        if line.endswith('？') or re.match(r'^[A-Z][\w\s]+\?$', line):
            question_lines += 1
    
    # 80% 以上が質問文なら質問リストページと判定
    return total_lines > 0 and (question_lines / total_lines) > 0.8


def _is_filler_content(title: str, body: str, url: str) -> bool:
    """除外対象（一覧、ページネーション、スローガンのみ）を判定"""
    if not body or len(body.strip()) < 20:
        return True
    
    source = f"{title} {body} {url}".lower()

    # 住所・手続き・料金などの事実情報がある場合は除外しない
    if _contains_factual_marker(body):
        return False
    
    # 一覧・ページネーション
    list_patterns = [
        r'(?:最新情報|news|お知らせ|topics)\s*(?:一覧|list|archive)',
        r'(?:page|ページ)\s*(?:\d+|next|previous)',
        r'サイトマップ|sitemap',
    ]
    for pattern in list_patterns:
        if re.search(pattern, source, re.IGNORECASE) and len(body.strip()) < 120:
            return True
    
    # スローガンのみ（短く、単語のみ）
    if len(body.strip()) < 40 and len(body.split()) < 5:
        return True
    
    return False


def _strip_template_phrases(text: str) -> str:
    """一覧・最新情報などのテンプレ文言を除去。"""
    if not text:
        return ""

    cleaned = text
    template_patterns = [
        r"(?:^|\s)(?:一覧|一覧ページ|記事一覧|最新情報|最新情報一覧|新着情報|更新情報|お知らせ一覧|ニュース一覧|トピックス|news|topics|list|archive)(?:\s|$)",
        r"(?:^|\s)(?:もっと見る|続きを読む|view\s*more|read\s*more)(?:\s|$)",
    ]
    for pattern in template_patterns:
        cleaned = re.sub(pattern, " ", cleaned, flags=re.IGNORECASE)

    return re.sub(r"\s+", " ", cleaned).strip()


def _strip_navigation_terms(text: str) -> str:
    """メニュー列挙由来の語を削って、本文情報を残しやすくする。"""
    if not text:
        return ""

    cleaned = text
    nav_terms = [
        "ホーム", "学校案内", "教育方針", "立学の精神", "先生紹介", "スクールユニフォーム", "キャンパスマップ",
        "校舎紹介", "学校の特徴", "総合学科について", "学校生活", "クラブ活動", "年間行事", "進路", "大学合格実績",
        "進路指導", "入試情報", "募集要項", "出願書類", "オープンスクール", "学費", "奨学金", "過去問題集", "よくある質問",
        "お問い合わせ", "サイトマップ", "news", "topics", "about", "menu",
    ]
    for term in nav_terms:
        cleaned = re.sub(rf"(?:^|\s){re.escape(term)}(?:\s|$)", " ", cleaned, flags=re.IGNORECASE)

    return re.sub(r"\s+", " ", cleaned).strip()


def _count_navigation_term_hits(text: str) -> int:
    if not text:
        return 0
    terms = [
        "ホーム", "学校案内", "教育方針", "立学の精神", "先生紹介", "スクールユニフォーム", "キャンパスマップ",
        "校舎紹介", "学校の特徴", "総合学科", "学校生活", "クラブ活動", "年間行事", "進路", "大学合格実績",
        "進路指導", "入試情報", "募集要項", "出願書類", "オープンスクール", "学費", "奨学金", "過去問題集", "よくある質問",
    ]
    return sum(1 for term in terms if term in text)


def _contains_factual_marker(text: str) -> bool:
    """住所・連絡先・数値条件など、回答に使える事実情報の手掛かりを検出。"""
    if not text:
        return False

    patterns = [
        r"〒\d{3}-?\d{4}",
        r"\d{2,4}-\d{2,4}-\d{3,4}",
        r"(?:所在地|住所|アクセス|地図|料金|費用|価格|手続|申請|申込|受付|対象|要件|時間|時刻|電話|メール)",
        r"(?:address|access|price|fee|apply|application|contact|support|hours)",
    ]
    for pattern in patterns:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False


def _contains_strong_factual_marker(text: str) -> bool:
    """本文として維持すべき強い事実情報（住所・連絡先・時刻等）を判定。"""
    if not text:
        return False

    strong_patterns = [
        r"〒\d{3}-?\d{4}",
        r"\d{2,4}-\d{2,4}-\d{3,4}",
        r"(?:所在地|住所|電話|メール|営業時間|受付時間|アクセス|地図)",
        r"(?:address|phone|email|hours|access|location)",
    ]
    for pattern in strong_patterns:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False


def _is_non_informative_sentence(text: str) -> bool:
    """感情表現・装飾文など、回答データとして無意味な文を除外する。"""
    if not text:
        return True

    sentence = text.strip()
    if len(sentence) < 8:
        return True

    if _count_navigation_term_hits(sentence) >= 4 and not _contains_strong_factual_marker(sentence):
        return True

    if _contains_strong_factual_marker(sentence):
        return False

    if re.search(r"\d+", sentence) and len(sentence) <= 120:
        return False

    # 典型的なキャッチコピー・装飾文
    decorative_patterns = [
        r"(?:だから|ずっと|もっと|いまこそ|ようこそ).*(?:元気|安心|楽しい|最高|素敵)",
        r"(?:夢|未来|笑顔|感動|ワクワク).*(?:広がる|つながる|始まる)",
        r"[!！]{1,}$",
        r"^(?:詳しくはこちら|お問い合わせはこちら|クリックしてください)$",
    ]
    for pattern in decorative_patterns:
        if re.search(pattern, sentence, re.IGNORECASE):
            return True

    # 名詞・数値情報が少ない短文は装飾文とみなす
    informative_tokens = len(re.findall(r"[一-龠ぁ-んァ-ヶA-Za-z0-9]{2,}", sentence))
    if len(sentence) <= 20 and informative_tokens <= 2:
        return True

    return False


def _clean_sentence_payload(text: str) -> str:
    """文中のナビ語・テンプレ語・キャッチコピー断片を削って情報本体を残す。"""
    if not text:
        return ""

    cleaned = text.strip()

    # 既知のキャッチコピーと汎用装飾句を除去
    cleaned = re.sub(r"だから\s*1時間目から\s*元気[!！]?", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"(?:遠くからの通学も楽な\s*9時始業の時間割り|駅から近くて、通学も楽。?)", " ", cleaned)

    # メニュー語を文中からも除去
    nav_terms = [
        "ホーム", "学校案内", "教育方針", "立学の精神", "先生紹介", "スクールユニフォーム", "キャンパスマップ",
        "校舎紹介", "学校の特徴", "総合学科について", "学校生活", "クラブ活動", "年間行事", "進路", "大学合格実績",
        "進路指導", "入試情報", "募集要項", "出願書類", "オープンスクール", "学費", "奨学金", "過去問題集", "よくある質問",
    ]
    for term in nav_terms:
        cleaned = cleaned.replace(term, " ")

    cleaned = _strip_template_phrases(cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" 、。")
    return cleaned


def _extract_informative_sentences(text: str) -> list[str]:
    """非情報文とテンプレ文言を除去した文リストを返す。"""
    if not text:
        return []

    cleaned_text = _strip_navigation_terms(_strip_template_phrases(text))
    candidates = [part.strip() for part in re.split(r"(?<=[。！？!?])\s*", cleaned_text) if part.strip()]
    filtered: list[str] = []
    for sentence in candidates:
        normalized_sentence = _clean_sentence_payload(sentence)
        if not normalized_sentence:
            continue
        if _is_non_informative_sentence(normalized_sentence):
            continue
        filtered.append(normalized_sentence)

    # 装飾文を落とした結果が空の場合、事実マーカーを含む文は救済する
    if not filtered:
        for sentence in candidates:
            normalized_sentence = _clean_sentence_payload(sentence)
            if normalized_sentence and _contains_factual_marker(normalized_sentence):
                filtered.append(normalized_sentence)

    return filtered


def _get_role_classification(title: str, body: str, url: str) -> str:
    """内容から役割を判定（9 つの役割を返す）"""
    source = f"{title} {body} {url}".lower()

    # 法務系は最優先で legal に固定
    legal_patterns = [
        r'(?:利用規約|個人情報|個人情報保護|プライバシー|ポリシー|ガイドライン|sns\s*ガイドライン)',
        r'(?:terms|privacy|policy|guideline|legal|cookie)',
        r'(?:著作権|免責|特定商取引)',
    ]
    for pattern in legal_patterns:
        if re.search(pattern, source):
            return "legal"
    
    # ① 説明系（WHAT）
    what_patterns = [
        r'(?:概要|特徴|特色|特長|について|理念|ミッション|ビジョン|コンセプト)',
        r'(?:mission|vision|philosophy|overview|feature|about)',
        r'(?:プロフィール|紹介|説明|解説|ガイド)',
    ]
    for pattern in what_patterns:
        if re.search(pattern, source):
            return "what"
    
    # ② 提供価値系（OFFER）
    offer_patterns = [
        r'(?:商品|サービス|プラン|コース|授業|科目|メニュー|オプション)',
        r'(?:product|service|plan|course|offering|menu)',
        r'(?:講座|研修|プログラム|パッケージ)',
    ]
    for pattern in offer_patterns:
        if re.search(pattern, source):
            return "offer"
    
    # ③ 手続き系（HOW）
    how_patterns = [
        r'(?:申込|申請|手続|登録|予約|出願|エントリー|応募)',
        r'(?:application|registration|enrollment|booking|procedure)',
        r'(?:方法|流れ|ステップ|プロセス|手順)',
        r'(?:証明書|成績|資格|修了)',
    ]
    for pattern in how_patterns:
        if re.search(pattern, source):
            return "how"
    
    # ④ 条件系（RULE）
    rule_patterns = [
        r'(?:料金|価格|費用|金額|レート|プライシング)',
        r'(?:price|pricing|cost|fee|rate)',
        r'(?:条件|注意|注意事項|制限|制約|ポイント)',
        r'(?:対象|要件|資格|必要|必須)',
    ]
    for pattern in rule_patterns:
        if re.search(pattern, source):
            return "rule"
    
    # ⑤ 接点系（CONTACT）
    contact_patterns = [
        r'(?:問い合わせ|連絡先|電話|メール|お問い合わせ|相談)',
        r'(?:contact|inquiry|support|phone|email)',
        r'(?:アクセス|交通|行き方|地図|所在地|location)',
    ]
    for pattern in contact_patterns:
        if re.search(pattern, source):
            return "contact"
    
    # ⑥ 更新系（UPDATE）
    update_patterns = [
        r'(?:お知らせ|ニュース|新着|更新|最新|イベント|行事)',
        r'(?:news|update|event|information|notice)',
        r'(?:トピックス|topics)',
    ]
    for pattern in update_patterns:
        if re.search(pattern, source):
            return "update"
    
    # ⑦ 組織系（WHO）
    who_patterns = [
        r'(?:会社|企業|学校|組織|団体|スタッフ|教員|講師)',
        r'(?:company|organization|team|staff|faculty)',
        r'(?:沿革|歴史|background|profile|about)',
    ]
    for pattern in who_patterns:
        if re.search(pattern, source):
            return "who"
    
    # ⑨ 特設・装飾系（SPECIAL）
    if _is_special_content_page(url, title, body):
        return "special"
    
    # デフォルト: offer（汎用サイトではサービス説明が多い）
    return "offer"


def _get_label_from_role(role: str, title: str, body: str, url: str) -> str:
    """role と内容からラベルを生成"""
    source = f"{title} {body} {url}".lower()
    
    if role == "what":
        if re.search(r'理念|ミッション|ビジョン|philosophy', source):
            return "philosophy"
        if re.search(r'特徴|特色|特長|feature', source):
            return "feature"
        return "overview"
    
    elif role == "offer":
        if re.search(r'(?:商品|product)', source):
            return "product"
        if re.search(r'(?:メニュー|menu)', source):
            return "menu"
        if re.search(r'(?:プラン|plan)', source):
            return "plan"
        if re.search(r'(?:コース|course|授業|科目)', source):
            return "course"
        return "service"
    
    elif role == "how":
        if re.search(r'(?:証明書|成績|資格)', source):
            return "certificate_procedure"
        if re.search(r'(?:申込|出願|エントリー|application|enrollment)', source):
            return "application"
        if re.search(r'(?:予約|reservation|booking)', source):
            return "reservation"
        return "procedure"
    
    elif role == "rule":
        if re.search(r'(?:料金|価格|price|pricing)', source):
            return "pricing"
        if re.search(r'(?:条件|制約|要件)', source):
            return "condition"
        return "restriction"
    
    elif role == "contact":
        if re.search(r'(?:アクセス|交通|行き方|地図|access|location)', source):
            return "access_info"
        if re.search(r'(?:問い合わせ|連絡先|contact|support)', source):
            return "contact_info"
        return "support"
    
    elif role == "update":
        if re.search(r'(?:イベント|行事|event)', source):
            return "event_info"
        return "news_update"
    
    elif role == "who":
        if re.search(r'(?:沿革|歴史|background)', source):
            return "history"
        return "organization"
    
    elif role == "legal":
        if re.search(r'(?:個人情報|プライバシー|privacy)', source):
            return "privacy_policy"
        if re.search(r'(?:sns|ガイドライン|guideline)', source):
            return "sns_policy"
        if re.search(r'(?:利用規約|terms)', source):
            return "terms_of_service"
        return "policy"
    
    elif role == "special":
        return "special_content"
    
    return "misc_info"


def _get_role_detail(role: str, title: str, body: str, url: str) -> str:
    """role を回答利用向けに細分化して返す。"""
    source = f"{title} {body} {url}".lower()

    if role == "what":
        if re.search(r"理念|ミッション|ビジョン|コンセプト|philosophy|concept", source):
            return "concept"
        if re.search(r"特徴|特色|特長|feature", source):
            return "feature"
        return "overview"

    if role == "how":
        if re.search(r"申込|申請|出願|エントリー|application|apply", source):
            return "application"
        if re.search(r"流れ|ステップ|フロー|手順|flow|step", source):
            return "flow"
        return "procedure"

    if role == "contact":
        if re.search(r"アクセス|交通|行き方|地図|所在地|access|location", source):
            return "access"
        if re.search(r"問い合わせ|連絡先|電話|メール|contact|inquiry", source):
            return "contact"
        return "support"

    if role == "rule":
        if re.search(r"条件|要件|対象|condition|eligibility", source):
            return "condition"
        return "restriction"

    if role == "legal":
        if re.search(r"個人情報|プライバシー|privacy", source):
            return "privacy"
        if re.search(r"ガイドライン|guideline|sns", source):
            return "guideline"
        return "policy"

    return role


def _get_priority_from_role(role: str, label: str) -> int:
    """role とラベルから優先度を決定"""
    # priority 1: ユーザーが質問しやすい情報
    if label in ["service", "product", "plan", "contact_info", "pricing", "access_info"]:
        return 1
    if role in ["offer", "contact"]:
        return 1
    
    # priority 2: 補助情報
    if label in ["course", "feature", "overview", "procedure", "organization", "event_info"]:
        return 2
    if role in ["what", "how", "update", "who"]:
        return 2
    
    # priority 3: 低頻度情報
    if role == "legal":
        return 3
    if label in ["policy", "privacy_policy", "sns_policy", "terms_of_service"]:
        return 3
    
    # priority 4: 特設
    if role == "special":
        return 4
    
    return 2


def _get_block_priority(label: str, title: str, url: str, body: str) -> int:
    """ブロックの優先度を決定（1=高優先、4=低優先）"""
    source = f"{label} {title} {url} {body}".lower()
    
    # priority 1: 基本案内、アクセス、入試、学科、問い合わせ
    p1_patterns = [
        r'access_info', r'admission_info', r'curriculum', r'contact_info',
        r'学科|コース|カリキュラム|アクセス|入試|問い合わせ|学校案内',
    ]
    for pattern in p1_patterns:
        if re.search(pattern, source, re.IGNORECASE):
            return 1
    
    # priority 2: 学校生活、施設、進路、証明書
    p2_patterns = [
        r'school_life', r'club_activity', r'facility', r'certificate_procedure', r'procedure',
        r'学校生活|部活|施設|進路|進学|卒業生|証明書|資格',
    ]
    for pattern in p2_patterns:
        if re.search(pattern, source, re.IGNORECASE):
            return 2
    
    # priority 3: ポリシー、ガイドライン
    p3_patterns = [
        r'policy', r'guideline', r'terms', r'privacy', r'copyright',
        r'ポリシー|ガイドライン|利用規約|個人情報|著作権|sns',
    ]
    for pattern in p3_patterns:
        if re.search(pattern, source, re.IGNORECASE):
            return 3
    
    # priority 4: 特設サイト、記念コンテンツ
    if _is_special_content_page(url, title, body) or _is_question_list_page(title, body):
        return 4
    
    # デフォルト: 2
    return 2


def _is_chinese_line(text: str) -> bool:
    """行に中国語（簡体字）要素が強く含まれるかを判定する。"""
    if not text or len(text.strip()) < 4:
        return False

    # 日本語に通常現れない簡体字を優先検出
    if re.search(r"[为时说语广场国车门线达这从仅们体龙]", text):
        return True

    # 中国語の機能語が複数ある場合は中国語とみなす
    function_words = re.findall(r"(?:的|了|在|是|和|及|并|通过|可以|进行|访问)", text)
    if len(function_words) >= 2:
        # かなが全くなく漢字主体なら中国語の可能性が高い
        if not re.search(r"[\u3040-\u30ff]", text):
            return True

    return False


def _strip_css_utility_tokens(text: str) -> str:
    """Tailwind系のクラス断片を除去し、本文のみを残す。"""
    if not text:
        return ""

    cleaned = text

    # 例: lg:[&>svg]:w-[20vw] / [&>svg]:h-auto
    cleaned = re.sub(r"(?:^|\s)(?:sm|md|lg|xl|2xl):\[[^\]]+\]:[^\s]+", " ", cleaned)
    cleaned = re.sub(r"(?:^|\s)\[[^\]]+\]:[^\s]+", " ", cleaned)

    # 例: 壊れた断片 `svg]:w-[40vw]`（先頭の `[` が欠落したケース）
    cleaned = re.sub(r"(?:^|\s)[a-zA-Z0-9_\-<>/&]+\]:[a-zA-Z0-9_\-]+-\[[^\]]+\]", " ", cleaned)
    cleaned = re.sub(r"(?:^|\s)[a-zA-Z0-9_\-<>/&]+\]:[a-zA-Z0-9_\-]+", " ", cleaned)

    # 例: w-[40vw], h-[20px], top-[calc(100%-1rem)]
    cleaned = re.sub(r"(?:^|\s)[a-zA-Z][a-zA-Z0-9_-]*-\[[^\]]+\]", " ", cleaned)

    # 例: drop-shadow-answer, text-center, max-w-7xl
    cleaned = re.sub(
        r"(?:^|\s)(?:drop-shadow|text|bg|border|rounded|max|min|w|h|p|m|gap|grid|flex|items|justify|leading|tracking|font|shadow)-[a-zA-Z0-9_\-/:%\.]+",
        " ",
        cleaned,
    )

    # よく混入する著作権フッター断片
    cleaned = re.sub(r"\bAll\s+Rights\s+Reserved\.?\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bRights\s+Reserved\.?\b", " ", cleaned, flags=re.IGNORECASE)

    # 壊れた属性断片や装飾記号を掃除
    cleaned = cleaned.replace('">', " ").replace("'>", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _is_noise_line(text: str) -> bool:
    """スタイル断片・記号ノイズ行を判定する。"""
    if not text:
        return True

    candidate = text.strip()
    if len(candidate) < 2:
        return True

    jp_chars = len(re.findall(r"[\u3040-\u30ff\u4e00-\u9fff]", candidate))
    classish_tokens = len(re.findall(r"(?:\[[^\]]+\]:|(?:sm|md|lg|xl|2xl):|-[a-zA-Z0-9_\-/:%\.]+)", candidate))

    # 日本語がほぼなく、クラス断片が多い行は捨てる
    if jp_chars <= 1 and classish_tokens >= 2:
        return True

    # 記号・英数字中心で、クラス記法が含まれる行は捨てる
    if re.fullmatch(r"[\w\s\[\]<>:/\-_%\.\(\)\"'&,;!?]+", candidate) and classish_tokens >= 1 and jp_chars == 0:
        return True

    return False


def _filter_multilingual_content(content: str, detected_lang: str | None) -> str:
    """多言語マーカーと中国語テキストをコンテンツから削除する。"""
    # 言語が中国語と判定できる場合は空にする（再混入を防ぐ）
    if detected_lang and detected_lang in ["zh", "cn", "zho"]:
        return ""

    # 多言語リンク記号（/en、/zh など）を除去
    content = re.sub(
        r"/(?:en|english|ja|jp|japanese|zh|chinese|cn|zhcn|zhtw|fr|french|de|german|es|spanish|ko|korean)\b",
        "",
        content,
        flags=re.IGNORECASE,
    )

    # 言語選択メニューを除去
    content = re.sub(
        r"(?:Language|言語|言語選択|Language Selection|多言語)(?::|：)?[^\n]*(?:English|日本語|中文|Français|Deutsch|Español|한국어)[^\n]*",
        "",
        content,
        flags=re.IGNORECASE,
    )

    # Tailwind等のユーティリティクラス断片を先に除去
    content = _strip_css_utility_tokens(content)

    # 文・行単位でノイズや中国語らしい部分を落とす
    chunks = re.split(r"(?<=[。！？!?])|\n", content)
    filtered_chunks: list[str] = []
    for chunk in chunks:
        candidate = _strip_css_utility_tokens(chunk).strip()
        if not candidate:
            continue
        if _is_noise_line(candidate):
            continue
        if _is_chinese_line(candidate):
            continue
        filtered_chunks.append(candidate)

    return "\n".join(filtered_chunks).strip()


# ─── ツール定義 ────────────────────────────────────────

@mcp.tool()
def fetch_url(url: str, max_length: int = 4000) -> str:
    """
    URL の内容を取得する（日本語コンテンツを優先）
    
    Args:
        url: 取得するウェブサイトの URL
        max_length: 取得するコンテンツの最大文字数（デフォルト: 4000）
    
    Returns:
        取得したコンテンツ（テキスト形式）。言語が日本語以外の場合は警告を含める
    """
    try:
        # URL の検証
        if not url.startswith(("http://", "https://")):
            return f"エラー: 無効な URL です。http:// または https:// で始まる必要があります: {url}"
        
        # ヘッダー設定（Accept-Language を日本語優先に）
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept-Language": "ja,ja-JP;q=0.9,en;q=0.8",
        }
        
        # URL を取得
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as response:
            raw_content = response.read()
            content = raw_content.decode('utf-8', errors='ignore')
        
        # ページの言語を検出
        detected_lang = _detect_page_language(content)
        
        # HTMLタグを削除
        content = re.sub(r'<script[^>]*>.*?</script>', '', content, flags=re.DOTALL)
        content = re.sub(r'<style[^>]*>.*?</style>', '', content, flags=re.DOTALL)
        content = re.sub(r'<[^>]+>', '', content)
        content = re.sub(r'\s+', ' ', content).strip()
        
        # 多言語マーカーを削除
        content = _filter_multilingual_content(content, detected_lang)
        
        # 言語が日本語以外の場合は警告を追加
        lang_warning = ""
        if detected_lang and detected_lang not in ["ja", "jp"]:
            lang_warning = f"\n【言語警告】このページは {detected_lang.upper()} 言語です。日本語コンテンツのみを使用してください。\n"
        
        # 長さ制限
        if len(content) > max_length:
            content = content[:max_length] + f"... (以下省略、全体 {len(content)} 文字)"
        
        result = content if content else "取得したコンテンツが空です"
        return lang_warning + result if lang_warning else result
    
    except urllib.error.URLError as e:
        return f"URL 取得エラー: {e.reason}"
    except Exception as e:
        return f"エラー: {e}"


class _SiteParser(HTMLParser):
    """サイトクロール用HTMLパーサー（stdlib のみ使用）"""

    def __init__(self, base_url: str):
        super().__init__()
        self.base_url = base_url
        self._parsed_base = urllib.parse.urlparse(base_url)
        self.title: str = ""
        self.headings: list[tuple[str, str]] = []  # (tag, text)
        self.links: list[str] = []
        self.text_parts: list[str] = []
        self._current_tag = ""
        self._skip_tags = {"script", "style", "noscript", "head", "meta", "link", "iframe"}
        self._heading_tags = {"h1", "h2", "h3"}
        self._in_skip = 0
        self._in_heading: str | None = None
        self._heading_buf: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "title":
            self._in_title = True
        if tag in self._skip_tags:
            self._in_skip += 1
        if tag in self._heading_tags:
            self._in_heading = tag
            self._heading_buf = []
        if tag == "a":
            href = dict(attrs).get("href") or ""
            href_lower = href.strip().lower()
            if href_lower.startswith(("#", "mailto:", "mailto：", "tel:", "javascript:")):
                return
            resolved = urllib.parse.urljoin(self.base_url, href)
            parsed = urllib.parse.urlparse(resolved)
            
            # 多言語パスをスキップ
            if _is_multilingual_path(parsed.path):
                return
            
            if (
                parsed.scheme in ("http", "https")
                and _normalize_host(parsed.netloc) == _normalize_host(self._parsed_base.netloc)
                and not parsed.path.lower().endswith((".pdf", ".zip", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".css", ".js", ".ico", ".webp"))
            ):
                clean = urllib.parse.urlunparse(parsed._replace(fragment=""))
                if clean not in self.links:
                    self.links.append(clean)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._skip_tags:
            self._in_skip = max(0, self._in_skip - 1)
        if tag == "title":
            self._in_title = False
        if tag in self._heading_tags and self._in_heading == tag:
            text = "".join(self._heading_buf).strip()
            if text:
                self.headings.append((tag, text))
            self._in_heading = None
            self._heading_buf = []

    def handle_data(self, data: str) -> None:
        if self._in_title:
            text = data.strip()
            if text:
                self.title += text
            return
        if self._in_skip:
            return
        text = data.strip()
        if not text:
            return
        if self._in_heading:
            self._heading_buf.append(text)
        else:
            self.text_parts.append(text)

    def get_body_text(self, max_length: int) -> str:
        raw = " ".join(self.text_parts)
        raw = re.sub(r" {2,}", " ", raw)
        return raw[:max_length] if len(raw) > max_length else raw


def _normalize_host(netloc: str) -> str:
    host = (netloc or "").lower()
    if ":" in host:
        host = host.split(":", 1)[0]
    if host.startswith("www."):
        host = host[4:]
    return host


def _canonicalize_url(url: str, seed_url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    seed_parsed = urllib.parse.urlparse(seed_url)
    cleaned = parsed._replace(fragment="")

    # http/https・www差異で同一ページが重複しないよう、起点URLに合わせる
    if _normalize_host(cleaned.netloc) == _normalize_host(seed_parsed.netloc):
        cleaned = cleaned._replace(scheme=seed_parsed.scheme, netloc=seed_parsed.netloc)

    path = cleaned.path or "/"
    cleaned = cleaned._replace(path=path)
    return urllib.parse.urlunparse(cleaned)


def _extract_declared_charset(html_head: bytes) -> str | None:
    head_text = html_head.decode("ascii", errors="ignore")
    patterns = [
        r"<meta[^>]+charset=['\"]?([a-zA-Z0-9_\-]+)",
        r"<meta[^>]+content=['\"][^>]*charset=([a-zA-Z0-9_\-]+)",
    ]
    for pattern in patterns:
        m = re.search(pattern, head_text, flags=re.IGNORECASE)
        if m:
            return m.group(1).lower()
    return None


def _is_link_aggregation_page(url: str) -> bool:
    """
    ブログアーカイブ、カテゴリ、タグなどのリンク集ページを検出
    これらのページからのリンク数制限を適用する
    """
    path = urllib.parse.urlparse(url).path.lower()
    
    # リンク集ページのパターン
    aggregation_patterns = [
        r'/blog/archive',
        r'/blog/archiv',
        r'/archive',
        r'/category/',
        r'/categories',
        r'/tag/',
        r'/tags',
        r'/search',
        r'/results',
        r'/news',
        r'/articles',
        r'/posts',
        r'/entries',
        r'/page/\d+',
        r'/\d+/\d+/',  # /2024/05/ などの日付ベースアーカイブ
    ]
    
    for pattern in aggregation_patterns:
        if re.search(pattern, path):
            return True
    
    return False


def _get_url_depth(url: str, seed_url: str) -> int:
    """ルートからのパス深度を計算（浅いほど優先度が高い）"""
    seed_path = urllib.parse.urlparse(seed_url).path.strip('/')
    url_path = urllib.parse.urlparse(url).path.strip('/')
    
    depth = url_path.count('/') - seed_path.count('/')
    return max(0, depth)


def _decode_html_bytes(raw: bytes, header_charset: str | None) -> str:
    tried: list[str] = []
    candidates: list[str] = []

    if header_charset:
        candidates.append(header_charset.lower())

    meta_charset = _extract_declared_charset(raw[:4096])
    if meta_charset and meta_charset not in candidates:
        candidates.append(meta_charset)

    # 日本語サイトを想定したフォールバック順
    for enc in ["utf-8", "cp932", "shift_jis", "euc_jp", "iso2022_jp"]:
        if enc not in candidates:
            candidates.append(enc)

    for enc in candidates:
        try:
            text = raw.decode(enc)
            if text.strip():
                return text
        except Exception:
            continue

    return raw.decode("utf-8", errors="ignore")


def _extract_text_fallback(html: str, max_length: int) -> str:
    # パーサーで本文が取れないページ（フレーム/独自構造）向けの保険
    text = re.sub(r"<!--.*?-->", " ", html, flags=re.DOTALL)
    text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<noscript[^>]*>.*?</noscript>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_length] if len(text) > max_length else text


def _fetch_raw_html(url: str, timeout: int = 10) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; ArkI-Crawler/1.0)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    }
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        header_charset = resp.headers.get_content_charset()
        raw = resp.read()
        return _decode_html_bytes(raw, header_charset)


def _normalize_page_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text)
    text = _strip_template_phrases(text)
    text = text.replace("|", " ").replace("/", "/")
    return text.strip()


def _collect_common_text_lines(all_bodies: list[str], min_frequency: float = 0.5) -> set[str]:
    """複数ページに共通する行テキストを検出（ナビゲーション・フッター用）"""
    if not all_bodies or len(all_bodies) < 2:
        return set()

    line_counter: Counter[str] = Counter()
    for body in all_bodies:
        seen_in_page: set[str] = set()
        for line in body.split('\n'):
            line = line.strip()
            if len(line) >= 4 and not _is_navigation_or_footer_noise(line):
                seen_in_page.add(line)
        for line in seen_in_page:
            line_counter[line] += 1

    threshold = max(2, int(len(all_bodies) * min_frequency))
    common_lines = {line for line, count in line_counter.items() if count >= threshold}
    return common_lines


def _remove_common_text(body: str, common_lines: set[str]) -> str:
    """ページから共通テキスト行を除去"""
    if not common_lines:
        return body

    lines = body.split('\n')
    filtered: list[str] = []
    for line in lines:
        if line.strip() not in common_lines:
            filtered.append(line)

    return '\n'.join(filtered).strip()


def _is_navigation_or_footer_noise(text: str) -> bool:
    """ナビゲーション・フッター・共通メニュー行を判定"""
    if not text or len(text.strip()) < 4:
        return True

    source = text.lower().strip()

    # 事実情報が含まれる行はナビ/フッター扱いしない
    if _contains_factual_marker(source):
        return False

    # ナビゲーション・メニュー行（複数カテゴリの羅列）
    nav_patterns = [
        r'学校案内|教育方針|立学の精神|学科紹介|キャンパス|入試情報|受験生向け|在校生向け|卒業生向け',
        r'home|top|about|contact|privacy|terms|sitemap|news|blog|help',
    ]
    for pattern in nav_patterns:
        if re.search(pattern, source, re.IGNORECASE):
            return True

    # フッター・共通リンク導線
    footer_patterns = [
        r'copyright|©|rights reserved|著作権|著作権表示',
        r'資料請求|お問い合わせ|contact us|inquiry|お問い合わせフォーム',
        r'sns|sns\s*ガイドライン|twitter|facebook|instagram|youtube|line',
        r'個人情報保護|プライバシー|privacy\s*policy',
        r'利用規約|terms\s*of\s*service|サイトマップ|sitemap',
    ]
    for pattern in footer_patterns:
        if re.search(pattern, source, re.IGNORECASE):
            return True

    return False


def _estimate_noise_ratio(text: str) -> float:
    """テキストのノイズ率を推定（0.0～1.0）"""
    if not text or len(text.strip()) < 10:
        return 1.0

    lines = text.split('\n')
    noise_count = 0
    for line in lines:
        if _is_navigation_or_footer_noise(line):
            noise_count += 1

    return noise_count / len(lines) if lines else 0.0


def _looks_like_form_page(url: str, title: str, body: str, html: str) -> bool:
    # フォームの実要素がHTML内に複数ある場合のみ形成と判定
    # 単なるキーワード出現だけではform扱いしない（誤判定を抑制）
    form_elements = len(re.findall(r"<(?:form|input|textarea|select)\b", html, re.IGNORECASE))
    
    # フォーム要素が複数（2個以上）ある場合は確実にフォーム
    if form_elements >= 2:
        return True
    
    # form 要素が 1 個で、本文が短くキーワードがある場合のみ
    if form_elements >= 1:
        source_content = f"{title} {body}".lower()
        if len(body.strip()) < 200 and bool(
            re.search(r"(フォーム|入力|送信|問い合わせ|申込|申請|資料請求|応募|予約)", source_content)
        ):
            return True
    
    return False


def _looks_like_system_page(url: str, title: str, body: str) -> bool:
    source = f"{url} {title} {body}".lower()
    return bool(
        re.search(
            r"(privacy|policy|terms|cookie|accessibility|disclaimer|copyright|利用規約|個人情報|プライバシー|サイトポリシー|著作権|免責)",
            source,
        )
    )


def _looks_like_navigation_page(title: str, body: str, link_count: int) -> bool:
    source = f"{title} {body}".lower()
    if bool(re.search(r"(menu|navigation|ナビ|パンくず|breadcrumb|サイトマップ|sitemap|グローバルナビ)", source)):
        return True
    if link_count >= 12 and len(body.strip()) < 180:
        return True
    return False


def _looks_like_list_page(url: str, title: str, body: str) -> bool:
    source = f"{url} {title} {body}".lower()
    if _is_link_aggregation_page(url):
        return True
    return bool(
        re.search(
            r"(一覧|記事一覧|お知らせ一覧|ニュース一覧|更新情報|アーカイブ|目次|topics|news|blog|archive|list)",
            source,
        )
    )


def _classify_page_type(url: str, title: str, body: str, html: str, link_count: int) -> str:
    if _looks_like_form_page(url, title, body, html):
        return "form"
    if _looks_like_system_page(url, title, body):
        return "system"
    if _looks_like_navigation_page(title, body, link_count):
        return "navigation"
    if _looks_like_list_page(url, title, body):
        return "list"
    return "content"


def _split_into_chunks(text: str, max_sentences: int = 3, max_chars: int = 260) -> list[str]:
    normalized = _normalize_page_text(text)
    if not normalized:
        return []

    # 短いテキストでもそのまま chunk として返す
    if len(normalized) < 60:
        return [normalized] if normalized.strip() else []

    sentences = [part.strip() for part in re.split(r"(?<=[。！？!?])\s*", normalized) if part.strip()]
    if not sentences:
        return [normalized[:max_chars]] if normalized.strip() else []

    chunks: list[str] = []
    current: list[str] = []
    current_length = 0

    for sentence in sentences:
        current.append(sentence)
        current_length += len(sentence)
        if len(current) >= max_sentences or current_length >= max_chars:
            chunks.append("".join(current).strip())
            current = []
            current_length = 0

    if current:
        chunks.append("".join(current).strip())

    return [chunk for chunk in chunks if chunk]


def _extract_keywords(text: str, limit: int = 6) -> list[str]:
    normalized = _normalize_page_text(text)
    if not normalized:
        return []

    # より厳密な stopwords: 意味が薄い機能語・助詞のみ
    stopwords = {
        "こと", "もの", "ため", "それ", "ここ", "この", "その", "あれ", "これ", "よう", "ように", "について",
        "できます", "してください", "です", "ます", "ある", "いる", "なる", "する",
        "案内", "情報", "内容", "ページ", "こちら",  # 汎用単語
        # 教育機関名詞は除外しない: "学校", "高校", "生徒" は残す
    }

    candidates = re.findall(r"[ぁ-んァ-ヶー一-龠A-Za-z0-9]{2,16}", normalized)
    counter: Counter[str] = Counter()
    for candidate in candidates:
        if candidate in stopwords:
            continue
        if candidate.isdigit():
            continue
        counter[candidate] += 1

    return [word for word, _ in counter.most_common(limit)]


def _normalize_label_candidate(text: str) -> str:
    if not text:
        return "misc_info"

    normalized = re.sub(r"[^a-zA-Z0-9ぁ-んァ-ヶ一-龠]+", "_", text.lower()).strip("_")
    normalized = re.sub(r"_+", "_", normalized)
    return normalized[:40] or "misc_info"


def _generate_label(title: str, chunk: str, url: str) -> str:
    source = f"{title} {chunk} {url}".lower()
    rules = [
        # 採用情報を最優先
        (r"(採用|求人|職員|人事|recruit|hire|employment)", "recruitment"),
        # 入試情報
        (r"(入試|募集要項|出願|願書|選抜|受験|admission|exam)", "admission_info"),
        # その他の教育関連
        (r"(教育方針|理念|建学|philosophy|mission|vision)", "philosophy"),
        (r"(学科|コース|カリキュラム|授業|curriculum|course)", "curriculum"),
        (r"(施設|校舎|設備|facility|campus)", "facility"),
        (r"(行事|イベント|event|schedule|calendar)", "event_info"),
        (r"(部活動|クラブ|club|sports)", "club_activity"),
        (r"(学校生活|生徒会|life|student)", "school_life"),
        (r"(お知らせ|news|更新情報|topics)", "news_update"),
        # アクセス・問い合わせ
        (r"(アクセス|交通|行き方|所在地|地図|access|map|location)", "access_info"),
        (r"(お問い合わせ|連絡先|contact|電話|mail|問い合わせ|inquiry)", "contact_info"),
        # サポート関連
        (r"(食堂|保健|相談|支援|support|health|counseling)", "support_info"),
        # ポリシー・ガイドライン（細分化）
        (r"(sns\s*ガイドライン|sns\s*guideline|guideline)", "guideline"),
        (r"(個人情報保護|プライバシー|privacy\s*policy|プライバシーポリシー)", "privacy_policy"),
        (r"(利用規約|terms\s*of\s*service|service\s*terms)", "terms_of_service"),
        (r"(著作権|copyright|rights\s*reserved)", "copyright_info"),
        # 手続・その他
        (r"(手続|申請|証明書|届出|procedure|form|application)", "procedure"),
    ]

    for pattern, label in rules:
        if re.search(pattern, source, re.IGNORECASE):
            return label

    keywords = _extract_keywords(f"{title} {chunk}", limit=3)
    if keywords:
        return _normalize_label_candidate("_".join(keywords))

    return "misc_info"


def _build_block(title: str, chunk: str, url: str, section_title: str | None = None, site_name: str = "") -> dict | None:
    normalized_chunk = _normalize_page_text(_filter_multilingual_content(chunk, None))

    # 一覧系テンプレ・装飾文だけの塊は捨てる
    if _is_filler_content(title, normalized_chunk, url):
        return None
    
    # ノイズ率が高い場合はブロックを作成しない
    noise_ratio = _estimate_noise_ratio(normalized_chunk)
    if noise_ratio > 0.7:
        return None
    
    # ナビゲーション・フッター専用の文言の場合は除外
    if _is_navigation_or_footer_noise(normalized_chunk):
        return None
    
    # 質問リストだけのページは priority 4
    if _is_question_list_page(title, normalized_chunk):
        return None
    
    sentences = _extract_informative_sentences(normalized_chunk)
    if not sentences:
        return None

    summary = sentences[0]
    details = sentences[:6]
    keywords = _extract_keywords(f"{title} {section_title or ''} {normalized_chunk}")

    block_title = section_title or title or ""
    # タイトルを正規化
    normalized_title = _normalize_page_title(block_title, site_name)
    
    # Role-based classification
    role = _get_role_classification(normalized_title, normalized_chunk, url)
    role_detail = _get_role_detail(role, normalized_title, normalized_chunk, url)
    label = _get_label_from_role(role, normalized_title, normalized_chunk, url)
    priority = _get_priority_from_role(role, label)

    return {
        "label": label,
        "role": role,
        "role_detail": role_detail,
        "title": _normalize_page_text(normalized_title),
        "summary": summary,
        "details": details,
        "keywords": keywords,
        "source_url": url,
        "priority": priority,
    }



def _dedupe_blocks(blocks: list[dict]) -> list[dict]:
    seen: set[tuple[str, str, str]] = set()
    deduped: list[dict] = []
    for block in blocks:
        key = (
            str(block.get("label", "")),
            str(block.get("title", "")),
            str(block.get("summary", "")),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(block)
    return deduped


def _merge_related_blocks(blocks: list[dict]) -> list[dict]:
    """同一URL・同一テーマの分割ブロックを統合する。"""
    grouped: dict[tuple[str, str, str, str], dict] = {}

    for block in blocks:
        source_url = str(block.get("source_url", ""))
        role = str(block.get("role", ""))
        role_detail = str(block.get("role_detail", ""))
        label = str(block.get("label", ""))
        key = (source_url, role, role_detail, label)

        if key not in grouped:
            grouped[key] = {
                **block,
                "details": list(block.get("details", [])),
                "keywords": list(block.get("keywords", [])),
            }
            continue

        target = grouped[key]

        # details を重複排除しつつ結合
        merged_details = list(target.get("details", []))
        for detail in block.get("details", []):
            if detail and detail not in merged_details:
                merged_details.append(detail)
        target["details"] = merged_details[:8]

        # summary は情報量の高いほうを採用
        current_summary = str(target.get("summary", ""))
        candidate_summary = str(block.get("summary", ""))
        if len(candidate_summary) > len(current_summary):
            target["summary"] = candidate_summary

        # title は短く具体的なものを優先
        current_title = str(target.get("title", ""))
        candidate_title = str(block.get("title", ""))
        if candidate_title and (not current_title or len(candidate_title) < len(current_title)):
            target["title"] = candidate_title

        # keywords を統合
        merged_keywords = list(target.get("keywords", []))
        for keyword in block.get("keywords", []):
            if keyword and keyword not in merged_keywords:
                merged_keywords.append(keyword)
        target["keywords"] = merged_keywords[:8]

    return list(grouped.values())


def _build_site_summary(site_title: str, site_url: str, page_count: int, block_count: int, page_type_counts: dict[str, int], blocks: list[dict]) -> str:
    labels = [str(block.get("label", "")) for block in blocks if block.get("label")]
    label_counts = Counter(labels)
    main_topics = [label for label, _ in label_counts.most_common(5)]
    page_type_text = ", ".join([f"{key}:{value}" for key, value in page_type_counts.items() if value > 0])
    topics_text = "、".join(main_topics) if main_topics else "未特定"
    site_name = site_title or "サイト"

    return (
        f"{site_name} の構造化サイト解析結果です。"
        f" 起点URLは {site_url} です。"
        f" {page_count} ページから {block_count} 件の意味ブロックを抽出しました。"
        f" ページ種別の内訳は {page_type_text or '未集計'} です。"
        f" 主なトピックは {topics_text} です。"
    )


@mcp.tool()
def crawl_site(url: str, max_pages: int = 10, max_length_per_page: int = 2000) -> str:
    """サイトを意味単位の JSON へ構造化して返す。"""
    if not url.startswith(("http://", "https://")):
        return f"エラー: 無効な URL です: {url}"

    max_pages = max(1, min(max_pages, 30))
    seed_url = _canonicalize_url(url, url)
    visited: set[str] = set()
    # 優先度付きキュー: (優先度スコア, URL)
    # 優先度スコアが低いほど先に処理される（パス深度が浅い、またはトップページに近い）
    queue: list[tuple[int, str]] = [(0, seed_url)]
    pages: list[dict] = []
    detected_languages: set[str] = set()
    aggregation_page_count = 0
    page_type_counts: dict[str, int] = {"content": 0, "list": 0, "form": 0, "navigation": 0, "system": 0}
    site_title = ""
    structured_blocks: list[dict] = []

    while queue and len(visited) < max_pages:
        # 優先度スコアが最も低い（トップページに最も近い）ページを処理
        queue.sort(key=lambda x: x[0])
        _, current_url = queue.pop(0)
        
        clean_url = _canonicalize_url(current_url, seed_url)
        if clean_url in visited:
            continue
        visited.add(clean_url)

        try:
            html = _fetch_raw_html(clean_url)
        except Exception as e:
            pages.append(f"## [{clean_url}]\nエラー: {e}\n")
            continue

        # ページの言語を検出
        detected_lang = _detect_page_language(html)
        if detected_lang:
            detected_languages.add(detected_lang)

        parser = _SiteParser(clean_url)
        try:
            parser.feed(html)
        except Exception:
            pass

        # 見出し構造の整形
        heading_lines: list[str] = []
        for tag, text in parser.headings:
            indent = "  " * (int(tag[1]) - 1)
            heading_lines.append(f"{indent}[{tag.upper()}] {text}")

        body = parser.get_body_text(max_length_per_page)
        if len(body.strip()) < 40:
            body = _extract_text_fallback(html, max_length_per_page)
        
        # 多言語マーカーを削除
        body = _filter_multilingual_content(body, detected_lang)
        
        title = parser.title.strip() or "(タイトルなし)"
        if not site_title and title and title != "(タイトルなし)":
            site_title = title

        page_type = _classify_page_type(clean_url, title, body, html, len(parser.links))
        page_type_counts[page_type] = page_type_counts.get(page_type, 0) + 1

        page_record = {
            "url": clean_url,
            "title": title,
            "page_type": page_type,
            "headings": [text for _, text in parser.headings],
            "body": body,
        }
        pages.append(page_record)

        if page_type in {"list", "form", "navigation"}:
            # 一覧・フォーム・ナビは知識ブロックとして扱わない
            pass
        else:
            section_titles = page_record["headings"] if page_record["headings"] else [title]
            chunks = _split_into_chunks(body)
            if not chunks and title:
                chunks = [title]

            for idx, chunk in enumerate(chunks):
                section_title = section_titles[idx] if idx < len(section_titles) else title
                block = _build_block(title, chunk, clean_url, section_title=section_title)
                if block is not None:
                    if page_type == "system":
                        block["label"] = block["label"] if block["label"] != "misc_info" else "system_info"
                    structured_blocks.append(block)

        # リンク集ページからのリンク追加を制限
        is_aggregation = _is_link_aggregation_page(clean_url)
        links_to_add = parser.links[:5] if is_aggregation else parser.links
        
        if is_aggregation:
            aggregation_page_count += 1

        # 未訪問リンクをキューに優先度スコア付きで追加
        for link in links_to_add:
            link_clean = _canonicalize_url(link, seed_url)
            if link_clean not in visited and not any(u == link_clean for _, u in queue):
                depth = _get_url_depth(link_clean, seed_url)
                queue.append((depth, link_clean))

    if not pages:
        return f"コンテンツを取得できませんでした: {url}"

    # 共通テキストを検出・除去（ナビゲーション・フッター削減）
    all_bodies = [p.get("body", "") for p in pages if isinstance(p, dict)]
    common_lines = _collect_common_text_lines(all_bodies, min_frequency=0.5)
    
    # ページから共通テキストを除去し、ブロック生成を再実行
    structured_blocks = []
    for page_record in pages:
        if not isinstance(page_record, dict) or page_record.get("page_type") in {"list", "form", "navigation"}:
            continue
        
        title = page_record.get("title", "")
        body = page_record.get("body", "")
        clean_url = page_record.get("url", "")
        page_type = page_record.get("page_type", "content")
        
        # 共通テキストを除去
        if common_lines:
            body = _remove_common_text(body, common_lines)
        
        if not body.strip():
            continue
        
        headings = page_record.get("headings", [])
        section_titles = headings if headings else [title]
        chunks = _split_into_chunks(body)
        if not chunks and title:
            chunks = [title]

        for idx, chunk in enumerate(chunks):
            section_title = section_titles[idx] if idx < len(section_titles) else title
            block = _build_block(title, chunk, clean_url, section_title=section_title, site_name=site_title)
            if block is not None:
                if page_type == "system":
                    block["label"] = block["label"] if block["label"] != "misc_info" else "system_info"
                structured_blocks.append(block)

    # 同一テーマの分割を統合し、重複を削減
    structured_blocks = _merge_related_blocks(_dedupe_blocks(structured_blocks))

    # 言語警告を追加（JSON内の説明として保持）
    lang_warning = ""
    if detected_languages and not all(lang in ["ja", "jp"] for lang in detected_languages):
        other_langs = [lang.upper() for lang in detected_languages if lang not in ["ja", "jp"]]
        lang_warning = f"このサイトに {', '.join(other_langs)} などの多言語コンテンツが含まれています。解析時は日本語コンテンツのみを使用してください。"

    summary = _build_site_summary(
        site_title=site_title,
        site_url=url,
        page_count=len(pages),
        block_count=len(structured_blocks),
        page_type_counts=page_type_counts,
        blocks=structured_blocks,
    )

    # priority 4（特設コンテンツ）を分離
    normal_blocks = [b for b in structured_blocks if b.get("priority", 2) < 4]
    special_blocks = [b for b in structured_blocks if b.get("priority", 2) == 4]

    payload = {
        "site_summary": summary if not lang_warning else f"{summary} {lang_warning}",
        "blocks": normal_blocks,
        "site_special_content": special_blocks,
        "meta": {
            "source_url": url,
            "max_pages": max_pages,
            "page_count": len(pages),
            "page_type_counts": page_type_counts,
            "language_warning": lang_warning,
            "aggregation_page_count": aggregation_page_count,
            "normal_block_count": len(normal_blocks),
            "special_content_count": len(special_blocks),
        },
    }

    return json.dumps(payload, ensure_ascii=False, indent=2)


def _strip_html_tags(text: str) -> str:
    """HTML?????????????????"""
    if not text:
        return ""
    cleaned = re.sub(r"<[^>]+>", " ", text)
    cleaned = html.unescape(cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def _fetch_raw_html(url: str, timeout: int = 15) -> str:
    """URL?????????HTML?????????"""
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0 Safari/537.36"
            ),
            "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def _normalize_duckduckgo_url(url: str) -> str:
    """DuckDuckGo???????URL??URL????"""
    if not url:
        return ""

    cleaned = html.unescape(url.strip())
    if cleaned.startswith("//"):
        cleaned = "https:" + cleaned

    parsed = urllib.parse.urlparse(cleaned)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        params = urllib.parse.parse_qs(parsed.query)
        for key in ("uddg", "u", "rut"):
            values = params.get(key)
            if values and values[0]:
                return urllib.parse.unquote(values[0])

    return cleaned


def _sanitize_search_query(query: str) -> str:
    """search_web に渡されたクエリを検索向けに正規化する。"""
    q = (query or "").strip()
    if not q:
        return ""

    q = re.sub(r"\[(?:neutral|joyful|sad|angry)\]", "", q, flags=re.IGNORECASE)
    q = re.sub(r"\s+", " ", q).strip()
    q = re.sub(r"^(?:私(?:、|は)?|わたし(?:、|は)?)\s*", "", q)

    intro_topic = re.search(
        r"^(?:インターネット|web|ウェブ)で([^\n。！？]{1,40}?)(?:を|に関する)",
        q,
        flags=re.IGNORECASE,
    )
    if intro_topic and intro_topic.group(1).strip():
        return intro_topic.group(1).strip()

    quoted = re.search(r"[「\"]([^「」\"\n]{2,80})[」\"]", q)
    if quoted and quoted.group(1).strip():
        return quoted.group(1).strip()

    tried = re.search(r"([^\n。！？]{2,80}?)を調べ(?:ようとした|ました|た結果)", q)
    if tried and tried.group(1).strip():
        return tried.group(1).strip()

    about = re.search(r"([^\n。！？]{2,60}?)に関する(?:情報|知識)", q)
    if about and about.group(1).strip():
        return about.group(1).strip()

    web_about = re.search(
        r"(?:web|ウェブ|インターネット)で([^\n。！？]{1,40}?)に関する(?:情報|知識)を?検索",
        q,
        flags=re.IGNORECASE,
    )
    if web_about and web_about.group(1).strip():
        return web_about.group(1).strip()

    if len(q) > 120:
        sentence = re.split(r"[。！？!?]", q, maxsplit=1)[0].strip()
        q = sentence or q[:120]

    return q[:120].strip()


def _extract_duckduckgo_results(raw_html: str, limit: int = 5) -> list[dict[str, str]]:
    """DuckDuckGo?HTML????????????"""
    results: list[dict[str, str]] = []
    if not raw_html:
        return results

    title_pattern = re.compile(
        r'<a\b(?=[^>]*\bclass="[^"]*\bresult__a\b[^"]*")(?=[^>]*\bhref="(?P<href>[^"]+)")[^>]*>(?P<title>.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )
    matches = list(title_pattern.finditer(raw_html))

    # DuckDuckGo lite 形式の保険
    if not matches:
        lite_pattern = re.compile(
            r'<a\b(?=[^>]*\bclass="[^"]*\bresult-link\b[^"]*")(?=[^>]*\bhref="(?P<href>[^"]+)")[^>]*>(?P<title>.*?)</a>',
            re.IGNORECASE | re.DOTALL,
        )
        matches = list(lite_pattern.finditer(raw_html))

    for index, match in enumerate(matches):
        if len(results) >= limit:
            break

        next_start = matches[index + 1].start() if index + 1 < len(matches) else len(raw_html)
        chunk = raw_html[match.end():next_start]

        title = _strip_html_tags(match.group("title"))
        url = _normalize_duckduckgo_url(match.group("href"))

        snippet = ""
        snippet_match = re.search(
            r'class="result__snippet"[^>]*>(?P<snippet>.*?)</(?:a|span)>',
            chunk,
            re.IGNORECASE | re.DOTALL,
        )
        if snippet_match:
            snippet = _strip_html_tags(snippet_match.group("snippet"))

        if not title and not url and not snippet:
            continue

        domain = urllib.parse.urlparse(url).netloc if url else ""
        if domain.startswith("www."):
            domain = domain[4:]

        results.append(
            {
                "title": title or url or f"???? {len(results) + 1}",
                "url": url,
                "domain": domain,
                "snippet": snippet,
            }
        )

    return results


def _is_search_challenge_page(raw_html: str) -> bool:
    """検索結果ページではなく challenge ページが返っているか判定する。"""
    if not raw_html:
        return False

    lowered = raw_html.lower()
    challenge_markers = (
        "anomaly",
        "challenge",
        "captcha",
        "unusual traffic",
        "automated requests",
    )

    has_marker = any(marker in lowered for marker in challenge_markers)
    has_result_anchor = (
        "result__a" in lowered
        or "result-link" in lowered
        or "result__snippet" in lowered
    )

    return has_marker and not has_result_anchor


def _search_wikipedia_results(query: str, limit: int = 5) -> list[dict[str, str]]:
    """検索フォールバック: Wikipedia API から結果候補を取得する。"""
    results: list[dict[str, str]] = []
    if not query:
        return results

    def _fetch(lang: str) -> list[dict[str, str]]:
        api_url = (
            f"https://{lang}.wikipedia.org/w/api.php"
            f"?action=query&list=search&srsearch={urllib.parse.quote_plus(query)}"
            f"&format=json&utf8=1&srlimit={max(1, min(limit, 10))}"
        )
        raw = _fetch_raw_html(api_url, timeout=12)
        data = json.loads(raw)
        search_items = (data.get("query", {}) or {}).get("search", []) or []

        fetched: list[dict[str, str]] = []
        for item in search_items:
            title = _strip_html_tags(str(item.get("title", "")))
            snippet = _strip_html_tags(str(item.get("snippet", "")))
            if not title:
                continue

            page_path = urllib.parse.quote(title.replace(" ", "_"), safe="()")
            url = f"https://{lang}.wikipedia.org/wiki/{page_path}"
            fetched.append(
                {
                    "title": title,
                    "url": url,
                    "domain": f"{lang}.wikipedia.org",
                    "snippet": snippet,
                }
            )

        return fetched

    try:
        results = _fetch("ja")
        if not results:
            results = _fetch("en")
    except Exception:
        return []

    return results[:limit]





def _derive_query_keywords(query: str) -> list[str]:
    tokens = re.findall(r"[A-Za-z0-9]+|[?-??-??-?]{2,}", query or "")
    stopwords = {
        "??", "????", "??", "???", "????", "???", "???", "??", "??", "??",
        "?", "??", "??", "??", "???", "????", "Web", "web", "???", "???????",
        "????", "??", "???", "???", "??", "????",
    }
    keywords: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        normalized = token.lower()
        if len(normalized) < 2:
            continue
        if normalized in stopwords:
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        keywords.append(token)
    return keywords[:8]



def _score_search_result_for_deep_fetch(item: dict[str, str], query_keywords: list[str], index: int) -> int:
    title = item.get("title", "")
    snippet = item.get("snippet", "")
    domain = item.get("domain", "")

    score = max(0, 12 - index)

    title_lower = title.lower()
    snippet_lower = snippet.lower()
    domain_lower = domain.lower()

    if query_keywords and any(keyword.lower() in title_lower for keyword in query_keywords):
        score += 5
    if query_keywords and any(keyword.lower() in snippet_lower for keyword in query_keywords):
        score += 4
    if domain_lower.endswith((".go.jp", ".gov", ".ac.jp")):
        score += 4
    if any(host in domain_lower for host in ("jma.go.jp", "noaa.gov", "nhk.or.jp", "weathernews.jp", "yahoo.co.jp")):
        score += 3
    if snippet:
        score += 1

    return score



def _select_top_search_results(results: list[dict[str, str]], query: str, limit: int = 3) -> list[dict[str, str]]:
    keywords = _derive_query_keywords(query)
    ranked: list[tuple[int, int, dict[str, str]]] = []
    for index, item in enumerate(results):
        ranked.append((_score_search_result_for_deep_fetch(item, keywords, index), index, item))

    ranked.sort(key=lambda pair: (pair[0], -pair[1]), reverse=True)
    selected = [item for score, _, item in ranked if score > 0]
    if not selected:
        selected = [item for _, _, item in ranked]
    return selected[:limit]




def _clean_summary_text(text: str) -> str:
    if not text:
        return ""

    lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if re.search(r'^(?:????|Language warning|WARNING|NOTE:)', line, re.IGNORECASE):
            continue
        lines.append(line)

    cleaned = "\n".join(lines)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()



def _summarize_page_text(text: str, query: str, max_length: int = 700) -> str:
    cleaned = _clean_summary_text(text)
    if not cleaned:
        return ""

    raw_chunks = re.split(r'(?<=[???!?])\s+|[\r\n]+', cleaned)
    chunks: list[str] = []
    for raw_chunk in raw_chunks:
        chunk = re.sub(r"\s+", " ", raw_chunk).strip()
        if len(chunk) < 18:
            continue
        if _is_navigation_or_footer_noise(chunk):
            continue
        chunks.append(chunk)

    if not chunks:
        normalized = re.sub(r"\s+", " ", cleaned).strip()
        return _truncate_text(normalized, max_length)

    keywords = _derive_query_keywords(query)
    scored: list[tuple[int, int, str]] = []
    for index, chunk in enumerate(chunks):
        score = 0
        chunk_lower = chunk.lower()
        for keyword in keywords:
            if keyword.lower() in chunk_lower:
                score += 3
        if _contains_japanese(chunk):
            score += 1
        score += min(len(chunk) // 120, 3)
        scored.append((score, index, chunk))

    scored.sort(key=lambda pair: (pair[0], -pair[1]), reverse=True)
    picked: list[tuple[int, str]] = []
    for score, index, chunk in scored:
        if score <= 0 and picked:
            continue
        picked.append((index, chunk))
        if len(picked) >= 4:
            break

    if not picked:
        picked = list(enumerate(chunks[:4]))

    picked.sort(key=lambda pair: pair[0])
    summary = " ".join(chunk for _, chunk in picked)
    summary = re.sub(r"\s+", " ", summary).strip()
    return _truncate_text(summary, max_length)



def _fetch_url_summary(url: str, query: str, max_length: int = 2500) -> dict[str, str]:
    try:
        page_text = fetch_url(url, max_length=max_length)
        cleaned = _clean_summary_text(page_text)
        summary = _summarize_page_text(cleaned, query, max_length=700)
        if not summary:
            summary = _truncate_text(cleaned, 700)
        return {
            "status": "ok",
            "summary": summary,
            "excerpt": _truncate_text(cleaned, 1200),
        }
    except Exception as err:
        return {
            "status": "error",
            "summary": "",
            "excerpt": "",
            "error": str(err),
        }


def _format_search_results(query: str, results: list[dict[str, str]]) -> str:
    """??????????????????"""
    lines = [f"????: {query}", ""]
    for index, item in enumerate(results, 1):
        title = item.get("title", "")
        url = item.get("url", "")
        domain = item.get("domain", "")
        snippet = item.get("snippet", "")

        lines.append(f"{index}. {title}")
        if domain:
            lines.append(f"???: {domain}")
        if url:
            lines.append(f"URL: {url}")
        if snippet:
            lines.append(f"??: {snippet}")
        lines.append("")

    return "\\n".join(lines).rstrip()


def _contains_japanese(text: str) -> bool:
    """??????????????????"""
    return bool(re.search(r"[?-??-??-?]", text or ""))


def _prefer_japanese_results(results: list[dict[str, str]], limit: int = 5) -> list[dict[str, str]]:
    """????????????????"""
    ranked: list[tuple[int, dict[str, str]]] = []
    for item in results:
        title = item.get("title", "")
        snippet = item.get("snippet", "")
        domain = item.get("domain", "")
        score = 0
        if _contains_japanese(title):
            score += 3
        if _contains_japanese(snippet):
            score += 2
        if _contains_japanese(domain):
            score += 1
        ranked.append((score, item))

    ranked.sort(key=lambda pair: pair[0], reverse=True)
    preferred = [item for score, item in ranked if score > 0]
    return (preferred or results)[:limit]


def _truncate_text(text: str, max_length: int) -> str:
    """????????????????????"""
    if not text:
        return ""
    if max_length <= 0:
        return text
    if len(text) <= max_length:
        return text
    suffix = "\n... (??)"
    if max_length <= len(suffix):
        return text[:max_length]
    return text[: max_length - len(suffix)].rstrip() + suffix


def _maybe_save_search_web_json(payload: dict, suffix: str = "") -> None:
    """調査用: search_web の JSON ペイロードをローカル保存する。"""
    # 既定は有効。無効化する場合は FETCH_DEBUG_SAVE_JSON=0 を設定。
    enabled = os.getenv("FETCH_DEBUG_SAVE_JSON", "1").strip().lower() not in {"0", "false", "off", "no"}
    if not enabled:
        return

    try:
        output_dir = Path(os.getenv("FETCH_DEBUG_DIR", str(Path(__file__).resolve().parent / "debug-json")))
        output_dir.mkdir(parents=True, exist_ok=True)

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        suffix_part = f"_{suffix}" if suffix else ""
        output_path = output_dir / f"search_web{suffix_part}_{stamp}.json"

        with output_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception:
        # デバッグ保存の失敗で本処理は止めない
        pass


def _maybe_log_search_web_debug(stage: str, payload: dict) -> None:
    """調査用: search_web の各段階をログと JSON に出力する。"""
    enabled = os.getenv("FETCH_DEBUG_LOG_SEARCH_WEB", "1").strip().lower() not in {"0", "false", "off", "no"}
    if not enabled:
        return

    try:
        _LOGGER.warning("[search_web:%s] %s", stage, json.dumps(payload, ensure_ascii=False))
    except Exception:
        pass

    _maybe_save_search_web_json({"stage": stage, **payload}, suffix=stage)



def _safe_truncate(text: str, limit: int) -> str:
    text = text or ""
    if limit <= 0:
        return ""
    return text if len(text) <= limit else text[:limit] + "..."


def _safe_log_search_web_debug(stage: str, payload: dict) -> None:
    try:
        _maybe_log_search_web_debug(stage, payload)
    except Exception:
        pass


def _safe_save_search_web_json(payload: dict, suffix: str | None = None) -> None:
    try:
        if suffix:
            _maybe_save_search_web_json(payload, suffix=suffix)
        else:
            _maybe_save_search_web_json(payload)
    except Exception:
        pass


def _is_safe_fetch_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False

        host = (parsed.hostname or "").lower()
        if not host:
            return False

        blocked_hosts = {
            "localhost",
            "127.0.0.1",
            "0.0.0.0",
            "::1",
        }

        if host in blocked_hosts:
            return False

        if host.startswith("10.") or host.startswith("192.168."):
            return False

        if host.startswith("172."):
            parts = host.split(".")
            if len(parts) >= 2 and parts[1].isdigit():
                if 16 <= int(parts[1]) <= 31:
                    return False

        return True
    except Exception:
        return False

@mcp.tool()
def search_web(query: str, max_length: int = 5000) -> str:
    """Search web pages and return structured JSON with fetched summaries."""
    try:
        original_q = (query or "").strip()
        q = _sanitize_search_query(original_q)
        max_length = max(500, int(max_length or 5000))

        if not q:
            return json.dumps(
                {
                    "query": "",
                    "engine": "duckduckgo",
                    "results": [],
                    "notes": ["query is empty"],
                },
                ensure_ascii=False,
            )

        search_url = f"https://duckduckgo.com/html/?q={urllib.parse.quote_plus(q)}&kl=jp-ja"

        _safe_log_search_web_debug(
            "query",
            {
                "query": q,
                "original_query": original_q,
                "search_url": search_url,
                "max_length": max_length,
            },
        )

        raw_html = _fetch_raw_html(search_url)
        challenge_detected = _is_search_challenge_page(raw_html)
        results = _extract_duckduckgo_results(raw_html, limit=10)
        used_fallback = False

        if not results and challenge_detected:
            wiki_results = _search_wikipedia_results(q, limit=10)
            if wiki_results:
                results = wiki_results
                used_fallback = True

        results = _prefer_japanese_results(results, limit=10)
        selected_results = _select_top_search_results(results, q, limit=3)

        page_title_match = re.search(r"<title[^>]*>(.*?)</title>", raw_html, re.IGNORECASE | re.DOTALL)
        page_title = _strip_html_tags(page_title_match.group(1)) if page_title_match else ""

        _safe_log_search_web_debug(
            "results",
            {
                "query": q,
                "original_query": original_q,
                "search_url": search_url,
                "raw_html_length": len(raw_html),
                "page_title": page_title,
                "challenge_detected": challenge_detected,
                "used_fallback": used_fallback,
                "has_result_anchor": ("result__a" in raw_html) or ("result-link" in raw_html),
                "result_count": len(results),
                "selected_count": len(selected_results),
                "results": results,
                "selected_results": selected_results,
            },
        )

        enriched_results: list[dict[str, str]] = []

        if selected_results:
            safe_selected_results = [
                item for item in selected_results
                if _is_safe_fetch_url(item.get("url", ""))
            ]

            max_workers = min(3, len(safe_selected_results))
            summaries: dict[int, dict[str, str]] = {}

            if max_workers > 0:
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    future_to_index = {
                        executor.submit(_fetch_url_summary, item.get("url", ""), q): index
                        for index, item in enumerate(safe_selected_results)
                    }

                    for future in as_completed(future_to_index):
                        index = future_to_index[future]
                        try:
                            summaries[index] = future.result()
                        except Exception as err:
                            summaries[index] = {
                                "status": "error",
                                "summary": "",
                                "excerpt": "",
                                "error": str(err),
                            }

            safe_url_to_summary = {
                item.get("url", ""): summaries.get(index, {
                    "status": "error",
                    "summary": "",
                    "excerpt": "",
                })
                for index, item in enumerate(safe_selected_results)
            }

            for index, item in enumerate(selected_results, 1):
                url = item.get("url", "")

                if not _is_safe_fetch_url(url):
                    summary_info = {
                        "status": "blocked_url",
                        "summary": "",
                        "excerpt": "",
                    }
                else:
                    summary_info = safe_url_to_summary.get(url, {
                        "status": "error",
                        "summary": "",
                        "excerpt": "",
                    })

                enriched_results.append(
                    {
                        "rank": str(index),
                        "title": _safe_truncate(item.get("title", ""), 300),
                        "url": url,
                        "domain": item.get("domain", ""),
                        "snippet": _safe_truncate(item.get("snippet", ""), 1000),
                        "page_summary": _safe_truncate(
                            summary_info.get("summary", ""),
                            max_length,
                        ),
                        "page_excerpt": _safe_truncate(
                            summary_info.get("excerpt", ""),
                            min(max_length, 2000),
                        ),
                        "page_summary_status": summary_info.get("status", "error"),
                    }
                )

        notes: list[str] = []

        if not results:
            notes.append("検索結果が見つかりませんでした")
            if challenge_detected:
                notes.append("検索エンジンの challenge ページを検出しました")
        elif not selected_results:
            notes.append("検索結果はありましたが、取得対象を選択できませんでした")
        elif not enriched_results:
            notes.append("取得対象のページ本文を取得できませんでした")

        if used_fallback:
            notes.append("DuckDuckGo challenge 回避のため Wikipedia API フォールバックを使用しました")

        payload = {
            "query": q,
            "original_query": original_q,
            "engine": "duckduckgo+wikipedia" if used_fallback else "duckduckgo",
            "search_url": search_url,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "challenge_detected": challenge_detected,
            "used_fallback": used_fallback,
            "result_count": len(results),
            "selected_count": len(enriched_results),
            "results": enriched_results,
            "notes": notes,
        }

        _safe_log_search_web_debug("final", payload)
        _safe_save_search_web_json(payload)

        return json.dumps(payload, ensure_ascii=False)

    except Exception as err:
        error_payload = {
            "query": query or "",
            "engine": "duckduckgo",
            "results": [],
            "notes": [f"error: {err}"],
        }

        _safe_log_search_web_debug("error", error_payload)
        _safe_save_search_web_json(error_payload, suffix="error")

        return json.dumps(error_payload, ensure_ascii=False)

@mcp.tool()
def search_files(directory: str, pattern: str, max_results: int = 20) -> str:
    """
    ディレクトリ内のファイルを検索する
    
    Args:
        directory: 検索するディレクトリパス
        pattern: 検索パターン（ファイル名用の正規表現またはワイルドカード）
        max_results: 最大結果数（デフォルト: 20）
    
    Returns:
        マッチしたファイル一覧
    """
    try:
        dir_path = Path(directory).resolve()
        
        # セキュリティ: パスが存在するか確認
        if not dir_path.exists():
            return f"エラー: ディレクトリが見つかりません: {directory}"
        
        if not dir_path.is_dir():
            return f"エラー: ディレクトリではありません: {directory}"
        
        # ワイルドカード形式を正規表現に変換
        regex_pattern = pattern.replace(".", r"\.").replace("*", ".*").replace("?", ".")
        
        matches = []
        for file_path in dir_path.rglob("*"):
            if len(matches) >= max_results:
                break
            
            if re.search(regex_pattern, file_path.name, re.IGNORECASE):
                rel_path = file_path.relative_to(dir_path)
                file_type = "フォルダ" if file_path.is_dir() else "ファイル"
                matches.append(f"  {rel_path} ({file_type})")
        
        if not matches:
            return f"マッチするファイルが見つかりません (パターン: {pattern})"
        
        result = f"検索結果 ({len(matches)} 件):\n" + "\n".join(matches)
        if len(list(dir_path.rglob("*"))) > max_results:
            result += f"\n... (最大 {max_results} 件まで表示)"
        return result
    
    except Exception as e:
        return f"検索エラー: {e}"


@mcp.tool()
def read_file(file_path: str, max_lines: int = 100) -> str:
    """
    ファイルの内容を読み込む
    
    Args:
        file_path: 読み込むファイルパス
        max_lines: 最大行数（デフォルト: 100）
    
    Returns:
        ファイルの内容
    """
    try:
        path = Path(file_path).resolve()
        
        # セキュリティ: ファイルが存在するか確認
        if not path.exists():
            return f"エラー: ファイルが見つかりません: {file_path}"
        
        if not path.is_file():
            return f"エラー: ファイルではありません: {file_path}"
        
        # テキストファイルのみ対応
        try:
            with open(path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
        except UnicodeDecodeError:
            return f"エラー: UTF-8 で読み込めません（バイナリファイルの可能性）: {file_path}"
        
        # 行数制限
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            content = "".join(lines) + f"\n... (以下省略、全体 {len(lines)} 行)"
        else:
            content = "".join(lines)
        
        return content
    
    except Exception as e:
        return f"ファイル読み込みエラー: {e}"


@mcp.tool()
def search_content(directory: str, query: str, file_pattern: str = "*", max_matches: int = 10) -> str:
    """
    ディレクトリ内のファイルから特定のテキストを検索する
    
    Args:
        directory: 検索するディレクトリ
        query: 検索するテキスト（正規表現対応）
        file_pattern: ファイル名パターン（デフォルト: すべてのファイル）
        max_matches: 最大マッチ数（デフォルト: 10）
    
    Returns:
        マッチした行一覧
    """
    try:
        dir_path = Path(directory).resolve()
        
        if not dir_path.exists() or not dir_path.is_dir():
            return f"エラー: ディレクトリが見つかりません: {directory}"
        
        matches = []
        
        # ファイルパターンを正規表現に変換
        file_regex = file_pattern.replace(".", r"\.").replace("*", ".*").replace("?", ".")
        
        for file_path in dir_path.rglob("*"):
            if len(matches) >= max_matches:
                break
            
            if not file_path.is_file():
                continue
            
            if not re.search(file_regex, file_path.name, re.IGNORECASE):
                continue
            
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    for line_num, line in enumerate(f, 1):
                        if len(matches) >= max_matches:
                            break
                        
                        if re.search(query, line, re.IGNORECASE):
                            rel_path = file_path.relative_to(dir_path)
                            matches.append(f"  {rel_path}:{line_num} - {line.strip()}")
            except (UnicodeDecodeError, IOError):
                # バイナリファイルはスキップ
                continue
        
        if not matches:
            return f"マッチするコンテンツが見つかりません (クエリ: {query})"
        
        return f"検索結果 ({len(matches)} 件):\n" + "\n".join(matches)
    
    except Exception as e:
        return f"検索エラー: {e}"


@mcp.tool()
def list_directory(directory: str, max_items: int = 50) -> str:
    """
    ディレクトリの内容を一覧表示する
    
    Args:
        directory: 一覧するディレクトリパス
        max_items: 最大表示数（デフォルト: 50）
    
    Returns:
        ディレクトリ内のアイテム一覧
    """
    try:
        dir_path = Path(directory).resolve()
        
        if not dir_path.exists():
            return f"エラー: ディレクトリが見つかりません: {directory}"
        
        if not dir_path.is_dir():
            return f"エラー: ディレクトリではありません: {directory}"
        
        items = []
        for item in sorted(dir_path.iterdir()):
            if len(items) >= max_items:
                break
            
            item_type = "📁 フォルダ" if item.is_dir() else "📄 ファイル"
            items.append(f"  {item_type:12} {item.name}")
        
        result = f"ディレクトリ: {dir_path}\n\n" + "\n".join(items)
        total = len(list(dir_path.iterdir()))
        if total > max_items:
            result += f"\n... ({total - max_items} 個のアイテムを省略)"
        return result
    
    except Exception as e:
        return f"ディレクトリ読み込みエラー: {e}"


# ─── メタデータ取得ツール ──────────────────────────────

@mcp.tool()
def get_server_metadata() -> str:
    """
    このMCPサーバーのメタデータを JSON 形式で返す
    injection-tool での自動インポート用
    
    Returns:
        MCPサーバーの設定情報を JSON 形式で返す
    """
    import json
    
    metadata = {
        "name": "fetch-server",
        "description": "ウェブサイトとファイルの検索・取得機能を提供するMCPサーバー",
        "tools": [
            {
                "name": "fetch_url",
                "description": "URL のコンテンツを取得する（HTMLタグを削除してテキスト化）"
            },
            {
                "name": "search_web",
                "description": "キーワードでWeb検索し、検索結果ページの内容を取得する"
            },
            {
                "name": "search_files",
                "description": "ディレクトリ内のファイルをワイルドカード/正規表現で検索"
            },
            {
                "name": "read_file",
                "description": "ファイルの内容を読み込む（行数制限可能）"
            },
            {
                "name": "search_content",
                "description": "ディレクトリ内のファイルからテキストを検索（grep的）"
            },
            {
                "name": "list_directory",
                "description": "ディレクトリの内容を一覧表示"
            },
            {
                "name": "crawl_site",
                "description": "指定URLからサイト全体をクロールし、見出し・本文を構造化して返す（AIプロンプト素材生成用）"
            }
        ],
        "defaultConfig": {
            "mode": "ai",
            "timeout": 10000,
            "aiRouting": {
                "provider": "ollama",
                "model": "qwen2.5:7b",
                "temperature": 0.7,
                "maxTokens": 2000,
                "confidenceThreshold": 0.6,
                "allowedTools": [
                    "fetch_url",
                    "search_web",
                    "search_files",
                    "read_file",
                    "search_content",
                    "list_directory",
                    "crawl_site"
                ],
                "fallbackTool": "list_directory",
                "systemPrompt": "あなたは Web / ファイル検索と取得を支援する共通 MCP 補助役です。どのドメインでも一貫して、検索結果を優先し、要点を整理して返してください。\\n\\n最重要ルール:\\n- 取得した結果がある場合は、その内容を最優先で使う。\\n- 結果をそのまま長く貼らず、会話に使いやすい形で短く要約する。\\n- 結果にないことは推測で断定しない。必要なら「確認できた範囲では」と添える。\\n- 「直接調べられません」などの回避表現は、結果があるのに使わない。\\n- 複数結果がある場合は、共通点を優先し、差分があれば明示する。\\n- 最新性が重要な質問では、取得日時や更新時点を明示する。\\n- ドメイン固有の事情に引っ張られすぎず、どの対象にも再利用できる形でまとめる。\\n\\n出力方針:\\n- 先に結論\\n- 次に根拠\\n- 必要なら注意点\\n- 余計な前置きはしない\\n- 日本語で、丁寧かつ簡潔に返す"
            }
        }
    }
    
    return json.dumps(metadata, ensure_ascii=False, indent=2)


# ─── リソース定義 ──────────────────────────────────────

@mcp.resource("info://server")
def get_server_info() -> str:
    """サーバー情報を返す"""
    return (
        "Fetch MCP サーバー v1.0\n"
        "ウェブサイトとファイルの検索・取得機能を提供します。\n"
        "\n利用可能なツール:\n"
        "  - fetch_url: URL のコンテンツを取得\n"
        "  - search_web: キーワードでWeb検索\n"
        "  - search_files: ファイルを検索\n"
        "  - read_file: ファイルの内容を読み込む\n"
        "  - search_content: ファイル内のテキストを検索\n"
        "  - list_directory: ディレクトリの内容を一覧表示\n"
        "  - crawl_site: サイト全体をクロールして構造化コンテンツを返す"
    )


# ─── エントリーポイント ────────────────────────────────

if __name__ == "__main__":
    # SSE トランスポートで起動（HTTP: http://0.0.0.0:8000/sse）
    mcp.run(transport="sse")
