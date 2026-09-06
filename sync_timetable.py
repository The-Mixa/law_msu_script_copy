#!/usr/bin/env python3
"""Синхронизация расписания юрфака МГУ в ICS-файл на GitHub."""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup, Tag
from icalendar import Calendar, Event

BASE_URL = "https://cacs.law.msu.ru"
TIMETABLE_URL = f"{BASE_URL}/time-table/group?type=0"
TIMEZONE = ZoneInfo("Europe/Moscow")
ICS_FILENAME = "schedule.ics"
SETTINGS_KEY = "X-LAW-MSU-SETTINGS"

FACULTY_OPTIONS = {
    "Бакалавриат (основное отделение)": "8",
    "Бакалавриат (международно-правовой профиль)": "23",
    "Магистратура": "4",
    'Спецотделение "Второе высшее образование"': "9",
    "Межфакультетские курсы юридического факультета": "13",
    "Подготовительные курсы": "10",
}


class TimetableError(Exception):
    pass


def load_settings(path: Path) -> dict[str, str]:
    with path.open(encoding="utf-8") as f:
        settings = json.load(f)
    for key in ("faculty", "course", "group"):
        if not settings.get(key):
            raise TimetableError(f"В settings.json не указано поле '{key}'")
    return settings


def settings_fingerprint(settings: dict[str, str]) -> str:
    return f"{settings['faculty']}|{settings['course']}|{settings['group']}"


def group_label_from_settings(settings: dict[str, str]) -> str:
    return f"{settings['faculty']}-{settings['course']}-{settings['group']}"


def fetch_csrf(session: requests.Session) -> str:
    response = session.get(TIMETABLE_URL, timeout=30)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "lxml")
    token = soup.find("input", {"name": "_csrf-frontend"})
    if not token or not token.get("value"):
        raise TimetableError("Не удалось получить CSRF-токен с сайта расписания")
    return token["value"]


def post_filter(
    session: requests.Session,
    csrf: str,
    faculty_id: str,
    course: str | None = None,
    group_id: str | None = None,
) -> BeautifulSoup:
    data: dict[str, str] = {
        "_csrf-frontend": csrf,
        "TimeTableForm[facultyId]": faculty_id,
    }
    if course is not None:
        data["TimeTableForm[course]"] = course
    if group_id is not None:
        data["TimeTableForm[groupId]"] = group_id

    response = session.post(TIMETABLE_URL, data=data, timeout=30)
    response.raise_for_status()
    return BeautifulSoup(response.text, "lxml")


def resolve_group_id(session: requests.Session, csrf: str, settings: dict[str, str]) -> tuple[str, str, str]:
    faculty_name = settings["faculty"]
    faculty_id = FACULTY_OPTIONS.get(faculty_name)
    if not faculty_id:
        known = ", ".join(sorted(FACULTY_OPTIONS))
        raise TimetableError(f"Неизвестное отделение '{faculty_name}'. Доступные: {known}")

    course = str(settings["course"])
    group_name = str(settings["group"]).strip()

    soup = post_filter(session, csrf, faculty_id, course=course)
    group_select = soup.select_one("#timetableform-groupid")
    if not group_select:
        raise TimetableError("Не удалось загрузить список групп")

    group_id = None
    for option in group_select.find_all("option"):
        value = option.get("value", "").strip()
        label = option.get_text(strip=True)
        if value and label == group_name:
            group_id = value
            break

    if not group_id:
        available = [
            option.get_text(strip=True)
            for option in group_select.find_all("option")
            if option.get("value")
        ]
        raise TimetableError(
            f"Группа '{group_name}' не найдена для {faculty_name}, курс {course}. "
            f"Доступные группы: {', '.join(available)}"
        )

    return faculty_id, course, group_id


def parse_pair_row(first_cell_text: str) -> tuple[str, str, str] | None:
    normalized = re.sub(r"\s+", "", first_cell_text)
    match = re.search(r"(\d+)пара(\d{2}:\d{2})(\d{2}:\d{2})", normalized)
    if not match:
        return None
    return match.group(1), match.group(2), match.group(3)


def parse_date(value: str) -> date:
    return datetime.strptime(value.strip(), "%d.%m.%Y").date()


def parse_lesson_div(div: Tag) -> dict[str, str] | None:
    inner = div.find("div", attrs={"data-content": True}) or div
    if inner.find(string=re.compile(r"^Скрыто")):
        return None

    data_content = inner.get("data-content", "")
    if data_content:
        parts = [part.strip() for part in html.unescape(data_content).split("<br>") if part.strip()]
        if not parts:
            return None

        subject = parts[0]
        auditorium = ""
        teacher = ""
        for part in parts[1:]:
            if part.startswith("ауд."):
                auditorium = part.replace("ауд.", "", 1).strip()
            elif part.startswith("Добавлено:"):
                continue
            elif part.isdigit():
                continue
            elif not teacher:
                teacher = part

        if subject.startswith("Скрыто"):
            return None

        return {"subject": subject, "auditorium": auditorium, "teacher": teacher}

    lines = [line.strip() for line in inner.get_text("\n", strip=True).splitlines() if line.strip()]
    if not lines or lines[0].startswith("Скрыто"):
        return None

    auditorium = ""
    teacher = ""
    subject_parts: list[str] = []

    for line in lines:
        if line.startswith("ауд."):
            auditorium = line.replace("ауд.", "", 1).strip()
        elif inner.find("i") and line == inner.find("i").get_text(strip=True):
            teacher = line
        else:
            subject_parts.append(line)

    subject = " ".join(subject_parts).strip()
    if not subject and not auditorium:
        return None

    return {"subject": subject, "auditorium": auditorium, "teacher": teacher}


