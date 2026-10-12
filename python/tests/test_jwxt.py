import io
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from qfnu import jwxt_auth, jwxt_client, trace
from qfnu.cli import run
from qfnu.jwxt import run_jwxt
from qfnu.jwxt_auth import (
    SESSION_EXPIRED_HINT,
    WEEK_MANUAL_HINT,
    encode_credentials,
    fetch_teaching_week,
    login_failure_hint,
    parse_login_message,
    parse_teaching_week,
    resolve_week,
)
from qfnu.jwxt_auth import status as jwxt_status
from qfnu.jwxt_client import (
    JWXT_BASE,
    PROFILE_URL,
    JWXTClient,
    JWXTError,
    cookies_for_url,
    is_credential_request,
    make_cookie,
)
from qfnu.jwxt_exams import exams, parse_exams, selected_semester, term_category
from qfnu.jwxt_grades import grades, parse_grades
from qfnu.jwxt_program import parse_program, program
from qfnu.jwxt_schedule import parse_schedule, schedule, schedule_url
from qfnu.result import failure, write_json


class JWXTAuthTest(unittest.TestCase):
    def test_encode_credentials_matches_qfnu_protocol(self):
        self.assertEqual(encode_credentials("abc", "pw", "XYZ123", "10120"), "aXbcY%Z1%%pw")

    def test_encode_credentials_keeps_characters_after_limit(self):
        username = "12345678901234567890"
        self.assertEqual(encode_credentials(username, "pw", "", ""), username[:20] + "%%%pw")

    def test_parse_login_message_returns_exact_show_msg_text(self):
        raw = (
            '<li class="input_li" id="showMsg" style="color: red; margin-bottom: 0;">'
            "\n\t\t\t&nbsp;验证码错误!!\n\t\t</li>"
        )
        self.assertEqual(parse_login_message(raw), "验证码错误!!")

    def test_parse_login_message_supports_nested_markup(self):
        raw = '<div id=\'showMsg\'><font color="red">用户名或密码错误</font></div>'
        self.assertEqual(parse_login_message(raw), "用户名或密码错误")

    def test_parse_login_message_ignores_page_without_show_msg(self):
        self.assertEqual(parse_login_message("<html><body>登录失败</body></html>"), "")

    def test_login_failure_hint_matches_captcha_message(self):
        self.assertEqual(
            login_failure_hint("验证码错误!!"),
            "重新运行 easy-qfnu jwxt captcha 获取新验证码",
        )

    def test_status_reports_corrupt_session(self):
        with tempfile.TemporaryDirectory() as temp:
            path = os.path.join(temp, "session.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{")
            out = io.StringIO()
            run_jwxt(["status", "--session-path", path], out)
            self.assertIn("parse JWXT session", out.getvalue())

    def test_logout_removes_corrupt_session(self):
        with tempfile.TemporaryDirectory() as temp:
            path = os.path.join(temp, "session.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{")
            out = io.StringIO()
            run_jwxt(["logout", "--session-path", path], out)
            self.assertFalse(os.path.exists(path))
            self.assertIn('"logged_in": false', out.getvalue())

    def test_logout_reports_removal_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            path = os.path.join(temp, "session")
            os.mkdir(path)
            with open(os.path.join(path, "child"), "w", encoding="utf-8") as handle:
                handle.write("x")
            out = io.StringIO()
            run_jwxt(["logout", "--session-path", path], out)
            self.assertIn("clear JWXT session", out.getvalue())

    def test_cli_dispatches_jwxt_and_rejects_unknown_action(self):
        out = io.StringIO()
        code = run(["jwxt", "nope"], out, io.StringIO())
        self.assertEqual(code, 0)
        body = json.loads(out.getvalue())
        self.assertFalse(body["ok"])
        self.assertIn("unknown action: nope", body["error"])

    def test_login_without_captcha_requires_manual_reading(self):
        trace.reset(False)
        with tempfile.TemporaryDirectory() as temp:
            path = os.path.join(temp, "session.json")
            out = io.StringIO()
            code = run_jwxt(
                [
                    "login",
                    "--username",
                    "2023413695",
                    "--password",
                    "placeholder",
                    "--session-path",
                    path,
                ],
                out,
            )
        self.assertEqual(code, 0)
        body = json.loads(out.getvalue())
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"], "captcha is required")
        self.assertIn("jwxt captcha", body["hint"])
        self.assertIsNone(trace.last_exchange())

    def test_status_keeps_login_when_profile_enrichment_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            client = JWXTClient(os.path.join(temp, "session.json"))
            client.meta["username"] = "student"
            client.jar.set_cookie(
                make_cookie("JSESSIONID", "active", "/", "zhjw.qfnu.edu.cn", False, None)
            )

            def fake_request(method, target, body=None, headers=None, same_origin=False):
                del method, body, headers, same_origin
                if target.endswith("/jsxsd/framework/xsMain.jsp"):
                    return 200, target, "教学一体化服务平台".encode()
                raise OSError("profile unavailable")

            client.request = fake_request
            result = jwxt_status(client)
            self.assertTrue(result["logged_in"])
            self.assertIn("资料补全", result.get("profile_warning", ""))

    def test_session_persists_cookies_for_scoped_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            path = os.path.join(temp, "session.json")
            client = JWXTClient(path)
            client.jar.set_cookie(make_cookie("JSESSIONID", "root", "/", "zhjw.qfnu.edu.cn", False, None))
            client.jar.set_cookie(
                make_cookie("JSESSIONID", "jsxsd", "/jsxsd", "zhjw.qfnu.edu.cn", False, None)
            )
            client.persist({})
            with open(path, "r", encoding="utf-8") as handle:
                saved = json.load(handle)
            paths = {item["Path"]: item["Value"] for item in saved["cookies"]}
            self.assertEqual(paths.get("/"), "root")
            self.assertEqual(paths.get("/jsxsd"), "jsxsd")

            loaded = JWXTClient(path)
            loaded.load()
            root_values = cookie_values(cookies_for_url(loaded.jar, JWXT_BASE + "/"), "JSESSIONID")
            jsxsd_values = cookie_values(
                cookies_for_url(loaded.jar, JWXT_BASE + "/jsxsd/framework/xsMain.jsp"),
                "JSESSIONID",
            )
            self.assertEqual(root_values, ["root"])
            self.assertIn("jsxsd", jsxsd_values)


GRADE_TABLE = """
<table>
<tr><th>开课学期</th><th>课程编号</th><th>课程名称</th><th>成绩</th><th>学分</th><th>绩点</th><th>备注</th></tr>
<tr><td>2024-2025-1</td><td>CS101</td><td>程序设计</td><td>90</td><td>3.0</td><td>4.0</td><td>忽略</td></tr>
</table>
"""


