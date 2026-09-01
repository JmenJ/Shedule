from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from database import Group, LessonTime, Repository

DAYS_RU = (
    "Понедельник",
    "Вторник",
    "Среда",
    "Четверг",
    "Пятница",
    "Суббота",
    "Воскресенье",
)
SHORT_DAYS_RU = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
WEEK_LABELS = {"upper": "Верхняя", "lower": "Нижняя"}


def format_lesson_line(
    lesson_number: int,
    text: str,
    lesson_times: dict[int, LessonTime],
) -> str:
    configured_time = lesson_times.get(lesson_number)
    if configured_time is None:
        return text
    return (
        f"{configured_time.start_time}–"
        f"{configured_time.end_time}: {text}"
    )


def monday_for(value: date) -> date:
    return value - timedelta(days=value.weekday())


def opposite_week(week_type: str) -> str:
    return "lower" if week_type == "upper" else "upper"


def week_type_for_date(group: Group, target_date: date) -> str:
    weeks_delta = (monday_for(target_date) - group.anchor_monday).days // 7
    return (
        group.anchor_week_type
        if weeks_delta % 2 == 0
        else opposite_week(group.anchor_week_type)
    )


def local_now(group: Group, now: datetime | None = None) -> datetime:
    timezone = ZoneInfo(group.timezone)
    if now is None:
        return datetime.now(timezone)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone)
    return now.astimezone(timezone)


def target_date_for_action(
    group: Group, action: str, now: datetime | None = None
) -> date:
    today = local_now(group, now).date()
    if action == "today":
        return today
    if action == "tomorrow":
        return today + timedelta(days=1)
    if action.startswith("day:"):
        day_of_week = int(action.split(":", maxsplit=1)[1])
        if day_of_week not in range(7):
            raise ValueError("Некорректный день недели")
        return monday_for(today) + timedelta(days=day_of_week)
    raise ValueError("Неизвестное действие")


def format_schedule(
    repository: Repository,
    group: Group,
    target_date: date,
    subgroup_id: int | None = None,
) -> str:
    day_of_week = target_date.weekday()
    week_type = week_type_for_date(group, target_date)
    common_entries = repository.list_schedule(
        group.chat_id, week_type, day_of_week, subgroup_id=None
    )
    subgroup = None
    subgroup_entries = []
    if subgroup_id is not None:
        subgroup = repository.get_subgroup(group.chat_id, subgroup_id)
        if subgroup is not None:
            subgroup_entries = repository.list_schedule(
                group.chat_id, week_type, day_of_week, subgroup_id=subgroup.id
            )
    entries = sorted(
        [*common_entries, *subgroup_entries],
        key=lambda entry: (entry.position, entry.id),
    )
    lesson_times = {
        item.lesson_number: item
        for item in repository.list_lesson_times(group.chat_id)
    }
    header = f"📅 {DAYS_RU[day_of_week]} ({WEEK_LABELS[week_type]} неделя):"
    if subgroup is not None:
        header += f"\n👤 {subgroup.name}"
    body = (
        "\n\n".join(
            format_lesson_line(index, entry.text, lesson_times)
            for index, entry in enumerate(entries, start=1)
        )
        if entries
        else group.empty_day_text
    )
    return f"{header}\n\n{body}"


def notification_messages(
    repository: Repository,
    group: Group,
    target_date: date,
) -> list[str]:
    subgroups = repository.list_subgroups(group.chat_id)
    if not subgroups:
        return [f"🌅 Доброе утро!\n\n{format_schedule(repository, group, target_date)}"]
    return [
        f"🌅 Доброе утро!\n\n{format_schedule(repository, group, target_date, subgroup.id)}"
        for subgroup in subgroups
    ]


def current_week_type(group: Group, now: datetime | None = None) -> str:
    return week_type_for_date(group, local_now(group, now).date())
