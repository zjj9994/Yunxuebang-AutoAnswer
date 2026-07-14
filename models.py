"""
共享数据结构与提示词/解析逻辑
被 deepseek_web_client.py 和自动化模块共用
"""

import re
import logging
from typing import List, Tuple
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class Question:
    """题目数据结构"""
    index: int                        # 题目序号
    text: str                         # 题目正文
    options: List[Tuple[str, str]]    # 选项列表 [(字母, 文本), ...]
    question_type: str = "single"     # 题型: single / multiple / judge / fill
    raw_html: str = ""                # 原始 HTML（调试用）


@dataclass
class AnswerResult:
    """答题结果"""
    question: Question
    answer_letters: List[str]         # 选中的选项字母
    raw_response: str                 # 模型原始回复
    reasoning: str = ""               # 解题思路
    success: bool = True              # 是否成功获取答案
    error: str = ""                   # 错误信息


# 题型映射到中文描述
TYPE_NAMES = {
    "single": "单选题",
    "multiple": "多选题",
    "judge": "判断题",
    "fill": "填空题",
}


def build_prompt(question: Question) -> str:
    """构建发送给 AI 的提示词，要求严格的输出格式"""
    type_name = TYPE_NAMES.get(question.question_type, "选择题")
    lines = []
    lines.append(f"这是一道{type_name}，请选出正确答案。")
    lines.append("")
    lines.append(f"题目：{question.text}")
    lines.append("")

    if question.options:
        for letter, text in question.options:
            lines.append(f"{letter}. {text}")
        lines.append("")

    # 严格格式要求
    lines.append("要求：")
    lines.append("1. 仔细阅读题目和所有选项后再作答")
    if question.question_type == "multiple":
        lines.append("2. 这是多选题，可能有一个或多个正确答案")
        lines.append("3. 请只输出答案字母，不要输出其他内容")
        lines.append('输出格式：【答案】ABC（多个字母连写，不要有空格和逗号）')
    elif question.question_type == "judge":
        lines.append("2. 请判断题目说法是否正确")
        lines.append("3. 请只输出答案，不要输出其他内容")
        lines.append('输出格式：【答案】正确 或 【答案】错误')
    elif question.question_type == "fill":
        lines.append("2. 请填写题目的空白处")
        lines.append("3. 请只输出答案，不要输出其他内容")
        lines.append("输出格式：【答案】填空内容")
    else:
        lines.append("2. 这是单选题，只有一个正确答案")
        lines.append("3. 请只输出答案字母，不要输出其他内容")
        lines.append("输出格式：【答案】A")
    lines.append("")
    lines.append("注意：必须严格按照上述格式输出，第一行必须是【答案】开头。")

    return "\n".join(lines)


def parse_response(response: str, question: Question) -> Tuple[List[str], str]:
    """
    从 AI 回复中解析答案

    Returns:
        (答案字母列表, 解析文本)
    """
    reasoning = ""
    answer_letters = []

    # 策略1：匹配 【答案】XXX 格式（最可靠）
    answer_pattern = r"【答案】\s*(.+?)(?:\n|【|$)"
    answer_match = re.search(answer_pattern, response, re.DOTALL)

    if answer_match:
        answer_str = answer_match.group(1).strip()
        logger.debug(f"匹配到答案文本: '{answer_str}'")

        if question.question_type == "judge":
            if "正确" in answer_str or answer_str.strip() == "对":
                answer_letters = ["A"]
            elif "错误" in answer_str or answer_str.strip() == "错":
                answer_letters = ["B"]
            else:
                answer_letters = [answer_str]
        elif question.question_type == "fill":
            answer_letters = [answer_str]
        else:
            # 选择题：提取连续的大写字母
            letter_matches = re.findall(r"[A-Z]", answer_str.upper())
            if letter_matches:
                seen = set()
                answer_letters = []
                for l in letter_matches:
                    if l not in seen:
                        seen.add(l)
                        answer_letters.append(l)

        # 尝试提取解析
        reasoning_pattern = r"【解析】\s*(.+)"
        reasoning_match = re.search(reasoning_pattern, response, re.DOTALL)
        if reasoning_match:
            reasoning = reasoning_match.group(1).strip()
        else:
            # 取答案之后的内容作为解析
            idx = response.find(answer_match.group(0))
            if idx >= 0:
                after = response[idx + len(answer_match.group(0)):].strip()
                if after:
                    reasoning = after[:500]

    # 策略2：匹配 "答案是X" 或 "选X" 等常见格式
    if not answer_letters:
        patterns = [
            r"答案[是为：:]\s*([A-D]+)",
            r"选\s*([A-D]+)",
            r"正确答案[是为：:]\s*([A-D]+)",
            r"应选\s*([A-D]+)",
        ]
        for pat in patterns:
            m = re.search(pat, response, re.IGNORECASE)
            if m:
                letters = m.group(1).upper()
                seen = set()
                answer_letters = []
                for l in letters:
                    if l not in seen:
                        seen.add(l)
                        answer_letters.append(l)
                logger.debug(f"通过模式 '{pat}' 匹配到: {answer_letters}")
                break

    # 策略3：判断题特殊处理
    if not answer_letters and question.question_type == "judge":
        # 检查回复前100字符
        head = response[:200]
        if "正确" in head and "不正确" not in head and "错误" not in head:
            answer_letters = ["A"]
        elif "错误" in head or "不正确" in head:
            answer_letters = ["B"]

    # 策略4：最后回退 - 找到回复中第一个出现的选项字母
    if not answer_letters and question.options:
        valid_letters = [opt[0] for opt in question.options]
        for char in response.upper():
            if char in valid_letters:
                answer_letters = [char]
                logger.debug(f"回退匹配到第一个有效字母: {char}")
                break

    # 验证答案是否在有效选项范围内
    if question.options and answer_letters:
        valid_letters = {opt[0] for opt in question.options}
        answer_letters = [l for l in answer_letters if l in valid_letters]

    if not reasoning:
        reasoning = response[:500]

    return answer_letters, reasoning