def parse_timetable(soup: BeautifulSoup) -> list[dict[str, Any]]:
    table = soup.find("table")
    if not table:
        raise TimetableError("На странице не найдена таблица расписания")

    lessons: list[dict[str, Any]] = []
    dates: list[str] = []

    for row in table.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if not cells:
            continue

        first_text = cells[0].get_text(strip=True)
        if first_text in {"Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"}:
            dates = [cell.get_text(strip=True) for cell in cells[1:]]
            continue

        pair_info = parse_pair_row(first_text)
        if not pair_info:
            continue

        pair_num, start_time, end_time = pair_info
        for index, cell in enumerate(cells[1:]):
            if index >= len(dates) or not dates[index]:
                continue

            lesson_date = parse_date(dates[index])
            for div in cell.select(".cell div[class^='lesson']"):
                parsed = parse_lesson_div(div)
                if not parsed:
                    continue
                lessons.append(
                    {
                        "date": lesson_date,
                        "pair": pair_num,
                        "start": start_time,
                        "end": end_time,
                        **parsed,
                    }
                )

    if not lessons:
        raise TimetableError("Расписание пустое — проверьте настройки группы")

    return lessons


def build_weekly_pattern(lessons: list[dict[str, Any]]) -> dict[tuple[int, str], dict[str, str]]:
    pattern: dict[tuple[int, str], dict[str, str]] = {}
    for lesson in lessons:
        key = (lesson["date"].weekday(), lesson["pair"])
        pattern[key] = {
            "subject": lesson["subject"],
            "auditorium": lesson["auditorium"],
            "teacher": lesson["teacher"],
            "start": lesson["start"],
            "end": lesson["end"],
        }
    return pattern


def expand_lessons(
    parsed_lessons: list[dict[str, Any]],
    window_start: date,
    window_end: date,
) -> list[dict[str, Any]]:
    by_date: dict[date, list[dict[str, Any]]] = {}
    for lesson in parsed_lessons:
        by_date.setdefault(lesson["date"], []).append(lesson)

    pattern = build_weekly_pattern(parsed_lessons)
    result: dict[tuple[date, str, str, str], dict[str, Any]] = {}

    week_start = window_start - timedelta(days=window_start.weekday())
    while week_start <= window_end:
        for day_offset in range(7):
            day = week_start + timedelta(days=day_offset)
            if day < window_start or day > window_end:
                continue

            if day in by_date:
                for lesson in by_date[day]:
                    key = (day, lesson["pair"], lesson["subject"], lesson["auditorium"])
                    result[key] = lesson
                continue

            for (weekday, pair), template in pattern.items():
                if weekday != day.weekday():
                    continue
                key = (day, pair, template["subject"], template["auditorium"])
                if key in result:
                    continue
                result[key] = {
                    "date": day,
                    "pair": pair,
                    "start": template["start"],
                    "end": template["end"],
                    "subject": template["subject"],
                    "auditorium": template["auditorium"],
                    "teacher": template["teacher"],
                }

        week_start += timedelta(days=7)

    return sorted(result.values(), key=lambda item: (item["date"], item["start"], item["subject"]))


def make_event_uid(lesson: dict[str, Any], group_label: str) -> str:
    return (
        f"law-msu-{group_label}-{lesson['date'].isoformat()}-"
        f"{lesson['pair']}-{lesson['subject']}-{lesson['auditorium']}"
    )


def lesson_to_event(lesson: dict[str, Any], group_label: str) -> Event:
    start_dt = datetime.combine(
        lesson["date"],
        datetime.strptime(lesson["start"], "%H:%M").time(),
        tzinfo=TIMEZONE,
    )
    end_dt = datetime.combine(
        lesson["date"],
        datetime.strptime(lesson["end"], "%H:%M").time(),
        tzinfo=TIMEZONE,
    )

    summary = f"{lesson['auditorium']}, {lesson['subject']}".strip(", ")
    event = Event()
    event.add("uid", make_event_uid(lesson, group_label))
    event.add("dtstamp", datetime.now(TIMEZONE))
    event.add("dtstart", start_dt)
    event.add("dtend", end_dt)
    event.add("summary", summary)
    if lesson["teacher"]:
        event.add("description", lesson["teacher"])
    event.add("location", f"ауд. {lesson['auditorium']}" if lesson["auditorium"] else "")
    return event