class JWXTGradesTest(unittest.TestCase):
    def test_parse_grades_maps_known_headers(self):
        _rows, items = parse_grades(GRADE_TABLE, "")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["course_id"], "CS101")
        self.assertEqual(items[0]["course_name"], "程序设计")
        self.assertEqual(items[0]["score"], "90")
        self.assertEqual(items[0]["credits"], "3.0")
        self.assertEqual(items[0]["gpa"], "4.0")
        self.assertEqual(items[0]["semester"], "2024-2025-1")
        self.assertNotIn("备注", items[0])

    def test_parse_grades_overrides_semester_query(self):
        _rows, items = parse_grades(GRADE_TABLE, "2025-2026-3")
        self.assertEqual(items[0]["semester"], "2025-2026-3")

    def test_parse_grades_header_only_is_empty(self):
        raw = "<table><tr><th>开课学期</th><th>课程名称</th></tr></table>"
        _rows, items = parse_grades(raw, "2025-2026-3")
        self.assertEqual(items, [])

    def test_grades_requires_login_page(self):
        client = JWXTClient()
        client.text = lambda *_args, **_kwargs: (
            200,
            "http://zhjw.qfnu.edu.cn/jsxsd/kscj/cjcx_list",
            "请输入账号 请输入密码 请输入验证码",
        )
        with self.assertRaises(Exception) as caught:
            grades(client, "2025-2026-3")
        self.assertEqual(caught.exception.message, "grades page requires login")

    def test_cli_grades_returns_items(self):
        original_text = JWXTClient.text

        def fake_text(self, method, target, body=None, headers=None, same_origin=False):
            del self, method, body, headers, same_origin
            self_target = target
            if "kksj=2025-2026-3" not in self_target:
                raise AssertionError("missing semester query: " + self_target)
            return 200, self_target, GRADE_TABLE

        JWXTClient.text = fake_text
        try:
            with tempfile.TemporaryDirectory() as temp:
                path = os.path.join(temp, "session.json")
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write('{"cookies":[]}\n')
                out = io.StringIO()
                code = run_jwxt(["grades", "--semester", "2025-2026-3", "--session-path", path], out)
        finally:
            JWXTClient.text = original_text
        self.assertEqual(code, 0)
        body = json.loads(out.getvalue())
        self.assertTrue(body["ok"])
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["items"], body["grades"])
        self.assertEqual(body["items"][0]["course_name"], "程序设计")
        self.assertEqual(body["semester"], "2025-2026-3")

    def test_cli_grades_kksj_alias_and_login_failure(self):
        original_text = JWXTClient.text

        def fake_text(self, method, target, body=None, headers=None, same_origin=False):
            del self, method, body, headers, same_origin
            return 200, target, "请输入账号请输入密码请输入验证码"

        JWXTClient.text = fake_text
        try:
            with tempfile.TemporaryDirectory() as temp:
                path = os.path.join(temp, "session.json")
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write("{}\n")
                out = io.StringIO()
                code = run_jwxt(["grades", "--kksj", "2025-2026-3", "--session-path", path], out)
        finally:
            JWXTClient.text = original_text
        self.assertEqual(code, 0)
        body = json.loads(out.getvalue())
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"], "grades page requires login")


SCHEDULE_TABLE = """
<table id="kbtable">
<tr><th>节次</th><th>星期一</th><th>星期二</th><th>备注</th></tr>
<tr>
  <td>第1-2节</td>
  <td>
    <div class="kbcontent">大学英语<br>张三<br>教1-101</div>
    <div class="kbcontent" style="display:none">&nbsp;</div>
  </td>
  <td>&nbsp;</td>
  <td>忽略</td>
</tr>
</table>
"""


class JWXTScheduleTest(unittest.TestCase):
    def test_parse_schedule_maps_weekday_columns(self):
        _rows, items = parse_schedule(SCHEDULE_TABLE)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["day"], "周一")
        self.assertEqual(items[0]["period"], "第1-2节")
        self.assertEqual(items[0]["course_name"], "大学英语")
        self.assertEqual(items[0]["lines"][1], "张三")
        self.assertIn("教1-101", items[0]["text"])

    def test_parse_schedule_skips_empty_and_unknown_columns(self):
        raw = """
        <table id="kbtable">
        <tr><th>节次</th><th>星期一</th><th>星期二</th><th>备注</th></tr>
        <tr><td>第3-4节</td><td>-</td><td>&nbsp;</td><td>其它</td></tr>
        </table>
        """
        _rows, items = parse_schedule(raw)
        self.assertEqual(items, [])

    def test_parse_schedule_header_only_is_empty(self):
        raw = '<table id="kbtable"><tr><th>节次</th><th>星期一</th></tr></table>'
        _rows, items = parse_schedule(raw)
        self.assertEqual(items, [])

    def test_schedule_url_includes_week_and_mode(self):
        target = schedule_url("2025-2026-3", "1", "ABC")
        self.assertIn("xskb_list.do?", target)
        self.assertIn("sfFD=1", target)
        self.assertIn("xnxq01id=2025-2026-3", target)
        self.assertIn("zc=1", target)
        self.assertIn("kbjcmsid=ABC", target)

    def test_schedule_requires_login_page(self):
        client = JWXTClient()
        client.text = lambda *_args, **_kwargs: (
            200,
            "http://zhjw.qfnu.edu.cn/jsxsd/xskb/xskb_list.do",
            "请输入账号 请输入密码 请输入验证码",
        )
        with self.assertRaises(Exception) as caught:
            schedule(client, "2025-2026-3", "1", "")
        self.assertEqual(caught.exception.message, "schedule page requires login")

    def test_cli_schedule_returns_items(self):
        original_text = JWXTClient.text

        def fake_text(self, method, target, body=None, headers=None, same_origin=False):
            del self, method, body, headers, same_origin
            if "xnxq01id=2025-2026-3" not in target or "zc=1" not in target:
                raise AssertionError("missing schedule query: " + target)
            return 200, target, SCHEDULE_TABLE

        JWXTClient.text = fake_text
        try:
            with tempfile.TemporaryDirectory() as temp:
                path = os.path.join(temp, "session.json")
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write('{"cookies":[]}\n')
                out = io.StringIO()
                code = run_jwxt(
                    ["schedule", "--semester", "2025-2026-3", "--week", "1", "--session-path", path],
                    out,
                )
        finally:
            JWXTClient.text = original_text
        self.assertEqual(code, 0)
        body = json.loads(out.getvalue())
        self.assertTrue(body["ok"])
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["items"], body["schedule"])
        self.assertEqual(body["items"][0]["day"], "周一")
        self.assertEqual(body["week"], "1")


