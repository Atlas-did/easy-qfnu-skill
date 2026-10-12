"""教务验证码、登录与本地凭据。"""

import html
import json
import os
import re
from urllib.parse import urlencode

from . import trace
from .jwxt_client import (
    CAPTCHA_URL,
    JWXT_BASE,
    LOGIN_URL,
    MAIN_URL,
    PROFILE_URL,
    SESS_URL,
    JWXTError,
    default_captcha_path,
    default_credentials_path,
    write_private_file,
)
from .result import failure, success

SHOW_MSG_RE = re.compile(
    r'<([a-z][a-z0-9]*)\b[^>]*\bid\s*=\s*["\']showMsg["\'][^>]*>',
    re.IGNORECASE | re.DOTALL,
)
SCRIPT_RE = re.compile(r"<script\b[^>]*>.*?</script\s*>", re.IGNORECASE | re.DOTALL)
STYLE_RE = re.compile(r"<style\b[^>]*>.*?</style\s*>", re.IGNORECASE | re.DOTALL)
BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>", re.IGNORECASE | re.DOTALL)
PROFILE_LABELS = {
    "学生姓名": "name",
    "姓名": "name",
    "学生编号": "student_id",
    "学号": "student_id",
    "所属院系": "college",
    "专业名称": "major",
    "班级名称": "class_name",
}

# 周次容器的开标签：按 id 锚定，页面上「第N周」这类文本与 main_color 这类通用 class 都不能当锚点。
WEEK_BOX_RE = re.compile(
    r'<([a-z][a-z0-9]*)\b[^>]*\bid\s*=\s*["\']li_showWeek["\'][^>]*>',
    re.IGNORECASE | re.DOTALL,
)
# 容器内的当前周次与总周数：形如「第3周」与「/16周」。
CURRENT_WEEK_RE = re.compile(r"第\s*(\d+)\s*周")
TOTAL_WEEK_RE = re.compile(r"/\s*(\d+)\s*周")
# 教学周的合法闭区间，与 jwxt_classroom 的周次校验同口径；越界值一律按探测失败处理。
WEEK_BOUNDS = (1, 30)
# 会话过期时的提示：只能重新取验证码登录，探测周次与 status 共用这句话。
SESSION_EXPIRED_HINT = "会话已过期；运行 easy-qfnu jwxt captcha 取新图片，读出验证码后用 easy-qfnu jwxt login --captcha 提交"
# 主页给不出周次时的提示：用户自己知道周次，只能手动传给查询。
WEEK_MANUAL_HINT = "请手动传 --week 指定周次，例如 --week 3"
# 正文出现这些字样说明拿到的是登录页；本模块内会话判定与周次探测共用这一份。
# 不直接引用 jwxt_html.LOGIN_MARKERS：那个模块反过来要导入本模块的 strip_tags，会形成循环导入。
LOGIN_BODY_MARKERS = ["请输入账号", "请输入密码", "请输入验证码"]


def encode_credentials(username, password, scode, sxh):
    raw = username + "%%%" + password
    chars = list(raw)
    out = []
    scode_pos = 0
    for index, char in enumerate(chars):
        if index >= 20:
            out.append("".join(chars[index:]))
            break
        out.append(char)
        if index >= len(sxh):
            continue
        count = ord(sxh[index]) - ord("0")
        if count <= 0 or scode_pos >= len(scode):
            continue
        end = min(scode_pos + count, len(scode))
        out.append(scode[scode_pos:end])
        scode_pos = end
    return "".join(out)


def contains_any(text, markers):
    for marker in markers:
        if marker in text:
            return True
    return False


def strip_tags(raw):
    raw = SCRIPT_RE.sub(" ", raw)
    raw = STYLE_RE.sub(" ", raw)
    raw = BR_RE.sub(" ", raw)
    raw = TAG_RE.sub(" ", raw)
    return " ".join(raw.split())


def parse_login_message(raw):
    match = SHOW_MSG_RE.search(raw)
    if match is None:
        return ""
    tag = match.group(1)
    end_re = re.compile(r"</\s*" + re.escape(tag) + r"\s*>", re.IGNORECASE | re.DOTALL)
    end = end_re.search(raw, match.end())
    if end is None:
        return ""
    message = html.unescape(strip_tags(raw[match.end() : end.start()]))
    return " ".join(message.split())


def login_failure_hint(message):
    if contains_any(message, ["验证码错误", "验证码不正确"]):
        return "重新运行 easy-qfnu jwxt captcha 获取新验证码"
    if contains_any(message, ["密码错误", "用户名或密码错误", "用户名密码错误", "用户名或者密码有误"]):
        return "核对学号和学校服务大厅密码，不要重复提交错误密码"
    if contains_any(message, ["其他地方登录", "别处登录", "异地登录"]):
        return "账号已在其他地方登录，请先退出已有会话后再重试"
    return "请根据教务系统返回的错误核对登录信息后重试"


