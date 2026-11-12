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
import sys
import hashlib
import html as html_mod
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
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

# 热搜聚合 API：每个平台配多个兜底接口，任一失败自动跳过。
# 响应结构因服务而异，fetch_hotlist 会做兼容解析。
DOUYIN_SOURCES = [
    "https://api-hot.imsyy.top/douyin",
    "https://api.vvhan.com/api/hotlist?type=douyinHot",
    "https://api.tenapi.cn/v2/douyinhot",
    "https://api.rebang.today/v1/items?type=douyin",
]
XIAOHONGSHU_SOURCES = [
    "https://api-hot.imsyy.top/xiaohongshu",
    "https://api.vvhan.com/api/hotlist?type=xhsHot",
    "https://api.tenapi.cn/v2/xhshot",
    "https://api.rebang.today/v1/items?type=xiaohongshu",
]
WEIBO_SOURCES = [
    "https://api-hot.imsyy.top/weibo",
    "https://api.vvhan.com/api/hotlist?type=weiboHot",
    "https://api.tenapi.cn/v2/weibohot",
]
# B站官方公开 API（无需登录，直接返回 JSON）
BILIBILI_SOURCES = [
    "https://api.bilibili.com/x/web-interface/popular?ps=30&pn=1",   # 热门视频
    "https://api.bilibili.com/x/web-interface/ranking/v2?rid=0&type=all",  # 全站排行榜
    "https://app.bilibili.com/x/v2/search/trending/ranking",         # 热搜词
]
# 每个平台抓取后保留的条目数（按平台配额，避免单一源挤占）
PLATFORM_QUOTA = {"douyin": 15, "xiaohongshu": 15, "weibo": 10, "bilibili": 20}