PROGRAM_PAGE = """
<table id="dataList">
<caption>网络空间安全教学计划培养方案及教学计划</caption>
<tr><td><span id="pymb">培养德智体美劳全面发展的人才</span></td></tr>
<tr><td><span id="pymb">本专业培养网络安全人才。<br>1.掌握基础。<br>2.具备实践。</span></td></tr>
<table id="mxh">
<tbody>
<tr>
  <th>课程体系</th><th>选课组</th><th>课程编号</th><th>课程名称</th>
  <th>完成情况</th><th>课程性质</th><th>课程属性</th><th>学分</th>
  <th>讲课学时</th><th>实践学时</th><th>讲座学时</th><th>实验学时</th>
  <th>设计学时</th><th>其中上机学时</th><th>讨论辅导学时</th><th>课外学时</th>
  <th>网络学时</th><th>总学时</th><th>开设学期</th>
</tr>
<tr>
  <td align="center" rowspan="2">通识课-创新创业课组<br>(应修 2 / 已修 2)</td>
  <td>&nbsp;</td>
  <td>540001</td><td>大学生职业规划</td><td>已修(优)</td>
  <td>公共必修课</td><td>必修</td><td>1</td>
  <td>18</td><td>0</td><td>0</td><td>0</td><td>0</td><td>0</td><td>0</td><td>0</td><td>0</td>
  <td>18</td><td>2</td>
</tr>
<tr>
  <td>&nbsp;</td>
  <td>540005</td><td>大学生就业与创业指导</td><td>已修(良)</td>
  <td>公共必修课</td><td>必修</td><td>1</td>
  <td>18</td><td>0</td><td>0</td><td>0</td><td>0</td><td>0</td><td>0</td><td>0</td><td>0</td>
  <td>18</td><td>5</td>
</tr>
<tr><td colspan="7">小计</td><td>2</td></tr>
<tr>
  <td align="center" rowspan="1">专业课-专业核心课程模块<br>(应修 68 / 已修 64)</td>
  <td>&nbsp;</td>
  <td>301001</td><td>高等数学1</td><td></td>
  <td>专业必修课</td><td>必修</td><td>4</td>
  <td>72</td><td>0</td><td>0</td><td>0</td><td>0</td><td>0</td><td>0</td><td>0</td><td>0</td>
  <td>72</td><td>1</td>
</tr>
<tr><td colspan="7">小计</td><td>68</td></tr>
<tr><td colspan="2">学年学期</td></tr>
<tr><td colspan="7">合计</td></tr>
</tbody>
</table>
</table>
"""


class JWXTProgramTest(unittest.TestCase):
    def test_parse_program_maps_mxh_groups_and_continuation_rows(self):
        meta, groups, items = parse_program(PROGRAM_PAGE)
        self.assertEqual(meta["program_name"], "网络空间安全教学计划培养方案及教学计划")
        self.assertEqual(meta["objectives"], "培养德智体美劳全面发展的人才")
        self.assertIn("本专业培养网络安全人才", meta["description"])
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0]["group_name"], "通识课-创新创业课组")
        self.assertEqual(groups[0]["required_credits"], "2")
        self.assertEqual(groups[0]["earned_credits"], "2")
        self.assertEqual(len(groups[0]["courses"]), 2)
        self.assertEqual(groups[0]["courses"][0]["course_code"], "540001")
        self.assertEqual(groups[0]["courses"][0]["status"], "已修(优)")
        self.assertEqual(groups[0]["courses"][1]["course_name"], "大学生就业与创业指导")
        self.assertEqual(groups[0]["courses"][1]["term"], "5")
        self.assertEqual(groups[1]["group_name"], "专业课-专业核心课程模块")
        self.assertEqual(groups[1]["earned_credits"], "64")
        self.assertEqual(items[-1]["course_name"], "高等数学1")
        self.assertEqual(items[-1]["group_name"], "专业课-专业核心课程模块")
        self.assertEqual(len(items), 3)

    def test_parse_program_empty_without_mxh(self):
        meta, groups, items = parse_program("<div>培养方案及完成情况</div>")
        self.assertEqual(meta["objectives"], "")
        self.assertEqual(groups, [])
        self.assertEqual(items, [])

    def test_program_requires_login_page(self):
        client = JWXTClient()
        client.text = lambda *_args, **_kwargs: (
            200,
            "http://zhjw.qfnu.edu.cn/jsxsd/pyfa/topyfamx",
            "请输入账号 请输入密码 请输入验证码",
        )
        with self.assertRaises(Exception) as caught:
            program(client, "")
        self.assertEqual(caught.exception.message, "program page requires login")

    def test_cli_program_follows_iframe_filters(self):
        original_text = JWXTClient.text

        def fake_text(self, method, target, body=None, headers=None, same_origin=False):
            del self, method, body, headers, same_origin
            if target.endswith("/pyfa/topyfamx"):
                return 200, target, '<iframe src="/jsxsd/pyfa/inner.jsp"></iframe>'
            if target.endswith("/pyfa/inner.jsp"):
                return 200, target, PROGRAM_PAGE
            raise AssertionError("unexpected program url: " + target)

        JWXTClient.text = fake_text
        try:
            with tempfile.TemporaryDirectory() as temp:
                path = os.path.join(temp, "session.json")
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write('{"cookies":[]}\n')
                out = io.StringIO()
                code = run_jwxt(["program", "--keyword", "高等数学", "--session-path", path], out)
        finally:
            JWXTClient.text = original_text
        self.assertEqual(code, 0)
        body = json.loads(out.getvalue())
        self.assertTrue(body["ok"])
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["items"], body["program"])
        self.assertEqual(body["items"][0]["course_name"], "高等数学1")
        self.assertEqual(body["keyword"], "高等数学")
        self.assertEqual(body["groups"][0]["group_name"], "专业课-专业核心课程模块")
        self.assertEqual(body["objectives"], "培养德智体美劳全面发展的人才")

    def test_cli_pyfa_alias_returns_program(self):
        original_text = JWXTClient.text
        JWXTClient.text = lambda *_args, **_kwargs: (
            200,
            "http://zhjw.qfnu.edu.cn/jsxsd/pyfa/topyfamx",
            PROGRAM_PAGE,
        )
        try:
            with tempfile.TemporaryDirectory() as temp:
                path = os.path.join(temp, "session.json")
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write("{}\n")
                out = io.StringIO()
                code = run_jwxt(["pyfa", "--session-path", path], out)
        finally:
            JWXTClient.text = original_text
        self.assertEqual(code, 0)
        body = json.loads(out.getvalue())
        self.assertTrue(body["ok"])
        self.assertEqual(body["count"], 3)


def cookie_values(cookies, name):
    values = []
    for cookie in cookies:
        if cookie.name == name:
            values.append(cookie.value)
    return values


