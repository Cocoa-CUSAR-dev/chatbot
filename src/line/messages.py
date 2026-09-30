"""Canned Thai strings the router replies with, in one place rather than
inline at each call site (US2-11 / docs-and-plan#185, #186).

Two reasons they live here and not in src/conversation: they are presentation
only -- no state, no decision -- and the team edits this copy far more often
than the flow around it, so a wording tweak shouldn't touch routing code. The
polite particle "ครับ" matches the strings already in service.py (e.g.
"บันทึกข้อมูลเรียบร้อยแล้ว ขอบคุณครับ").

Anything the farmer could be stuck on ends with a way forward -- either the
`เริ่ม` hint text below or, for replies sent with START_QUICK_REPLY, an
actual one-tap button. That is the whole point of #186: no reply in this
service should be a dead end.
"""

from src.line.schemas import QuickReplyOption
from src.line.temp_task_picker import PRIMARY_START_KEYWORD

# Appended to (or sent as) any reply where the farmer has nothing else to go
# on. One fixed keyword, never whichever one a set happened to yield first
# (#186 item 1).
START_HINT = f'พิมพ์ "{PRIMARY_START_KEYWORD}" เพื่อดูงานที่ต้องทำ'

# A single Quick Reply button that sends the start keyword as ordinary text,
# so it lands on exactly the same router branch as typing it by hand -- no
# second code path to keep in sync (deliberately a MessageAction, unlike the
# task list's own PostbackAction buttons).
START_QUICK_REPLY = [QuickReplyOption(label=PRIMARY_START_KEYWORD, text=PRIMARY_START_KEYWORD)]

NOT_LINKED = "บัญชี LINE นี้ยังไม่ได้เชื่อมกับบัญชีในระบบ"

NO_PENDING_TASKS = "ไม่มีงานที่ต้องทำในตอนนี้"

# #186 item 2: a sticker/photo/location/etc. used to be logged and silently
# dropped, leaving the farmer staring at no reply at all. Two variants --
# mid-form the farmer is being asked something specific, so pointing them at
# the task list instead would be wrong.
UNSUPPORTED_MESSAGE_TYPE = f"ตอนนี้ยังรับได้แค่ข้อความตัวอักษรครับ {START_HINT}"
UNSUPPORTED_MESSAGE_TYPE_IN_FORM = "ตอนนี้ยังรับได้แค่ข้อความตัวอักษรครับ กรุณาพิมพ์คำตอบเป็นข้อความ"

# docs-and-plan#189: typed text at the confirmation step used to hit
# handle_answer's "no open question" path and come back as "ไม่พบบทสนทนานี้แล้ว",
# which was simply untrue -- the conversation was alive and waiting for a
# button. Unrecognised text now re-shows the summary with this line on top.
PRESS_A_BUTTON = "กรุณากดปุ่มด้านล่างครับ"