def create_empty_calendar() -> Calendar:
    calendar = Calendar()
    calendar.add("prodid", "-//law_msu_timetable_app//RU")
    calendar.add("version", "2.0")
    calendar.add("calscale", "GREGORIAN")
    calendar.add("x-wr-timezone", "Europe/Moscow")
    return calendar


def load_existing_calendar(path: Path) -> Calendar:
    if not path.exists():
        return create_empty_calendar()

    with path.open("rb") as f:
        return Calendar.from_ical(f.read())


def get_calendar_settings_key(calendar: Calendar) -> str | None:
    value = calendar.get(SETTINGS_KEY)
    if value:
        return str(value)
    return None


def calendar_belongs_to_settings(calendar: Calendar, fingerprint: str, group_label: str) -> bool:
    stored = get_calendar_settings_key(calendar)
    if stored:
        return stored == fingerprint

    for component in calendar.walk():
        if component.name != "VEVENT":
            continue
        uid = str(component.get("uid", ""))
        if uid and not uid.startswith(f"law-msu-{group_label}-"):
            return False

    return True


def event_start_date(component: Any) -> date | None:
    dtstart = component.get("dtstart")
    if not dtstart:
        return None
    value = dtstart.dt
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def merge_calendar(
    existing: Calendar,
    new_lessons: list[dict[str, Any]],
    group_label: str,
    fingerprint: str,
    window_start: date,
    window_end: date,
    retention_cutoff: date,
) -> Calendar:
    new_uids = {make_event_uid(lesson, group_label) for lesson in new_lessons}
    merged = create_empty_calendar()
    merged.add(SETTINGS_KEY, fingerprint)

    for component in existing.walk():
        if component.name != "VEVENT":
            continue

        uid = str(component.get("uid", ""))
        start = event_start_date(component)
        if not start:
            continue

        if uid in new_uids:
            continue
        if start < retention_cutoff:
            continue
        if window_start <= start <= window_end:
            continue

        merged.add_component(component)

    for lesson in new_lessons:
        merged.add_component(lesson_to_event(lesson, group_label))

    return merged


def write_calendar(calendar: Calendar, path: Path) -> None:
    path.write_bytes(calendar.to_ical())


def fetch_and_parse(settings: dict[str, str]) -> list[dict[str, Any]]:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "law_msu_timetable_app/1.0",
            "Accept": "text/html,application/xhtml+xml",
        }
    )

    csrf = fetch_csrf(session)
    faculty_id, course, group_id = resolve_group_id(session, csrf, settings)
    soup = post_filter(session, csrf, faculty_id, course=course, group_id=group_id)
    return parse_timetable(soup)


def main() -> int:
    parser = argparse.ArgumentParser(description="Синхронизация расписания в ICS")
    parser.add_argument(
        "--settings",
        type=Path,
        default=Path(__file__).resolve().parent / "settings.json",
        help="Путь к settings.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parent / ICS_FILENAME,
        help="Локальный путь к ICS-файлу",
    )
    parser.add_argument("--weeks-before", type=int, default=1, help="Сколько недель назад добавлять")
    parser.add_argument("--weeks-after", type=int, default=2, help="Сколько недель вперёд добавлять")
    parser.add_argument(
        "--retention-months",
        type=int,
        default=2,
        help="Удалять события старше указанного числа месяцев",
    )
    parser.add_argument("--no-upload", action="store_true", help="Не выводить подсказку про GitHub")
    args = parser.parse_args()

    today = datetime.now(TIMEZONE).date()
    window_start = today - timedelta(weeks=args.weeks_before)
    window_end = today + timedelta(weeks=args.weeks_after)
    retention_cutoff = today - timedelta(days=30 * args.retention_months)

    settings = load_settings(args.settings)
    fingerprint = settings_fingerprint(settings)
    group_label = group_label_from_settings(settings)

    parsed_lessons = fetch_and_parse(settings)
    lessons = expand_lessons(parsed_lessons, window_start, window_end)

    existing = load_existing_calendar(args.output)
    if not calendar_belongs_to_settings(existing, fingerprint, group_label):
        stored = get_calendar_settings_key(existing)
        if stored:
            print(f"Настройки изменились ({stored} → {fingerprint}), ICS очищен")
        else:
            print("Настройки изменились, ICS очищен")
        existing = create_empty_calendar()

    merged = merge_calendar(
        existing,
        lessons,
        group_label,
        fingerprint,
        window_start,
        window_end,
        retention_cutoff,
    )
    write_calendar(merged, args.output)

    print(f"Сохранено событий в окне [{window_start} .. {window_end}]: {len(lessons)}")
    print(f"Файл: {args.output}")

    if not args.no_upload:
        print(
            "Загрузите schedule.ics в GitHub-репозиторий и подпишитесь по ссылке:\n"
            "https://raw.githubusercontent.com/<owner>/<repo>/<branch>/schedule.ics"
        )

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except TimetableError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
