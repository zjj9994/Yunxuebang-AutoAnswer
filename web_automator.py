"""
Web 端自动化模块（基于 Playwright）
使用 DeepSeek 网页版获取答案，自动在云学帮 Web 页面上选择并提交

工作流程：
  浏览器启动 → 打开两个标签页（DeepSeek + 云学帮）
  → 用户分别登录 → 提取题目 → DeepSeek 答题 → 云学帮选题 → 提交
"""

import re
import time
import logging
from typing import List, Optional

from config import WebAutomationConfig, DeepSeekWebConfig
from models import Question, AnswerResult, build_prompt, parse_response, TYPE_NAMES
from deepseek_web_client import DeepSeekWebClient

logger = logging.getLogger(__name__)


class WebAutomator:
    """Web 端自动化控制器，使用 Playwright 驱动浏览器"""

    def __init__(self, config: WebAutomationConfig, ds_config: DeepSeekWebConfig):
        self.config = config
        self.ds_config = ds_config
        self.playwright = None
        self.browser = None
        self.page = None              # 云学帮页面
        self.ds_client = None         # DeepSeek 网页客户端

    async def start(self):
        """启动浏览器"""
        from playwright.async_api import async_playwright

        self.playwright = await async_playwright().start()

        launch_kwargs = {
            "headless": self.config.headless,
            "args": ["--disable-blink-features=AutomationControlled"],
        }

        browsers = {
            "chromium": self.playwright.chromium,
            "firefox": self.playwright.firefox,
            "webkit": self.playwright.webkit,
        }
        browser_type = browsers.get(self.config.browser_type, self.playwright.chromium)

        # 使用持久化上下文以保持登录状态
        if self.config.user_data_dir:
            self.browser = await browser_type.launch_persistent_context(
                user_data_dir=self.config.user_data_dir,
                headless=self.config.headless,
                viewport={
                    "width": self.config.viewport_width,
                    "height": self.config.viewport_height,
                },
                args=["--disable-blink-features=AutomationControlled"],
            )
            self.page = self.browser.pages[0] if self.browser.pages else await self.browser.new_page()
        else:
            self.browser = await browser_type.launch(**launch_kwargs)
            self.page = await self.browser.new_page()
            await self.page.set_viewport_size({
                "width": self.config.viewport_width,
                "height": self.config.viewport_height,
            })

        # 反自动化检测
        await self.page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        """)

        logger.info("浏览器已启动")

    async def init_deepseek(self):
        """初始化 DeepSeek 网页客户端（共享浏览器，新开标签页）"""
        self.ds_client = DeepSeekWebClient(self.ds_config)
        await self.ds_client.init_with_shared_browser(self.playwright, self.browser)

    async def navigate(self, url: str):
        """导航到指定 URL（云学帮页面）"""
        await self.page.bring_to_front()
        await self.page.goto(url, wait_until="networkidle", timeout=30000)
        logger.info(f"云学帮页面已导航到: {url}")

    async def wait_for_user_ready(self, message: str = ""):
        """等待用户确认（在控制台按回车）"""
        await self.page.bring_to_front()
        try:
            await self.page.evaluate(f"""
                () => {{
                    const div = document.createElement('div');
                    div.id = 'auto-answer-notice';
                    div.style.cssText = 'position:fixed;top:20px;left:50%;transform:translateX(-50%);'
                        + 'background:#4CAF50;color:white;padding:15px 30px;border-radius:8px;'
                        + 'font-size:18px;z-index:99999;box-shadow:0 4px 12px rgba(0,0,0,0.3);';
                    div.textContent = '{message} - 完成后请在终端按回车继续';
                    document.body.appendChild(div);
                    setTimeout(() => div.remove(), 30000);
                }}
            """)
        except Exception:
            pass
        input(f"\n>>> {message}，完成后按回车继续...")

    async def extract_questions(self) -> List[Question]:
        """从当前页面提取所有题目"""
        await self.page.bring_to_front()
        questions = []

        # 策略1：尝试常见的选择器模式
        question_selectors = [
            ".question-item", ".exam-question", ".topic-item",
            ".q-item", ".question", "[class*='question']",
            "[class*='topic']", "[class*='exam-item']",
            ".el-card", ".ant-card", ".list-item",
        ]

        question_elements = []
        for selector in question_selectors:
            elements = await self.page.query_selector_all(selector)
            if elements and len(elements) > 0:
                logger.info(f"使用选择器 '{selector}' 找到 {len(elements)} 个元素")
                question_elements = elements
                break

        if not question_elements:
            logger.info("未找到题目元素，尝试智能识别...")
            question_elements = await self._smart_find_questions()

        if not question_elements:
            logger.warning("当前页面未检测到题目，请确认已进入答题页面")
            return questions

        for i, elem in enumerate(question_elements):
            question = await self._parse_question_element(elem, i)
            if question and question.text:
                questions.append(question)

        logger.info(f"共提取到 {len(questions)} 道题目")
        return questions

    async def _smart_find_questions(self):
        """使用 JavaScript 智能识别页面上的题目元素"""
        js_code = """
        () => {
            const allElements = document.querySelectorAll('div, section, article, li');
            const candidates = [];
            for (const el of allElements) {
                const text = el.textContent || '';
                const hasOptions = /[A-D][.、）)]\\s/.test(text) ||
                                   /[A-D][.、）)]\\s/.test(el.innerHTML);
                const hasInputs = el.querySelectorAll(
                    'input[type="radio"], input[type="checkbox"], [class*="option"], [class*="choice"]'
                ).length > 0;
                if ((hasOptions || hasInputs) && text.length > 10 && text.length < 5000) {
                    const isChild = candidates.some(c => c.contains(el));
                    const containsExisting = candidates.some(c => el.contains(c));
                    if (isChild) continue;
                    if (containsExisting) {
                        for (let i = candidates.length - 1; i >= 0; i--) {
                            if (el.contains(candidates[i])) candidates.splice(i, 1);
                        }
                    }
                    candidates.push(el);
                }
            }
            return candidates;
        }
        """
        try:
            elements = await self.page.evaluate_handle(js_code)
            props = await elements.get_properties()
            result = []
            for prop in props.values():
                elem = prop.as_element()
                if elem:
                    result.append(elem)
            return result
        except Exception as e:
            logger.debug(f"智能识别失败: {e}")
            return []

    async def _parse_question_element(self, element, index: int) -> Optional[Question]:
        """解析单个题目元素"""
        try:
            inner_text = await element.inner_text()
            inner_html = await element.inner_html()
        except Exception:
            return None

        text = inner_text.strip()
        text = re.sub(r"^\s*\d+[.、）)\]]\s*", "", text)
        text = text.strip()

        question_type = self._detect_question_type(text, inner_html)
        options = await self._extract_options(element, inner_text)

        if options:
            first_option_text = options[0][1] if options else ""
            if first_option_text and first_option_text in text:
                idx = text.find(first_option_text)
                for offset in range(min(10, idx)):
                    if text[idx - offset - 1:idx - offset] in "ABCDEFGH":
                        text = text[:idx - offset - 1].strip()
                        break

        return Question(
            index=index, text=text, options=options,
            question_type=question_type, raw_html=inner_html[:500],
        )

    def _detect_question_type(self, text: str, html: str) -> str:
        """根据文本和 HTML 判断题型"""
        combined = text + " " + html.lower()
        if "判断题" in combined or ("正确" in text and "错误" in text and len(text) < 100):
            if "正确" in text and "错误" in text:
                return "judge"
        if "多选题" in combined or "多选" in combined:
            return "multiple"
        if "填空题" in combined or "____" in text or "（）" in text or "()" in text:
            if "input" in html.lower() or "textarea" in html.lower():
                return "fill"
        if 'type="checkbox"' in html.lower() or "checkbox" in html.lower():
            return "multiple"
        if 'type="radio"' in html.lower() or "radio" in html.lower():
            return "single"
        return "single"

    async def _extract_options(self, element, full_text: str) -> List[tuple]:
        """从题目元素中提取选项"""
        options = []

        # 策略1：查找 label
        try:
            labels = await element.query_selector_all("label")
            for label in labels:
                label_text = (await label.inner_text()).strip()
                if not label_text:
                    continue
                match = re.match(r"^([A-Z])\s*[.、）)\]]\s*(.+)", label_text)
                if match:
                    options.append((match.group(1), match.group(2).strip()))
                    continue
                for_attr = await label.get_attribute("for")
                if for_attr:
                    letter = chr(ord("A") + len(options))
                    options.append((letter, label_text))
        except Exception:
            pass

        if options:
            return options

        # 策略2：查找选项类名元素
        for selector in [".option", ".choice", "[class*='option']", "[class*='choice']",
                         ".el-radio", ".el-checkbox", ".ant-radio-wrapper", ".ant-checkbox-wrapper"]:
            try:
                opt_elems = await element.query_selector_all(selector)
                if opt_elems:
                    for opt_elem in opt_elems:
                        opt_text = (await opt_elem.inner_text()).strip()
                        match = re.match(r"^([A-Z])\s*[.、）)\]]\s*(.+)", opt_text)
                        if match:
                            options.append((match.group(1), match.group(2).strip()))
                        elif opt_text:
                            letter = chr(ord("A") + len(options))
                            options.append((letter, opt_text))
                    if options:
                        break
            except Exception:
                continue

        if options:
            return options

        # 策略3：正则提取
        pattern = r"([A-D])\s*[.、）)\]]\s*([^\n\r]+?)(?=\s*[A-D]\s*[.、）)\]]|$)"
        matches = re.findall(pattern, full_text)
        for letter, text in matches:
            text = text.strip()
            if text:
                options.append((letter, text))
        return options

    async def select_answer(self, question: Question, answer_letters: List[str]):
        """在云学帮页面上选择答案"""
        await self.page.bring_to_front()
        if not answer_letters:
            logger.warning(f"题目 {question.index} 无答案可选，跳过")
            return False

        question_elements = await self.page.query_selector_all(
            ".question-item, .exam-question, .topic-item, .q-item, .question, [class*='question'], [class*='topic']"
        )
        if question.index < len(question_elements):
            elem = question_elements[question.index]
        else:
            logger.warning(f"题目 {question.index} 元素未找到")
            return False

        success_count = 0
        for letter in answer_letters:
            clicked = await self._click_option(elem, letter, question)
            if clicked:
                success_count += 1
                logger.info(f"题目 {question.index} 已选择选项 {letter}")
            else:
                logger.warning(f"题目 {question.index} 选项 {letter} 点击失败")
            await self.page.wait_for_timeout(300)
        return success_count > 0

    async def _click_option(self, question_elem, letter: str, question: Question) -> bool:
        """点击指定选项"""
        # 策略1：label 文本
        try:
            labels = await question_elem.query_selector_all("label")
            for label in labels:
                text = (await label.inner_text()).strip()
                if text.startswith(letter) or re.match(rf"^{letter}\s*[.、）)\]]", text):
                    await label.click()
                    return True
        except Exception:
            pass

        # 策略2：选项类名
        for selector in [".option", ".choice", "[class*='option']", "[class*='choice']",
                         ".el-radio", ".el-checkbox", ".ant-radio-wrapper", ".ant-checkbox-wrapper"]:
            try:
                opts = await question_elem.query_selector_all(selector)
                for i, opt in enumerate(opts):
                    if i < len(question.options) and question.options[i][0] == letter:
                        await opt.click()
                        return True
            except Exception:
                continue

        # 策略3：JavaScript
        try:
            result = await self.page.evaluate("""
            (args) => {
                const [elem, letter] = args;
                const allText = elem.querySelectorAll('*');
                for (const el of allText) {
                    const text = el.textContent || '';
                    if (text.trim().startsWith(letter) && text.length < 200) {
                        el.click();
                        return true;
                    }
                }
                const inputs = elem.querySelectorAll('input[type="radio"], input[type="checkbox"]');
                const index = letter.charCodeAt(0) - 65;
                if (index < inputs.length) {
                    inputs[index].click();
                    return true;
                }
                return false;
            }
            """, [question_elem, letter])
            if result:
                return True
        except Exception:
            pass
        return False

    async def fill_answer(self, question: Question, answer_text: str):
        """填空题填写"""
        await self.page.bring_to_front()
        question_elements = await self.page.query_selector_all(
            ".question-item, .exam-question, .topic-item, .q-item, .question, [class*='question'], [class*='topic']"
        )
        if question.index >= len(question_elements):
            return False
        elem = question_elements[question.index]
        try:
            inputs = await elem.query_selector_all("input[type='text'], textarea, input:not([type])")
            if inputs:
                await inputs[0].fill(answer_text)
                logger.info(f"题目 {question.index} 已填写: {answer_text}")
                return True
        except Exception as e:
            logger.error(f"题目 {question.index} 填写失败: {e}")
        return False

    async def submit_exam(self):
        """提交试卷"""
        await self.page.bring_to_front()
        for selector in ["button:has-text('提交')", "button:has-text('交卷')",
                         "button:has-text('确认提交')", "[class*='submit']",
                         "input[type='submit']", ".submit-btn", "#submit"]:
            try:
                btn = await self.page.query_selector(selector)
                if btn and await btn.is_visible():
                    await btn.click()
                    logger.info("已点击提交按钮")
                    await self.page.wait_for_timeout(1000)
                    confirm = await self.page.query_selector(
                        "button:has-text('确认'), button:has-text('确定'), button:has-text('是')"
                    )
                    if confirm:
                        await confirm.click()
                        logger.info("已确认提交")
                    return True
            except Exception:
                continue
        logger.warning("未找到提交按钮")
        return False

    async def run_auto_answer(self):
        """执行完整的自动答题流程"""
        results: List[AnswerResult] = []

        # 1. 提取题目
        questions = await self.extract_questions()
        if not questions:
            logger.error("未检测到题目，请确认页面内容")
            return results

        # 2. 逐题回答
        for i, question in enumerate(questions):
            logger.info(f"\n{'='*60}")
            logger.info(f"第 {i+1}/{len(questions)} 题: {question.text[:50]}...")
            logger.info(f"题型: {question.question_type}, 选项数: {len(question.options)}")

            # 调用 DeepSeek 网页版获取答案
            result = await self.ds_client.answer_question(question)
            results.append(result)

            if result.success and result.answer_letters:
                # 切回云学帮页面选择答案
                if question.question_type == "fill":
                    await self.fill_answer(question, result.answer_letters[0])
                else:
                    await self.select_answer(question, result.answer_letters)

                if result.reasoning:
                    logger.info(f"解析: {result.reasoning[:100]}...")
            else:
                logger.warning(f"题目 {i+1} 未能获取答案: {result.error}")

            # 延迟
            if i < len(questions) - 1:
                await self.page.wait_for_timeout(int(self.config.question_delay * 1000))

        # 3. 提交
        if self.config.confirm_before_submit:
            await self.wait_for_user_ready("答题完成，请检查答案")

        if self.config.confirm_before_submit or self.config.auto_submit:
            await self.submit_exam()

        return results

    async def inspect_page(self):
        """页面检查模式"""
        await self.page.bring_to_front()
        logger.info("=== 页面检查模式 ===")
        title = await self.page.title()
        url = self.page.url
        logger.info(f"页面标题: {title}")
        logger.info(f"页面 URL: {url}")

        for selector in [".question-item", ".exam-question", ".topic-item", ".q-item",
                         ".question", "[class*='question']", "[class*='topic']", ".el-card", ".ant-card"]:
            elements = await self.page.query_selector_all(selector)
            if elements:
                logger.info(f"选择器 '{selector}' 匹配到 {len(elements)} 个元素")
                for i, elem in enumerate(elements[:3]):
                    text = (await elem.inner_text())[:200]
                    logger.info(f"  [{i}] {text[:100]}...")

    async def close(self):
        """关闭浏览器"""
        if self.ds_client:
            await self.ds_client.close()
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
        logger.info("浏览器已关闭")
