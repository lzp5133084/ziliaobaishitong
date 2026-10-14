# -*- coding: utf-8 -*-
"""
AI 智能搜索服务模块（V1.5）
封装豆包大模型API调用，实现自然语言资料查询
"""
import json
import os
import re
import urllib.request
import urllib.error
from datetime import datetime

# 豆包API配置（从环境变量或配置读取，勿硬编码到前端）
AI_API_KEY = os.environ.get("DOUBAO_API_KEY", "")
AI_API_URL = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
AI_MODEL = "doubao-lite-4k"  # 轻量版，响应快，成本低


def is_ai_available():
    """检查AI服务是否可用"""
    return bool(AI_API_KEY and AI_API_KEY != "")


def call_doubao(prompt, system_prompt=""):
    """
    调用豆包大模型API
    返回: (成功标志, 回复内容或错误信息)
    """
    if not is_ai_available():
        return False, "AI服务未配置API Key，请联系管理员配置"

    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + AI_API_KEY
    }

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": AI_MODEL,
        "messages": messages,
        "max_tokens": 1024,
        "temperature": 0.3  # 低温度，更确定性
    }

    try:
        req = urllib.request.Request(
            AI_API_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if "choices" in data and len(data["choices"]) > 0:
                content = data["choices"][0]["message"]["content"]
                return True, content
            else:
                return False, "AI返回格式异常"
    except urllib.error.HTTPError as e:
        return False, "API调用失败: HTTP %d" % e.code
    except urllib.error.URLError as e:
        return False, "网络连接失败: %s" % str(e.reason)
    except Exception as e:
        return False, "AI服务异常: %s" % str(e)


def parse_search_intent(user_query, available_categories=None):
    """
    使用AI解析用户搜索意图，提取结构化查询条件

    返回: {
        "province": "省份或空",
        "city": "城市或空",
        "org_name": "机构或空",
        "business": "业务或空",
        "doc_type": "资料类型或空",
        "keywords": ["关键词1", "关键词2"],
        "raw_query": "原始查询",
        "confidence": "high/medium/low",
        "need_clarify": bool,
        "clarify_question": "需要澄清的问题或空"
    }
    """
    if not is_ai_available():
        # AI不可用时的降级方案：简单关键词匹配
        return fallback_parse(user_query)

    system_prompt = """你是一个资料搜索意图解析助手。请从用户的自然语言查询中提取以下信息：
- province: 省份（如"四川省"）
- city: 城市（如"泸州市"）
- org_name: 机构名称（如"平安人寿"、"XX医院"）
- business: 业务类型（如"住院医疗理赔"）
- doc_type: 资料类型（如"出院记录"）
- keywords: 其他关键词列表

如果某个字段无法确定，请留空。如果信息不足无法搜索，设置need_clarify为true并给出clarify_question。

请以JSON格式返回，不要包含其他解释文字。"""

    ok, content = call_doubao(user_query, system_prompt)
    if not ok:
        return fallback_parse(user_query)

    try:
        # 尝试提取JSON部分
        json_match = re.search(r'\{[^{}]*\}', content, re.DOTALL)
        if json_match:
            result = json.loads(json_match.group())
        else:
            result = json.loads(content)

        # 标准化字段
        parsed = {
            "province": result.get("province", "").strip(),
            "city": result.get("city", "").strip(),
            "org_name": result.get("org_name", "").strip(),
            "business": result.get("business", "").strip(),
            "doc_type": result.get("doc_type", "").strip(),
            "keywords": result.get("keywords", []) if isinstance(result.get("keywords"), list) else [],
            "raw_query": user_query,
            "confidence": result.get("confidence", "medium"),
            "need_clarify": bool(result.get("need_clarify", False)),
            "clarify_question": result.get("clarify_question", "").strip()
        }
        return parsed
    except (json.JSONDecodeError, AttributeError):
        return fallback_parse(user_query)


def fallback_parse(user_query):
    """
    AI不可用时的降级解析：基于规则的简单提取
    """
    result = {
        "province": "",
        "city": "",
        "org_name": "",
        "business": "",
        "doc_type": "",
        "keywords": [],
        "raw_query": user_query,
        "confidence": "low",
        "need_clarify": False,
        "clarify_question": ""
    }

    # 简单关键词匹配
    provinces = ["北京", "上海", "天津", "重庆", "四川", "广东", "浙江", "江苏", "山东", "河南"]
    for p in provinces:
        if p in user_query:
            result["province"] = p + "省" if p not in ["北京", "上海", "天津", "重庆"] else p + "市"
            break

    # 提取城市：找到省份后，取后面的2-4个汉字（但不包含机构/资料类型关键词）
    if result["province"]:
        prov_name = result["province"].replace("省", "").replace("市", "")
        if prov_name in user_query:
            after_prov = user_query.split(prov_name)[-1]
            # 先提取机构和资料类型，从剩余文本中找城市
            remaining = after_prov
            # 移除常见机构词
            for kw in ["平安", "人寿", "保险", "医院", "银行", "社保", "医保"]:
                remaining = remaining.replace(kw, "|")
            # 移除常见资料类型词
            for kw in ["出院记录", "出院证明", "诊断证明", "发票", "清单", "结算单", "申请书", "病历", "报告", "理赔"]:
                remaining = remaining.replace(kw, "|")
            # 取第一个|之前的部分作为城市
            city_part = remaining.split("|")[0].strip()
            if len(city_part) >= 2 and len(city_part) <= 4:
                result["city"] = city_part + "市" if not city_part.endswith("市") else city_part

    # 常见机构关键词
    org_keywords = ["平安人寿", "中国人寿", "太平洋保险", "泰康人寿", "新华保险",
                    "平安", "人寿", "医院", "银行", "社保", "医保"]
    for kw in org_keywords:
        if kw in user_query:
            # 直接返回匹配到的关键词，不做过度扩展
            result["org_name"] = kw
            break

    # 常见资料类型
    doc_types = ["出院记录", "出院证明", "诊断证明", "发票", "清单", "结算单", "申请书", "病历", "报告"]
    for dt in doc_types:
        if dt in user_query:
            result["doc_type"] = dt
            break

    # 剩余关键词
    keywords = re.findall(r'[一-龥]{2,}', user_query)
    result["keywords"] = [k for k in keywords if k not in
                         [result["province"], result["city"], result["org_name"], result["doc_type"]]]

    return result


def generate_search_response(parsed, search_results, total_count):
    """
    根据解析结果和搜索结果，生成AI回复

    返回: {
        "answer": "文字回答",
        "suggested_docs": [资料列表],
        "has_results": bool,
        "need_clarify": bool,
        "clarify_question": "澄清问题"
    }
    """
    if parsed.get("need_clarify"):
        return {
            "answer": parsed.get("clarify_question", "为了更准确地帮您找到资料，请补充一些信息。"),
            "suggested_docs": [],
            "has_results": False,
            "need_clarify": True,
            "clarify_question": parsed.get("clarify_question", "")
        }

    if not search_results:
        # 无结果时的AI回复
        no_result_prompt = """用户查询：%s
解析条件：省份=%s，城市=%s，机构=%s，业务=%s，资料类型=%s
搜索结果：未找到匹配的资料

请生成一段友好的回复，说明：
1. 当前没有找到匹配的资料
2. 可能的原因（如该地区/机构资料暂未收录）
3. 建议用户可以尝试的操作（如更换关键词、上传资料、联系客服）
4. 强调平台资料为用户共创分享，仅供参考

请用中文回复，语气友好专业。""" % (
            parsed["raw_query"], parsed["province"], parsed["city"],
            parsed["org_name"], parsed["business"], parsed["doc_type"]
        )

        if is_ai_available():
            ok, answer = call_doubao(no_result_prompt)
            if not ok:
                answer = generate_fallback_no_result(parsed)
        else:
            answer = generate_fallback_no_result(parsed)

        return {
            "answer": answer,
            "suggested_docs": [],
            "has_results": False,
            "need_clarify": False,
            "clarify_question": ""
        }

    # 有结果时的AI回复
    docs_summary = []
    for i, doc in enumerate(search_results[:5]):  # 最多取5条
        docs_summary.append("%d. %s（%s %s %s，%s，%s）" % (
            i+1, doc.get("title", ""), doc.get("province", ""),
            doc.get("city", ""), doc.get("org_name", ""),
            doc.get("doc_type", ""), doc.get("updated_at", "")[:10]
        ))

    result_prompt = """用户查询：%s
解析条件：省份=%s，城市=%s，机构=%s，业务=%s，资料类型=%s
找到 %d 条相关资料：
%s

请生成一段友好的回复，说明：
1. 找到了多少条相关资料
2. 列出最相关的几条资料（包含名称、地区、机构、类型）
3. 提醒用户这些资料由用户共创分享，仅供参考
4. 建议用户查看详情或进一步筛选

请用中文回复，语气友好专业。不要编造资料内容。""" % (
        parsed["raw_query"], parsed["province"], parsed["city"],
        parsed["org_name"], parsed["business"], parsed["doc_type"],
        total_count, "\n".join(docs_summary)
    )

    if is_ai_available():
        ok, answer = call_doubao(result_prompt)
        if not ok:
            answer = generate_fallback_result(parsed, search_results, total_count)
    else:
        answer = generate_fallback_result(parsed, search_results, total_count)

    return {
        "answer": answer,
        "suggested_docs": search_results[:10],  # 最多返回10条
        "has_results": True,
        "need_clarify": False,
        "clarify_question": ""
    }


def generate_fallback_no_result(parsed):
    """无结果时的降级回复"""
    parts = ["很抱歉，没有找到匹配的资料。"]
    if parsed["province"] or parsed["city"]:
        parts.append("您查询的地区（%s %s）可能暂未收录相关资料。" % (
            parsed["province"], parsed["city"]))
    if parsed["org_name"]:
        parts.append("机构「%s」的资料可能较少。" % parsed["org_name"])
    parts.append("建议您：1) 尝试更换关键词重新搜索；2) 如果您有相关经验，欢迎上传分享；3) 联系客服获取帮助。")
    parts.append("\n（平台资料由用户共创分享，仅供参考，不保证符合具体机构最新要求）")
    return "".join(parts)


def generate_fallback_result(parsed, docs, total):
    """有结果时的降级回复"""
    lines = ["为您找到 %d 条相关资料：\n" % total]
    for i, doc in enumerate(docs[:5]):
        lines.append("%d. 《%s》" % (i+1, doc.get("title", "")))
        lines.append("   地区：%s %s | 机构：%s | 类型：%s" % (
            doc.get("province", ""), doc.get("city", ""),
            doc.get("org_name", ""), doc.get("doc_type", "")))
    if total > 5:
        lines.append("\n... 以及另外 %d 条资料" % (total - 5))
    lines.append("\n（以上资料由用户共创分享，仅供参考，请以办理机构实际要求为准）")
    return "\n".join(lines)


# 便捷函数
def ai_search(user_query, search_func):
    """
    完整的AI搜索流程

    参数:
        user_query: 用户自然语言查询
        search_func: 资料搜索函数，签名为 search_func(parsed_dict) -> (results, total)

    返回: {
        "ok": bool,
        "answer": str,
        "parsed": dict,
        "docs": list,
        "total": int,
        "ai_available": bool
    }
    """
    if not user_query or not user_query.strip():
        return {
            "ok": False,
            "answer": "请输入您要查询的内容",
            "parsed": {},
            "docs": [],
            "total": 0,
            "ai_available": is_ai_available()
        }

    # 1. 解析意图
    parsed = parse_search_intent(user_query.strip())

    # 2. 如果需要澄清，直接返回
    if parsed.get("need_clarify"):
        return {
            "ok": True,
            "answer": parsed["clarify_question"],
            "parsed": parsed,
            "docs": [],
            "total": 0,
            "ai_available": is_ai_available()
        }

    # 3. 执行搜索
    results, total = search_func(parsed)

    # 4. 生成回复
    response = generate_search_response(parsed, results, total)

    return {
        "ok": True,
        "answer": response["answer"],
        "parsed": parsed,
        "docs": response["suggested_docs"],
        "total": total,
        "ai_available": is_ai_available()
    }


def summarize_community(discussions):
    """
    AI整理社区高频讨论问题（V2.0）

    参数:
        discussions: 讨论列表 [{"content": ..., "doc_title": ...}, ...]

    返回: {
        "ok": bool,
        "summary": "AI整理的高频问题摘要",
        "hot_keywords": ["关键词1", ...],
        "total_discussions": int,
        "ai_available": bool
    }
    """
    total = len(discussions)
    if total == 0:
        return {
            "ok": True,
            "summary": "社区讨论区刚刚开通，还没有讨论内容。欢迎大家积极分享经验、提问交流！",
            "hot_keywords": [],
            "total_discussions": 0,
            "ai_available": is_ai_available()
        }

    # 降级方案：关键词频率统计
    def _fallback():
        words = {}
        stop = {"什么", "怎么", "如何", "请问", "一下", "需要", "可以", "没有", "这个", "那个",
                "我们", "你们", "他们", "就是", "不是", "知道", "时候", "现在", "已经"}
        for d in discussions:
            for w in re.findall(r'[一-龥]{2,4}', d.get("content", "")):
                if w not in stop:
                    words[w] = words.get(w, 0) + 1
        hot = sorted(words.items(), key=lambda x: -x[1])[:8]
        hot_words = [w for w, _c in hot]
        lines = ["近期社区讨论热点（基于 %d 条讨论的关键词统计）：\n" % total]
        for i, (w, c) in enumerate(hot[:5]):
            lines.append("%d. 「%s」被提及 %d 次" % (i + 1, w, c))
        lines.append("\n（配置豆包API Key后可获得AI智能摘要）")
        return "\n".join(lines), hot_words

    if not is_ai_available():
        summary, hot_words = _fallback()
        return {
            "ok": True,
            "summary": summary,
            "hot_keywords": hot_words,
            "total_discussions": total,
            "ai_available": False
        }

    # AI模式：大模型摘要
    content_lines = []
    for i, d in enumerate(discussions[:30]):
        content_lines.append("%d. [%s] %s" % (i + 1, d.get("doc_title", ""), d.get("content", "")))
    prompt = "以下是资料分享社区最近的 %d 条用户讨论：\n%s" % (total, "\n".join(content_lines))
    system_prompt = """你是社区运营助手。请分析用户讨论内容，总结：
1. 用户最关心的3-5个高频问题（用简洁的语言概括）
2. 讨论中反映出的共性需求
请以JSON格式返回：{"summary": "摘要文字", "hot_keywords": ["关键词1", "关键词2"]}
不要编造讨论中不存在的内容。"""

    ok, content = call_doubao(prompt, system_prompt)
    if not ok:
        summary, hot_words = _fallback()
        return {
            "ok": True,
            "summary": summary,
            "hot_keywords": hot_words,
            "total_discussions": total,
            "ai_available": False
        }

    try:
        json_match = re.search(r'\{[^{}]*\}', content, re.DOTALL)
        result = json.loads(json_match.group()) if json_match else json.loads(content)
        return {
            "ok": True,
            "summary": result.get("summary", "").strip() or "暂无足够讨论内容生成摘要",
            "hot_keywords": result.get("hot_keywords", []) if isinstance(result.get("hot_keywords"), list) else [],
            "total_discussions": total,
            "ai_available": True
        }
    except (json.JSONDecodeError, AttributeError):
        summary, hot_words = _fallback()
        return {
            "ok": True,
            "summary": summary,
            "hot_keywords": hot_words,
            "total_discussions": total,
            "ai_available": False
        }