class RedirectTest(unittest.TestCase):
    def test_request_follows_same_origin_redirect(self):
        server = start_redirect_server()
        try:
            origin = "http://127.0.0.1:" + str(server.server_address[1])
            client = JWXTClient()
            status, final_url, body = client.request_with_origin(
                "POST",
                origin + "/start",
                b"payload",
                None,
                origin,
            )
        finally:
            stop_server(server)
        self.assertEqual(status, 200)
        self.assertEqual(final_url, origin + "/finish")
        self.assertEqual(body, b"authenticated")

    def test_request_stops_cross_origin_redirect(self):
        hits = {"count": 0}

        class DestHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                hits["count"] += 1
                payload = b"must not follow"
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args):
                return

        dest = HTTPServer(("127.0.0.1", 0), DestHandler)
        dest_thread = threading.Thread(target=dest.serve_forever, daemon=True)
        dest_thread.start()
        dest_url = "http://127.0.0.1:" + str(dest.server_address[1])

        class SourceHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", dest_url + "/finish")
                self.end_headers()
                body = ('<a href="' + dest_url + '/finish">Found</a>').encode("utf-8")
                self.wfile.write(body)

            def log_message(self, *_args):
                return

        source = HTTPServer(("127.0.0.1", 0), SourceHandler)
        source_thread = threading.Thread(target=source.serve_forever, daemon=True)
        source_thread.start()
        try:
            origin = "http://127.0.0.1:" + str(source.server_address[1])
            client = JWXTClient()
            status, final_url, body = client.request_with_origin(
                "GET",
                origin + "/start",
                None,
                None,
                origin,
            )
        finally:
            stop_server(source)
            stop_server(dest)
        self.assertEqual(status, 302)
        self.assertEqual(final_url, origin + "/start")
        self.assertEqual(hits["count"], 0)
        self.assertIn(dest_url, body.decode("utf-8", errors="replace"))


def start_redirect_server():
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or "0")
            self.rfile.read(length)
            if self.path == "/start":
                self.send_response(302)
                self.send_header("Location", "/finish")
                self.end_headers()
                return
            self.send_response(404)
            self.end_headers()

        def do_GET(self):
            if self.path == "/finish":
                payload = b"authenticated"
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, *_args):
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def stop_server(server):
    server.shutdown()
    server.server_close()


EXAM_QUERY_PAGE = """
<th>学年学期：</th>
<select id="xnxqid" name="xnxqid" style="width: 150px;">
\t<option selected value="2026-2027-1">2026-2027-1</option>
\t<option  value="2025-2026-3">2025-2026-3</option>
</select>
"""


EXAM_TABLE = """
<table id="dataList" width="100%" border="0" cellspacing="0" cellpadding="0" class="Nsb_r_list Nsb_table">
<tr>
<th class="Nsb_r_list_thb" style="width: 35px;">序号</th>
<th class="Nsb_r_list_thb" style="width: 80px;">校区</th>
<th class="Nsb_r_list_thb" style="width: 120px;">考试场次</th>
<th class="Nsb_r_list_thb" style="width: 120px;">课程编号</th>
<th class="Nsb_r_list_thb" style="width: 160px;">课程名称</th>
<th class="Nsb_r_list_thb" style="width: 110px;">授课教师</th>
<th class="Nsb_r_list_thb">考试时间</th>
<th class="Nsb_r_list_thb" style="width: 140px;">考场</th>
<th class="Nsb_r_list_thb" style="width: 60px;">座位号</th>
<th class="Nsb_r_list_thb" style="width: 120px;">准考证号</th>
<th class="Nsb_r_list_thb" style="width: 120px;">备注</th>
<th class="Nsb_r_list_thb" style="width: 50px;">操作</th>
</tr>
<tr><td>1</td><td>曲阜</td><td>期末考试</td><td>CS101</td><td>程序设计</td><td>张三</td><td>2026-07-06 08:00-10:00</td><td>综合楼101</td><td>12</td><td>2023000000</td><td></td><td></td></tr>
<tr><td>2</td><td>日照</td><td>期末考试</td><td>MA102</td><td>高等数学</td><td>李四</td><td>2026-07-08 14:00-16:00</td><td>教学楼202</td><td>7</td><td>2023000000</td><td><a href="#">查看</a></td><td></td></tr>
</table>
"""


EXAM_EMPTY_TABLE = """
<table id="dataList" width="100%" border="0" cellspacing="0" cellpadding="0" class="Nsb_r_list Nsb_table">
<tr>
<th class="Nsb_r_list_thb">序号</th>
<th class="Nsb_r_list_thb">校区</th>
<th class="Nsb_r_list_thb">课程名称</th>
<th class="Nsb_r_list_thb">考试时间</th>
<th class="Nsb_r_list_thb">操作</th>
</tr>
<tr><td colspan="10">未查询到数据</td></tr>
</table>
"""


