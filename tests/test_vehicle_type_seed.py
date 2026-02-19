from sqlalchemy import delete, func, select

from app.models import VehicleType
from app.seed import SEED_VEHICLE_TYPES, seed_vehicle_types


def test_seed_vehicle_types_is_insert_once_when_table_empty(db_session):
    db_session.execute(delete(VehicleType))
    db_session.commit()

    created_first = seed_vehicle_types(db_session)
    count_after_first = db_session.execute(
        select(func.count(VehicleType.id))
    ).scalar_one()
    codes = set(db_session.execute(select(VehicleType.code)).scalars().all())

    created_second = seed_vehicle_types(db_session)
    count_after_second = db_session.execute(
        select(func.count(VehicleType.id))
    ).scalar_one()

    assert created_first == 10
    assert count_after_first == 10
    assert codes == {entry["code"] for entry in SEED_VEHICLE_TYPES}
    assert created_second == 0
    assert count_after_second == 10
