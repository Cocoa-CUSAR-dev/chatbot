"""Canned Thai strings the router replies with, in one place rather than
inline at each call site (US2-11 / docs-and-plan#185, #186).

Two reasons they live here and not in src/conversation: they are presentation
only -- no state, no decision -- and the team edits this copy far more often
than the flow around it, so a wording tweak shouldn't touch routing code. The
polite particle "ครับ" matches the strings already in service.py (e.g.
"บันทึกข้อมูลเรียบร้อยแล้ว ขอบคุณครับ").

Tone, by team request: humble and clear (อ่อนน้อมและชัดเจน). Humble means
asking rather than instructing (รบกวน... นะครับ, ขออภัยครับ), never blaming the
farmer for what the bot can't do. Clear means one idea per line and the next
action spelled out. Copy never names a specific role to contact (e.g. a
field researcher) -- the team isn't sure every farmer has one, and pointing
someone at a person who doesn't exist is its own dead end.

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
START_HINT = f'รบกวนพิมพ์ "{PRIMARY_START_KEYWORD}" เพื่อดูงานที่ต้องทำนะครับ'

# A single Quick Reply button that sends the start keyword as ordinary text,
# so it lands on exactly the same router branch as typing it by hand -- no
# second code path to keep in sync (deliberately a MessageAction, unlike the
# task list's own PostbackAction buttons).
START_QUICK_REPLY = [QuickReplyOption(label=PRIMARY_START_KEYWORD, text=PRIMARY_START_KEYWORD)]

# Linking (ADR 0002) is still undecided, so this names no procedure and no
# specific role -- only that someone on the team can help.
NOT_LINKED = "ขออภัยครับ บัญชี LINE นี้ยังไม่ได้เชื่อมกับระบบ\nรบกวนติดต่อทีมงานเพื่อเชื่อมบัญชีก่อนนะครับ"

NO_PENDING_TASKS = "ตอนนี้ยังไม่มีงานที่ต้องบันทึกครับ ขอบคุณมากนะครับ"

# #186 item 2: a sticker/photo/location/etc. used to be logged and silently
# dropped, leaving the farmer staring at no reply at all. Two variants --
# mid-form the farmer is being asked something specific, so pointing them at
# the task list instead would be wrong.
UNSUPPORTED_MESSAGE_TYPE = f"ขออภัยครับ ตอนนี้น้องโกโก้ยังอ่านได้เฉพาะข้อความที่พิมพ์เท่านั้น\n{START_HINT}"
UNSUPPORTED_MESSAGE_TYPE_IN_FORM = (
    "ขออภัยครับ ตอนนี้น้องโกโก้ยังอ่านได้เฉพาะข้อความที่พิมพ์เท่านั้น\nรบกวนพิมพ์คำตอบเป็นข้อความนะครับ"
)

# docs-and-plan#189: typed text at the confirmation step used to hit
# handle_answer's "no open question" path and come back as "ไม่พบบทสนทนานี้แล้ว",
# which was simply untrue -- the conversation was alive and waiting for a
# button. Unrecognised text now re-shows the summary with this line on top.
PRESS_A_BUTTON = "รบกวนกดปุ่มด้านล่างเพื่อยืนยัน แก้ไข หรือยกเลิกนะครับ"

# US2-11 (#184/#185). All canned: a farmer must never receive LLM-authored
# prose from this bot, because anything it writes freely is something the
# team can't review -- the classifier picks WHICH fixed message to send,
# never what it says.
WELCOME_BACK = f"สวัสดีครับ 🙏 น้องโกโก้ยินดีช่วยบันทึกข้อมูลแปลงให้นะครับ\n{START_HINT}"

# The what-can-you-do answer. Deliberately numbered steps the farmer can
# actually follow, not a description of the system.
HELP = (
    "น้องโกโก้ช่วยบันทึกข้อมูลแปลงโกโก้ให้ครับ วิธีใช้มีดังนี้ครับ\n"
    f'1. พิมพ์ "{PRIMARY_START_KEYWORD}" เพื่อดูงานที่ต้องทำ\n'
    "2. กดเลือกงาน แล้วตอบคำถามทีละข้อ\n"
    '3. ถ้าต้องการหยุดก่อน กด "⏸️ พักไว้ก่อน" คำตอบที่ตอบไปแล้วจะไม่หายครับ\n'
    "4. เมื่อตอบครบ น้องโกโก้จะสรุปให้ตรวจอีกครั้ง กดยืนยัน แก้ไข หรือยกเลิกได้ครับ"
)

# Weather, prices, agronomy, anything else -- declined, never answered. The
# bot has no grounding for those and a wrong answer here reaches a farmer's
# actual crop. Says what the bot CAN do rather than only "no".
OFF_TOPIC = (
    "ขออภัยครับ เรื่องนี้น้องโกโก้ยังตอบให้ไม่ได้ครับ 🙏\n"
    "น้องโกโก้ช่วยได้เฉพาะการบันทึกข้อมูลแปลงเท่านั้นครับ\n"
    f"{START_HINT}"
)

# FollowEvent (#185) -- a farmer who just added the OA as a friend.
WELCOME_NEW_FRIEND = (
    f"ยินดีต้อนรับครับ 🙏 ขอบคุณที่เพิ่มน้องโกโก้เป็นเพื่อนนะครับ\nน้องโกโก้จะช่วยบันทึกข้อมูลแปลงโกโก้ให้ครับ\n{START_HINT}"
)
WELCOME_NOT_LINKED = f"ยินดีต้อนรับครับ 🙏 ขอบคุณที่เพิ่มน้องโกโก้เป็นเพื่อนนะครับ\n{NOT_LINKED}"