def validate_login_response(raw):
    message = parse_login_message(raw)
    if message:
        trace.note("login showMsg")
        raise JWXTError(message, login_failure_hint(message))
    if contains_any(raw, ["密码错误", "用户名或密码错误", "用户名密码错误", "您提供的用户名或者密码有误"]):
        raise JWXTError(
            "username or password is wrong",
            "核对学号和学校服务大厅密码，不要重复提交错误密码",
        )
    if contains_any(raw, ["验证码错误", "验证码不正确"]):
        raise JWXTError("captcha rejected by 教务系统", "重新运行 easy-qfnu jwxt captcha 获取新验证码")


def parse_profile(raw):
    profile = {}
    plain = strip_tags(raw)
    for label, key in PROFILE_LABELS.items():
        index = plain.find(label)
        if index < 0:
            continue
        value = plain[index + len(label) :].strip().lstrip(" :：\t")
        fields = value.split()
        if fields:
            profile[key] = fields[0]
    return profile


def merge_profile(dst, src):
    for key, value in src.items():
        if isinstance(value, str) and value != "":
            dst[key] = value


def week_box_end(raw, start, tag):
    """取周次容器闭合标签的起点；没有闭合标签时返回 -1。

    容器里还套着 span 这类子标签，必须按同名标签闭合定位，取第一个闭合标签会切掉总周数。
    """
    end_re = re.compile(r"</\s*" + re.escape(tag) + r"\s*>", re.IGNORECASE | re.DOTALL)
    match = end_re.search(raw, start)
    # 没有闭合标签时容器范围到不了头，调用方按取不出周次处理。
    if match is None:
        return -1
    return match.start()


def week_number(pattern, text):
    """按模式取一个周次数字；没匹配到或不在 1 到 30 之间时返回 None。"""
    match = pattern.search(text)
    # 这一项没有数字就取不出周次。
    if match is None:
        return None
    number = int(match.group(1))
    # 0 周或超过 30 周都不是真实教学周，按取不出处理，免得把坏值塞给查询。
    if number < WEEK_BOUNDS[0] or number > WEEK_BOUNDS[1]:
        return None
    return number


def parse_teaching_week(raw):
    """解析主页的当前教学周与总周数，返回 {"current": 当前周, "total": 总周数}；取不出时返回 None。

    只认锚定 id 为 li_showWeek 的容器：同一页上还有学期代码、日期与周次脚本变量这类数字，
    宽松取数会把它们当成周次。容器内取「第N周」与「/N周」两个数字，两者都要落在 1 到 30
    之间，任何一个缺失或越界都按取不出处理。
    """
    match = WEEK_BOX_RE.search(raw)
    # 没有周次容器说明这一页给不出教学周，不猜页面上其他位置的数字。
    if match is None:
        return None
    end = week_box_end(raw, match.end(), match.group(1))
    # 容器没有闭合标签时范围不确定，按取不出处理。
    if end < 0:
        return None
    text = strip_tags(raw[match.end():end])
    current = week_number(CURRENT_WEEK_RE, text)
    total = week_number(TOTAL_WEEK_RE, text)
    # 当前周次或总周数缺一个都算探测失败，调用方按「无法自动获取当前教学周」处理。
    if current is None or total is None:
        return None
    return {"current": current, "total": total}


def fetch_captcha(client):
    status, _, data = client.request("GET", CAPTCHA_URL)
    if status != 200 or not data:
        raise OSError("captcha image HTTP " + str(status))
    return data


def init_session(client):
    status, _, _ = client.request("GET", JWXT_BASE + "/")
    if status >= 400:
        raise OSError("session init HTTP " + str(status))


# 上游 verifycode.servlet 返回的是 JPEG，但默认输出名历史上用的是 .png。
# 按真实字节纠正后缀，避免下游"扩展名与内容不符"打不开。
IMAGE_MAGIC = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"RIFF", ".webp"),
)


def detect_image_extension(data):
    """按魔数返回图片扩展名；识别不出返回空字符串。"""
    for magic, suffix in IMAGE_MAGIC:
        if data.startswith(magic):
            if suffix == ".webp" and data[8:12] != b"WEBP":
                continue
            return suffix
    return ""