class JWXTExamsTest(unittest.TestCase):
    def test_term_category_accepts_code_and_label(self):
        self.assertEqual(term_category("3"), ("3", "期末"))
        self.assertEqual(term_category("期末"), ("3", "期末"))
        self.assertEqual(term_category(""), ("", ""))
        self.assertEqual(term_category("未知"), ("未知", ""))

    def test_selected_semester_prefers_selected_option(self):
        self.assertEqual(selected_semester(EXAM_QUERY_PAGE), "2026-2027-1")
        self.assertEqual(selected_semester("<select id='xnxqid'><option value='2025-2026-3'>x</option></select>"), "2025-2026-3")
        self.assertEqual(selected_semester("<html></html>"), "")

    def test_parse_exams_maps_known_headers_and_skips_index_and_action(self):
        _rows, items, notice = parse_exams(EXAM_TABLE)
        self.assertEqual(notice, "")
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["course_id"], "CS101")
        self.assertEqual(items[0]["course_name"], "程序设计")
        self.assertEqual(items[0]["campus"], "曲阜")
        self.assertEqual(items[0]["session"], "期末考试")
        self.assertEqual(items[0]["exam_time"], "2026-07-06 08:00-10:00")
        self.assertEqual(items[0]["room"], "综合楼101")
        self.assertEqual(items[0]["seat_no"], "12")
        self.assertEqual(items[0]["admission_no"], "2023000000")
        self.assertNotIn("序号", items[0])
        self.assertNotIn("操作", items[0])
        self.assertNotIn("remark", items[0])
        self.assertEqual(items[1]["remark"], "查看")

    def test_parse_exams_reports_no_data_notice(self):
        _rows, items, notice = parse_exams(EXAM_EMPTY_TABLE)
        self.assertEqual(items, [])
        self.assertEqual(notice, "未查询到数据")

    def test_parse_exams_ignores_tables_other_than_data_list(self):
        raw = "<table><tr><td>无关</td></tr></table>" + EXAM_TABLE
        _rows, items, _notice = parse_exams(raw)
        self.assertEqual(len(items), 2)

    def test_exams_requires_login_page(self):
        client = JWXTClient()
        client.text = lambda *_args, **_kwargs: (
            200,
            "http://zhjw.qfnu.edu.cn/jsxsd/xsks/xsksap_query",
            "请输入账号 请输入密码 请输入验证码",
        )
        with self.assertRaises(Exception) as caught:
            exams(client, "2026-2027-1", "")
        self.assertEqual(caught.exception.message, "exam query page requires login")

    def test_exams_posts_form_and_returns_items(self):
        calls = []
        client = JWXTClient()

        def fake_text(method, target, body=None, headers=None, same_origin=False):
            del same_origin
            calls.append((method, target, body, headers))
            if method == "GET":
                return 200, target, EXAM_QUERY_PAGE
            return 200, target, EXAM_TABLE

        client.text = fake_text
        result = exams(client, "2026-2027-1", "期末")
        self.assertTrue(result["ok"])
        self.assertEqual(result["semester"], "2026-2027-1")
        self.assertEqual(result["term_category"], "3")
        self.assertEqual(result["term_category_name"], "期末")
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["items"], result["exams"])
        self.assertNotIn("message", result)
        method, target, body, headers = calls[1]
        self.assertEqual(method, "POST")
        self.assertTrue(target.endswith("/jsxsd/xsks/xsksap_list"))
        posted = body.decode("utf-8")
        self.assertIn("xnxqid=2026-2027-1", posted)
        self.assertIn("xqlb=3", posted)
        self.assertIn("xqlbmc=%E6%9C%9F%E6%9C%AB", posted)
        self.assertIn("application/x-www-form-urlencoded", headers["Content-Type"])
        self.assertIn("/xsks/xsksap_query", headers["Referer"])

    def test_exams_uses_query_page_semester_when_missing(self):
        client = JWXTClient()

        def fake_text(method, target, body=None, headers=None, same_origin=False):
            del body, headers, same_origin
            if method == "GET":
                return 200, target, EXAM_QUERY_PAGE
            return 200, target, EXAM_TABLE

        client.text = fake_text
        result = exams(client, "", "")
        self.assertEqual(result["semester"], "2026-2027-1")

    def test_exams_no_data_returns_message(self):
        client = JWXTClient()

        def fake_text(method, target, body=None, headers=None, same_origin=False):
            del body, headers, same_origin
            if method == "GET":
                return 200, target, EXAM_QUERY_PAGE
            return 200, target, EXAM_EMPTY_TABLE

        client.text = fake_text
        result = exams(client, "2026-2027-1", "")
        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["items"], [])
        self.assertEqual(result["message"], "未查到数据")

    def test_exams_unrecognised_page_raises_instead_of_no_data(self):
        client = JWXTClient()

        def fake_text(method, target, body=None, headers=None, same_origin=False):
            del body, headers, same_origin
            if method == "GET":
                return 200, target, EXAM_QUERY_PAGE
            return 200, target, "<html><body>系统维护中</body></html>"

        client.text = fake_text
        with self.assertRaises(Exception) as caught:
            exams(client, "2026-2027-1", "")
        self.assertEqual(caught.exception.message, "exam list page is not recognised")

    def test_cli_exams_returns_items(self):
        original_text = JWXTClient.text

        def fake_text(self, method, target, body=None, headers=None, same_origin=False):
            del self, body, headers, same_origin
            if method == "GET":
                return 200, target, EXAM_QUERY_PAGE
            return 200, target, EXAM_TABLE

        JWXTClient.text = fake_text
        try:
            with tempfile.TemporaryDirectory() as temp:
                path = os.path.join(temp, "session.json")
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write('{"cookies":[]}\n')
                out = io.StringIO()
                code = run_jwxt(
                    ["exams", "--semester", "2026-2027-1", "--term-category", "期末", "--session-path", path],
                    out,
                )
        finally:
            JWXTClient.text = original_text
        self.assertEqual(code, 0)
        body = json.loads(out.getvalue())
        self.assertTrue(body["ok"])
        self.assertEqual(body["count"], 2)
        self.assertEqual(body["items"][1]["course_name"], "高等数学")

    def test_cli_exams_no_data_returns_message(self):
        original_text = JWXTClient.text

        def fake_text(self, method, target, body=None, headers=None, same_origin=False):
            del self, body, headers, same_origin
            if method == "GET":
                return 200, target, EXAM_QUERY_PAGE
            return 200, target, EXAM_EMPTY_TABLE

        JWXTClient.text = fake_text
        try:
            with tempfile.TemporaryDirectory() as temp:
                path = os.path.join(temp, "session.json")
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write("{}\n")
                out = io.StringIO()
                code = run_jwxt(["exams", "--session-path", path], out)
        finally:
            JWXTClient.text = original_text
        self.assertEqual(code, 0)
        body = json.loads(out.getvalue())
        self.assertTrue(body["ok"])
        self.assertEqual(body["count"], 0)
        self.assertEqual(body["message"], "未查到数据")

    def test_cli_exams_login_failure(self):
        original_text = JWXTClient.text
        JWXTClient.text = lambda *_args, **_kwargs: (
            200,
            "http://zhjw.qfnu.edu.cn/jsxsd/xsks/xsksap_query",
            "请输入账号请输入密码请输入验证码",
        )
        try:
            with tempfile.TemporaryDirectory() as temp:
                path = os.path.join(temp, "session.json")
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write("{}\n")
                out = io.StringIO()
                code = run_jwxt(["exams", "--session-path", path], out)
        finally:
            JWXTClient.text = original_text
        self.assertEqual(code, 0)
        body = json.loads(out.getvalue())
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"], "exam query page requires login")


class JWXTRelayOfflineTest(unittest.TestCase):
    def test_relay_points_users_to_qq_group(self):
        out = io.StringIO()
        code = run_jwxt(["relay", "recommendation"], out)
        self.assertEqual(code, 0)
        body = json.loads(out.getvalue())
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"], "反馈与推荐提交暂时不可用")
        self.assertIn("1087015770", body["hint"])