# 电商 + AI 关键词过滤
ECOM_KEYWORDS = [
    "电商", "直播", "带货", "商家", "品牌", "成交", "GMV", "转化",
    "抖音", "快手", "小红书", "淘宝", "天猫", "京东", "拼多多", "美团", "视频号",
    "双11", "618", "大促", "上新", "类目", "流量", "投放", "ROI", "达人", "主播",
    "店铺", "选品", "退货", "客单", "私域", "复购",
]
AI_KEYWORDS = [
    "AI", "大模型", "GPT", "豆包", "文心", "通义", "可灵", "Sora", "Midjourney",
    "Agent", "智能体", "AIGC", "生成式", "多模态", "数字人", "虚拟主播",
    "算法", "自动化", "提示词", "模型",
]
ECOM_AI_KEYWORDS = ECOM_KEYWORDS + AI_KEYWORDS + [
    "平台", "内容", "短视频", "图文", "种草", "社区", "用户", "数据", "报告",
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
HOTLIST_TIMEOUT = 5  # 热榜接口超时更短，失败快速跳过


def http_get(url, timeout=TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
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


def _extract_rows(data):
    """兼容多种热榜 API 响应结构，返回条目列表。"""
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    # imsyy: {data: [...]}  ；vvhan: {data: [...]}  ；tenapi: {data: [...]}
    rows = data.get("data")
    if isinstance(rows, list):
        return rows
    # B站: {data: {list: [...]}}
    if isinstance(rows, dict) and isinstance(rows.get("list"), list):
        return rows["list"]
    # rebang.today: {code, items: [...]}
    rows = data.get("items")
    if isinstance(rows, list):
        return rows
    return []


def fetch_hotlist(url, platform):
    items = []
    try:
        raw = http_get(url, timeout=HOTLIST_TIMEOUT)
        data = json.loads(raw.decode("utf-8", errors="ignore"))
    except Exception:
        return items
    rows = _extract_rows(data)
    for r in rows:
        if not isinstance(r, dict):
            continue
        # B站热搜字段为 show_name/keyword，视频为 title
        title = (r.get("title") or r.get("show_name") or r.get("keyword")
                 or r.get("name") or r.get("word") or "").strip()
        # B站视频简介在 desc，播放量在 stat.view
        desc = (r.get("desc") or r.get("hot") or r.get("num")
                or r.get("tag") or "").strip()
        if title:
            items.append({"title": title, "desc": str(desc), "link": "", "pub": "", "platform": platform})
    return items


def fetch_all_hotlist():
    """一次性并发抓取所有平台的所有热榜接口，按平台合并后按配额截断。"""
    platform_sources = [
        ("douyin", DOUYIN_SOURCES),
        ("xiaohongshu", XIAOHONGSHU_SOURCES),
        ("weibo", WEIBO_SOURCES),
        ("bilibili", BILIBILI_SOURCES),
    ]
    results = {plat: [] for plat, _ in platform_sources}
    ex = ThreadPoolExecutor(max_workers=14)
    futures = {}
    for plat, sources in platform_sources:
        for url in sources:
            futures[ex.submit(fetch_hotlist, url, plat)] = (plat, url)
    try:
        for fut in as_completed(futures):
            plat, url = futures[fut]
            items = fut.result()
            if items:
                results[plat].extend(items)
                print(f"[hotlist] {plat} <- {url} (+{len(items)} 条)")
    finally:
        for f in futures:
            f.cancel()
        ex.shutdown(wait=False)
    # 按平台配额截断
    for plat in results:
        quota = PLATFORM_QUOTA.get(plat, 10)
        results[plat] = results[plat][:quota]
    return results


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


JUNK_DESC_PATTERNS = ("作者｜", "编辑｜", "#欢迎关注", "更多精彩内容", "欢迎关注")
JUNK_DESC_RE = re.compile(r"传说|众神|巴德尔|光明之神")


def desc_is_junk(desc):
    """判断摘要是否是照抄的原文噪音（版式文字、故事开头、公众号尾巴）。"""
    if not desc:
        return True
    if any(p in desc for p in JUNK_DESC_PATTERNS):
        return True
    if JUNK_DESC_RE.search(desc):
        return True
    return False


def fix_echoed_desc(feed):
    """模型照抄原文时，把不合格摘要替换成标题，保证线上不出现正文噪音。"""
    for k in ["hotTopics", "aiTools", "insights", "competitorNews", "topicIdeas"]:
        items = feed.get(k) or []
        for it in items:
            if isinstance(it, dict) and desc_is_junk(str(it.get("desc", ""))):
                it["desc"] = f"今日热点：{it.get('title', '')}"[:120]


def is_ecom(text):
    t = text.lower()
    return any(k.lower() in t for k in ECOM_KEYWORDS)


def is_ai(text):
    t = text.lower()
    return any(k.lower() in t for k in AI_KEYWORDS)


def relevance_score(item):
    """2 = 电商与 AI 双命中，1 = 仅电商，0 = 其他。用于让素材包优先落在交叉赛道。"""
    text = item.get("title", "") + " " + item.get("desc", "")
    if is_ecom(text) and is_ai(text):
        return 2
    return 1 if is_ecom(text) else 0


def clean_desc(text, n=110):
    """清洗 RSS 正文：去掉作者/编辑等版式噪音，取首句并限长。"""
    text = clean_html(text or "")
    text = re.sub(r"(作者|编辑|来源|记者)\s*[｜|]\s*\S*", "", text)
    text = re.sub(r"\s+", " ", text).strip(" ··-—、")
    # 取第一句完整表达
    m = re.match(r"[^。！？]{20,}。[^。！？]*[。！？]?", text)
    if m:
        text = m.group(0)
    if len(text) > n:
        text = text[:n].rstrip("，。、；：,.;: ") + "…"
    return text


def shorten(text, n=120):
    text = clean_html(text)
    if len(text) <= n:
        return text
    return text[:n].rstrip("，。、；：,.;: ") + "…"


# ============ LLM 增强（可选） ============
LLM_SYSTEM_PROMPT = """你是电商+AI赛道的爆款内容操盘手。给你一批当天热点新闻，输出 JSON，包含：
1. classified: 每条新闻分类到 hotTopics(平台热点)/aiTools(AI工具)/insights(运营洞察)/competitorNews(竞品动态)，
   每条含 title(原标题)、desc(≤120字摘要，保留数字与来源)、tag(关键词)。
   desc 硬性约束：必须是你自己重新概括的摘要，禁止与给定原文连续 10 字以上相同，禁止照抄开头段落、禁止保留「作者/编辑/来源」等版式文字。
   摘要要写清楚「谁做了什么、带来什么变化、对电商从业者意味着什么」。
2. topicIdeas: 5 个短视频选题，含 title、desc、tag、category(photo/content/live/product/service/trend)。
3. contentMaterials: 从热点中挑 5 个最适合做内容的，每个生成可直接用的文案素材包。
   硬性要求：素材必须落在「电商 × AI」交叉赛道——讲清楚商家/运营者如何用 AI 工具或自动化提升效率、降低成本、放大流量。
   优先选同时涉及电商与 AI 的热点；若热点只涉及电商规则或平台玩法，必须从 AI 工具/自动化视角给出可落地的做法，禁止写成纯平台资讯。
   - source: 来源热点标题
   - hook: 前3秒钩子（一句话制造悬念或反差）
   - angle: 切入角度（这个热点可以从什么视角讲）
   - script: 3-5 个分镜的脚本大纲，每个分镜必须是对象，键名固定为 shot(镜头序号) / visual(画面描述) / voiceover(口播文案)
   - keyPoints: 2-3 条关键话术/金句
   - cta: 引导动作（关注/评论/进主页等）
   - category: photo/content/live/product/service/trend
输出必须是纯 JSON，不要 Markdown 代码块。"""


def llm_enrich(items, key, base_url, model, diag=None):
    if not key:
        return None
    payload = {
        "model": model,
        "temperature": 0.7,
        "messages": [
            {"role": "system", "content": LLM_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(items[:25], ensure_ascii=False)},
        ],
    }
    last_err = ""
    for attempt in range(1, 3):
        try:
            req = urllib.request.Request(
                base_url.rstrip("/") + "/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=300) as r:
                resp = json.loads(r.read().decode("utf-8"))
            content = resp["choices"][0]["message"]["content"]
            # 提取 JSON（兼容 ```json ... ``` 包裹）
            m = re.search(r"\{[\s\S]*\}", content)
            if not m:
                last_err = "响应中未找到 JSON，返回内容前 80 字：" + content[:80]
                print(f"[llm] 第{attempt}次：{last_err}")
                continue
            result = json.loads(m.group(0))
            print(f"[llm] 第{attempt}次调用成功，文案素材包生成成功")
            return result
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            print(f"[llm] 第{attempt}次调用失败：{last_err}")
    if diag is not None:
        diag["error"] = last_err[:220]
    return None


def heuristic_content_materials(items):
    """无 LLM 时的兜底文案素材：基于标题生成简单钩子和脚本大纲。"""
    materials = []
    for it in items[:5]:
        title = it["title"]
        materials.append({
            "source": title,
            "hook": f"你知道吗？{title}，背后藏着电商人必须看懂的信号。",
            "angle": "从商家实战角度拆解这个热点对运营的影响",
            "script": [
                {"shot": "开场", "visual": "热点数据截图", "voiceover": f"今天聊{title}，这事跟每个做电商的都有关系。"},
                {"shot": "拆解", "visual": "要点列表", "voiceover": "三个关键点你必须知道……"},
                {"shot": "落地", "visual": "操作步骤", "voiceover": "具体怎么用，我给你拆成三步走。"},
            ],
            "keyPoints": [f"核心：{title}", "趋势：平台正在加码这个方向"],
            "cta": "关注我，每天更新电商+AI的实战干货。",
            "category": infer_topic_category(title),
        })
    return materials


# ============ 组装 ============
def build_feed():
    all_items = []
    # 1) RSS 媒体源（并发抓取，失败源自动跳过）
    rss_ex = ThreadPoolExecutor(max_workers=len(RSS_SOURCES))
    rss_futs = [rss_ex.submit(fetch_rss, url) for url in RSS_SOURCES]
    for fut in as_completed(rss_futs):
        all_items.extend(fut.result())
    rss_ex.shutdown(wait=False)
    # 2) 平台热搜：抖音 / 小红书 / 微博（每平台多接口兜底，按配额截取）
    hot_results = fetch_all_hotlist()
    for plat in ("douyin", "xiaohongshu", "weibo", "bilibili"):
        all_items.extend(hot_results.get(plat, []))
    # 平台热榜全部失败时提示，但不阻塞后续 RSS 数据
    if not any(hot_results.values()):
        print("[hotlist] 所有平台热榜接口均不可用，仅使用 RSS 媒体源")

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
            "desc": clean_desc(it.get("desc", "")) or it["title"],
            "tag": tag,
            "time": "今日",
            "source": it.get("link", ""),
        })

    # 每个桶取前 5
    feed = {"hotTopics": [], "aiTools": [], "insights": [],
            "topicIdeas": [], "competitorNews": [], "contentMaterials": []}
    for k in ["hotTopics", "aiTools", "insights", "competitorNews"]:
        feed[k] = buckets[k][:5]

    # 选题推荐：从 hotTopics + insights 顶部生成
    # 素材优先落在「电商 × AI」交叉条目，其次纯电商，避免出现与 AI 无关的钩子
    topic_pool = buckets["hotTopics"] + buckets["insights"]
    topic_pool.sort(key=relevance_score, reverse=True)
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

    # 文案素材包：热点 → 可直接用的钩子/脚本大纲/关键话术
    key = os.environ.get("LLM_API_KEY")
    base_url = os.environ.get("LLM_BASE_URL") or "https://ark.cn-beijing.volces.com/api/v3"
    model = os.environ.get("LLM_MODEL") or "doubao-seed-2-1-lite-260915"
    diag = {}
    if key:
        # 只把清洗后的标题+摘要喂给模型，并对双命中条目加权排序，避免纯电商/纯 AI 抢占首条
        cross = [it for it in filtered if relevance_score(it) == 2]
        base = cross if len(cross) >= 6 else filtered
        base = sorted(base, key=relevance_score, reverse=True)[:25]
        llm_input = [
            {"title": it["title"], "desc": clean_desc(it.get("desc", "")), "link": it.get("link", "")}
            for it in base
        ]
        enriched = llm_enrich(llm_input, key, base_url, model, diag)
        if enriched and isinstance(enriched, dict):
            for k in ["hotTopics", "aiTools", "insights", "competitorNews"]:
                if isinstance(enriched.get(k), list) and enriched[k]:
                    feed[k] = enriched[k][:5]
            if isinstance(enriched.get("topicIdeas"), list) and enriched["topicIdeas"]:
                feed["topicIdeas"] = enriched["topicIdeas"][:5]
            if isinstance(enriched.get("contentMaterials"), list) and enriched["contentMaterials"]:
                feed["contentMaterials"] = enriched["contentMaterials"][:5]
                print("[llm] 已用 LLM 生成文案素材包")
            fix_echoed_desc(feed)
        feed["llmStatus"] = {"enabled": True, "ok": bool(enriched), "error": diag.get("error", "")}
    else:
        feed["llmStatus"] = {"enabled": False, "ok": False, "error": "未配置 LLM_API_KEY"}
        print("[heuristic] 无 LLM Key，使用启发式文案素材")

    # 无论 LLM 是否成功，素材包为空时一律用启发式兜底，避免线上出现空栏目
    if not feed["contentMaterials"]:
        feed["contentMaterials"] = heuristic_content_materials(topic_pool)
        print("[heuristic] 素材包为空，已用启发式兜底")

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
    # 热榜抓取线程为非守护线程，数据已落盘，强制退出避免等待超时线程
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)
