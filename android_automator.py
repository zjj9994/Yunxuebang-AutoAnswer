"""
Android 端自动化模块（基于 uiautomator2 + Playwright）
手机端操作云学帮 APP，电脑端通过 DeepSeek 网页版获取答案

工作流程：
  电脑启动浏览器 → 打开 DeepSeek 网页 → 用户登录
  手机连接设备 → 启动云学帮 APP → 用户进入答题页面
  → 提取题目(手机) → DeepSeek答题(电脑) → 选择答案(手机) → 下一题
"""

import re
import time
import asyncio
import logging
from typing import List, Optional

from config import AndroidAutomationConfig, DeepSeekWebConfig
from models import Question, AnswerResult, build_prompt, parse_response, TYPE_NAMES
from deepseek_web_client import DeepSeekWebClient

logger = logging.getLogger(__name__)


class AndroidAutomator:
    """Android 端自动化控制器，手机操作云学帮 + 电脑操作 DeepSeek"""

    def __init__(self, config: AndroidAutomationConfig, ds_config: DeepSeekWebConfig):
        self.config = config
        self.ds_config = ds_config
        self.device = None       # uiautomator2 设备
        self.ds_client = None    # DeepSeek 网页客户端

    async def start(self):
        """连接 Android 设备并初始化 DeepSeek 客户端"""
        # 1. 连接 Android 设备（在子线程中执行，避免阻塞事件循环）
        await asyncio.to_thread(self._connect_device)

        # 2. 初始化 DeepSeek 网页客户端（独立浏览器）
        self.ds_client = DeepSeekWebClient(self.ds_config)
        await self.ds_client.init_standalone()

    def _connect_device(self):
        """连接 Android 设备（同步操作）"""
        import uiautomator2 as u2

        if self.config.device_serial:
            self.device = u2.connect(self.config.device_serial)
        else:
            self.device = u2.connect()

        logger.info(f"已连接设备: {self.device.info}")
        self.device.implicitly_wait(self.config.ui_timeout)

        if self.config.app_package:
            try:
                self.device.app_start(self.config.app_package)
                logger.info(f"已启动 APP: {self.config.app_package}")
                time.sleep(3)
            except Exception as e:
                logger.warning(f"启动 APP 失败（可能已在运行）: {e}")

    async def init_deepseek_login(self):
        """导航到 DeepSeek 并等待用户登录"""
        await self.ds_client.navigate_and_login()

    def wait_for_user_ready(self, message: str = ""):
        """等待用户确认"""
        input(f"\n>>> {message}，完成后按回车继续...")

    async def extract_questions(self) -> List[Question]:
        """从当前手机屏幕提取题目"""
        # 在子线程中执行 uiautomator2 操作
        xml_content = await asyncio.to_thread(self.device.dump_hierarchy)
        questions = self._parse_ui_xml(xml_content)

        if not questions:
            logger.info("当前屏幕未检测到题目，尝试滚动...")
            await asyncio.to_thread(self.device.scroll, True)
            await asyncio.sleep(1)
            xml_content = await asyncio.to_thread(self.device.dump_hierarchy)
            questions = self._parse_ui_xml(xml_content)

        logger.info(f"共提取到 {len(questions)} 道题目")
        return questions

    def _parse_ui_xml(self, xml_content: str) -> List[Question]:
        """解析 UI XML 层次结构，提取题目"""
        import xml.etree.ElementTree as ET

        questions = []
        try:
            root = ET.fromstring(xml_content)
        except ET.ParseError as e:
            logger.error(f"XML 解析失败: {e}")
            return questions

        text_nodes = []
        for node in root.iter("node"):
            text = node.get("text", "").strip()
            if text:
                text_nodes.append({"text": text})

        if not text_nodes:
            return questions

        full_text = "\n".join([n["text"] for n in text_nodes])
        question_splits = re.split(r"(?=\n\s*\d+\s*[.、）)\]])", full_text)

        for idx, q_text in enumerate(question_splits):
            q_text = q_text.strip()
            if not q_text or len(q_text) < 5:
                continue
            clean_text = re.sub(r"^\s*\d+\s*[.、）)\]]\s*", "", q_text).strip()
            if not clean_text:
                continue
            question_type = self._detect_type(clean_text)
            options = self._extract_options_from_text(clean_text)

            question_text = clean_text
            if options:
                for letter, opt_text in options:
                    pattern = rf"{letter}\s*[.、）)\]]\s*{re.escape(opt_text)}"
                    question_text = re.sub(pattern, "", question_text).strip()

            if question_text:
                questions.append(Question(
                    index=idx, text=question_text, options=options,
                    question_type=question_type,
                ))

        if not questions:
            question = self._parse_single_question(text_nodes)
            if question:
                questions.append(question)

        return questions

    def _parse_single_question(self, text_nodes: list) -> Optional[Question]:
        """解析单题模式"""
        all_text = "\n".join([n["text"] for n in text_nodes])
        options = self._extract_options_from_text(all_text)
        question_type = self._detect_type(all_text)

        question_text = ""
        for node in text_nodes:
            text = node["text"].strip()
            if len(text) > 10 and not re.match(r"^[A-D]\s*[.、）)\]]", text):
                question_text = text
                break
        if not question_text:
            question_text = all_text[:200]

        if options:
            for letter, opt_text in options:
                pattern = rf"{letter}\s*[.、）)\]]\s*{re.escape(opt_text)}"
                question_text = re.sub(pattern, "", question_text).strip()

        if question_text:
            return Question(index=0, text=question_text, options=options,
                            question_type=question_type)
        return None

    def _detect_type(self, text: str) -> str:
        if "判断题" in text or ("正确" in text and "错误" in text and len(text) < 200):
            return "judge"
        if "多选题" in text or "多选" in text:
            return "multiple"
        if "填空题" in text or "____" in text or "（）" in text:
            return "fill"
        return "single"

    def _extract_options_from_text(self, text: str) -> List[tuple]:
        options = []
        pattern = r"([A-D])\s*[.、）)\]]\s*([^\n\r]+?)(?=\s*[A-D]\s*[.、）)\]]|$)"
        matches = re.findall(pattern, text)
        for letter, opt_text in matches:
            opt_text = opt_text.strip()
            if opt_text and len(opt_text) < 500:
                options.append((letter, opt_text))
        return options

    async def select_answer(self, question: Question, answer_letters: List[str]):
        """在设备上选择答案"""
        if not answer_letters:
            logger.warning(f"题目 {question.index} 无答案可选")
            return False

        xml_content = await asyncio.to_thread(self.device.dump_hierarchy)
        success_count = 0
        for letter in answer_letters:
            clicked = await asyncio.to_thread(
                self._click_option_by_letter, xml_content, letter, question
            )
            if clicked:
                success_count += 1
                logger.info(f"题目 {question.index} 已选择选项 {letter}")
            else:
                logger.warning(f"题目 {question.index} 选项 {letter} 点击失败")
            await asyncio.sleep(0.5)
        return success_count > 0

    def _click_option_by_letter(self, xml_content: str, letter: str, question: Question) -> bool:
        """通过选项字母点击对应元素（同步操作）"""
        import xml.etree.ElementTree as ET

        try:
            root = ET.fromstring(xml_content)
        except ET.ParseError:
            return False

        for node in root.iter("node"):
            text = node.get("text", "").strip()
            desc = node.get("content-desc", "").strip()
            for check_text in [text, desc]:
                if not check_text:
                    continue
                if check_text == letter:
                    bounds = self._parse_bounds(node.get("bounds", ""))
                    if bounds:
                        self.device.click(*bounds)
                        return True
                if re.match(rf"^{letter}\s*[.、）)\]]", check_text):
                    bounds = self._parse_bounds(node.get("bounds", ""))
                    if bounds:
                        self.device.click(*bounds)
                        return True

        for node in root.iter("node"):
            text = node.get("text", "").strip()
            for opt_letter, opt_text in question.options:
                if opt_letter == letter and opt_text and opt_text in text:
                    bounds = self._parse_bounds(node.get("bounds", ""))
                    if bounds:
                        self.device.click(*bounds)
                        return True

        # 回退：坐标比例点击
        try:
            info = self.device.info
            screen_w = info["displayWidth"]
            screen_h = info["displayHeight"]
            letter_idx = ord(letter) - ord("A")
            total_opts = max(len(question.options), 4)
            y_start = int(screen_h * 0.4)
            y_end = int(screen_h * 0.85)
            y_step = (y_end - y_start) / total_opts
            target_y = int(y_start + y_step * (letter_idx + 0.5))
            target_x = int(screen_w * 0.5)
            self.device.click(target_x, target_y)
            logger.info(f"通过坐标比例点击: ({target_x}, {target_y})")
            return True
        except Exception as e:
            logger.error(f"坐标点击失败: {e}")
            return False

    def _parse_bounds(self, bounds_str: str) -> Optional[tuple]:
        match = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds_str)
        if match:
            x1, y1, x2, y2 = map(int, match.groups())
            return ((x1 + x2) // 2, (y1 + y2) // 2)
        return None

    async def fill_answer(self, question: Question, answer_text: str):
        """填空题"""
        def _fill():
            edit = self.device(resourceClass="android.widget.EditText")
            if edit.exists:
                edit.set_text(answer_text)
                logger.info(f"题目 {question.index} 已填写: {answer_text}")
                return True
            return False
        return await asyncio.to_thread(_fill)

    async def click_submit(self):
        """点击提交按钮"""
        def _submit():
            for text in ["提交", "交卷", "确认提交", "提交试卷", "确定"]:
                btn = self.device(text=text)
                if btn.exists:
                    btn.click()
                    logger.info(f"已点击按钮: {text}")
                    time.sleep(1)
                    for confirm_text in ["确认", "确定", "是"]:
                        confirm_btn = self.device(text=confirm_text)
                        if confirm_btn.exists:
                            confirm_btn.click()
                            logger.info(f"已确认: {confirm_text}")
                            return True
                    return True
            return False
        return await asyncio.to_thread(_submit)

    async def click_next_question(self):
        """点击下一题按钮"""
        def _next():
            for text in ["下一题", "下一页", "继续", "下一道"]:
                btn = self.device(text=text)
                if btn.exists:
                    btn.click()
                    logger.info(f"点击: {text}")
                    time.sleep(1)
                    return True
            return False
        return await asyncio.to_thread(_next)

    async def run_auto_answer(self):
        """执行完整的自动答题流程"""
        results: List[AnswerResult] = []
        question_count = 0

        while True:
            question_count += 1
            logger.info(f"\n{'='*60}")
            logger.info(f"处理第 {question_count} 题...")

            # 提取题目（手机端）
            questions = await self.extract_questions()
            if not questions:
                logger.warning("未检测到题目，可能已完成")
                break

            question = questions[0]
            question.index = question_count - 1
            logger.info(f"题目: {question.text[:80]}...")
            logger.info(f"题型: {question.question_type}, 选项数: {len(question.options)}")

            # 调用 DeepSeek 网页版获取答案（电脑端）
            result = await self.ds_client.answer_question(question)
            results.append(result)

            if result.success and result.answer_letters:
                if question.question_type == "fill":
                    await self.fill_answer(question, result.answer_letters[0])
                else:
                    await self.select_answer(question, result.answer_letters)
                if result.reasoning:
                    logger.info(f"解析: {result.reasoning[:100]}...")
            else:
                logger.warning(f"题目 {question_count} 未能获取答案: {result.error}")

            await asyncio.sleep(self.config.question_delay)

            if not await self.click_next_question():
                logger.info("未找到下一题按钮，可能已到最后一题")
                break

        # 提交
        if self.config.confirm_before_submit:
            self.wait_for_user_ready("答题完成，请检查答案")

        if self.config.confirm_before_submit:
            await self.click_submit()

        return results

    async def inspect_screen(self):
        """屏幕检查模式"""
        def _inspect():
            logger.info("=== 屏幕检查模式 ===")
            xml_content = self.device.dump_hierarchy()
            import xml.etree.ElementTree as ET
            try:
                root = ET.fromstring(xml_content)
            except ET.ParseError as e:
                logger.error(f"XML 解析失败: {e}")
                return
            logger.info("当前屏幕文本元素:")
            for i, node in enumerate(root.iter("node")):
                text = node.get("text", "").strip()
                desc = node.get("content-desc", "").strip()
                clickable = node.get("clickable", "false") == "true"
                rid = node.get("resource-id", "")
                bounds = node.get("bounds", "")
                if text or desc:
                    display = text or desc
                    logger.info(f"  [{i}] text='{display[:80]}' clickable={clickable} id='{rid}' bounds='{bounds}'")
        await asyncio.to_thread(_inspect)

    async def close(self):
        """关闭资源"""
        if self.ds_client:
            await self.ds_client.close()
        logger.info("资源已释放")
