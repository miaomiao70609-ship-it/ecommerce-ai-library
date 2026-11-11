#!/usr/bin/env python3
"""电商+AI 动态内容库 · 每日数据采集与整理脚本

运行方式：
  python3 scripts/update_feed.py
环境变量（可选，提供后走 LLM 高质量摘要与选题生成）：
  LLM_API_KEY   LLM 服务的 API Key
  LLM_BASE_URL  OpenAI 兼容接口地址（含 /v3 或 /v1），默认 https://ark.cn-beijing.volces.com/api/v3
  LLM_MODEL     模型名，默认 doubao-seed-1.6
输出：
  data/daily-feed.json  结构化数据
  data/daily-feed.js    页面直接加载的 window.DAILY_FEED
"""
import json
import os
import re
import hashlib
import html as html_mod
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from xml.etree import ElementTree as ET

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
TIMEOUT = 12

# ============ 数据源 ============
RSS_SOURCES = [
    "https://www.tmtpost.com/rss.xml",           # 钛媒体
    "https://www.ifanr.com/feed",                 # 爱范儿
    "https://www.geekpark.net/rss",               # 极客公园
    "https://36kr.com/feed",                       # 36氪（可能返回 HTML，容错）
    "https://www.huxiu.com/rss/0.xml",             # 虎嗅
    "https://www.ebrun.com/rss.xml",               # 亿邦动力
    "https://www.dsb.cn/rss",                      # 电商报
    "https://www.paidai.com/rss.xml",              # 派代
]

# 热搜聚合 API（返回 JSON，容错，失败则跳过）
HOTLIST_SOURCES = [
    ("https://api.vvhan.com/api/hotlist?type=douyinHot", "douyin"),
    ("https://api.vvhan.com/api/hotlist?type=weiboHot", "weibo"),
    ("https://api.tenapi.cn/v1/douyin", "douyin"),
]

# 电商 + AI 关键词过滤
ECOM_AI_KEYWORDS = [
    "电商", "直播", "带货", "商家", "品牌", "平台", "成交", "GMV", "转化",
    "抖音", "快手", "小红书", "淘宝", "天猫", "京东", "拼多多", "美团", "视频号",
    "双11", "618", "大促", "上新", "类目", "流量", "投放", "ROI", "达人", "主播",
    "AI", "大模型", "GPT", "豆包", "文心", "通义", "可灵", "Sora", "Midjourney",
    "Agent", "智能体", "AIGC", "生成式", "多模态", "数字人", "虚拟主播",
    "内容", "短视频", "图文", "种草", "社区", "用户", "数据", "报告",
]

CATEGORY_KEYWORDS = {
    "aiTools": ["AI", "大模型", "GPT", "豆包", "文心", "通义", "可灵", "Sora", "Midjourney",
                "Agent", "智能体", "AIGC", "生成式", "多模态", "数字人", "虚拟主播", "模型"],
    "insights": ["数据", "报告", "白皮书", "研究", "趋势", "洞察", "分析", "用户", "市场", "调研"],
    "competitorNews": ["财报", "上市", "融资", "收购", "李佳琦", "主播", "字节", "阿里", "腾讯",
                       "美团", "京东", "拼多多", "快手", "小红书", "品牌"],
    "hotTopics": ["双11", "618", "大促", "电商", "直播", "带货", "商家", "平台", "成交", "GMV",
                  "抖音", "淘宝", "天猫", "流量", "投放", "ROI"],
}

TOPIC_CATEGORY_MAP = {
    "双11": "trend", "618": "trend", "复盘": "trend", "趋势": "trend",
    "AI": "photo", "模型": "photo", "作图": "photo", "生图": "photo",
    "内容": "content", "短视频": "content", "种草": "content", "起号": "content",
    "工具": "product", "软件": "product", "测评": "product", "实测": "product",
    "直播": "live", "带货": "live", "店播": "live", "主播": "live",
}


# ============ 抓取 ============
def http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read()