def align_image_extension(path, data):
    """把路径后缀改成与真实图片格式一致，返回最终路径。

    只纠正"内容与后缀不符"的情况；用户显式给了正确后缀时保持原样。
    """
    actual = detect_image_extension(data)
    if not actual:
        return path
    current = os.path.splitext(path)[1].lower()
    if current == actual:
        return path
    # .jpeg 与 .jpg 视为同一种
    if actual == ".jpg" and current in (".jpg", ".jpeg"):
        return path
    aligned = os.path.splitext(path)[0] + actual
    return aligned


def captcha(client, output):
    client.reset_jar()
    init_session(client)
    image = fetch_captcha(client)
    path = os.path.abspath(output)
    path = align_image_extension(path, image)
    write_private_file(path, image, "wb")
    client.persist({"captcha_pending": True})
    return success(
        "jwxt",
        {
            "captcha_image_path": path,
            "image_extension": detect_image_extension(image),
            "session_path": client.session_path,
            "next": "easy-qfnu jwxt login --username <学号> --password <密码> --captcha <识图结果>",
            "hint": "请用模型或用户读取验证码；验证码错误时重新运行 jwxt captcha",
        },
    )


def prepare_login_captcha(captcha_text):
    """验证码只接受模型识图或用户人工识别的结果，不再调用外部 OCR 服务。"""
    if captcha_text:
        return captcha_text
    raise JWXTError(
        "captcha is required",
        "先运行 easy-qfnu jwxt captcha 获取验证码图片，读出后用 jwxt login --captcha <识图结果> 提交",
    )


def login_session_codes(client):
    if not client.has_origin_cookies():
        raise JWXTError(
            "no active captcha session",
            "先运行 easy-qfnu jwxt captcha，再用 --captcha 提交识别结果",
        )
    status, _, raw = client.text(
        "POST",
        SESS_URL,
        b"",
        {"Content-Type": "application/x-www-form-urlencoded"},
    )
    if status >= 400:
        raise JWXTError("failed to obtain scode/sxh")
    parts = raw.strip().split("#", 1)
    if len(parts) != 2 or parts[0] == "" or parts[1] == "":
        raise JWXTError("invalid scode/sxh")
    return parts[0], parts[1]


def submit_login(client, username, password, captcha_text):
    scode, sxh = login_session_codes(client)
    encoded = encode_credentials(username, password, scode, sxh)
    form = urlencode(
        {
            "userAccount": "",
            "userPassword": "",
            "RANDOMCODE": captcha_text,
            "encoded": encoded,
        }
    ).encode("utf-8")
    _, _, body = client.text(
        "POST",
        LOGIN_URL,
        form,
        {"Content-Type": "application/x-www-form-urlencoded"},
        True,
    )
    return body


def profile_page(client):
    """读个人资料页正文，返回 (正文, 警告说明)；拿不到正文时正文是空串。

    status 与资料补全都只调它一次，周次解析复用同一份正文，不为周次再发第二次请求。
    """
    try:
        code, _, body = client.text("GET", PROFILE_URL)
    except OSError as exc:
        return "", "个人资料补全请求失败：" + str(exc)
    # 非 200 说明这次没拿到资料页，正文按空串处理，状态写进警告。
    if code != 200:
        return "", "个人资料补全返回 HTTP " + str(code)
    return body, ""


def enrich_profile(client, profile):
    """把资料页里的资料合并进 profile，返回警告说明；正文取不到时只返回警告。"""
    body, warning = profile_page(client)
    # 正文为空说明这次没拿到资料页，profile 保持调用方给的那份。
    if not body:
        return warning
    merge_profile(profile, parse_profile(body))
    return ""


def fetch_teaching_week(client):
    """读主页探测当前教学周，返回 (周次信息, 失败结果)。

    登录页与「页面没有周次标记」是两种失败：前者说明会话已过期、只能重新登录，后者提示用户
    手动传 --week。两种情况调用方都不得继续发课表请求。
    """
    try:
        code, _, body = client.text("GET", PROFILE_URL)
    except OSError as exc:
        return None, failure("jwxt", "获取当前教学周失败：" + str(exc), WEEK_MANUAL_HINT)
    # 非 200 拿不到主页正文，只能请用户手动传周次。
    if code != 200:
        return None, failure("jwxt", "获取当前教学周返回 HTTP " + str(code), WEEK_MANUAL_HINT)
    # 正文出现登录表单字样说明会话已过期，与 status 的判定口径一致，改参数没用。
    if contains_any(body, LOGIN_BODY_MARKERS):
        return None, failure("jwxt", "会话已过期，无法获取当前教学周", SESSION_EXPIRED_HINT)
    week = parse_teaching_week(body)
    # 页面拿到了却没有可用的周次标记时，提示用户手动指定周次。
    if week is None:
        return None, failure("jwxt", "无法自动获取当前教学周", WEEK_MANUAL_HINT)
    return week, None


