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
    """构建发送给 AI 的提示词"""
    type_name = TYPE_NAMES.get(question.question_type, "选择题")
    lines = [f"【{type_name}】\n"]
    lines.append(f"题目：{question.text}\n")

    if question.options:
        lines.append("选项：")
        for letter, text in question.options:
            lines.append(f"  {letter}. {text}")
        lines.append("")

    if question.question_type == "multiple":
        lines.append("请选择所有正确的选项（可能不止一个）。")
        lines.append("输出格式：在第一行写【答案】，后面跟选项字母（如 ABCD）。")
        lines.append("在第二行开始写【解析】，简要说明理由。")
        lines.append("示例：\n【答案】AB\n【解析】A选项...正确，B选项...正确...")
    elif question.question_type == "judge":
        lines.append('请判断题目说法是否正确。')
        lines.append('输出格式：在第一行写【答案】，后面跟"正确"或"错误"。')
        lines.append("在第二行开始写【解析】，简要说明理由。")
        lines.append("示例：\n【答案】正确\n【解析】因为...")
    elif question.question_type == "fill":
        lines.append("请填写题目的空白处。")
        lines.append("输出格式：在第一行写【答案】，后面跟填空内容。")
        lines.append("在第二行开始写【解析】，简要说明理由。")
    else:
        lines.append("请选择最正确的一个选项。")
        lines.append("输出格式：在第一行写【答案】，后面跟选项字母（如 A）。")
        lines.append("在第二行开始写【解析】，简要说明理由。")
        lines.append("示例：\n【答案】A\n【解析】因为...")

    return "\n".join(lines)


def parse_response(response: str, question: Question) -> Tuple[List[str], str]:
    """
    从 AI 回复中解析答案

    Returns:
        (答案字母列表, 解析文本)
    """
    reasoning = ""
    answer_letters = []

    # 尝试匹配【答案】XXX 格式
    answer_pattern = r"【答案】\s*(.+?)(?:\n|$)"
    reasoning_pattern = r"【解析】\s*(.+)"
    answer_match = re.search(answer_pattern, response, re.DOTALL)

    if answer_match:
        answer_str = answer_match.group(1).strip()

        if question.question_type == "judge":
            # 判断题：正确/错误 对应 A/B
            if "正确" in answer_str or "对" == answer_str.strip():
                answer_letters = ["A"]
            elif "错误" in answer_str or "错" == answer_str.strip():
                answer_letters = ["B"]
            else:
                answer_letters = [answer_str]
        elif question.question_type == "fill":
            # 填空题：直接返回文本
            answer_letters = [answer_str]
        else:
            # 选择题：提取字母
            letter_matches = re.findall(r"[A-Z]", answer_str.upper())
            if letter_matches:
                # 去重并保持顺序
                seen = set()
                answer_letters = []
                for l in letter_matches:
                    if l not in seen:
                        seen.add(l)
                        answer_letters.append(l)

        # 提取解析
        reasoning_match = re.search(reasoning_pattern, response, re.DOTALL)
        if reasoning_match:
            reasoning = reasoning_match.group(1).strip()
    else:
        # 回退：直接从回复中提取字母
        if question.question_type in ("single", "multiple"):
            letter_matches = re.findall(r"[A-D]", response.upper())
            if letter_matches:
                seen = set()
                answer_letters = []
                for l in letter_matches[:4]:  # 最多取4个
                    if l not in seen:
                        seen.add(l)
                        answer_letters.append(l)
        elif question.question_type == "judge":
            if "正确" in response or "对" in response:
                answer_letters = ["A"]
            elif "错误" in response or "错" in response:
                answer_letters = ["B"]

        reasoning = response

    # 验证答案是否在有效选项范围内
    if question.options and answer_letters:
        valid_letters = {opt[0] for opt in question.options}
        answer_letters = [l for l in answer_letters if l in valid_letters]

    return answer_letters, reasoning