class LoginRedactionTest(unittest.TestCase):
    """登录失败时上游回显的明文口令不能进 JSON（真实教务系统确实回显）。"""

    ECHO_PAGE = (
        '<li class="input_li" id="showMsg" style="color: red;">&nbsp;验证码错误!!</li>'
        '<input type="text" class="form-control" id="userAccount" name="userAccount" '
        'placeholder="请输入账号" value="0000000000">'
        '<input type="password" class="form-control" id="userPassword" name="userPassword" '
        'placeholder="请输入密码" value="fake-hunter2">'
    )

    def test_credential_request_detection(self):
        self.assertTrue(
            is_credential_request("http://zhjw.qfnu.edu.cn/Logon.do?method=logonLdap")
        )
        self.assertTrue(
            is_credential_request("http://127.0.0.1:1/Logon.do?method=logon&flag=sess")
        )
        self.assertFalse(
            is_credential_request("http://zhjw.qfnu.edu.cn/jsxsd/framework/xsMain.jsp")
        )
        self.assertFalse(
            is_credential_request("http://zhjw.qfnu.edu.cn/jsxsd/kscj/cjcx_list?gnmkdm=N305005")
        )

    def test_login_failure_does_not_export_password(self):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or "0")
                self.rfile.read(length)
                if self.path.startswith("/Logon.do?method=logon&flag=sess"):
                    payload = b"A1B2C3#31201"
                else:
                    payload = LoginRedactionTest.ECHO_PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args):
                return

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:" + str(server.server_address[1])
        original = (jwxt_auth.SESS_URL, jwxt_auth.LOGIN_URL, jwxt_client.JWXT_BASE)
        try:
            jwxt_auth.SESS_URL = base + "/Logon.do?method=logon&flag=sess"
            jwxt_auth.LOGIN_URL = base + "/Logon.do?method=logonLdap"
            jwxt_client.JWXT_BASE = base
            trace.reset(False)
            client = JWXTClient()
            client.jar.set_cookie(make_cookie("JSESSIONID", "stub", "/", "127.0.0.1", False, None))
            body = jwxt_auth.submit_login(client, "0000000000", "fake-hunter2", "0000")
            with self.assertRaises(JWXTError) as caught:
                jwxt_auth.validate_login_response(body)
            out = io.StringIO()
            write_json(out, failure("jwxt", caught.exception.message, caught.exception.hint))
            payload = out.getvalue()
        finally:
            server.shutdown()
            server.server_close()
            trace.reset(False)
            jwxt_auth.SESS_URL, jwxt_auth.LOGIN_URL, jwxt_client.JWXT_BASE = original

        self.assertIn("fake-hunter2", body)
        self.assertNotIn("fake-hunter2", payload)
        self.assertIn("[redacted credentials response", payload)
        self.assertIn("验证码错误!!", payload)
        self.assertIn("logonLdap", payload)


