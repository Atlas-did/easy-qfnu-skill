"""曲阜师范大学作息时间表。

来源：教务处《曲阜师范大学作息时间表》官方 PDF 附件
（https://jwc.qfnu.edu.cn/info/1104/4345.htm 的下载附件，注明"本作息时间自2026年8月开始执行"）。

原文（春、秋、冬季 / 夏季"五一"至"十一"）：

    第1节   8:00-8:05（时政5分钟），8:05-8:45   / 同左
    第2节   8:50-9:30                          / 同左
    第3节   9:50-10:30                         / 同左
    第4节   10:35-11:15                        / 同左
    第5节   11:20-12:00                        / 同左
    第6节   14:00-14:40                        / 14:30-15:10
    第7节   14:45-15:25                        / 15:15-15:55
    第8节   15:45-16:25                        / 16:15-16:55
    第9节   16:30-17:10                        / 17:00-17:40
    第10节  19:00-19:40                        / 同左
    第11节  19:45-20:25                        / 同左
    第12节  20:30-21:10                        / 同左

第 1 节含 5 分钟时政，8:05 才正式上课；导出日历时按 8:05 起算更贴近真实上课时间。
教务处课表接口不返回节次时间，这张表是导出 ICS 所需的唯一外部依据。
"""

# 春、秋、冬季作息：节次 -> (开始, 结束)
WINTER_SPRING_AUTUMN = {
    1: ("08:05", "08:45"),
    2: ("08:50", "09:30"),
    3: ("09:50", "10:30"),
    4: ("10:35", "11:15"),
    5: ("11:20", "12:00"),
    6: ("14:00", "14:40"),
    7: ("14:45", "15:25"),
    8: ("15:45", "16:25"),
    9: ("16:30", "17:10"),
    10: ("19:00", "19:40"),
    11: ("19:45", "20:25"),
    12: ("20:30", "21:10"),
}

# 夏季（"五一"至"十一"）：只有下午第 6-9 节不同
SUMMER = dict(WINTER_SPRING_AUTUMN)
SUMMER.update({
    6: ("14:30", "15:10"),
    7: ("15:15", "15:55"),
    8: ("16:15", "16:55"),
    9: ("17:00", "17:40"),
})

# 第 1 节前的 5 分钟时政，单独暴露以便需要时使用
MORNING_POLITICS = ("08:00", "08:05")

SEASONS = ("winter", "summer")


def season_table(season="winter"):
    """season: 'winter' 春/秋/冬（默认），'summer' 夏季。"""
    return SUMMER if season == "summer" else WINTER_SPRING_AUTUMN


def span_for_sections(sections, season="winter"):
    """给定节次序列，返回 (开始, 结束) 时钟串：首节开始到末节结束。

    中间的大课间不扣除，日历上显示为一段连续占用更符合直觉。
    无有效节次时返回 None。
    """
    table = season_table(season)
    valid = [value for value in sections if value in table]
    if not valid:
        return None
    first, last = min(valid), max(valid)
    return table[first][0], table[last][1]


def describe(season="winter"):
    """返回整张作息表，便于 CLI 直接输出。"""
    table = season_table(season)
    return [
        {"section": section, "start": table[section][0], "end": table[section][1]}
        for section in sorted(table)
    ]
