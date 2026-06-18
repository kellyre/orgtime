from datetime import datetime, timedelta

from orgtime.model import (
    check_consistency,
    comment_lines,
    format_duration,
    parse,
    tombstoned,
)

SAMPLE = """\
* [#2] Website Redesign
# Client prefers blue tones
** IN-PROGRESS [#1] Design mockups
# Start with desktop layout
   CLOCK: [2026-06-09 Tue 09:00]--[2026-06-09 Tue 10:30] => 1:30
# Header done
# Footer still rough
   CLOCK: [2026-06-10 Wed 11:00]
** TODO [#3] Write copy
## ** TODO [#4] Old deleted task
### deleted comment

* [#4] Admin
** DONE [#5] File taxes
   CLOCK: [2026-04-01 Wed 13:00]--[2026-04-01 Wed 15:15] => 2:15
"""


def test_roundtrip():
    doc, issues = parse(SAMPLE)
    assert issues == []
    assert len(doc.projects) == 2
    p = doc.projects[0]
    assert p.name == "Website Redesign" and p.priority == 2
    t = p.tasks[0]
    assert t.status == "IN-PROGRESS" and t.priority == 1
    assert len(t.clocks) == 2 and t.clocks[1].running
    assert doc.running()[1] is t
    # serialize -> parse again is stable
    text = doc.serialize()
    doc2, issues2 = parse(text)
    assert issues2 == []
    assert doc2.serialize() == text


def test_comments_attach_to_nearest_item():
    doc, issues = parse(SAMPLE)
    assert issues == []
    p = doc.projects[0]
    assert p.comments == ["Client prefers blue tones"]
    mockups, write_copy = p.tasks
    assert mockups.comments == ["Start with desktop layout"]
    assert mockups.clocks[0].comments == ["Header done", "Footer still rough"]
    assert mockups.clocks[1].comments == []
    # soft-deleted lines anchored to the task above them, kept verbatim
    assert write_copy.tombstones == [
        "## ** TODO [#4] Old deleted task",
        "### deleted comment",
    ]


def test_legacy_description_migrates_to_comments():
    text = """\
* [#2] Proj
  :DESCRIPTION: project desc one
  :DESCRIPTION: project desc two
# real comment
** TODO [#1] Task
   :DESCRIPTION: task desc
   CLOCK: [2026-06-09 Tue 09:00]--[2026-06-09 Tue 10:00] => 1:00
"""
    doc, issues = parse(text)
    assert issues == []
    p = doc.projects[0]
    # description lines become comments, in order, before the real comment
    assert p.comments == ["project desc one", "project desc two", "real comment"]
    assert p.tasks[0].comments == ["task desc"]
    # once saved, the file no longer contains :DESCRIPTION:
    assert ":DESCRIPTION:" not in doc.serialize()
    # and Project/Task no longer have a description attribute
    assert not hasattr(p, "description")


def test_created_modified_parse_serialize_resolve_touch():
    from orgtime.model import resolve_times

    text = """\
* [#2] Proj
  :CREATED: [2026-06-01 Mon 09:00] :MODIFIED: [2026-06-02 Tue 10:00]
** TODO [#1] HasTimes
   :CREATED: [2026-06-01 Mon 09:30] :MODIFIED: [2026-06-01 Mon 09:30]
   CLOCK: [2026-06-05 Fri 08:00]--[2026-06-05 Fri 09:00] => 1:00
** TODO [#3] NoTimes
   CLOCK: [2026-06-07 Sun 08:00]--[2026-06-07 Sun 08:30] => 0:30
"""
    doc, issues = parse(text)
    assert issues == []
    p = doc.projects[0]
    assert p.created == datetime(2026, 6, 1, 9, 0)
    assert p.modified == datetime(2026, 6, 2, 10, 0)
    has, no = p.tasks
    assert has.created == datetime(2026, 6, 1, 9, 30)
    assert no.created is None  # not yet resolved

    # resolve fills missing times from the most recent clock (else now)
    now = datetime(2026, 6, 18, 12, 0)
    resolve_times(doc, now)
    assert no.created == datetime(2026, 6, 7, 8, 30)   # its latest clock
    assert no.modified == datetime(2026, 6, 7, 8, 30)
    assert has.created == datetime(2026, 6, 1, 9, 30)  # kept, not overwritten

    # serialize round-trips the timestamps
    text2 = doc.serialize()
    assert ":CREATED: [2026-06-01 Mon 09:00]" in text2
    doc2, _ = parse(text2)
    assert doc2.projects[0].created == p.created

    # touch bumps task + its project; clock touch bubbles up too
    doc.touch(has, now=datetime(2026, 6, 18, 13, 0))
    assert has.modified == datetime(2026, 6, 18, 13, 0)
    assert p.modified == datetime(2026, 6, 18, 13, 0)
    doc.touch(no.clocks[0], now=datetime(2026, 6, 18, 14, 0))
    assert no.modified == datetime(2026, 6, 18, 14, 0)
    assert p.modified == datetime(2026, 6, 18, 14, 0)