class JWXTStatusTest(unittest.TestCase):
    """未登录/会话过期必须是 ok:false，并给出 logged_in 与 session_expired。"""

    LOGIN_PAGE = (
        '<li class="input_li" id="showMsg" style="color: red;">&nbsp;请先登录系统</li>'
        '<input type="text" class="form-control" id="userAccount" name="userAccount" '
        'placeholder="请输入账号">'
        '<input type="password" class="form-control" id="userPassword" name="userPassword" '
        'placeholder="请输入密码">'
        '<input type="text" class="form-control" id="RANDOMCODE" name="RANDOMCODE" '
        'placeholder="请输入验证码">'
    )

    def test_status_without_session_is_not_logged_in(self):
        with tempfile.TemporaryDirectory() as temp:
            path = os.path.join(temp, "session.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('{"cookies": []}\n')
            out = io.StringIO()
            code = run_jwxt(["status", "--session-path", path], out)
        self.assertEqual(code, 0)
        body = json.loads(out.getvalue())
        self.assertFalse(body["ok"])
        self.assertFalse(body["logged_in"])
        self.assertFalse(body["session_expired"])
        self.assertIn("jwxt login", body["hint"])
        self.assertEqual(body["session_path"], path)

    def test_status_expired_session_reports_expired(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                payload = JWXTStatusTest.LOGIN_PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args):
                return

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:" + str(server.server_address[1])
        original = (jwxt_auth.MAIN_URL, jwxt_client.JWXT_BASE)
        try:
            jwxt_auth.MAIN_URL = base + "/jsxsd/framework/xsMain.jsp"
            jwxt_client.JWXT_BASE = base
            trace.reset(False)
            with tempfile.TemporaryDirectory() as temp:
                path = os.path.join(temp, "session.json")
                with open(path, "w", encoding="utf-8") as handle:
                    json.dump(
                        {
                            "username": "2023413695",
                            "cookies": [
                                {
                                    "Name": "JSESSIONID",
                                    "Value": "stale",
                                    "Path": "/",
                                    "Domain": "127.0.0.1",
                                    "Expires": "0001-01-01T00:00:00Z",
                                    "Secure": False,
                                }
                            ],
                        },
                        handle,
                    )
                out = io.StringIO()
                code = run_jwxt(["status", "--session-path", path], out)
            body = json.loads(out.getvalue())
        finally:
            server.shutdown()
            server.server_close()
            trace.reset(False)
            jwxt_auth.MAIN_URL, jwxt_client.JWXT_BASE = original
        self.assertEqual(code, 0)
        self.assertFalse(body["ok"])
        self.assertFalse(body["logged_in"])
        self.assertTrue(body["session_expired"])
        self.assertEqual(body["error"], "jwxt session expired")
        self.assertEqual(body["upstream"]["status"], 200)


class JWXTDispatchTest(unittest.TestCase):
    """命令表化以后：未知动作给提示，别名走同一张表。"""

    def test_unknown_action_hints_help(self):
        out = io.StringIO()
        code = run_jwxt(["not-an-action"], out)
        self.assertEqual(code, 0)
        body = json.loads(out.getvalue())
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"], "unknown action: not-an-action")
        self.assertIn("jwxt --help", body["hint"])

    def test_alias_action_uses_same_table_entry(self):
        with tempfile.TemporaryDirectory() as temp:
            path = os.path.join(temp, "session.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('{"cookies": []}\n')
            out = io.StringIO()
            run_jwxt(["whoami", "--session-path", path], out)
        body = json.loads(out.getvalue())
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"], "not logged in")

    def test_forget_credentials_removes_saved_file(self):
        original = os.environ.get("QFNU_JWXT_CREDENTIALS_PATH")
        with tempfile.TemporaryDirectory() as temp:
            path = os.path.join(temp, "credentials.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('{"username": "2023413695", "password": "placeholder"}\n')
            os.environ["QFNU_JWXT_CREDENTIALS_PATH"] = path
            try:
                out = io.StringIO()
                code = run_jwxt(["forget-credentials"], out)
            finally:
                if original is None:
                    os.environ.pop("QFNU_JWXT_CREDENTIALS_PATH", None)
                else:
                    os.environ["QFNU_JWXT_CREDENTIALS_PATH"] = original
            body = json.loads(out.getvalue())
            self.assertEqual(code, 0)
            self.assertTrue(body["credentials_removed"])
            self.assertFalse(os.path.exists(path))

# 主页周次片段：结构与真实页面一致（容器里套着 span，总周数在容器内、不在子标签里），
# 周次、学期代码与日期全部虚构；容器外故意放上别的周次文字，用来断言取值只认容器。
WEEK_PAGE = """
<html><body>
<script>var xxdm = 10446; var zc = '3'; var rq = '2026-10-08';</script>
<div class="middletopleftdqrqbox" style="height: 12%;">
  <div class=" pr5 middletopleftzc" id="li_showWeek">
    <span class="main_text main_color">第3周</span>/16周
  </div>
  <div class="middletopleftdqrq">
    <input type="text" id="rq" value="2026-10-08" readonly class="form-control">
  </div>
</div>
<div class="middletopleftzc">第9周</div>
<div class="main_text main_color">第21周/30周</div>
</body></html>
"""

# 只有通用 class 没有 li_showWeek 的片段：拿到的页面给不出教学周。
WEEK_PAGE_WITHOUT_MARKER = (
    '<html><body><div class="middletopleftdqrqbox">'
    '<div class="middletopleftzc">第3周</div>/16周</div></body></html>'
)

# 登录页片段：会话过期后主页返回的就是它。
WEEK_LOGIN_PAGE = "<html><body>请输入账号 请输入密码 请输入验证码</body></html>"


class JWXTWeekParseTest(unittest.TestCase):
    """任务：从教务主页解析当前教学周与总周数，只吃 HTML、零网络。"""

    def test_week_box_gives_current_and_total(self):
        """真实结构的片段给出第 3 周与总 16 周。"""
        self.assertEqual(parse_teaching_week(WEEK_PAGE), {"current": 3, "total": 16})

    def test_single_quoted_id_and_reordered_attributes_are_accepted(self):
        """单引号写法与属性顺序变化都要认：id 排在 class 前面同样能定位容器。"""
        raw = WEEK_PAGE.replace(
            '<div class=" pr5 middletopleftzc" id="li_showWeek">',
            "<div id='li_showWeek' class=' pr5 middletopleftzc'>",
        )
        self.assertNotEqual(raw, WEEK_PAGE)
        self.assertEqual(parse_teaching_week(raw), {"current": 3, "total": 16})

    def test_page_without_the_box_gives_nothing(self):
        """没有 li_showWeek 容器时取不出周次，不拿别的容器或正文数字顶替。"""
        self.assertIsNone(parse_teaching_week(WEEK_PAGE_WITHOUT_MARKER))

    def test_other_numbers_on_the_page_are_ignored(self):
        """学期代码 10446、日期与容器外的第 9 周、第 21 周都不参与取值。"""
        week = parse_teaching_week(WEEK_PAGE)
        self.assertNotEqual(week["current"], 9)
        self.assertNotEqual(week["current"], 21)
        self.assertNotEqual(week["current"], 10446)
        self.assertEqual(week["total"], 16)

    def test_zero_week_is_not_accepted(self):
        """第 0 周不是真实教学周，按探测失败处理。"""
        self.assertIsNone(parse_teaching_week(WEEK_PAGE.replace("第3周", "第0周")))

    def test_week_above_thirty_is_not_accepted(self):
        """超过 30 周的当前周次与总周数都按探测失败处理。"""
        self.assertIsNone(parse_teaching_week(WEEK_PAGE.replace("第3周", "第31周")))
        self.assertIsNone(parse_teaching_week(WEEK_PAGE.replace("/16周", "/31周")))

    def test_non_numeric_week_is_not_accepted(self):
        """周次位置上不是数字时取不出周次。"""
        self.assertIsNone(parse_teaching_week(WEEK_PAGE.replace("第3周", "第A周")))

    def test_missing_total_is_not_accepted(self):
        """缺总周数时两个值不齐，按探测失败处理。"""
        self.assertIsNone(parse_teaching_week(WEEK_PAGE.replace("/16周", "")))

    def test_box_without_closing_tag_is_not_accepted(self):
        """容器没有闭合标签时范围不确定，按取不出周次处理。"""
        self.assertIsNone(parse_teaching_week('<div id="li_showWeek"><span>第3周</span>/16周'))


class FakeTextClient:
    """假客户端：只实现 text，按队列给响应并记录每次请求。"""

    def __init__(self, responses):
        # 逐次调用要返回的 (状态码, 正文)，也可以是表示传输失败的异常。
        self.responses = list(responses)
        # 每次请求的方法与地址，供用例断言探了哪一页、探了几次。
        self.calls = []

    def text(self, method, target, body=None, headers=None, same_origin=False):
        """按 JWXTClient.text 的返回形状给出 (状态码, 最终地址, 正文)。"""
        del body, headers, same_origin
        self.calls.append((method, target))
        # 队列里没有响应说明代码多发了一次请求，直接报错而不是继续编造正文。
        if not self.responses:
            raise AssertionError("unexpected extra request: " + method + " " + target)
        item = self.responses.pop(0)
        # 队列项是异常时按传输失败抛出，用来模拟网络错误。
        if isinstance(item, Exception):
            raise item
        code, raw = item
        return int(code), target, raw


class JWXTWeekProbeTest(unittest.TestCase):
    """任务：省略 --week 时探测当前教学周，探测失败要给出手动传 --week 的提示。"""

    def test_given_week_is_used_without_any_request(self):
        """显式传了周次就直接用它，这一步一个请求都不发。"""
        client = FakeTextClient([])
        self.assertEqual(resolve_week(client, "6"), ("6", None))
        self.assertEqual(client.calls, [])

    def test_empty_week_probes_the_profile_page(self):
        """空周次时读主页探测，并把当前周次交给查询。"""
        client = FakeTextClient([("200", WEEK_PAGE)])
        self.assertEqual(resolve_week(client, ""), ("3", None))
        self.assertEqual(client.calls, [("GET", PROFILE_URL)])

    def test_probe_without_marker_asks_for_a_manual_week(self):
        """页面拿到了但没有周次标记时报无法自动获取，并提示手动传 --week。"""
        client = FakeTextClient([("200", WEEK_PAGE_WITHOUT_MARKER)])
        info, error = fetch_teaching_week(client)
        self.assertIsNone(info)
        self.assertFalse(error["ok"])
        self.assertIn("无法自动获取当前教学周", error["error"])
        self.assertEqual(error["hint"], WEEK_MANUAL_HINT)
        self.assertIn("--week", error["hint"])

    def test_probe_on_login_page_reports_the_expired_session(self):
        """主页返回登录页说明会话已过期，提示与 status 的过期口径是同一句。"""
        client = FakeTextClient([("200", WEEK_LOGIN_PAGE)])
        info, error = fetch_teaching_week(client)
        self.assertIsNone(info)
        self.assertIn("会话已过期", error["error"])
        self.assertEqual(error["hint"], SESSION_EXPIRED_HINT)

    def test_probe_transport_and_http_failures_ask_for_a_manual_week(self):
        """非 200 与网络失败都拿不到页面，同样提示手动传 --week。"""
        for response in (("500", ""), OSError("probe boom")):
            with self.subTest(response=response):
                client = FakeTextClient([response])
                info, error = fetch_teaching_week(client)
                self.assertIsNone(info)
                self.assertEqual(error["hint"], WEEK_MANUAL_HINT)

    def test_resolve_week_hands_the_probe_failure_back(self):
        """探测失败时 resolve_week 把失败原样交出，周次返回空串。"""
        client = FakeTextClient([("200", WEEK_PAGE_WITHOUT_MARKER)])
        week, error = resolve_week(client, "")
        self.assertEqual(week, "")
        self.assertFalse(error["ok"])


class JWXTStatusWeekTest(unittest.TestCase):
    """任务：status 带上探测到的周次与总周数；探测不到时既有语义与字段都不变。"""

    MAIN_PAGE = "教学一体化服务平台"

    def status_client(self, temp, profile_response, calls):
        """造一个只走假响应的会话：主页给成功标记，资料页按传入的响应交正文。"""
        client = JWXTClient(os.path.join(temp, "session.json"))
        client.meta["username"] = "student"
        client.jar.set_cookie(
            make_cookie("JSESSIONID", "active", "/", "zhjw.qfnu.edu.cn", False, None)
        )

        def fake_request(method, target, body=None, headers=None, same_origin=False):
            """按目标地址给响应：资料页那一条用调用方传入的，其余给主页成功标记。"""
            del method, body, headers, same_origin
            calls.append(target)
            # 资料页这一条交给调用方决定：有标记、没有标记或传输失败都能覆盖。
            if target == PROFILE_URL:
                return profile_response(target)
            return 200, target, self.MAIN_PAGE.encode()

        client.request = fake_request
        return client

    def test_status_adds_the_week_from_the_profile_page(self):
        """资料页有周次标记时 status 带上 week，且资料页只请求一次。"""
        with tempfile.TemporaryDirectory() as temp:
            calls = []
            client = self.status_client(
                temp, lambda target: (200, target, WEEK_PAGE.encode()), calls
            )
            result = jwxt_status(client)
        self.assertTrue(result["ok"])
        self.assertTrue(result["logged_in"])
        self.assertFalse(result["session_expired"])
        self.assertEqual(result["week"], {"current": 3, "total": 16})
        # 周次复用资料补全那次响应，不为周次再发一次请求。
        self.assertEqual(calls.count(PROFILE_URL), 1)

    def test_status_stays_successful_without_the_week_marker(self):
        """探测不到周次时仍是 ok:true，没有 week 字段，既有字段一个都没变。"""
        with tempfile.TemporaryDirectory() as temp:
            calls = []
            client = self.status_client(
                temp,
                lambda target: (200, target, WEEK_PAGE_WITHOUT_MARKER.encode()),
                calls,
            )
            result = jwxt_status(client)
        self.assertTrue(result["ok"])
        self.assertTrue(result["logged_in"])
        self.assertFalse(result["session_expired"])
        self.assertNotIn("week", result)
        self.assertIn("profile", result)
        self.assertIn("session_path", result)
        self.assertEqual(calls.count(PROFILE_URL), 1)

    def test_status_without_the_profile_page_has_no_week(self):
        """资料页拿不到时 status 依旧成功，只带资料补全警告、不带 week。"""

        def failing_profile_page(target):
            """资料页请求直接失败，模拟网络错误。"""
            del target
            raise OSError("profile boom")

        with tempfile.TemporaryDirectory() as temp:
            calls = []
            client = self.status_client(temp, failing_profile_page, calls)
            result = jwxt_status(client)
        self.assertTrue(result["ok"])
        self.assertNotIn("week", result)
        self.assertIn("资料补全", result.get("profile_warning", ""))

if __name__ == "__main__":
    unittest.main()


# ===== QFNU 强智课表：隐藏层（教师/精确节次）与同格多课拆分 =====

QFNU_CELL_TABLE = """
<table id="kbtable">
<tr><th>节次</th><th>星期一</th><th>星期二</th></tr>
<tr>
  <td>第3~5节&nbsp;</td>
  <td>
    <div id="AA-1-1" class="kbcontent1">C语言程序设计<br/><font title='周次(节次)'>1-18(周)</font><br/><font title='教室'>数学楼506、508</font><br/>----------------------<br/>C语言程序设计<br/><font title='周次(节次)'>1-16(周)</font><br/><font title='教室'>数学楼101</font><br/></div>
    <div id="AA-1-2" style="display: none;" class="kbcontent">C语言程序设计<br/><font title='老师'>陈仁栋</font><br/><font title='周次(节次)'>1-18(周)[05节]</font><br/><font title='教室'>数学楼506、508</font><br/>---------------------<br/>C语言程序设计<br/><font title='老师'>陈仁栋</font><br/><font title='周次(节次)'>1-16(周)[03-04节]</font><br/><font title='教室'>数学楼101</font><br/></div>
  </td>
  <td>&nbsp;</td>
</tr>
</table>
"""


class QFNUScheduleDetailTest(unittest.TestCase):
    """一个格子里两门课必须拆成两条，且各自的教师/节次/教室不能串。"""

    def test_splits_multi_course_cell(self):
        _rows, items = parse_schedule(QFNU_CELL_TABLE)
        self.assertEqual(len(items), 2)
        first, second = items
        self.assertEqual(first["course_name"], "C语言程序设计")
        self.assertEqual(second["course_name"], "C语言程序设计")
        # 第一条是 1-18 周、第 5 节、506/508
        self.assertEqual(first["weeks"], list(range(1, 19)))
        self.assertEqual(first["sections"], [5])
        self.assertEqual(first["room"], "数学楼506、508")
        # 第二条是 1-16 周、3-4 节、101
        self.assertEqual(second["weeks"], list(range(1, 17)))
        self.assertEqual(second["sections"], [3, 4])
        self.assertEqual(second["room"], "数学楼101")
        # 教师来自隐藏层
        self.assertEqual(first["teacher"], "陈仁栋")
        self.assertEqual(second["teacher"], "陈仁栋")

    def test_period_has_no_html_entity(self):
        _rows, items = parse_schedule(QFNU_CELL_TABLE)
        for item in items:
            self.assertNotIn("&nbsp;", item["period"])
            self.assertNotIn("\xa0", item["period"])
        self.assertEqual(items[0]["period"], "第3~5节")

    def test_weeks_are_integers(self):
        _rows, items = parse_schedule(QFNU_CELL_TABLE)
        for item in items:
            self.assertTrue(all(isinstance(week, int) for week in item["weeks"]))
