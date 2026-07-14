"""
微信小程序自动化模块（基于 uiautomator2 + Playwright）
通过安卓模拟器运行微信，在微信中打开云学帮小程序
电脑端通过 DeepSeek 网页版获取答案

工作流程：
  电脑启动浏览器 → 打开 DeepSeek 网页 → 用户登录
  模拟器运行微信 → 打开云学帮小程序 → 进入答题页面
  → 提取题目(手机) → DeepSeek答题(电脑) → 选择答案(手机) → 下一题
"""

import re
import time
import asyncio
import logging
from typing import List, Optional

from config import WeChatMiniProgramConfig, DeepSeekWebConfig
from models import Question, AnswerResult, build_prompt, parse_response, TYPE_NAMES
from deepseek_web_client import DeepSeekWebClient

logger = logging.getLogger(__name__)


class WeChatMiniProgramAutomator:
    """微信小程序自动化控制器"""

    def __init__(self, config: WeChatMiniProgramConfig, ds_config: DeepSeekWebConfig):
        self.config = config
        self.ds_config = ds_config
        self.device = None       # uiautomator2 设备
        self.ds_client = None    # DeepSeek 网页客户端

    async def start(self):
        """连接 Android 设备并初始化 DeepSeek 客户端"""
        # 1. 连接 Android 设备
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

    async def open_wechat(self):
        """启动微信"""
        def _open():
            self.device.app_start(self.config.wechat_package)
            logger.info(f"已启动微信: {self.config.wechat_package}")
            time.sleep(3)
        await asyncio.to_thread(_open)

    async def open_mini_program(self):
        """
        在微信中搜索并打开云学帮小程序
        通过微信的搜索功能查找小程序
        """
        def _open_mp():
            try:
                # 方式1：通过微信首页下拉搜索
                # 在微信首页顶部下拉
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
                    search_input = self.device(resourceId="com.tencent.mm:id/b4m")
                    if not search_input.exists:
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

        result = await asyncio.to_thread(_open_mp)
        return result

    async def init_deepseek_login(self):
        """导航到 DeepSeek 并等待用户登录"""
        await self.ds_client.navigate_and_login()

    def wait_for_user_ready(self, message: str = ""):
        """等待用户确认"""
        input(f"\n>>> {message}，完成后按回车继续...")

    async def extract_questions(self) -> List[Question]:
        """从当前手机屏幕提取题目"""
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

        # 收集所有文本节点
        text_nodes = []
        for node in root.iter("node"):
            text = node.get("text", "").strip()
            desc = node.get("content-desc", "").strip()
            if text or desc:
                text_nodes.append({
                    "text": text,
                    "desc": desc,
                    "bounds": node.get("bounds", ""),
                    "clickable": node.get("clickable", "false") == "true",
                    "class": node.get("class", ""),
                    "resource_id": node.get("resource-id", ""),
                })

        if not text_nodes:
            return questions

        full_text = "\n".join([n["text"] or n["desc"] for n in text_nodes])

        # 策略1：通过题号分割题目
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

        # 策略2：单题模式（小程序通常一屏一题）
        if not questions:
            question = self._parse_single_question(text_nodes)
            if question:
                questions.append(question)

        return questions

    def _parse_single_question(self, text_nodes: list) -> Optional[Question]:
        """解析单题模式（小程序一屏一题）"""
        all_text = "\n".join([n["text"] or n["desc"] for n in text_nodes])
        options = self._extract_options_from_text(all_text)
        question_type = self._detect_type(all_text)

        # 找最长的文本作为题目
        question_text = ""
        for node in text_nodes:
            text = (node["text"] or node["desc"]).strip()
            if len(text) > 10 and not re.match(r"^[A-D]\s*[.、）)\]]", text):
                # 排除按钮文字、导航文字等
                skip_keywords = ["提交", "下一题", "上一题", "返回", "确定", "取消",
                                 "查看", "收藏", "分享", "设置", "首页", "我的",
                                 "学习", "练习", "考试", "课程"]
                if any(kw in text for kw in skip_keywords) and len(text) < 20:
                    continue
                question_text = text
                break

        if not question_text:
            # 回退：取所有文本的前200字符
            question_text = all_text[:200]

        # 去掉选项部分
        if options:
            for letter, opt_text in options:
                pattern = rf"{letter}\s*[.、）)\]]\s*{re.escape(opt_text)}"
                question_text = re.sub(pattern, "", question_text).strip()

        if question_text:
            return Question(index=0, text=question_text, options=options,
                            question_type=question_type)
        return None

    def _detect_type(self, text: str) -> str:
        """判断题型"""
        if "判断题" in text or ("正确" in text and "错误" in text and len(text) < 200):
            return "judge"
        if "多选题" in text or "多选" in text:
            return "multiple"
        if "填空题" in text or "____" in text or "（）" in text:
            return "fill"
        return "single"

    def _extract_options_from_text(self, text: str) -> List[tuple]:
        """从文本中提取选项"""
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
        """通过选项字母点击对应元素"""
        import xml.etree.ElementTree as ET

        try:
            root = ET.fromstring(xml_content)
        except ET.ParseError:
            return False

        # 策略1：精确匹配选项字母文本
        for node in root.iter("node"):
            text = node.get("text", "").strip()
            desc = node.get("content-desc", "").strip()
            for check_text in [text, desc]:
                if not check_text:
                    continue
                # 精确匹配单个字母
                if check_text == letter:
                    bounds = self._parse_bounds(node.get("bounds", ""))
                    if bounds:
                        self.device.click(*bounds)
                        return True
                # 匹配 "A. xxx" 或 "A、 xxx" 格式
                if re.match(rf"^{letter}\s*[.、）)\]]", check_text):
                    bounds = self._parse_bounds(node.get("bounds", ""))
                    if bounds:
                        self.device.click(*bounds)
                        return True

        # 策略2：匹配选项内容文本
        for node in root.iter("node"):
            text = node.get("text", "").strip()
            for opt_letter, opt_text in question.options:
                if opt_letter == letter and opt_text and opt_text in text:
                    bounds = self._parse_bounds(node.get("bounds", ""))
                    if bounds:
                        self.device.click(*bounds)
                        return True

        # 策略3：查找可点击的列表项，按顺序匹配
        clickable_nodes = []
        for node in root.iter("node"):
            clickable = node.get("clickable", "false") == "true"
            text = node.get("text", "").strip()
            if clickable and text:
                # 排除按钮和导航
                skip_keywords = ["提交", "下一题", "上一题", "返回", "确定", "取消"]
                if any(kw in text for kw in skip_keywords):
                    continue
                clickable_nodes.append((text, node))

        # 如果可点击项数量与选项数量匹配
        if len(clickable_nodes) >= len(question.options):
            letter_idx = ord(letter) - ord("A")
            if letter_idx < len(clickable_nodes):
                _, node = clickable_nodes[letter_idx]
                bounds = self._parse_bounds(node.get("bounds", ""))
                if bounds:
                    self.device.click(*bounds)
                    return True

        # 策略4：坐标比例点击（小程序选项通常从上到下排列）
        try:
            info = self.device.info
            screen_w = info["displayWidth"]
            screen_h = info["displayHeight"]
            letter_idx = ord(letter) - ord("A")
            total_opts = max(len(question.options), 4)
            y_start = int(screen_h * 0.35)
            y_end = int(screen_h * 0.80)
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
                logger.info(f"题目 {question.index} 已填写: {answer_text}")
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
            for text in ["下一题", "下一页", "继续", "下一道", "下一问"]:
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

        if self.config.confirm_before_submit or self.config.auto_submit:
            await self.click_submit()

        return results

    async def inspect_screen(self):
        """屏幕检查模式：输出当前 UI 结构"""
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
                cls = node.get("class", "")
                if text or desc:
                    display = text or desc
                    logger.info(
                        f"  [{i}] text='{display[:80]}' "
                        f"clickable={clickable} "
                        f"class='{cls}' "
                        f"id='{rid}' "
                        f"bounds='{bounds}'"
                    )
        await asyncio.to_thread(_inspect)

    async def close(self):
        """关闭资源"""
        if self.ds_client:
            await self.ds_client.close()
        logger.info("资源已释放")
