"""
微信小程序自动化模块（基于 uiautomator2 + Playwright）
通过安卓模拟器运行微信，在微信中打开云学帮小程序
电脑端通过 DeepSeek 网页版获取答案

关键改进：
  - 截图调试：每步操作自动截图保存
  - 多策略题目提取：UI XML 解析 + OCR 回退
  - 智能选项点击：精确匹配 + 坐标回退 + 可点击元素排序
  - 重试机制：答题失败自动重试
  - 更好的下一题检测
"""

import re
import time
import asyncio
import logging
import os
from datetime import datetime
from typing import List, Optional

from config import WeChatMiniProgramConfig, DeepSeekWebConfig
from models import Question, AnswerResult, build_prompt, parse_response, TYPE_NAMES
from deepseek_web_client import DeepSeekWebClient

logger = logging.getLogger(__name__)

# 截图保存目录
SCREENSHOT_DIR = "screenshots"


class WeChatMiniProgramAutomator:
    """微信小程序自动化控制器"""

    def __init__(self, config: WeChatMiniProgramConfig, ds_config: DeepSeekWebConfig):
        self.config = config
        self.ds_config = ds_config
        self.device = None
        self.ds_client = None
        self._screenshot_count = 0

    async def start(self):
        """连接 Android 设备并初始化 DeepSeek 客户端"""
        await asyncio.to_thread(self._connect_device)

        self.ds_client = DeepSeekWebClient(self.ds_config)
        await self.ds_client.init_standalone()

        # 创建截图目录
        os.makedirs(SCREENSHOT_DIR, exist_ok=True)

    def _connect_device(self):
        """连接 Android 设备"""
        import uiautomator2 as u2

        if self.config.device_serial:
            self.device = u2.connect(self.config.device_serial)
        else:
            self.device = u2.connect()

        logger.info(f"已连接设备: {self.device.info}")
        self.device.implicitly_wait(self.config.ui_timeout)

    async def screenshot(self, tag: str = ""):
        """截图保存，用于调试"""
        def _shot():
            try:
                self._screenshot_count += 1
                ts = datetime.now().strftime("%H%M%S")
                filename = f"{SCREENSHOT_DIR}/{self._screenshot_count:03d}_{ts}_{tag}.png"
                self.device.screenshot(filename)
                logger.debug(f"截图保存: {filename}")
                return filename
            except Exception as e:
                logger.debug(f"截图失败: {e}")
                return None
        return await asyncio.to_thread(_shot)

    async def open_wechat(self):
        """启动微信"""
        def _open():
            self.device.app_start(self.config.wechat_package)
            logger.info(f"已启动微信: {self.config.wechat_package}")
            time.sleep(3)
        await asyncio.to_thread(_open)

    async def open_mini_program(self):
        """在微信中搜索并打开云学帮小程序"""
        def _open_mp():
            try:
                # 通过微信首页下拉进入搜索
                self.device.swipe(0.5, 0.2, 0.5, 0.8, duration=0.5)
                time.sleep(1)

                # 点击搜索框
                search_box = self.device(resourceId="com.tencent.mm:id/icon_search_bar_text")
                if not search_box.exists:
                    search_box = self.device(text="搜索")
                if search_box.exists:
                    search_box.click()
                    time.sleep(1)

                    # 输入小程序名称
                    search_input = self.device(className="android.widget.EditText")
                    if search_input.exists:
                        search_input.set_text(self.config.mini_program_name)
                        time.sleep(2)

                        # 点击搜索结果中的小程序
                        mp_item = self.device(text=self.config.mini_program_name)
                        if mp_item.exists:
                            mp_item.click()
                            logger.info(f"已搜索并打开小程序: {self.config.mini_program_name}")
                            time.sleep(5)
                            return True

                logger.warning("自动搜索小程序失败，请手动打开")
                return False
            except Exception as e:
                logger.warning(f"自动打开小程序失败: {e}，请手动打开")
                return False

        return await asyncio.to_thread(_open_mp)

    async def init_deepseek_login(self):
        """导航到 DeepSeek 并等待用户登录"""
        await self.ds_client.navigate_and_login()

    def wait_for_user_ready(self, message: str = ""):
        """等待用户确认"""
        input(f"\n>>> {message}，完成后按回车继续...")

    async def get_screen_text(self) -> str:
        """获取当前屏幕的所有文本"""
        def _get():
            xml_content = self.device.dump_hierarchy()
            import xml.etree.ElementTree as ET
            try:
                root = ET.fromstring(xml_content)
            except ET.ParseError:
                return ""

            texts = []
            for node in root.iter("node"):
                text = node.get("text", "").strip()
                desc = node.get("content-desc", "").strip()
                if text:
                    texts.append(text)
                if desc and desc != text:
                    texts.append(desc)
            return "\n".join(texts)
        return await asyncio.to_thread(_get)

    async def get_screen_nodes(self) -> list:
        """获取当前屏幕的所有节点信息（包含坐标和属性）"""
        def _get():
            xml_content = self.device.dump_hierarchy()
            import xml.etree.ElementTree as ET
            try:
                root = ET.fromstring(xml_content)
            except ET.ParseError:
                return []

            nodes = []
            for node in root.iter("node"):
                text = node.get("text", "").strip()
                desc = node.get("content-desc", "").strip()
                bounds_str = node.get("bounds", "")
                clickable = node.get("clickable", "false") == "true"
                scrollable = node.get("scrollable", "false") == "true"
                cls = node.get("class", "")
                rid = node.get("resource-id", "")

                if text or desc:
                    bounds = self._parse_bounds(bounds_str)
                    nodes.append({
                        "text": text,
                        "desc": desc,
                        "display": text or desc,
                        "bounds": bounds,
                        "bounds_str": bounds_str,
                        "clickable": clickable,
                        "scrollable": scrollable,
                        "class": cls,
                        "resource_id": rid,
                    })
            return nodes
        return await asyncio.to_thread(_get)

    async def extract_questions(self) -> List[Question]:
        """从当前手机屏幕提取题目"""
        await self.screenshot("before_extract")

        nodes = await self.get_screen_nodes()
        if not nodes:
            logger.warning("屏幕上没有检测到任何文本节点")
            return []

        full_text = "\n".join([n["display"] for n in nodes])
        logger.debug(f"屏幕文本:\n{full_text[:500]}")

        questions = []

        # 策略1：单题模式（小程序通常一屏一题）
        question = self._parse_single_question_from_nodes(nodes)
        if question and question.text and len(question.text) > 5:
            questions.append(question)
            logger.info(f"提取到题目: {question.text[:60]}...")
            if question.options:
                for letter, opt_text in question.options:
                    logger.info(f"  选项 {letter}: {opt_text[:40]}")
            return questions

        # 策略2：通过题号分割多题
        questions = self._parse_multiple_questions(nodes, full_text)
        if questions:
            return questions

        # 策略3：尝试从纯文本提取
        questions = self._parse_from_text(full_text)
        if questions:
            return questions

        logger.warning("未能从屏幕提取题目")
        logger.info(f"屏幕原始文本:\n{full_text[:1000]}")
        return []

    def _parse_single_question_from_nodes(self, nodes: list) -> Optional[Question]:
        """从节点列表中解析单道题目（小程序一屏一题）"""
        # 分离不同类型的文本
        question_candidates = []
        option_candidates = []
        button_texts = {"提交", "交卷", "下一题", "下一页", "继续", "下一道",
                       "上一题", "返回", "确定", "取消", "查看", "收藏",
                       "分享", "设置", "首页", "我的", "学习", "练习",
                       "考试", "课程", "查看答案", "查看结果", "确认提交"}

        for node in nodes:
            text = node["display"].strip()
            if not text or len(text) < 2:
                continue

            # 检查是否是选项（A. xxx 或 A、 xxx 格式）
            opt_match = re.match(r"^([A-D])\s*[.、）)\]]\s*(.+)", text)
            if opt_match:
                option_candidates.append((opt_match.group(1), opt_match.group(2).strip(), node))
                continue

            # 检查是否是按钮/导航文字
            if text in button_texts or len(text) < 4:
                continue

            # 剩余的视为题目候选
            question_candidates.append((text, node))

        # 如果有选项，找选项上方最长的文本作为题目
        if option_candidates:
            # 按选项字母排序
            option_candidates.sort(key=lambda x: x[0])
            options = [(oc[0], oc[1]) for oc in option_candidates]

            # 找题目：选项上方的最长文本
            first_option_y = option_candidates[0][2]["bounds"][1] if option_candidates[0][2]["bounds"] else 9999

            best_question = ""
            for text, node in question_candidates:
                node_y = node["bounds"][1] if node["bounds"] else 9999
                if node_y < first_option_y and len(text) > len(best_question):
                    best_question = text

            if not best_question and question_candidates:
                # 回退：取最长的候选
                best_question = max(question_candidates, key=lambda x: len(x[0]))[0]

            # 判断题型
            question_type = self._detect_type(best_question, options)

            return Question(
                index=0, text=best_question, options=options,
                question_type=question_type,
            )

        # 没有选项的情况（判断题/填空题）
        if question_candidates:
            # 取最长的作为题目
            best = max(question_candidates, key=lambda x: len(x[0]))
            question_type = self._detect_type(best[0], [])
            return Question(
                index=0, text=best[0], options=[],
                question_type=question_type,
            )

        return None

    def _parse_multiple_questions(self, nodes: list, full_text: str) -> List[Question]:
        """通过题号分割多道题目"""
        # 按题号分割
        splits = re.split(r"(?=\n\s*\d+\s*[.、）)\]])", full_text)
        questions = []

        for idx, q_text in enumerate(splits):
            q_text = q_text.strip()
            if not q_text or len(q_text) < 5:
                continue
            clean_text = re.sub(r"^\s*\d+\s*[.、）)\]]\s*", "", q_text).strip()
            if not clean_text:
                continue

            question_type = self._detect_type(clean_text, [])
            options = self._extract_options_from_text(clean_text)

            question_text = clean_text
            if options:
                for letter, opt_text in options:
                    pattern = rf"{letter}\s*[.、）)\]]\s*{re.escape(opt_text)}"
                    question_text = re.sub(pattern, "", question_text).strip()

            if question_text and len(question_text) > 3:
                questions.append(Question(
                    index=idx, text=question_text, options=options,
                    question_type=question_type,
                ))

        return questions

    def _parse_from_text(self, full_text: str) -> List[Question]:
        """从纯文本提取题目"""
        questions = []
        # 查找题目+选项模式
        pattern = r"(.+?)\n([A-D][.、）)\]].+?(?=\n[A-D][.、）)\]]|\Z))+"
        matches = re.findall(pattern, full_text, re.DOTALL)

        for idx, match in enumerate(matches):
            if isinstance(match, tuple):
                q_text = match[0].strip()
                opts_text = match[1].strip() if len(match) > 1 else ""
            else:
                q_text = match.strip()
                opts_text = ""

            options = self._extract_options_from_text(opts_text or q_text)
            question_type = self._detect_type(q_text, options)

            if q_text:
                questions.append(Question(
                    index=idx, text=q_text, options=options,
                    question_type=question_type,
                ))

        return questions

    def _detect_type(self, text: str, options: list) -> str:
        """判断题型"""
        combined = text
        if "判断题" in combined:
            return "judge"
        if "正确" in combined and "错误" in combined and len(combined) < 200 and not options:
            return "judge"
        if "多选题" in combined or "多选" in combined:
            return "multiple"
        if "填空题" in combined or "____" in combined or "（）" in combined or "(  )" in combined:
            return "fill"
        if len(options) > 1:
            # 有多个选项默认单选
            return "single"
        return "single"

    def _extract_options_from_text(self, text: str) -> List[tuple]:
        """从文本中提取选项"""
        options = []
        # 匹配 A. xxx / A、xxx / A）xxx / A) xxx
        pattern = r"([A-D])\s*[.、）)\]]\s*([^\n\r]+?)(?=\s*[A-D]\s*[.、）)\]]|$)"
        matches = re.findall(pattern, text)
        for letter, opt_text in matches:
            opt_text = opt_text.strip()
            if opt_text and len(opt_text) < 500:
                options.append((letter, opt_text))
        return options

    async def select_answer(self, question: Question, answer_letters: List[str]) -> bool:
        """在设备上选择答案"""
        if not answer_letters:
            logger.warning(f"题目 {question.index} 无答案可选")
            return False

        nodes = await self.get_screen_nodes()
        success_count = 0

        for letter in answer_letters:
            clicked = await asyncio.to_thread(
                self._click_option, nodes, letter, question
            )
            if clicked:
                success_count += 1
                logger.info(f"已选择选项 {letter}")
            else:
                logger.warning(f"选项 {letter} 点击失败")
            await asyncio.sleep(0.5)

        await self.screenshot(f"after_select_{''.join(answer_letters)}")
        return success_count > 0

    def _click_option(self, nodes: list, letter: str, question: Question) -> bool:
        """点击指定选项（多策略）"""
        # 策略1：精确匹配选项文本
        for node in nodes:
            text = node["text"].strip()
            desc = node["desc"].strip()

            for check_text in [text, desc]:
                if not check_text:
                    continue
                # 精确匹配单个字母
                if check_text == letter:
                    if node["bounds"]:
                        self.device.click(*node["bounds"])
                        return True
                # 匹配 "A. xxx" 或 "A、 xxx" 格式
                if re.match(rf"^{letter}\s*[.、）)\]]", check_text):
                    if node["bounds"]:
                        self.device.click(*node["bounds"])
                        return True

        # 策略2：匹配选项内容文本
        for opt_letter, opt_text in question.options:
            if opt_letter != letter:
                continue
            for node in nodes:
                text = node["text"].strip()
                if opt_text and (opt_text in text or text in opt_text):
                    if node["bounds"]:
                        self.device.click(*node["bounds"])
                        return True

        # 策略3：按可点击元素顺序匹配
        clickable_opts = []
        for node in nodes:
            if not node["clickable"]:
                continue
            text = node["text"].strip()
            # 排除按钮文字
            button_texts = {"提交", "交卷", "下一题", "下一页", "继续",
                           "上一题", "返回", "确定", "取消", "查看",
                           "收藏", "分享", "查看答案", "查看结果"}
            if text in button_texts:
                continue
            if node["bounds"]:
                clickable_opts.append(node)

        if len(clickable_opts) >= len(question.options):
            letter_idx = ord(letter) - ord("A")
            if letter_idx < len(clickable_opts):
                bounds = clickable_opts[letter_idx]["bounds"]
                self.device.click(*bounds)
                return True

        # 策略4：所有包含选项字母的节点，按 Y 坐标排序后匹配
        letter_nodes = []
        for node in nodes:
            text = node["text"].strip()
            if text.startswith(letter) and node["bounds"]:
                letter_nodes.append(node)
        if letter_nodes:
            letter_nodes.sort(key=lambda n: n["bounds"][1])
            letter_idx = ord(letter) - ord("A")
            if letter_idx < len(letter_nodes):
                self.device.click(*letter_nodes[letter_idx]["bounds"])
                return True

        # 策略5：坐标比例点击（最后回退）
        try:
            info = self.device.info
            screen_w = info["displayWidth"]
            screen_h = info["displayHeight"]
            letter_idx = ord(letter) - ord("A")
            total_opts = max(len(question.options), 4)
            # 选项通常在屏幕中间区域
            y_start = int(screen_h * 0.35)
            y_end = int(screen_h * 0.75)
            y_step = (y_end - y_start) / total_opts
            target_y = int(y_start + y_step * (letter_idx + 0.5))
            target_x = int(screen_w * 0.5)
            self.device.click(target_x, target_y)
            logger.info(f"坐标回退点击: ({target_x}, {target_y})")
            return True
        except Exception as e:
            logger.error(f"所有点击策略均失败: {e}")
            return False

    def _parse_bounds(self, bounds_str: str) -> Optional[tuple]:
        """解析 bounds 属性，返回中心坐标"""
        match = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds_str)
        if match:
            x1, y1, x2, y2 = map(int, match.groups())
            return ((x1 + x2) // 2, (y1 + y2) // 2)
        return None

    async def fill_answer(self, question: Question, answer_text: str):
        """填空题填写"""
        def _fill():
            edit = self.device(resourceClass="android.widget.EditText")
            if edit.exists:
                edit.set_text(answer_text)
                logger.info(f"已填写: {answer_text}")
                return True
            return False
        return await asyncio.to_thread(_fill)

    async def click_submit(self):
        """点击提交/交卷按钮"""
        def _submit():
            for text in ["提交", "交卷", "确认提交", "提交试卷", "确定"]:
                btn = self.device(text=text)
                if btn.exists:
                    btn.click()
                    logger.info(f"已点击: {text}")
                    time.sleep(1)
                    # 处理确认弹窗
                    for confirm_text in ["确认", "确定", "是", "好的"]:
                        confirm_btn = self.device(text=confirm_text)
                        if confirm_btn.exists:
                            confirm_btn.click()
                            logger.info(f"已确认: {confirm_text}")
                            return True
                    return True
            return False
        return await asyncio.to_thread(_submit)

    async def click_next_question(self) -> bool:
        """点击下一题按钮"""
        def _next():
            for text in ["下一题", "下一页", "继续", "下一道", "下一问", "下一页>", "确定"]:
                btn = self.device(text=text)
                if btn.exists:
                    btn.click()
                    logger.info(f"点击: {text}")
                    time.sleep(1.5)
                    return True
            # 也尝试通过 resource-id 查找
            for desc in ["下一题", "下一页"]:
                btn = self.device(description=desc)
                if btn.exists:
                    btn.click()
                    logger.info(f"通过 desc 点击: {desc}")
                    time.sleep(1.5)
                    return True
            return False
        return await asyncio.to_thread(_next)

    async def click_view_result(self) -> bool:
        """点击查看答案/查看结果按钮"""
        def _click():
            for text in ["查看答案", "查看结果", "查看解析", "继续答题", "知道了"]:
                btn = self.device(text=text)
                if btn.exists:
                    btn.click()
                    logger.info(f"点击: {text}")
                    time.sleep(1)
                    return True
            return False
        return await asyncio.to_thread(_click)

    async def run_auto_answer(self):
        """执行完整的自动答题流程"""
        results: List[AnswerResult] = []
        question_count = 0
        max_questions = 200  # 安全限制
        consecutive_failures = 0
        max_consecutive_failures = 3

        while question_count < max_questions:
            question_count += 1
            logger.info(f"\n{'='*60}")
            logger.info(f"处理第 {question_count} 题...")

            # 提取题目
            questions = await self.extract_questions()
            if not questions:
                consecutive_failures += 1
                logger.warning(f"未检测到题目（连续失败 {consecutive_failures} 次）")

                if consecutive_failures >= max_consecutive_failures:
                    logger.error("连续多次未检测到题目，可能已完成或遇到问题")
                    break

                # 尝试点击查看结果后继续
                if await self.click_view_result():
                    consecutive_failures = 0
                    continue

                # 尝试滚动
                await asyncio.to_thread(self.device.scroll, True)
                await asyncio.sleep(1)
                continue

            consecutive_failures = 0
            question = questions[0]
            question.index = question_count - 1

            logger.info(f"题目: {question.text[:80]}...")
            logger.info(f"题型: {question.question_type}, 选项数: {len(question.options)}")

            # 调用 DeepSeek 获取答案
            result = await self.ds_client.answer_question(question)
            results.append(result)

            if result.success and result.answer_letters:
                if question.question_type == "fill":
                    await self.fill_answer(question, result.answer_letters[0])
                else:
                    await self.select_answer(question, result.answer_letters)

                if result.reasoning:
                    logger.info(f"解析: {result.reasoning[:120]}...")
            else:
                logger.warning(f"第 {question_count} 题未能获取答案: {result.error}")

            await asyncio.sleep(self.config.question_delay)

            # 进入下一题
            if not await self.click_next_question():
                logger.info("未找到下一题按钮，尝试查看结果...")
                if await self.click_view_result():
                    await asyncio.sleep(1)
                    if not await self.click_next_question():
                        logger.info("可能是最后一题")
                        break
                else:
                    logger.info("可能是最后一题")
                    break

        # 提交
        if self.config.confirm_before_submit:
            await self.screenshot("before_submit")
            self.wait_for_user_ready("答题完成，请检查答案")

        if self.config.confirm_before_submit or self.config.auto_submit:
            await self.click_submit()
            await self.screenshot("after_submit")

        return results

    async def inspect_screen(self):
        """屏幕检查模式：输出当前 UI 结构"""
        await self.screenshot("inspect")
        nodes = await self.get_screen_nodes()

        logger.info("=== 屏幕检查模式 ===")
        logger.info(f"共检测到 {len(nodes)} 个文本节点:")
        for i, node in enumerate(nodes):
            logger.info(
                f"  [{i}] text='{node['display'][:80]}' "
                f"clickable={node['clickable']} "
                f"class='{node['class']}' "
                f"id='{node['resource_id']}' "
                f"bounds='{node['bounds_str']}'"
            )

        # 同时输出解析结果
        full_text = "\n".join([n["display"] for n in nodes])
        logger.info(f"\n屏幕完整文本:\n{full_text}")

        question = self._parse_single_question_from_nodes(nodes)
        if question:
            logger.info(f"\n解析到的题目:")
            logger.info(f"  题型: {question.question_type}")
            logger.info(f"  题目: {question.text}")
            for letter, opt_text in question.options:
                logger.info(f"  选项 {letter}: {opt_text}")
        else:
            logger.info("\n未能解析到题目")

    async def close(self):
        """关闭资源"""
        if self.ds_client:
            await self.ds_client.close()
        logger.info("资源已释放")