def fetch_rss(url):
    items = []
    try:
        raw = http_get(url)
    except Exception:
        return items
    text = raw.decode("utf-8", errors="ignore")
    # 容错：非 XML 直接跳过
    if "<item" not in text and "<entry" not in text:
        return items
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return items
    ns = {"atom": "http://www.w3.org/2005/Atom", "content": "http://purl.org/rss/1.0/modules/content/"}
    for item in root.iter():
        tag = item.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        title = (item.findtext("title") or "").strip()
        desc = (item.findtext("description") or item.findtext("summary") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title:
            continue
        items.append({"title": html_mod.unescape(title),
                      "desc": clean_html(desc),
                      "link": link,
                      "pub": (item.findtext("pubDate") or item.findtext("published") or "")})
    return items


def fetch_hotlist(url, platform):
    items = []
    try:
        raw = http_get(url)
        data = json.loads(raw.decode("utf-8", errors="ignore"))
    except Exception:
        return items
    rows = data.get("data") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return items
    for r in rows:
        if not isinstance(r, dict):
            continue
        title = (r.get("title") or r.get("name") or "").strip()
        desc = (r.get("desc") or r.get("hot") or "").strip()
        if title:
            items.append({"title": title, "desc": str(desc), "link": "", "pub": ""})
    return items


def clean_html(text):
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", text)
    text = html_mod.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ============ 去重 / 过滤 / 分类 ============
def norm(s):
    return re.sub(r"[\s\W_]+", "", s).lower()


def dedupe(items):
    seen = {}
    for it in items:
        key = hashlib.md5(norm(it["title"]).encode("utf-8")).hexdigest()[:12]
        if key in seen:
            # 保留描述更完整的一条
            if len(it.get("desc", "")) > len(seen[key].get("desc", "")):
                seen[key] = it
        else:
            seen[key] = it
    return list(seen.values())


def matches_ecom_ai(text):
    return any(kw.lower() in text.lower() for kw in ECOM_AI_KEYWORDS)


def classify(title, desc):
    text = (title + " " + desc).lower()
    scores = {}
    for bucket, kws in CATEGORY_KEYWORDS.items():
        scores[bucket] = sum(1 for k in kws if k.lower() in text)
    best = max(scores, key=scores.get)
    if scores[best] == 0:
        best = "hotTopics"
    # 标签取命中的第一个关键词
    for k in CATEGORY_KEYWORDS[best]:
        if k.lower() in text:
            return best, k
    return best, "综合"


def infer_topic_category(title):
    for k, cat in TOPIC_CATEGORY_MAP.items():
        if k in title:
            return cat
    return "trend"


def shorten(text, n=120):
    text = clean_html(text)
    if len(text) <= n:
        return text
    return text[:n].rstrip("，。、；：,.;: ") + "…"


# ============ LLM 增强（可选） ============
def llm_enrich(items, key, base_url, model):
    if not key:
        return None
    payload = {
        "model": model,
        "temperature": 0.3,
        "messages": [
            {"role": "system", "content": "你是电商+AI赛道的内容研究员。给你一批新闻标题与摘要，请完成三件事："
             "1) 判断每条新闻归属：hotTopics(平台热点)/aiTools(AI工具)/insights(运营洞察)/competitorNews(竞品动态)；"
             "2) 为每条写一段不超过120字的中文摘要，保留具体数字与来源；"
             "3) 从中提炼5个适合做短视频的选题，返回 JSON。"},
            {"role": "user", "content": json.dumps(items[:40], ensure_ascii=False)},
        ],
    }
    try:
        req = urllib.request.Request(
            base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            resp = json.loads(r.read().decode("utf-8"))
        content = resp["choices"][0]["message"]["content"]
        # 提取 JSON
        m = re.search(r"\{.*\}", content, re.S)
        if not m:
            return None
        return json.loads(m.group(0))
    except Exception as e:
        print(f"[llm] 调用失败，使用启发式：{e}")
        return None


# ============ 组装 ============
def build_feed():
    all_items = []
    for url in RSS_SOURCES:
        all_items.extend(fetch_rss(url))
    for url, plat in HOTLIST_SOURCES:
        all_items.extend(fetch_hotlist(url, plat))

    print(f"[collect] 原始条目 {len(all_items)} 条")
    all_items = dedupe(all_items)
    print(f"[collect] 去重后 {len(all_items)} 条")

    # 过滤电商+AI相关；若太少则放宽（保留全部）
    filtered = [it for it in all_items if matches_ecom_ai(it["title"] + " " + it.get("desc", ""))]
    if len(filtered) < 10:
        filtered = all_items
    print(f"[collect] 电商+AI相关 {len(filtered)} 条")

    # 优先有摘要的、标题较长的
    filtered.sort(key=lambda x: (len(x.get("desc", "")), len(x["title"])), reverse=True)

    # 分类
    buckets = {"hotTopics": [], "aiTools": [], "insights": [], "competitorNews": []}
    for it in filtered:
        bucket, tag = classify(it["title"], it.get("desc", ""))
        buckets[bucket].append({
            "title": it["title"],
            "desc": shorten(it.get("desc", ""), 120) or it["title"],
            "tag": tag,
            "time": "今日",
            "source": it.get("link", ""),
        })

    # 每个桶取前 5
    feed = {"hotTopics": [], "aiTools": [], "insights": [], "topicIdeas": [], "competitorNews": []}
    for k in ["hotTopics", "aiTools", "insights", "competitorNews"]:
        feed[k] = buckets[k][:5]

    # 选题推荐：从 hotTopics + insights 顶部生成
    topic_pool = buckets["hotTopics"] + buckets["insights"]
    topic_ideas = []
    for it in topic_pool[:8]:
        title = it["title"]
        cat = infer_topic_category(title)
        topic_ideas.append({
            "title": title,
            "desc": it["desc"],
            "tag": it["tag"],
            "type": "topic",
            "category": cat,
            "time": "今日推荐",
        })
    feed["topicIdeas"] = topic_ideas[:5]

    # LLM 增强（覆盖摘要与选题）
    key = os.environ.get("LLM_API_KEY")
    base_url = os.environ.get("LLM_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
    model = os.environ.get("LLM_MODEL", "doubao-seed-1.6")
    if key:
        enriched = llm_enrich(filtered[:40], key, base_url, model)
        if enriched and isinstance(enriched, dict):
            for k in ["hotTopics", "aiTools", "insights", "competitorNews"]:
                if isinstance(enriched.get(k), list) and enriched[k]:
                    feed[k] = enriched[k][:5]
            if isinstance(enriched.get("topicIdeas"), list) and enriched["topicIdeas"]:
                feed["topicIdeas"] = enriched["topicIdeas"][:5]
            print("[llm] 已用 LLM 重写摘要与选题")

    return feed


def write_outputs(feed):
    tz = timezone(timedelta(hours=8))
    now = datetime.now(tz)
    feed_with_meta = {"lastUpdate": now.strftime("%Y-%m-%d %H:%M"), **feed}

    data_dir = os.path.join(os.path.dirname(__file__), "..", "data")
    os.makedirs(data_dir, exist_ok=True)

    json_path = os.path.join(data_dir, "daily-feed.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(feed_with_meta, f, ensure_ascii=False, indent=2)

    js_path = os.path.join(data_dir, "daily-feed.js")
    js = (
        "// 电商+AI 内容库 · 每日动态 Feed（自动生成，勿手改）\n"
        f"// 生成时间: {now.strftime('%Y-%m-%d %H:%M')} (Asia/Shanghai)\n"
        "window.DAILY_FEED = " + json.dumps(feed_with_meta, ensure_ascii=False, indent=2) + ";\n"
    )
    with open(js_path, "w", encoding="utf-8") as f:
        f.write(js)

    print(f"[write] {json_path}")
    print(f"[write] {js_path}")
    for k in ["hotTopics", "aiTools", "insights", "topicIdeas", "competitorNews"]:
        print(f"  {k}: {len(feed[k])}")


def main():
    feed = build_feed()
    # 完全无数据时不覆盖，保留旧数据
    if sum(len(v) for v in feed.values()) == 0:
        print("[warn] 本次未抓到任何数据，不覆盖现有文件")
        return
    write_outputs(feed)


if __name__ == "__main__":
    main()