def resolve_week(client, week):
    """取查询要用的周次，返回 (周次, 失败结果)。

    用户显式传了 --week 就直接用它，不再探测当前教学周；没给时读主页探测，探测失败时把失败
    结果交给调用方去停下，调用方不得再发空教室课表请求。
    """
    text = str(week if week is not None else "").strip()
    # 显式给了周次就以用户为准，这一步一个请求都不发。
    if text:
        return text, None
    info, error = fetch_teaching_week(client)
    # 会话过期或页面没有周次标记都把失败原样交出去。
    if error is not None:
        return "", error
    return str(info["current"]), None


def record_credential_save(result, username, password, enabled):
    result["credentials_saved"] = False
    if not enabled:
        return
    try:
        save_credentials_file(username, password)
    except OSError as exc:
        result["hint"] = str(exc)
        return
    result["credentials_saved"] = True
    result["credentials_path"] = default_credentials_path()


def build_login_result(client, username, password, save_credentials):
    status, _, main = client.text("GET", MAIN_URL)
    if status != 200 or not contains_any(main, ["教学一体化服务平台", "glyphicon-class"]):
        raise JWXTError("login failed: success marker missing on xsMain.jsp")
    profile = parse_profile(main)
    warning = enrich_profile(client, profile)
    client.persist({"username": username, "captcha_pending": False, "profile": profile})
    result = success(
        "jwxt",
        {
            "logged_in": True,
            "username": username,
            "profile": profile,
            "main_url": MAIN_URL,
            "session_path": client.session_path,
            "captcha": "vision",
        },
    )
    if warning:
        result["profile_warning"] = warning
    record_credential_save(result, username, password, save_credentials)
    return result


def login(client, username, password, captcha_text, save_credentials):
    username = username.strip()
    if username == "" or password == "":
        raise JWXTError(
            "username/password required",
            "传入 --username/--password 或设置 QFNU_JWXT_USERNAME/QFNU_JWXT_PASSWORD",
        )
    prepared = prepare_login_captcha(captcha_text)
    body = submit_login(client, username, password, prepared)
    validate_login_response(body)
    return build_login_result(client, username, password, save_credentials)


def status(client):
    if not client.has_origin_cookies():
        return not_logged_in(
            client,
            "not logged in",
            "run easy-qfnu jwxt login first",
            False,
        )
    try:
        code, final_url, main = client.text("GET", MAIN_URL)
    except OSError as exc:
        return not_logged_in(client, str(exc), "请检查网络和本地会话后重试", None)
    if (
        code != 200
        or contains_any(main, LOGIN_BODY_MARKERS)
        or not contains_any(main, ["教学一体化服务平台", "glyphicon-class"])
    ):
        return expired_status(client)
    profile = parse_profile(main)
    body, warning = profile_page(client)
    # 资料页正文同时用于资料补全与周次解析，本次请求不再为周次多发一次。
    merge_profile(profile, parse_profile(body))
    week = parse_teaching_week(body)
    client.persist({"profile": profile, "username": client.meta.get("username") or ""})
    result = success(
        "jwxt",
        {
            "logged_in": True,
            "session_expired": False,
            "username": client.meta.get("username") or "",
            "profile": profile,
            "main_url": final_url,
            "session_path": client.session_path,
        },
    )
    # 探测到周次时才带上这个字段：取不到周次时 status 仍然 ok:true，既有字段一个都不变。
    if week is not None:
        result["week"] = week
    if warning:
        result["profile_warning"] = warning
    return result


def expired_status(client):
    return not_logged_in(
        client,
        "jwxt session expired",
        SESSION_EXPIRED_HINT,
        True,
    )


def not_logged_in(client, message, hint, expired):
    """未登录/会话过期一律 ok:false，并显式给出 logged_in 与 session_expired。"""
    result = failure("jwxt", message, hint)
    result["logged_in"] = False
    result["session_expired"] = expired
    result["session_path"] = client.session_path
    return result


def load_credentials_file():
    path = default_credentials_path()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = handle.read()
    except FileNotFoundError:
        return "", ""
    except OSError as exc:
        raise OSError("read credentials: " + str(exc)) from exc
    try:
        value = json.loads(data)
    except ValueError as exc:
        raise OSError("parse credentials: " + str(exc)) from exc
    return value.get("username") or "", value.get("password") or ""


def save_credentials_file(username, password):
    path = default_credentials_path()
    body = json.dumps({"username": username, "password": password}, ensure_ascii=False, indent=2) + "\n"
    write_private_file(path, body, "w")


def clear_credentials_file():
    try:
        os.remove(default_credentials_path())
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise OSError("remove credentials: " + str(exc)) from exc
    return True


def default_captcha_output():
    return default_captcha_path()
