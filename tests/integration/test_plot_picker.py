"""src/line/plot_picker.py's scoped query, against a real Postgres.

Worth a real DB rather than a mocked session for the usual reason this repo
uses integration tests: the thing being asserted IS the SQL. The whole point
of this module is that it returns the farmer's OWN plots -- a mocked session
would happily "prove" that while the join scoped nothing at all.
"""

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.line import plot_picker
from tests.integration.helpers import seed_farm_with_plots, seed_user_with_line_identity


async def _seed_farmer(db_session: AsyncSession) -> uuid.UUID:
    return await seed_user_with_line_identity(db_session, line_user_id=f"U{uuid.uuid4().hex}")


class TestScoping:
    async def test_lists_the_farmers_own_plots(self, db_session: AsyncSession) -> None:
        farmer_id = await _seed_farmer(db_session)
        await seed_farm_with_plots(db_session, farmer_id=farmer_id, plot_names=["แปลง A", "แปลง B"])

        options = await plot_picker.list_plots(db_session, farmer_id)

        assert [option.label for option in options] == ["แปลง A", "แปลง B"]

    async def test_another_farmers_plots_are_not_listed(self, db_session: AsyncSession) -> None:
        """The reason this module exists: Kotlin's ref.plot_constant list is
        every plot in the system, which is not something a farmer should be
        able to file an activity against by tapping.
        """
        farmer_id = await _seed_farmer(db_session)
        other_farmer_id = await _seed_farmer(db_session)
        await seed_farm_with_plots(db_session, farmer_id=farmer_id, plot_names=["ของฉัน"])
        await seed_farm_with_plots(
            db_session, farmer_id=other_farmer_id, plot_names=["ของคนอื่น"], farm_name="ไร่อื่น"
        )

        options = await plot_picker.list_plots(db_session, farmer_id)

        assert [option.label for option in options] == ["ของฉัน"]

    async def test_farm_id_narrows_to_one_farm(self, db_session: AsyncSession) -> None:
        farmer_id = await _seed_farmer(db_session)
        north_farm_id, _ = await seed_farm_with_plots(
            db_session, farmer_id=farmer_id, plot_names=["เหนือ 1"], farm_name="ไร่เหนือ"
        )
        await seed_farm_with_plots(
            db_session, farmer_id=farmer_id, plot_names=["ใต้ 1"], farm_name="ไร่ใต้"
        )

        options = await plot_picker.list_plots(db_session, farmer_id, farm_id=str(north_farm_id))

        assert [option.label for option in options] == ["เหนือ 1"]

    async def test_no_farm_id_lists_every_farm_with_the_farm_name_shown(
        self, db_session: AsyncSession
    ) -> None:
        """ "แปลง 1" on two different farms is ambiguous on a button, so the
        farm name is appended -- but only when there really is more than one.
        """
        farmer_id = await _seed_farmer(db_session)
        await seed_farm_with_plots(
            db_session, farmer_id=farmer_id, plot_names=["แปลง 1"], farm_name="ไร่เหนือ"
        )
        await seed_farm_with_plots(
            db_session, farmer_id=farmer_id, plot_names=["แปลง 1"], farm_name="ไร่ใต้"
        )

        options = await plot_picker.list_plots(db_session, farmer_id)

        assert sorted(option.label for option in options) == [
            "แปลง 1 (ไร่เหนือ)",
            "แปลง 1 (ไร่ใต้)",
        ]

    async def test_single_farm_labels_stay_plain(self, db_session: AsyncSession) -> None:
        farmer_id = await _seed_farmer(db_session)
        await seed_farm_with_plots(db_session, farmer_id=farmer_id, plot_names=["แปลง 1"])

        options = await plot_picker.list_plots(db_session, farmer_id)

        assert [option.label for option in options] == ["แปลง 1"]

    async def test_a_farmer_with_no_farm_gets_nothing(self, db_session: AsyncSession) -> None:
        farmer_id = await _seed_farmer(db_session)

        assert await plot_picker.list_plots(db_session, farmer_id) == []

    async def test_an_unnamed_plot_still_gets_a_tappable_label(
        self, db_session: AsyncSession
    ) -> None:
        """plot_name is nullable in the real schema; a button with an empty
        label would be untappable in LINE.
        """
        farmer_id = await _seed_farmer(db_session)
        farm_id, _ = await seed_farm_with_plots(db_session, farmer_id=farmer_id, plot_names=[])
        await db_session.execute(
            text("INSERT INTO agriculture.plot (farm_id, plot_name) VALUES (:farm_id, NULL)"),
            {"farm_id": farm_id},
        )
        await db_session.commit()

        options = await plot_picker.list_plots(db_session, farmer_id)

        assert [option.label for option in options] == ["แปลงไม่มีชื่อ"]