def test_resolve_project_default_ignores_clockless_task_now():
    from orgtime.model import resolve_times

    # a project whose tasks include one with clocks and one without should
    # default its created to the real clock time, not "now"
    text = """\
* Proj
** TODO WithClock
   CLOCK: [2026-06-05 Fri 08:00]--[2026-06-05 Fri 09:00] => 1:00
** TODO NoClock
"""
    doc, _ = parse(text)
    resolve_times(doc, now=datetime(2026, 6, 18, 12, 0))
    assert doc.projects[0].created == datetime(2026, 6, 5, 9, 0)


def test_expunge():
    doc, _ = parse(SAMPLE)
    assert doc.tombstone_count() == 2
    assert doc.expunge() == 2
    assert doc.tombstone_count() == 0
    assert "##" not in doc.serialize()
    # live single-# comments survive
    assert "# Header done" in doc.serialize()


def test_tombstoned_helper():
    assert tombstoned(["* Proj", "   CLOCK: x", "# a comment"]) == [
        "## * Proj",
        "##    CLOCK: x",
        "### a comment",
    ]
    assert comment_lines(["hello", ""]) == ["# hello", "#"]


def test_clocking():
    doc, _ = parse(SAMPLE)
    other = doc.projects[0].tasks[1]
    now = datetime(2026, 6, 10, 12, 0)
    doc.clock_in(other, now)  # should close the running clock first
    assert doc.projects[0].tasks[0].running_clock() is None
    assert other.running_clock().start == now
    assert other.status == "IN-PROGRESS"
    doc.clock_out(datetime(2026, 6, 10, 12, 45))
    assert doc.running() is None
    assert other.total_time() == timedelta(minutes=45)


def test_format_issues_flagged():
    text = SAMPLE + "\n** [#2] No status here\nGARBAGE LINE\n"
    doc, issues = parse(text)
    assert any("unknown status" in i or "missing" in i for i in issues)
    assert any("unrecognized" in i for i in issues)
    # priority still recovered from a status-less line
    assert doc.projects[1].tasks[-1].priority == 2
    assert doc.projects[1].tasks[-1].name == "No status here"


def test_consistency():
    bad = """\
* Proj
** TODO Task A
   CLOCK: [2026-06-09 Mon 09:00]--[2026-06-09 Tue 10:00] => 2:00
   CLOCK: [2026-06-09 Tue 09:30]--[2026-06-09 Tue 11:00] => 1:30
** DONE Task B
   CLOCK: [2026-06-10 Wed 09:00]
** TODO Task C
   CLOCK: [2026-06-10 Wed 10:00]
"""
    doc, issues = parse(bad)
    problems = check_consistency(doc)
    text = "\n".join(problems)
    assert "day name" in text          # Mon vs actual Tue
    assert "stated duration" in text   # 2:00 vs actual 1:00
    assert "overlapping" in text
    assert "running clock on DONE" in text
    assert "more than one running clock" in text


def test_duration_format():
    from orgtime.model import human_duration

    assert format_duration(timedelta(hours=26, minutes=5)) == "26:05"
    assert format_duration(timedelta()) == "0:00"
    assert human_duration(timedelta(hours=22, minutes=43)) == "22:43"
    assert human_duration(timedelta(hours=48)) == "2d 0:00"
    assert human_duration(timedelta(hours=70, minutes=43)) == "2d 22:43"


def test_plausibility_warnings():
    text = """\
* Proj
** TODO Overnight
   CLOCK: [2026-06-10 Wed 21:30]--[2026-06-11 Thu 20:13] => 22:43
** TODO Typo future end
   CLOCK: [2026-06-11 Thu 20:28]--[2026-06-13 Sat 20:28] => 48:00
** TODO Forgotten running
   CLOCK: [2026-06-09 Tue 08:00]
** TODO Fine
   CLOCK: [2026-06-11 Thu 09:00]--[2026-06-11 Thu 10:00] => 1:00
"""
    doc, issues = parse(text)
    now = datetime(2026, 6, 11, 21, 0)
    problems = "\n".join(check_consistency(doc, now))
    assert "Typo future end: clock ends in the future" in problems
    assert "suspiciously long: 2d 0:00" in problems
    assert "Forgotten running: clock running for 2d 13:00" in problems
    # under 24h is fine, even overnight
    assert "Overnight" not in problems
    assert "Fine" not in problems


def test_parse_user_ts():
    from orgtime.model import parse_user_ts

    assert parse_user_ts("2026-06-10 09:30") == datetime(2026, 6, 10, 9, 30)
    base = datetime(2026, 6, 9, 23, 0)
    assert parse_user_ts("14:05", base) == datetime(2026, 6, 9, 14, 5)
    assert parse_user_ts(" 14:05 ", base) == datetime(2026, 6, 9, 14, 5)
    assert parse_user_ts("14:05").date() == datetime.now().date()
    assert parse_user_ts("nonsense") is None
    assert parse_user_ts("25:00") is None


if __name__ == "__main__":
    for fn in [test_roundtrip, test_comments_attach_to_nearest_item,
               test_legacy_description_migrates_to_comments,
               test_created_modified_parse_serialize_resolve_touch,
               test_resolve_project_default_ignores_clockless_task_now,
               test_expunge,
               test_tombstoned_helper, test_clocking, test_format_issues_flagged,
               test_consistency, test_duration_format, test_plausibility_warnings,
               test_parse_user_ts]:
        fn()
        print(f"PASS {fn.__name__}")
